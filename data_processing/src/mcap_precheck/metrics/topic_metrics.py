"""② Topic 指标：每个 topic 单独统计频率、最大间隔、条数。"""

from __future__ import annotations

import numpy as np

from ..config import TimeSource
from ..reader import McapScan, NS_PER_SECOND
from ..rules import Threshold
from .base import Metric, MetricResult, Severity, resolve_thresholds, skipped

CATEGORY = "topic"

_FALLBACK_STAMP_ROLLBACK = Threshold(maximum=0, severity=Severity.WARN)


class TopicMetric(Metric):
    category = CATEGORY
    section_key = "topic_metrics"

    def __init__(self, config, *, warn_ratio: float = 0.8):
        super().__init__(config, warn_ratio=warn_ratio)
        self._camera_topics = frozenset(self.config.section("camera_metrics").get("topics") or ())
        self._joint_topics = frozenset(
            entry.get("name")
            for entry in (self.config.section("joint_metrics").get("topics") or ())
            if isinstance(entry, dict)
        )

    def is_active(self) -> bool:
        return bool(self.section.get("catch_all", True)) or bool(self.section.get("topics"))

    def skip_reason(self) -> str | None:
        if self.is_active():
            return None
        return "topic_metrics.catch_all=false 且 topics 为空"

    def run(self, scan: McapScan) -> list[MetricResult]:
        configured = self.section.get("topics") or {}
        catch_all = bool(self.section.get("catch_all", True))
        targets: list[str] = []
        if catch_all:
            targets.extend(scan.topic_names)
        targets.extend(topic for topic in configured if topic not in scan.topics)
        if not targets:
            return []

        check_stamp_rollback = bool(self.section.get("check_stamp_rollback", False))
        results: list[MetricResult] = []
        for topic in targets:
            entry = configured.get(topic) or {}
            results.extend(
                self._topic_results(topic, scan.topics.get(topic), entry, check_stamp_rollback)
            )
        return results

    def _topic_results(
        self, topic: str, series, entry: dict, check_stamp_rollback: bool
    ) -> list[MetricResult]:
        if series is None or series.count == 0:
            # 缺失 topic 一律显式跳过：子集录制（如只有左手）不应判失败。
            # 早先这里依赖「topic_metrics 没有顶层 thresholds」才恰好 SKIPPED，属隐式行为。
            reason = "topic not present in file"
            return [
                skipped(CATEGORY, name, subject=topic, unit=unit, reason=reason)
                for name, unit in (
                    ("message_count", "count"),
                    ("rate_hz", "Hz"),
                    ("max_interval_ms", "ms"),
                )
            ]

        thresholds = resolve_thresholds(self.section, entry)
        source = self._time_source(topic)
        times = series.time_ns(source)
        count = int(times.size)
        duration_s = float(times[-1] - times[0]) / NS_PER_SECOND if count >= 2 else 0.0
        intervals = np.diff(times) if count >= 2 else np.empty(0, dtype=np.int64)
        max_interval_ms = float(intervals.max()) / 1e6 if intervals.size else 0.0
        rate_hz = (count - 1) / duration_s if duration_s > 0 else None

        results = [
            self.evaluate(
                "rate_hz",
                rate_hz,
                subject=topic,
                unit="Hz",
                threshold=self.pick_threshold(thresholds, "rate_hz"),
            ),
            self.evaluate(
                "max_interval_ms",
                max_interval_ms,
                subject=topic,
                unit="ms",
                threshold=self.pick_threshold(thresholds, "max_interval_ms"),
            ),
            self.evaluate(
                "message_count",
                count,
                subject=topic,
                unit="count",
                threshold=self.pick_threshold(thresholds, "message_count"),
            ),
        ]
        if check_stamp_rollback and series.has_stamps:
            stamps = series.stamp_ns[series.order_for(TimeSource.HEADER_STAMP)]
            rollbacks = int(np.count_nonzero(np.diff(stamps) < 0)) if stamps.size >= 2 else 0
            results.append(
                self.evaluate(
                    "stamp_rollback_count",
                    rollbacks,
                    subject=topic,
                    unit="count",
                    threshold=self.pick_threshold(thresholds, "stamp_rollback_count")
                    or _FALLBACK_STAMP_ROLLBACK,
                )
            )
        return results

    def _time_source(self, topic: str) -> TimeSource:
        if topic in self._camera_topics:
            return self.config.time_source_for(category="camera", topic=topic)
        if topic in self._joint_topics:
            return self.config.time_source_for(category="joint", topic=topic)
        return self.config.time_source_for(topic=topic)

    # 阈值解析统一走 base.resolve_thresholds：段落 thresholds → group → 条目 thresholds。
