"""QcReport：一次检测的最终产物与序列化。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .metrics.base import MetricResult, MetricStatus
from .verdict import StatusCode


@dataclass(frozen=True)
class QcReport:
    inspect_id: str
    source: str
    status: StatusCode
    metrics: tuple[MetricResult, ...]
    started_at: str
    elapsed_ms: float
    profile: str
    enabled_categories: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def code(self) -> int:
        return int(self.status)

    @property
    def ok(self) -> bool:
        return not self.status.is_rejected()

    @property
    def failed_metrics(self) -> tuple[str, ...]:
        return tuple(r.label for r in self.metrics if r.status is MetricStatus.FAILED)

    @property
    def warn_metrics(self) -> tuple[str, ...]:
        return tuple(r.label for r in self.metrics if r.status is MetricStatus.WARN)

    @property
    def skipped_metrics(self) -> tuple[str, ...]:
        return tuple(r.label for r in self.metrics if r.status is MetricStatus.SKIPPED)

    @property
    def near_limit_metrics(self) -> tuple[str, ...]:
        """通过但已逼近阈值的指标（趋势预警，不影响判定码）。"""
        return tuple(r.label for r in self.metrics if r.detail.get("near_limit"))

    def metric_value(self, name: str, default=None):
        for result in self.metrics:
            if result.name == name:
                return result.value
        return default

    def to_dict(self) -> dict:
        return {
            "inspect_id": self.inspect_id,
            "source": self.source,
            "code": self.code,
            "status": self.status.name,
            "profile": self.profile,
            "enabled_categories": list(self.enabled_categories),
            "started_at": self.started_at,
            "elapsed_ms": self.elapsed_ms,
            "failed_metrics": list(self.failed_metrics),
            "warn_metrics": list(self.warn_metrics),
            "near_limit_metrics": list(self.near_limit_metrics),
            "errors": list(self.errors),
            "metrics": [result.to_dict() for result in self.metrics],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, default=str)
