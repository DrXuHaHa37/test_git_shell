"""指标结果的公共数据结构与 Metric 基类。

指标只负责**计算值**，阈值比较一律交给 :mod:`mcap_precheck.rules`。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Sequence

import numpy as np

from ..verdict import MetricStatus, Severity

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from ..config import QcConfig
    from ..reader import McapScan, ScanPlan

__all__ = [
    "Metric",
    "MetricResult",
    "MetricStatus",
    "Severity",
    "resolve_defaults",
    "resolve_group",
    "resolve_thresholds",
    "resolve_value",
    "skipped",
    "stack_values",
    "threshold_from",
]


def resolve_group(section: dict, entry: dict | None) -> dict:
    """条目通过 ``group`` 引用的命名配置片段；未引用或未定义时为空。"""
    if entry is None:
        return {}
    name = entry.get("group")
    if not name:
        return {}
    value = (section.get("groups") or {}).get(str(name))
    return value if isinstance(value, dict) else {}


def _merge(section: dict, entry: dict | None, key: str) -> dict:
    """``key`` 指向的映射按「段落 → group → 条目」逐项覆盖。"""
    merged = dict(section.get(key) or {})
    merged.update(resolve_group(section, entry).get(key) or {})
    if entry is not None:
        merged.update(entry.get(key) or {})
    return merged


def resolve_thresholds(section: dict, entry: dict | None) -> dict:
    """指标阈值：段落兜底 → group → 条目，按指标逐项覆盖。"""
    return _merge(section, entry, "thresholds")


def resolve_defaults(section: dict, entry: dict | None) -> dict:
    """非阈值的类别参数（如关节限位）：同样三层，按 key 逐项覆盖。"""
    return _merge(section, entry, "defaults")


def resolve_value(section: dict, entry: dict | None, key: str):
    """整体生效（不合并）的键，取最具体的一层：条目 > group > 段落。"""
    if entry is not None and entry.get(key) is not None:
        return entry[key]
    group = resolve_group(section, entry)
    if group.get(key) is not None:
        return group[key]
    return section.get(key)


def threshold_from(thresholds: dict, name: str):
    """从已合并好的阈值表里取一项。"""
    from ..rules import Threshold

    return Threshold.from_spec(thresholds.get(name), default_severity=Severity.ERROR)


@dataclass(frozen=True)
class MetricResult:
    category: str
    name: str
    subject: str = "-"
    value: float | int | None = None
    unit: str = ""
    status: MetricStatus = MetricStatus.SKIPPED
    threshold: dict | None = None
    severity: Severity = Severity.INFO
    detail: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """`category.metric`，用于日志中的 `failed_metrics` 列表。"""
        return f"{self.category}.{self.name}"

    @property
    def label(self) -> str:
        return self.key if self.subject in ("", "-") else f"{self.key}[{self.subject}]"

    @property
    def is_violation(self) -> bool:
        return self.status in (MetricStatus.WARN, MetricStatus.FAILED)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "metric": self.name,
            "subject": self.subject,
            "value": self.value,
            "unit": self.unit,
            "status": self.status.value,
            "threshold": self.threshold,
            "severity": self.severity.value,
            "detail": self.detail,
        }


def skipped(
    category: str,
    name: str,
    *,
    subject: str = "-",
    unit: str = "",
    reason: str = "",
) -> MetricResult:
    detail = {"reason": reason} if reason else {}
    return MetricResult(
        category=category,
        name=name,
        subject=subject,
        unit=unit,
        status=MetricStatus.SKIPPED,
        detail=detail,
    )


def stack_values(arrays: Sequence[np.ndarray]) -> tuple[np.ndarray, int]:
    """把逐条消息的数值向量堆成二维数组，丢弃长度与众数不一致的行。"""
    if not arrays:
        return np.empty((0, 0), dtype=np.float64), 0
    lengths = np.fromiter((int(np.asarray(a).size) for a in arrays), dtype=np.int64)
    target = int(np.bincount(lengths).argmax())
    keep = [np.asarray(a, dtype=np.float64).reshape(-1) for a, n in zip(arrays, lengths) if n == target]
    if not keep:
        return np.empty((0, target), dtype=np.float64), len(arrays)
    return np.stack(keep), len(arrays) - len(keep)


class Metric(ABC):
    """一个类别的指标集合。

    子类实现 :meth:`is_active` 与 :meth:`run`；需要 reader 额外采集数据时
    重写 :meth:`plan`。
    """

    category: ClassVar[str]
    section_key: ClassVar[str]

    def __init__(self, config: "QcConfig", *, warn_ratio: float = 0.8) -> None:
        self.config = config
        self.warn_ratio = warn_ratio

    @property
    def profile(self) -> dict[str, Any]:
        return self.config.data

    @property
    def section(self) -> dict[str, Any]:
        return self.config.section(self.section_key)

    @property
    def thresholds(self) -> dict[str, Any]:
        value = self.section.get("thresholds")
        return value if isinstance(value, dict) else {}

    def threshold(self, name: str):
        from ..rules import Threshold

        return Threshold.from_spec(self.thresholds.get(name), default_severity=Severity.ERROR)

    def pick_threshold(self, thresholds: dict, name: str):
        """从「段落 → group → 条目」合并后的阈值表里取一项。"""
        return threshold_from(thresholds, name)

    def evaluate(
        self,
        name: str,
        value: float | int | None,
        *,
        subject: str = "-",
        unit: str = "",
        threshold=None,
        detail: dict | None = None,
        severity=None,
    ) -> MetricResult:
        """用配置中的阈值（或显式传入的阈值）评估一个实测值。"""
        from ..rules import Threshold, evaluate

        spec = threshold if threshold is not None else self.threshold(name)
        if spec is None and severity is not None:
            spec = Threshold(severity=severity)
        return evaluate(
            spec,
            value,
            category=self.category,
            name=name,
            subject=subject,
            unit=unit,
            detail=detail,
            warn_ratio=self.warn_ratio,
        )

    def plan(self) -> "ScanPlan":
        from ..reader import ScanPlan

        return ScanPlan()

    @abstractmethod
    def is_active(self) -> bool:
        """配置是否足以运行本类别；false 时完全不执行、不产生日志噪声。"""

    def skip_reason(self) -> str | None:
        """`is_active()` 为 False 时给出原因——类别开关已开但领域配置为空是很常见的误配。"""
        return None if self.is_active() else f"{self.section_key} 未配置"

    @abstractmethod
    def run(self, scan: "McapScan") -> Sequence[MetricResult]:
        """基于单趟扫描结果计算本类别的全部指标。"""
