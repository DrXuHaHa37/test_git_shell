"""① 全局指标：全部消息按时间排序后的统一序列统计。"""

from __future__ import annotations

import numpy as np

from ..reader import McapScan, NS_PER_SECOND
from .base import Metric, MetricResult

CATEGORY = "global"


class GlobalMetric(Metric):
    """全局指标。

    回退检测用**文件顺序**的 log_time（排序后回退恒为 0，检测不到异常）；
    间隔/丢帧用排序后的序列。
    """

    category = CATEGORY
    section_key = "global_metrics"

    def is_active(self) -> bool:
        return True

    def run(self, scan: McapScan) -> list[MetricResult]:
        file_order = scan.message_log_times_ns
        count = int(file_order.size)
        sorted_times = np.sort(file_order) if count else file_order

        duration_s = float(sorted_times[-1] - sorted_times[0]) / NS_PER_SECOND if count >= 2 else 0.0
        intervals_ns = np.diff(sorted_times) if count >= 2 else np.empty(0, dtype=np.int64)
        max_interval_ms = float(intervals_ns.max()) / 1e6 if intervals_ns.size else 0.0

        rollback_count = 0
        rollback_max_ns = 0
        if count >= 2:
            deltas = np.diff(file_order)
            negative = deltas[deltas < 0]
            rollback_count = int(negative.size)
            rollback_max_ns = int(-negative.min()) if negative.size else 0

        results = [
            self.evaluate("duration_s", duration_s, unit="s"),
            self.evaluate("timestamp_rollback_count", rollback_count, unit="count"),
            self.evaluate("timestamp_rollback_max_ns", rollback_max_ns, unit="ns"),
            self.evaluate("max_frame_interval_ms", max_interval_ms, unit="ms"),
            self.evaluate(
                "dropped_frame_count",
                _dropped_frames(self.section, count, duration_s, intervals_ns),
                unit="count",
            ),
            self.evaluate("message_count", count, unit="count"),
        ]
        return results


def _dropped_frames(
    section: dict, count: int, duration_s: float, intervals_ns: np.ndarray
) -> int | None:
    """未配置 ``expected_rate_hz`` 时返回 ``None`` → 指标 SKIPPED（不判失败）。"""
    rate = _as_float(section.get("expected_rate_hz"))
    if rate is None or rate <= 0 or count < 2:
        return None
    detect = section.get("drop_detect") or {}
    mode = str(detect.get("mode", "interval")).lower()
    if mode == "expected_count":
        expected = int(round(duration_s * rate))
        return max(0, expected - count)
    if mode != "interval":
        return None
    factor = _as_float(detect.get("factor")) or 1.5
    period_ns = NS_PER_SECOND / rate
    gaps = intervals_ns[intervals_ns > factor * period_ns]
    if gaps.size == 0:
        return 0
    missed = np.round(gaps / period_ns) - 1
    return int(np.maximum(missed, 0).sum())


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
