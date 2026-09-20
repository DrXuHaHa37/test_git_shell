"""把检测结果渲染成日志事件——**领域相关**的部分留在 mcap_precheck。

通用传输（三通道、轮转、队列 listener、脱敏）在顶层 ``logger`` 包；
这里只负责：事件命名、指标字段布局、控制台的 ①②③ 表格。
"""

from __future__ import annotations

import logging
from typing import Any

from logger import LogSink, RunLog, adapt

from .metrics.base import MetricResult, MetricStatus, Severity
from .report import QcReport

_CATEGORY_LABELS = {
    "integrity": "⓪",
    "global": "①",
    "topic": "②",
    "camera": "③",
    "numeric": "④",
    "joint": "⑤",
    "cmd_state": "⑥",
}


class QcLog:
    """一次检测的日志入口：把 QcReport / MetricResult 渲染成通用日志事件。"""

    def __init__(self, run: RunLog, *, warn_ratio: float = 0.8) -> None:
        self._run = run
        self.warn_ratio = warn_ratio
        self._results: list[MetricResult] = []
        self._skipped: dict[str, str] = {}

    @property
    def inspect_id(self) -> str:
        return self._run.run_id

    def started(self, *, profile: str, categories: tuple[str, ...]) -> None:
        self._run.event("inspect.started", profile=profile, enabled_categories=list(categories))

    def config_loaded(self, *, path: str, profile: str) -> None:
        self._run.event("config.loaded", path=path, profile=profile)

    def metric(self, result: MetricResult) -> None:
        self._results.append(result)
        self._run.event(
            "metric.result",
            _level_for(result),
            category=result.category,
            metric=result.name,
            subject=result.subject,
            value=result.value,
            unit=result.unit,
            op=result.detail.get("op"),
            # 区间阈值（min~max）没有单一 bound，回显完整阈值
            threshold=result.detail.get("bound", result.threshold),
            status=result.status.value,
            severity=result.severity.value,
            detail={key: value for key, value in result.detail.items() if key not in ("op", "bound")},
        )

    def skipped(self, category: str, reason: str) -> None:
        """类别开关已开但配置不足，整类跳过。"""
        self._skipped[category] = reason
        self._run.event("metric.skipped", logging.INFO, category=category, reason=reason)

    def error(self, message: str) -> None:
        self._run.error(message)

    def finished(self, report: QcReport) -> None:
        self._run.event(
            "inspect.finished",
            logging.ERROR if report.status.is_rejected() else logging.INFO,
            code=report.code,
            status=report.status.name,
            elapsed_ms=report.elapsed_ms,
            failed=list(report.failed_metrics),
            warn=list(report.warn_metrics),
            near_limit=list(report.near_limit_metrics),
            errors=list(report.errors),
        )
        self._run.summary(
            code=report.code,
            status=report.status.name,
            profile=report.profile,
            enabled_categories=list(report.enabled_categories),
            duration_s=report.metric_value("duration_s"),
            message_count=report.metric_value("message_count"),
            elapsed_ms=report.elapsed_ms,
            failed_metrics=list(report.failed_metrics),
            warn_metrics=list(report.warn_metrics),
            near_limit_metrics=list(report.near_limit_metrics),
        )
        console, console_level = self.render_console(report)
        self._run.text(console, console_level)

    def render_console(self, report: QcReport) -> tuple[str, int]:
        """渲染控制台表格，返回 ``(文本, 级别)``。

        只渲染级别 >= ``LogSettings.level`` 的指标行：设成 WARNING 时通过的指标不再刷屏。
        整块文本的级别取「被留下的内容」的最高级别；全部被过滤掉则退回 INFO，
        于是 WARNING 档下这张表会被 console handler 挡掉（等于不打印）。
        """
        min_level = self._run.min_level
        shown = [result for result in self._results if _level_for(result) >= min_level]
        levels = [_level_for(result) for result in shown]
        if report.errors:
            levels.append(logging.ERROR)
        console_level = max(levels) if levels else logging.INFO

        lines = [f"[{report.inspect_id}] {self._run.display_source}   profile={report.profile}"]
        for category in report.enabled_categories:
            all_rows = [result for result in self._results if result.category == category]
            label = _CATEGORY_LABELS.get(category, "-")
            if not all_rows:
                lines.append(f"  {label} {category}   跳过：{self._skipped.get(category) or '无指标'}")
                continue
            rows = [result for result in all_rows if _level_for(result) >= min_level]
            if not rows:
                continue
            lines.append(f"  {label} {category}")
            for result in rows:
                lines.append(_format_line(result))
        lines.append("  " + "─" * 52)
        lines.append(f"  判定: {report.status.name} ({report.code})   耗时 {report.elapsed_ms:.0f} ms")
        for message in report.errors:
            lines.append(f"  错误: {message}")
        return "\n".join(lines), console_level


def as_emitter(logger: Any, inspect_id: str, source: str, *, warn_ratio: float = 0.8) -> QcLog:
    """把 ``inspect(logger=...)`` 的参数归一化成 QcLog。

    兼容 None / ``LogSink`` / ``RunLog`` / ``QcLog`` / 标准库 ``logging.Logger``。
    """
    if isinstance(logger, QcLog):
        return logger
    return QcLog(adapt(logger, inspect_id, source), warn_ratio=warn_ratio)


def batch_summary(sink: LogSink, reports: list[QcReport]) -> None:
    if not reports:
        return
    counts: dict[str, int] = {}
    for report in reports:
        counts[report.status.name] = counts.get(report.status.name, 0) + 1
    sink.write_summary(
        {
            "event": "batch.summary",
            "files": len(reports),
            "rejected": sum(1 for report in reports if report.status.is_rejected()),
            "by_status": counts,
        }
    )


def _level_for(result: MetricResult) -> int:
    if result.status is MetricStatus.FAILED:
        return logging.ERROR
    if result.status is MetricStatus.WARN:
        return logging.WARNING
    if result.detail.get("near_limit") and result.severity is not Severity.INFO:
        return logging.WARNING
    return logging.INFO


def _format_line(result: MetricResult) -> str:
    mark = {
        MetricStatus.PASSED: "✓",
        MetricStatus.WARN: "!",
        MetricStatus.FAILED: "✗",
        MetricStatus.SKIPPED: "-",
    }[result.status]
    subject = "" if result.subject in ("", "-") else f"  {result.subject}"
    value = "-" if result.value is None else _format_value(result.value)
    threshold = _format_threshold(result)
    if result.status is MetricStatus.FAILED:
        flag = "   ← 拒绝原因"
    elif result.detail.get("near_limit"):
        near = result.detail["near_limit"]
        flag = f"   ← 接近 {near['op']} {_format_value(near['bound'])}"
    else:
        flag = ""
    return f"    {mark} {result.name:<30}{value:>10} {result.unit:<6}({threshold}){subject}{flag}"


def _format_value(value: Any) -> str:
    return f"{value:.4g}" if isinstance(value, float) else str(value)


def _format_threshold(result: MetricResult) -> str:
    if not result.threshold:
        return "no threshold"
    from .rules import Threshold

    threshold = Threshold.from_spec(result.threshold)
    return threshold.format() if threshold else "no threshold"
