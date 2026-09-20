"""④ 非法数值：扫描指定字段（或全部数值字段）中的 NaN / Inf。"""

from __future__ import annotations

from ..reader import FieldRequest, McapScan, ScanPlan
from .base import Metric, MetricResult

CATEGORY = "numeric"


class NumericMetric(Metric):
    category = CATEGORY
    section_key = "numeric"

    @property
    def thresholds(self) -> dict:
        # numeric 的阈值直接写在类别根节点（numeric.nan_inf_count），没有 thresholds 子段
        return {key: value for key, value in self.section.items() if key != "scan_fields"}

    def is_active(self) -> bool:
        return self.threshold("nan_inf_count") is not None or bool(self.section.get("scan_fields"))

    def skip_reason(self) -> str | None:
        if self.is_active():
            return None
        return "numeric 未配置：需要 nan_inf_count 阈值或 scan_fields"

    def plan(self) -> ScanPlan:
        entries = self.section.get("scan_fields") or ()
        fields = tuple(
            FieldRequest(topic=str(entry["topic"]), path=str(entry.get("field", "value")))
            for entry in entries
            if isinstance(entry, dict) and entry.get("topic")
        )
        if fields:
            return ScanPlan(fields=fields)
        return ScanPlan(auto_numeric=True)

    def run(self, scan: McapScan) -> list[MetricResult]:
        threshold = self.threshold("nan_inf_count")
        entries = self.section.get("scan_fields") or ()
        if entries:
            return self._run_configured(scan, threshold, entries)
        return self._run_auto(scan, threshold)

    def _run_configured(self, scan: McapScan, threshold, entries) -> list[MetricResult]:
        results: list[MetricResult] = []
        total = 0
        for entry in entries:
            topic = str(entry.get("topic"))
            path = str(entry.get("field", "value"))
            key = (topic, path)
            count = scan.nan_inf_counts.get(key, 0)
            total += count
            results.append(
                self.evaluate(
                    "nan_inf_count",
                    count,
                    subject=f"{topic}:{path}",
                    unit="count",
                    threshold=threshold,
                    detail={"missing": scan.missing_fields.get(key, 0)},
                )
            )
        results.append(self.evaluate("nan_inf_count", total, unit="count", threshold=threshold))
        return results

    def _run_auto(self, scan: McapScan, threshold) -> list[MetricResult]:
        results: list[MetricResult] = []
        total = 0
        for topic, counts in sorted(scan.auto_numeric_counts.items()):
            topic_total = sum(counts.values())
            total += topic_total
            offenders = sorted(
                (name for name, value in counts.items() if value), key=lambda name: -counts[name]
            )
            results.append(
                self.evaluate(
                    "nan_inf_count",
                    topic_total,
                    subject=topic,
                    unit="count",
                    threshold=threshold,
                    detail={"fields": offenders[:10], "scanned_fields": len(counts)},
                )
            )
        results.append(self.evaluate("nan_inf_count", total, unit="count", threshold=threshold))
        return results
