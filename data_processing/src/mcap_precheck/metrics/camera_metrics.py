"""③ 相机指标（需解码，默认关闭）。

四个关键实现细节（决定结果是否可信）：
  1. 重复帧用**像素差**而非哈希（h264 解码后字节可能微变）；
  2. 黑屏要 ``mean`` 与 ``std`` **双条件**（防漏过曝、防误判纯色背景）；
  3. 模糊阈值须**分辨率归一化**——检测前统一 resize 到 ``blur.resize_to``；
  4. 相机卡顿是**相对中位数**的自适应判定，与全局绝对阈值互补。

帧以流式方式消费（sink），不驻留内存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from ..config import iter_entries
from ..reader import McapScan, NS_PER_SECOND, ScanPlan
from ..rules import Threshold
from .base import Metric, MetricResult, Severity, resolve_thresholds, threshold_from

CATEGORY = "camera"

_LUMA_WEIGHTS = np.array([0.299, 0.587, 0.114], dtype=np.float32)
_LAPLACIAN_KERNEL = np.array([[0.0, 1.0, 0.0], [1.0, -4.0, 1.0], [0.0, 1.0, 0.0]], dtype=np.float32)


def to_gray(image: np.ndarray) -> np.ndarray:
    """RGB/BGR uint8 → 灰度 float32（0~255）。"""
    array = np.asarray(image)
    if array.ndim == 2:
        return array.astype(np.float32)
    if array.ndim == 3 and array.shape[2] >= 3:
        return (array[..., :3].astype(np.float32) * _LUMA_WEIGHTS).sum(axis=2)
    raise ValueError(f"unsupported image shape {array.shape}")


def resize_nearest(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    height, width = int(size[0]), int(size[1])
    if height <= 0 or width <= 0:
        raise ValueError(f"invalid resize target {size}")
    source_h, source_w = image.shape[:2]
    if (source_h, source_w) == (height, width):
        return image
    rows = (np.arange(height) * source_h // height).astype(np.int64)
    cols = (np.arange(width) * source_w // width).astype(np.int64)
    return image[np.ix_(rows, cols)]


def laplacian_var(gray: np.ndarray) -> float:
    """拉普拉斯方差，用作清晰度指标。"""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    center = gray[1:-1, 1:-1]
    response = (
        gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4.0 * center
    )
    return float(response.var())


def mean_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return float("inf")
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())


def decode_frame(message: Any, state: "DecoderState") -> np.ndarray | None:
    """把一条图像/视频消息解码成 HxWx3 的 uint8 RGB；返回 None 表示无独立画面。"""
    data = getattr(message, "data", None)
    if data is None:
        raise ValueError("message has no data field")
    payload = bytes(data)

    image_format = str(getattr(message, "format", "") or "").lower()
    if any(name in image_format for name in ("h264", "h265", "hevc")):
        return state.decode_video(image_format, payload)
    if "jpeg" in image_format or "jpg" in image_format:
        return _decode_jpeg(payload)

    encoding = str(getattr(message, "encoding", "") or "").lower()
    if encoding:
        return _decode_raw(message, payload, encoding)
    raise ValueError(f"unsupported image message: format={image_format!r} encoding={encoding!r}")


def _decode_jpeg(payload: bytes) -> np.ndarray:
    from PIL import Image  # 延迟导入：只有 JPEG 相机才需要
    from io import BytesIO

    with Image.open(BytesIO(payload)) as image:
        return np.asarray(image.convert("RGB"))


def _decode_raw(message: Any, payload: bytes, encoding: str) -> np.ndarray:
    height = int(getattr(message, "height", 0) or 0)
    width = int(getattr(message, "width", 0) or 0)
    step = int(getattr(message, "step", 0) or 0) or width * 3
    if height <= 0 or width <= 0:
        raise ValueError("raw image has no width/height")
    raw = np.frombuffer(payload, dtype=np.uint8)
    if raw.size != height * step or step < width * 3:
        raise ValueError(f"invalid raw image: size={raw.size} height={height} step={step}")
    image = raw.reshape(height, step)[:, : width * 3].reshape(height, width, 3)
    if encoding == "bgr8":
        image = image[..., ::-1]
    elif encoding not in ("rgb8",):
        raise ValueError(f"unsupported raw encoding {encoding!r}")
    return image


class DecoderState:
    """h264/h265 的 P 帧依赖同一 GOP 的 I 帧，解码器状态要跨消息保留。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self._codec = None
        self._codec_name: str | None = None

    def decode_video(self, image_format: str, payload: bytes) -> np.ndarray | None:
        import av  # 延迟导入：只有开启相机类才需要

        codec_name = "h265" if ("h265" in image_format or "hevc" in image_format) else "h264"
        if self._codec is None or self._codec_name != codec_name:
            self._codec = av.CodecContext.create(codec_name, "r")
            self._codec_name = codec_name
        frames = self._codec.decode(av.packet.Packet(payload))
        if not frames:
            return None
        if len(frames) > 1:
            raise ValueError(
                f"{self.label}: one message decoded into {len(frames)} frames; "
                "expected one frame per message (I/P frames only, no B frames)"
            )
        return frames[-1].to_ndarray(format="rgb24")


@dataclass
class CameraSink:
    """流式消费一个相机 topic 的帧，只累计计数与统计量。"""

    topic: str
    section: dict               # 算法参数（blur/black/duplicate/stutter…），仍是段落级
    evaluate: Callable[..., MetricResult]
    thresholds: dict            # 已按「段落 → group → 条目」解析好的阈值表
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._decoder = DecoderState(self.topic)
        self._resize_to = self._size()
        self._max_frames = self.section.get("max_frames")
        self._prev_gray: np.ndarray | None = None
        self._prev_hash: str | None = None
        self._attempted = 0
        self._failures = 0
        self._duplicates = 0
        self._black = 0
        self._blur = 0
        self._intervals: list[int] = []
        self._log_minus_header: list[int] = []
        self._prev_stamp: int | None = None

    def _size(self) -> tuple[int, int]:
        size = (self.section.get("blur") or {}).get("resize_to") or (224, 224)
        return int(size[0]), int(size[1])

    def add(self, log_time_ns: int, stamp_ns: int | None, message: Any) -> None:
        if self._max_frames is not None and self._attempted >= int(self._max_frames):
            return
        self._attempted += 1
        if message is None:
            self._failures += 1
            return
        try:
            frame = decode_frame(message, self._decoder)
        except Exception as exc:  # noqa: BLE001 - 解码失败按指标处理，不中断扫描
            self._failures += 1
            text = f"{self.topic}: decode failed: {type(exc).__name__}: {exc}"
            if text not in self.errors:
                self.errors.append(text)
            return
        if frame is None:  # 流首还没碰到 I 帧，不构成独立画面
            return

        try:
            gray = to_gray(resize_nearest(np.asarray(frame), self._resize_to))
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            self.errors.append(f"{self.topic}: frame processing failed: {exc}")
            return

        duplicate_cfg = self.section.get("duplicate") or {}
        if self._prev_gray is not None:
            if str(duplicate_cfg.get("method", "pixel_diff")).lower() == "hash":
                if _hash(frame) == getattr(self, "_prev_hash", None):
                    self._duplicates += 1
            elif mean_abs_diff(gray, self._prev_gray) < float(duplicate_cfg.get("pixel_diff_eps", 1.0)):
                self._duplicates += 1
        self._prev_hash = _hash(frame)

        black_cfg = self.section.get("black") or {}
        if gray.mean() < float(black_cfg.get("max_mean_luma", 10.0)) and gray.std() < float(
            black_cfg.get("max_std_luma", 5.0)
        ):
            self._black += 1

        if self._use_opencv():
            lap = _cv2_laplacian_var(gray)
        else:
            lap = laplacian_var(gray)
        if lap < float((self.section.get("blur") or {}).get("min_laplacian_var", 100.0)):
            self._blur += 1

        self._prev_gray = gray
        if stamp_ns is not None:
            if self._prev_stamp is not None:
                self._intervals.append(int(stamp_ns - self._prev_stamp))
            self._prev_stamp = int(stamp_ns)
            self._log_minus_header.append(int(log_time_ns - int(stamp_ns)))

    def _use_opencv(self) -> bool:
        return str(self.section.get("backend", "av")).lower() == "opencv"

    def results(self) -> list[MetricResult]:
        analyzed = max(0, self._attempted - self._failures)
        detail = {"frames": self._attempted, "analyzed": analyzed, "errors": self.errors[:3]}
        intervals = np.asarray(self._intervals, dtype=np.int64)
        factor = float((self.section.get("stutter") or {}).get("interval_factor", 2.0))
        stutter = 0
        if intervals.size >= 3:
            median = float(np.median(intervals))
            stutter = int(np.count_nonzero(intervals > factor * median)) if median > 0 else 0

        offsets_ms = np.asarray(self._log_minus_header, dtype=np.float64) / 1e6
        p95_offset = float(np.percentile(offsets_ms, 95)) if offsets_ms.size else None
        drift = float(offsets_ms.max() - offsets_ms.min()) if offsets_ms.size else None

        ev = self._ev
        return [
            ev("decode_failure_count", self._failures, unit="count", detail=detail),
            ev(
                "duplicate_ratio",
                self._duplicates / max(1, analyzed - 1) if analyzed > 1 else 0.0,
                unit="ratio",
                detail=detail,
            ),
            ev("black_ratio", self._black / max(1, analyzed), unit="ratio", detail=detail),
            ev("blur_ratio", self._blur / max(1, analyzed), unit="ratio", detail=detail),
            ev("stutter_count", stutter, unit="count", detail=detail),
            ev("log_minus_header_p95_ms", p95_offset, unit="ms", detail=detail),
            ev("log_minus_header_drift_ms", drift, unit="ms", detail=detail),
        ]

    def _ev(self, name: str, value, *, unit: str, detail=None) -> MetricResult:
        """按本相机解析好的阈值表评估（段落 → group → 逐 topic 覆盖）。"""
        return self.evaluate(
            name,
            value,
            subject=self.topic,
            unit=unit,
            detail=detail,
            threshold=threshold_from(self.thresholds, name),
        )


def _hash(frame: np.ndarray) -> str:
    import hashlib

    return hashlib.md5(np.ascontiguousarray(frame).tobytes()).hexdigest()


def _cv2_laplacian_var(gray: np.ndarray) -> float:
    import cv2

    return float(cv2.Laplacian(gray.astype(np.float32), cv2.CV_32F).var())


class CameraMetric(Metric):
    category = CATEGORY
    section_key = "camera_metrics"

    def __init__(self, config, *, warn_ratio: float = 0.8):
        super().__init__(config, warn_ratio=warn_ratio)
        self._sinks: dict[str, CameraSink] = {}

    def is_active(self) -> bool:
        return bool(self.section.get("topics"))

    def skip_reason(self) -> str | None:
        if self.is_active():
            return None
        return "camera.topics 为空：相机类需显式列出要解码检查的 topic"

    def _camera_topics(self) -> list[tuple[str, dict]]:
        """``camera.topics`` 与 ②⑤⑥ 同构：{topic: spec} 映射或 spec 列表。"""
        return list(iter_entries(self.section, "topics"))

    def _topic_thresholds(self, entry: dict) -> dict:
        """段落 thresholds → group（按 camera.topics.<t>.group）→ 逐 topic 覆盖。"""
        return resolve_thresholds(self.section, entry)

    def plan(self) -> ScanPlan:
        ratio = float(self.section.get("sample_ratio") or 1.0)
        step = max(1, int(round(1.0 / ratio))) if 0 < ratio <= 1.0 else 1
        return ScanPlan(
            camera_topics=tuple(name for name, _ in self._camera_topics()),
            camera_sample_step=step,
        )

    def sinks(self) -> dict[str, CameraSink]:
        if not self._sinks:
            for topic, entry in self._camera_topics():
                self._sinks[topic] = CameraSink(
                    topic=topic,
                    section=self.section,
                    evaluate=self._evaluate_frame,
                    thresholds=self._topic_thresholds(entry),
                )
        return self._sinks

    def _evaluate_frame(
        self, name: str, value, *, subject: str, unit: str, detail=None, threshold=None
    ):
        return self.evaluate(
            name, value, subject=subject, unit=unit, detail=detail, threshold=threshold
        )

    def run(self, scan: McapScan) -> list[MetricResult]:
        results: list[MetricResult] = list(self._skew_results(scan))
        for sink in self.sinks().values():
            results.extend(sink.results())
        return results

    def _skew_results(self, scan: McapScan) -> list[MetricResult]:
        """相机间时间戳偏差：用采集时间戳互为参考做最近邻比对（无需解码）。"""
        topics = [
            topic
            for topic in (name for name, _ in self._camera_topics())
            if topic in scan.topics and scan.topics[topic].has_stamps
        ]
        if len(topics) < 2:
            return []
        reference = np.sort(np.asarray(scan.topics[topics[0]].stamp_ns, dtype=np.int64))
        worst = np.zeros(reference.size, dtype=np.int64)
        for topic in topics[1:]:
            others = np.sort(np.asarray(scan.topics[topic].stamp_ns, dtype=np.int64))
            deltas = _nearest_delta_ns(reference, others)
            worst = np.maximum(worst, deltas)
        if worst.size == 0:
            return []
        skew_ms = worst.astype(np.float64) / 1e6
        subject = ",".join(topics)
        return [
            self.evaluate("skew_p95_ms", float(np.percentile(skew_ms, 95)), subject=subject, unit="ms"),
            self.evaluate("skew_max_ms", float(skew_ms.max()), subject=subject, unit="ms"),
        ]


def _nearest_delta_ns(reference: np.ndarray, others: np.ndarray) -> np.ndarray:
    position = np.searchsorted(others, reference)
    right = np.clip(position, 0, others.size - 1)
    left = np.clip(position - 1, 0, others.size - 1)
    return np.minimum(np.abs(reference - others[right]), np.abs(reference - others[left]))
