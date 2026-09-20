"""⑤ 关节指标：限位越界次数与速度统计（逐 topic、逐关节产出）。

同一 `joint` 类别可描述多种构型（如手 21 维 + 臂 14 维）：关节规格按 topic 解析，
topic 可用 `group` 引用 `joint.groups.<name>` 复用左右手/左右臂一致的限位与阈值。
"""

from __future__ import annotations

import numpy as np

from ..config import TimeSource
from ..reader import FieldRequest, McapScan, NS_PER_SECOND, ScanPlan
from ..rules import Threshold
from .base import (
    Metric,
    MetricResult,
    Severity,
    resolve_defaults,
    resolve_thresholds,
    resolve_value,
    skipped,
    stack_values,
)

CATEGORY = "joint"


class JointMetric(Metric):
    category = CATEGORY
    section_key = "joint_metrics"

    def is_active(self) -> bool:
        return bool(self.section.get("topics")) and self._has_thresholds()

    def skip_reason(self) -> str | None:
        if self.is_active():
            return None
        if not self.section.get("topics"):
            return "joint.topics 为空：需配置 {name, position_field} 及关节限位"
        return "joint 无可用阈值：需在 joint.thresholds 或 joint.groups.*.thresholds 配置"

    def _has_thresholds(self) -> bool:
        if self.thresholds:
            return True
        for group in (self.section.get("groups") or {}).values():
            if isinstance(group, dict) and group.get("thresholds"):
                return True
        return False

    def plan(self) -> ScanPlan:
        requests: list[FieldRequest] = []
        for entry in self.section.get("topics") or ():
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            topic = str(entry["name"])
            requests.append(
                FieldRequest(topic=topic, path=str(entry.get("position_field", "position")), store=True)
            )
            if entry.get("velocity_field"):
                requests.append(
                    FieldRequest(topic=topic, path=str(entry["velocity_field"]), store=True)
                )
        return ScanPlan(fields=tuple(requests))

    def run(self, scan: McapScan) -> list[MetricResult]:
        results: list[MetricResult] = []
        for entry in self.section.get("topics") or ():
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            results.extend(self._topic_results(scan, entry))
        return results

    def _topic_results(self, scan: McapScan, entry: dict) -> list[MetricResult]:
        topic = str(entry["name"])
        series = scan.topics.get(topic)
        position_path = str(entry.get("position_field", "position"))
        if series is None or series.count == 0:
            return [skipped(CATEGORY, "out_of_range_count", subject=topic, reason="topic missing")]

        source = TimeSource.parse(
            entry.get("time_source"),
            default=self.config.time_source_for(category=CATEGORY, topic=topic),
        )
        order = series.order_for(source)
        times = np.asarray(series.time_ns(source), dtype=np.int64)[order]
        positions = self._values(scan, topic, position_path, order)
        if positions is not None and positions.shape[0] != times.size:
            times = times[: positions.shape[0]]  # 长度不一致的行已被丢弃
        if positions is None or positions.size == 0:
            return [
                skipped(
                    CATEGORY,
                    "out_of_range_count",
                    subject=topic,
                    reason=f"field {position_path!r} unavailable",
                )
            ]

        velocity_path = entry.get("velocity_field")
        velocities = self._values(scan, topic, str(velocity_path), order) if velocity_path else None
        if velocities is not None and velocities.shape != positions.shape:
            velocities = None
        if velocities is None:
            velocities = self._diff_velocity(times, positions)

        source = self._spec_source(entry)
        results: list[MetricResult] = []
        for index in range(positions.shape[1]):
            spec = self._joint_spec(index, positions.shape[1], source)
            column = positions[:, index]
            out_of_range = int(
                np.count_nonzero((column < spec["lower"]) | (column > spec["upper"]))
            )
            speed = np.abs(velocities[:, index]) if velocities.shape == positions.shape else np.empty(0)
            subject = f"joint[{spec['name']}]"
            results.append(
                self.evaluate(
                    "out_of_range_count",
                    out_of_range,
                    subject=subject,
                    unit="count",
                    threshold=self._threshold("out_of_range_count", spec, source["thresholds"]),
                )
            )
            results.append(
                self.evaluate(
                    "max_velocity",
                    float(speed.max()) if speed.size else None,
                    subject=subject,
                    unit="rad/s",
                    threshold=self._threshold("max_velocity", spec, source["thresholds"]),
                )
            )
            results.append(
                self.evaluate(
                    "velocity_p95",
                    float(np.percentile(speed, 95)) if speed.size else None,
                    subject=subject,
                    unit="rad/s",
                    threshold=self._threshold("velocity_p95", spec, source["thresholds"]),
                )
            )
        return results

    def _values(self, scan: McapScan, topic: str, path: str, order: np.ndarray) -> np.ndarray | None:
        rows = scan.values.get((topic, path))
        if not rows:
            return None
        ordered = [rows[i] for i in order.tolist()]
        matrix, _ = stack_values(ordered)
        if matrix.size == 0 or matrix.shape[1] == 0:
            return None
        return matrix

    @staticmethod
    def _diff_velocity(times: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """时间戳差分求速度；Δt == 0 的帧跳过，避免除零产生 inf 污染 max。"""
        if positions.shape[0] < 2:
            return np.empty((0, positions.shape[1]), dtype=np.float64)
        deltas = np.diff(positions, axis=0)
        dt = np.diff(times).astype(np.float64) / NS_PER_SECOND
        valid = dt > 0
        if not np.any(valid):
            return np.empty((0, positions.shape[1]), dtype=np.float64)
        return deltas[valid] / dt[valid][:, None]

    def _spec_source(self, entry: dict) -> dict:
        """关节规格统一走三层解析：段落 → group → 条目（见 base.resolve_*）。

        ``joints`` 是整体生效的键（取最具体的一层），``defaults`` / ``thresholds`` 按 key 逐项覆盖。
        """
        return {
            "defaults": resolve_defaults(self.section, entry),
            "joints": resolve_value(self.section, entry, "joints") or (),
            "thresholds": resolve_thresholds(self.section, entry),
        }

    def _joint_spec(self, index: int, total: int, source: dict) -> dict:
        """``velocity_limit`` 是**结构限速**（URDF/MJCF 的物理上限），不是指标阈值。

        名字必须与指标名 ``max_velocity`` 区分：两者是两个概念，
        且结构限速非空时会覆盖 ``thresholds.max_velocity.max``（见 _threshold）。
        """
        defaults = source.get("defaults") or {}
        spec = {
            "name": f"idx{index}",
            "lower": defaults.get("lower", -3.14),
            "upper": defaults.get("upper", 3.14),
            "velocity_limit": defaults.get("velocity_limit"),
        }
        for entry in source.get("joints") or ():
            if isinstance(entry, dict) and int(entry.get("index", -1)) == index:
                spec.update(
                    {
                        "name": str(entry.get("name", f"idx{index}")),
                        "lower": entry.get("lower", spec["lower"]),
                        "upper": entry.get("upper", spec["upper"]),
                        "velocity_limit": entry.get("velocity_limit", spec["velocity_limit"]),
                    }
                )
                break
        spec["index"] = index
        spec["total"] = total
        return spec

    def _threshold(self, name: str, spec: dict, thresholds: dict) -> Threshold | None:
        """逐关节结构限速（velocity_limit）非空时，覆盖 max_velocity 指标的 max。"""
        merged = dict(thresholds.get(name) or {})
        if name == "max_velocity" and spec.get("velocity_limit") is not None:
            merged["max"] = spec["velocity_limit"]
        if not merged:
            return None
        return Threshold.from_spec(merged, default_severity=Severity.ERROR)
