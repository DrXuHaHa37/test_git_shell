"""检测编排：输入适配 → 完整性 → 单趟扫描 → 各类指标 → 判定 → 报告。

``inspect()`` 不抛业务异常：可预期错误转成状态码写入 ``QcReport.errors``。
"""

from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence
from uuid import uuid4

from logger import LogSink, LogSettings

from .config import DEFAULT_PROFILE, ConfigError, QcConfig, load_config
from .metrics import build
from .metrics.base import Metric, MetricResult, MetricStatus
from .metrics.integrity import check as check_integrity
from .qclog import QcLog, as_emitter, batch_summary
from .reader import ScanPlan, scan
from .report import QcReport
from .source import SourceError, open_source
from .verdict import StatusCode

# mcap_precheck 的日志文件名前缀：qc-YYYYMMDD.jsonl
LOG_FILE_PREFIX = "qc"


def inspect(
    source,
    *,
    config: str | Path | None = None,
    profile: str = DEFAULT_PROFILE,
    categories: Iterable[str] | None = None,
    logger=None,
) -> QcReport:
    """检测单个 MCAP 文件（或流）。"""
    inspect_id = uuid4().hex[:8]
    name = _source_name(source)
    started_perf = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    try:
        settings = load_config(config, profile)
    except ConfigError as exc:
        emitter = as_emitter(logger, inspect_id, name)
        emitter.error(str(exc))
        return _report(
            inspect_id, name, StatusCode.INVALID_INPUT, (), started_at, started_perf,
            profile, (), (str(exc),), emitter,
        )

    try:
        enabled = settings.enabled_categories(list(categories) if categories is not None else None)
    except ConfigError as exc:
        emitter = as_emitter(logger, inspect_id, name, warn_ratio=settings.warn_ratio)
        emitter.error(str(exc))
        return _report(
            inspect_id, name, StatusCode.INVALID_INPUT, (), started_at, started_perf,
            profile, (), (str(exc),), emitter,
        )

    emitter = as_emitter(logger, inspect_id, name, warn_ratio=settings.warn_ratio)
    emitter.started(profile=profile, categories=enabled)
    emitter.config_loaded(path=str(settings.path), profile=profile)

    try:
        return _run(source, settings, enabled, inspect_id, name, started_at, started_perf, emitter)
    except Exception as exc:  # noqa: BLE001 - 未预期异常 → INTERNAL_ERROR
        message = f"{type(exc).__name__}: {exc}"
        emitter.error(f"{message}\n{traceback.format_exc(limit=5)}")
        return _report(
            inspect_id, name, StatusCode.INTERNAL_ERROR, (), started_at, started_perf,
            profile, enabled, (message,), emitter,
        )


def inspect_dir(
    directory,
    *,
    pattern: str = "*.mcap",
    recursive: bool = True,
    config: str | Path | None = None,
    profile: str = DEFAULT_PROFILE,
    categories: Iterable[str] | None = None,
    max_workers: int = 4,
    logger=None,
) -> list[QcReport]:
    """批量检测一个目录下的 MCAP 文件（并发，日志走同一个 sink）。"""
    root = Path(directory).expanduser()
    if not root.is_dir():
        raise SourceError(f"not a directory: {root}")
    files = sorted(root.rglob(pattern) if recursive else root.glob(pattern))
    if not files:
        return []

    settings = load_config(config, profile)
    log_settings = LogSettings.from_config(settings.section("logging")).with_overrides(
        prefix=LOG_FILE_PREFIX
    )
    if logger is not None:
        reports: list[QcReport] = [
            inspect(
                path,
                config=settings.path,
                profile=profile,
                categories=categories,
                logger=logger,
            )
            for path in files
        ]
        if isinstance(logger, LogSink):
            batch_summary(logger, reports)
        return reports

    reports = []
    with LogSink(log_settings) as sink, ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [
            pool.submit(
                inspect,
                path,
                config=settings.path,
                profile=profile,
                categories=list(categories) if categories is not None else None,
                logger=sink,
            )
            for path in files
        ]
        for future in futures:
            reports.append(future.result())
        batch_summary(sink, reports)
    return reports


def _run(source, settings, enabled, inspect_id, name, started_at, started_perf, emitter: QcLog):
    integrity_section = settings.section("integrity")
    try:
        handle = open_source(source)
    except (SourceError, OSError, ValueError) as exc:
        message = f"invalid input: {exc}"
        emitter.error(message)
        return _report(
            inspect_id, name, StatusCode.INVALID_INPUT, (), started_at, started_perf,
            settings.profile, enabled, (message,), emitter,
        )

    with handle:
        # 4.5 流式约束：不可 seek 的输入做不了结构类检查，返回 INVALID_INPUT 而非静默跳过
        if not handle.seekable and (
            integrity_section.get("required_topics") or integrity_section.get("require_summary", True)
        ):
            message = (
                "source is not seekable: structural checks (summary/required topics) are unavailable; "
                "pass a file path or set integrity.require_summary=false with no required_topics"
            )
            emitter.error(message)
            return _report(
                inspect_id, name, StatusCode.INVALID_INPUT, (), started_at, started_perf,
                settings.profile, enabled, (message,), emitter,
            )

        metrics: list[Metric] = []
        for category in enabled:
            if category == "integrity":
                continue
            metric = _build_metric(category, settings)
            if metric.is_active():
                metrics.append(metric)
            else:
                # 类别开关已开但领域配置为空——显式记录，避免控制台出现"空类别"让人以为跑了
                emitter.skipped(category, metric.skip_reason() or f"{category} 配置不完整")

        plan = _merge_plans(metrics)
        sinks = _camera_sinks(metrics)
        scanned = scan(handle, plan, sinks)

        results: list[MetricResult] = []
        outcome = check_integrity(scanned, settings)
        results.extend(outcome.results)
        for result in outcome.results:
            emitter.metric(result)

        status = outcome.status
        if status is not None and settings.fail_fast:
            for metric in metrics:
                emitter.skipped(metric.category, f"fail_fast：完整性检查已判定 {status.name}，未执行")
            return _report(
                inspect_id, name, status, tuple(results), started_at, started_perf,
                settings.profile, enabled, tuple(scanned.errors), emitter,
            )

        for metric in metrics:
            for result in metric.run(scanned):
                results.append(result)
                emitter.metric(result)

        if status is None:
            status = _decide(results, settings.fail_on_warning)
        return _report(
            inspect_id, name, status, tuple(results), started_at, started_perf,
            settings.profile, enabled, tuple(scanned.errors), emitter,
        )


def _build_metric(category: str, settings: QcConfig) -> Metric:
    return build(category, settings, warn_ratio=settings.warn_ratio)


def _merge_plans(metrics: Sequence[Metric]) -> ScanPlan:
    plan = ScanPlan()
    for metric in metrics:
        plan = plan.merge(metric.plan())
    return plan


def _camera_sinks(metrics: Sequence[Metric]) -> dict:
    sinks: dict = {}
    for metric in metrics:
        if metric.category == "camera":
            sinks.update(metric.sinks())
    return sinks


def _decide(results: Sequence[MetricResult], fail_on_warning: bool) -> StatusCode:
    if any(result.status is MetricStatus.FAILED for result in results):
        return StatusCode.REJECTED_METRIC
    if any(result.status is MetricStatus.WARN for result in results):
        return StatusCode.REJECTED_METRIC if fail_on_warning else StatusCode.ACCEPTED_WITH_WARNING
    return StatusCode.OK


def _report(
    inspect_id: str,
    source: str,
    status: StatusCode,
    metrics: Sequence[MetricResult],
    started_at: str,
    started_perf: float,
    profile: str,
    enabled: Sequence[str],
    errors: Sequence[str],
    emitter: QcLog,
) -> QcReport:
    elapsed_ms = (time.perf_counter() - started_perf) * 1000.0
    report = QcReport(
        inspect_id=inspect_id,
        source=source,
        status=status,
        metrics=tuple(metrics),
        started_at=started_at,
        elapsed_ms=elapsed_ms,
        profile=profile,
        enabled_categories=tuple(enabled),
        errors=tuple(errors),
    )
    emitter.finished(report)
    return report


def _source_name(source) -> str:
    if isinstance(source, (str, Path)):
        return str(source)
    return str(getattr(source, "name", "<stream>"))
