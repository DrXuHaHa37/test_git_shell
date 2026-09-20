"""MCAP 读取层：summary + 单趟消息迭代 + 按需解码。

一次迭代内累计所有非相机指标所需的原始数据；相机帧通过 sink 流式消费，
不驻留内存。
"""

from __future__ import annotations

import io
import logging
from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

import numpy as np
from mcap.exceptions import McapError
from mcap.reader import make_reader
from mcap.records import Channel, Message, Schema
from mcap.summary import Summary
from mcap_ros2.decoder import DecoderFactory

from .config import TimeSource
from .fields import FieldError, iter_numeric_fields, numeric_vector, select
from .source import Source

NS_PER_SECOND = 1_000_000_000
MAGIC = b"\x89MCAP0\r\n"

logger = logging.getLogger("mcap_precheck.reader")


@dataclass(frozen=True)
class FieldRequest:
    topic: str
    path: str
    store: bool = False  # True=保留数值（关节/命令-状态），False=只累计 NaN/Inf 计数

    @property
    def key(self) -> tuple[str, str]:
        return (self.topic, self.path)


@dataclass(frozen=True)
class ScanPlan:
    fields: tuple[FieldRequest, ...] = ()
    auto_numeric: bool = False
    camera_topics: tuple[str, ...] = ()
    camera_sample_step: int = 1

    def merge(self, other: "ScanPlan") -> "ScanPlan":
        merged: dict[tuple[str, str], FieldRequest] = {}
        for request in (*self.fields, *other.fields):
            existing = merged.get(request.key)
            merged[request.key] = (
                request if existing is None else FieldRequest(request.topic, request.path, True)
            )
        return ScanPlan(
            fields=tuple(merged.values()),
            auto_numeric=self.auto_numeric or other.auto_numeric,
            camera_topics=tuple(dict.fromkeys((*self.camera_topics, *other.camera_topics))),
            camera_sample_step=min(self.camera_sample_step, other.camera_sample_step),
        )

    @property
    def field_topics(self) -> dict[str, tuple[FieldRequest, ...]]:
        grouped: dict[str, list[FieldRequest]] = defaultdict(list)
        for request in self.fields:
            grouped[request.topic].append(request)
        return {topic: tuple(requests) for topic, requests in grouped.items()}

    @property
    def requests_store(self) -> bool:
        return any(request.store for request in self.fields)


@dataclass(frozen=True)
class TopicSeries:
    """单个 topic 的时间序列（按 log_time 稳定排序）。"""

    topic: str
    schema_name: str | None
    message_encoding: str
    log_time_ns: np.ndarray
    stamp_ns: np.ndarray | None = None

    @property
    def count(self) -> int:
        return int(self.log_time_ns.size)

    @property
    def has_stamps(self) -> bool:
        return self.stamp_ns is not None

    @property
    def duration_s(self) -> float:
        if self.count < 2:
            return 0.0
        return float(self.log_time_ns[-1] - self.log_time_ns[0]) / NS_PER_SECOND

    def time_ns(self, source: TimeSource) -> np.ndarray:
        if source is TimeSource.HEADER_STAMP and self.stamp_ns is not None:
            return self.stamp_ns
        return self.log_time_ns

    def order_for(self, source: TimeSource) -> np.ndarray:
        return np.argsort(self.time_ns(source), kind="stable")


@dataclass
class McapScan:
    name: str
    seekable: bool = True
    header: Any | None = None
    summary: Summary | None = None
    has_footer: bool = False
    topics: dict[str, TopicSeries] = field(default_factory=dict)
    values: dict[tuple[str, str], list[np.ndarray]] = field(default_factory=dict)
    nan_inf_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    auto_numeric_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    message_log_times_ns: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int64))
    missing_fields: dict[tuple[str, str], int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    iteration_error: str | None = None

    @property
    def message_count(self) -> int:
        return int(self.message_log_times_ns.size)

    @property
    def sorted_message_times_ns(self) -> np.ndarray:
        return np.sort(self.message_log_times_ns)

    @property
    def duration_s(self) -> float:
        if self.message_count < 2:
            return 0.0
        times = self.sorted_message_times_ns
        return float(times[-1] - times[0]) / NS_PER_SECOND

    @property
    def topic_names(self) -> tuple[str, ...]:
        return tuple(self.topics)


class FrameSink(Protocol):
    """相机帧的流式消费者（见 metrics.camera_metrics.CameraSink）。"""

    def add(self, log_time_ns: int, stamp_ns: int | None, message: Any) -> None: ...


def has_footer_magic(stream: Any) -> bool:
    """文件尾部 8 字节是否为 MCAP magic —— 用于判断截断。"""
    try:
        position = stream.tell()
        stream.seek(0, io.SEEK_END)
        size = stream.tell()
        if size < len(MAGIC):
            stream.seek(position)
            return False
        stream.seek(-len(MAGIC), io.SEEK_END)
        data = stream.read(len(MAGIC))
        stream.seek(position)
    except (OSError, ValueError, AttributeError):
        return False
    return bytes(data) == MAGIC


def scan(
    source: Source,
    plan: ScanPlan,
    sinks: dict[str, FrameSink] | None = None,
) -> McapScan:
    """单趟扫描：累计时间戳、字段值、相机帧。

    不会抛出 MCAP 解析异常——异常被记录进 ``McapScan.errors``，由完整性指标转成状态码。
    """
    sinks = sinks or {}
    result = McapScan(name=source.name, seekable=source.seekable)
    result.has_footer = has_footer_magic(source.stream) if source.seekable else False

    try:
        reader = make_reader(source.stream, decoder_factories=[DecoderFactory()])
    except (McapError, OSError, ValueError) as exc:
        result.errors.append(f"open failed: {exc}")
        return result

    try:
        result.header = reader.get_header()
    except (McapError, OSError, ValueError) as exc:
        result.errors.append(f"header unreadable: {exc}")
    try:
        result.summary = reader.get_summary()
    except (McapError, OSError, ValueError) as exc:
        result.errors.append(f"summary unreadable: {exc}")

    decoder = _Decoder()
    field_topics = plan.field_topics
    camera_topics = set(plan.camera_topics)
    decode_topics = set(field_topics) | camera_topics
    counters: dict[str, int] = defaultdict(int)
    camera_seen: dict[str, int] = defaultdict(int)
    encodings: dict[str, str] = {}
    schema_names = _schema_names(result.summary)
    log_times: dict[str, array] = defaultdict(lambda: array("q"))
    stamps: dict[str, array] = defaultdict(lambda: array("q"))
    stamp_missing: set[str] = set()
    values: dict[tuple[str, str], list[np.ndarray]] = defaultdict(list)
    missing: dict[tuple[str, str], int] = defaultdict(int)
    nan_inf: dict[tuple[str, str], int] = defaultdict(int)
    auto_counts: dict[str, dict[str, int]] = defaultdict(dict)
    file_order = array("q")

    try:
        for schema, channel, message in reader.iter_messages(log_time_order=False):
            topic = channel.topic
            encodings.setdefault(topic, channel.message_encoding)
            log_ns = int(message.log_time)
            file_order.append(log_ns)
            log_times[topic].append(log_ns)
            index = counters[topic]
            counters[topic] = index + 1

            decoded = None
            if plan.auto_numeric or topic in decode_topics:
                decoded = decoder.decode(schema, channel, message, result.errors)
            stamp_ns = _stamp_ns(decoded, message)
            if stamp_ns is None:
                stamp_missing.add(topic)
                stamps[topic].append(0)
            else:
                stamps[topic].append(stamp_ns)

            for request in field_topics.get(topic, ()):
                vector = None
                if decoded is not None:
                    try:
                        vector = numeric_vector(select(decoded, request.path))
                    except FieldError as exc:
                        missing[request.key] += 1
                        _warn_once(result, f"{topic}: {exc}")
                if vector is None:
                    if decoded is None:
                        missing[request.key] += 1
                    continue
                if request.store:
                    values[request.key].append(vector)
                else:
                    nan_inf[request.key] += int(np.count_nonzero(~np.isfinite(vector)))

            if plan.auto_numeric and decoded is not None and topic not in camera_topics:
                for path, vector in iter_numeric_fields(decoded):
                    bad = int(np.count_nonzero(~np.isfinite(vector)))
                    bucket = auto_counts.setdefault(topic, {})
                    bucket[path] = bucket.get(path, 0) + bad

            sink = sinks.get(topic)
            if sink is not None and index % max(1, plan.camera_sample_step) == 0:
                camera_seen[topic] += 1
                sink.add(log_ns, stamp_ns, decoded)
    except (McapError, OSError, ValueError, EOFError) as exc:
        result.iteration_error = str(exc)
        result.errors.append(f"iteration failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - 未预期异常也要转成结果而非抛出
        result.iteration_error = f"{type(exc).__name__}: {exc}"
        result.errors.append(f"iteration failed: {type(exc).__name__}: {exc}")

    result.message_log_times_ns = np.frombuffer(file_order, dtype=np.int64).copy()
    for topic, raw_log in log_times.items():
        log_array = np.frombuffer(raw_log, dtype=np.int64)
        order = np.argsort(log_array, kind="stable")
        order_list = order.tolist()
        stamp_array = None
        if topic not in stamp_missing:
            stamp_array = np.frombuffer(stamps[topic], dtype=np.int64)[order]
        result.topics[topic] = TopicSeries(
            topic=topic,
            schema_name=schema_names.get(topic),
            message_encoding=encodings.get(topic, "unknown"),
            log_time_ns=log_array[order],
            stamp_ns=stamp_array,
        )
        for (owner, path), rows in values.items():
            if owner == topic:
                result.values[(owner, path)] = [rows[i] for i in order_list]
    result.nan_inf_counts = dict(nan_inf)
    result.auto_numeric_counts = {topic: dict(counts) for topic, counts in auto_counts.items()}
    result.missing_fields = dict(missing)
    return result


def _schema_names(summary: Summary | None) -> dict[str, str]:
    if summary is None:
        return {}
    names: dict[str, str] = {}
    for channel in summary.channels.values():
        schema = summary.schemas.get(channel.schema_id)
        if schema is not None:
            names[channel.topic] = schema.name
    return names


def _stamp_ns(decoded: Any, message: Message) -> int | None:
    """消息自带采集时间戳：优先 header.stamp / timestamp，退回 MCAP publish_time。"""
    if decoded is not None:
        stamp = _header_stamp(decoded)
        if stamp is not None:
            return stamp
    publish_time = int(getattr(message, "publish_time", 0) or 0)
    return publish_time or None


def _header_stamp(message: Any) -> int | None:
    for candidate in (getattr(message, "header", None), message):
        if candidate is None:
            continue
        stamp = getattr(candidate, "stamp", None) or getattr(candidate, "timestamp", None)
        sec = getattr(stamp, "sec", None)
        nanosec = getattr(stamp, "nanosec", None)
        if sec is None or nanosec is None:
            continue
        try:
            return int(sec) * NS_PER_SECOND + int(nanosec)
        except (TypeError, ValueError):
            return None
    return None


def _warn_once(scan: McapScan, text: str) -> None:
    if text not in scan.errors:
        scan.errors.append(text)


class _Decoder:
    """按 channel 缓存解码器，解码失败只计数不中断扫描。"""

    def __init__(self) -> None:
        self._factory = DecoderFactory()
        self._callbacks: dict[int, Callable[[bytes], Any] | None] = {}

    def decode(
        self, schema: Schema | None, channel: Channel, message: Message, errors: list[str]
    ) -> Any | None:
        if channel.id not in self._callbacks:
            try:
                self._callbacks[channel.id] = self._factory.decoder_for(
                    channel.message_encoding, schema
                )
            except Exception as exc:  # noqa: BLE001
                self._callbacks[channel.id] = None
                errors.append(f"decoder unavailable for {channel.topic}: {exc}")
        callback = self._callbacks[channel.id]
        if callback is None:
            return None
        try:
            return callback(message.data)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"decode failed on {channel.topic}: {type(exc).__name__}: {exc}")
            return None
