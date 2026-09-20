"""阈值比较与严重级别：把「实测值 × 阈值」变成 MetricResult。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .metrics.base import MetricResult, MetricStatus, Severity


@dataclass(frozen=True)
class Threshold:
    minimum: float | None = None
    maximum: float | None = None
    equals: float | None = None
    severity: Severity = Severity.ERROR

    @classmethod
    def from_spec(
        cls, spec: Any, *, default_severity: Severity = Severity.ERROR
    ) -> "Threshold | None":
        if not isinstance(spec, dict):
            return None
        minimum = _as_float(spec.get("min"))
        maximum = _as_float(spec.get("max"))
        equals = _as_float(spec.get("equals", spec.get("eq")))
        if minimum is None and maximum is None and equals is None:
            return None
        return cls(
            minimum=minimum,
            maximum=maximum,
            equals=equals,
            severity=Severity.parse(spec.get("severity"), default=default_severity),
        )

    def as_dict(self) -> dict[str, Any]:
        spec: dict[str, Any] = {"severity": self.severity.value}
        if self.minimum is not None:
            spec["min"] = self.minimum
        if self.maximum is not None:
            spec["max"] = self.maximum
        if self.equals is not None:
            spec["equals"] = self.equals
        return spec

    def format(self) -> str:
        if self.minimum is not None and self.maximum is not None:
            return f"{_fmt(self.minimum)}~{_fmt(self.maximum)}"
        if self.minimum is not None:
            return f"min {_fmt(self.minimum)}"
        if self.maximum is not None:
            return f"max {_fmt(self.maximum)}"
        if self.equals is not None:
            return f"== {_fmt(self.equals)}"
        return "-"

    def violated(self, value: float) -> tuple[str, float] | None:
        if self.equals is not None and value != self.equals:
            return "equals", self.equals
        if self.minimum is not None and value < self.minimum:
            return "min", self.minimum
        if self.maximum is not None and value > self.maximum:
            return "max", self.maximum
        return None

    def near_limit(self, value: float, warn_ratio: float) -> tuple[str, float] | None:
        """通过但已逼近阈值时返回 (op, bound)，用于趋势预警。

        尺度选择很关键：
          * 双侧区间用**区间宽度**——[90,110] 里的 100 处于中部，不算逼近；
            若误用 bound 自身的比例，区间中部也会被判成逼近。
          * 单侧阈值用 bound 自身的比例——100ms 阈值实测 85ms（0.85）→ 预警。
        """
        if not 0.0 < warn_ratio < 1.0:
            return None
        margin = 1.0 - warn_ratio
        if self.minimum is not None and self.maximum is not None:
            span = self.maximum - self.minimum
            if span <= 0:
                return None
            if value <= self.minimum + margin * span:
                return "min", self.minimum
            if value >= self.maximum - margin * span:
                return "max", self.maximum
            return None
        if self.maximum is not None and self.maximum > 0 and value >= warn_ratio * self.maximum:
            return "max", self.maximum
        if self.minimum is not None and self.minimum > 0 and value <= self.minimum / warn_ratio:
            return "min", self.minimum
        return None


def evaluate(
    threshold: "Threshold | None",
    value: float | int | None,
    *,
    category: str,
    name: str,
    subject: str = "-",
    unit: str = "",
    detail: dict | None = None,
    warn_ratio: float = 0.8,
) -> MetricResult:
    payload = dict(detail or {})
    if threshold is None:
        payload.setdefault("reason", "no threshold configured")
        return MetricResult(
            category=category,
            name=name,
            subject=subject,
            value=value,
            unit=unit,
            status=MetricStatus.SKIPPED,
            detail=payload,
        )
    if value is None:
        payload.setdefault("reason", "value not computable")
        return MetricResult(
            category=category,
            name=name,
            subject=subject,
            value=None,
            unit=unit,
            status=MetricStatus.SKIPPED,
            threshold=threshold.as_dict(),
            severity=threshold.severity,
            detail=payload,
        )

    number = float(value)
    violation = threshold.violated(number)
    if violation is not None:
        op, bound = violation
        payload["op"] = op
        payload["bound"] = bound
        status = MetricStatus.FAILED if threshold.severity is Severity.ERROR else MetricStatus.WARN
    else:
        payload["op"] = _primary_op(threshold)
        near = threshold.near_limit(number, warn_ratio)
        if near is not None:
            payload["near_limit"] = {"op": near[0], "bound": near[1]}
        status = MetricStatus.PASSED
    return MetricResult(
        category=category,
        name=name,
        subject=subject,
        value=value,
        unit=unit,
        status=status,
        threshold=threshold.as_dict(),
        severity=threshold.severity,
        detail=payload,
    )


def _primary_op(threshold: Threshold) -> str:
    if threshold.equals is not None:
        return "equals"
    if threshold.minimum is not None and threshold.maximum is not None:
        return "min~max"
    if threshold.minimum is not None:
        return "min"
    if threshold.maximum is not None:
        return "max"
    return "-"


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: float) -> str:
    return f"{value:g}"
