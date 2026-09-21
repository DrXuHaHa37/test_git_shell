"""0 类：文件完整性（恒启用，不参与 categories 开关）。"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import QcConfig, integrity_thresholds
from ..reader import McapScan
from ..rules import Threshold, evaluate
from ..verdict import StatusCode
from .base import MetricResult, MetricStatus, Severity

CATEGORY = "integrity"


@dataclass(frozen=True)
class IntegrityOutcome:
    results: tuple[MetricResult, ...]
    status: StatusCode | None  # None 表示完整性通过


def check(scan: McapScan, config: QcConfig) -> IntegrityOutcome:
    """按「可解析 → 未截断 → 结构完整 → 非空」的顺序判定，返回要短路的状态码。

    5 项指标的判定线来自 ``integrity.thresholds``（缺省值见
    ``config.INTEGRITY_DEFAULT_THRESHOLDS``），不再写死在代码里。
    """
    section = config.section("integrity")
    require_summary = bool(section.get("require_summary", True))
    required_topics = tuple(section.get("required_topics") or ())
    spec = _threshold_specs(config, require_summary)

    open_errors = [text for text in scan.errors if text.startswith("open failed")]
    header_errors = [text for text in scan.errors if text.startswith("header unreadable")]
    readable = 0 if (open_errors or header_errors or scan.header is None) else 1

    if not scan.seekable:
        not_truncated = None  # 流式输入无法判断
    elif scan.iteration_error is not None:
        not_truncated = 0
    else:
        not_truncated = 1 if scan.has_footer else 0

    summary_present = 1 if scan.summary is not None and scan.summary.statistics is not None else 0
    present_topics = set(scan.topics)
    missing_topics = tuple(topic for topic in required_topics if topic not in present_topics)

    results: list[MetricResult] = [
        evaluate(
            spec["readable"],
            readable,
            category=CATEGORY,
            name="readable",
            unit="bool",
            detail={"errors": open_errors + header_errors[:1]} if readable == 0 else {},
        ),
        evaluate(
            spec["not_truncated"],
            not_truncated,
            category=CATEGORY,
            name="not_truncated",
            unit="bool",
            detail=({"error": scan.iteration_error} if scan.iteration_error else {}),
        ),
        evaluate(
            spec["summary_present"],
            summary_present,
            category=CATEGORY,
            name="summary_present",
            unit="bool",
            detail={"require_summary": require_summary},
        ),
        evaluate(
            spec["message_count"],
            int(scan.message_count),
            category=CATEGORY,
            name="message_count",
            unit="count",
        ),
        evaluate(
            spec["missing_topics_count"],
            len(missing_topics),
            category=CATEGORY,
            name="missing_topics_count",
            unit="count",
            detail={"missing": list(missing_topics)},
        ),
    ]

    status: StatusCode | None = None
    if _failed(results[0]) or _failed(results[1]):
        status = StatusCode.CORRUPTED
    elif _failed(results[2]) or _failed(results[4]):
        status = StatusCode.INCOMPLETE_STRUCTURE
    elif _failed(results[3]):
        status = StatusCode.EMPTY
    return IntegrityOutcome(results=tuple(results), status=status)


def _threshold_specs(config: QcConfig, require_summary: bool) -> dict[str, Threshold]:
    """取 5 项完整性指标的阈值；YAML 未列的项用内置缺省。

    ``summary_present`` 特殊：未显式配置时，严重级别沿用 ``require_summary``
    （true → error / false → warn），显式写了则以 YAML 为准。
    """
    specs = integrity_thresholds(config)
    if "summary_present" not in (config.section("integrity").get("thresholds") or {}):
        specs["summary_present"] = {
            "equals": 1,
            "severity": "error" if require_summary else "warn",
        }
    return {
        name: Threshold.from_spec(value, default_severity=Severity.ERROR)
        for name, value in specs.items()
    }


def _failed(result: MetricResult) -> bool:
    return result.status is MetricStatus.FAILED
