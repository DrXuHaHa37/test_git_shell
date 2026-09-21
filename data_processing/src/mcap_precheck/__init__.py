"""MCAP 文件前置检测：通用质量门禁，与业务转换脚本完全解耦。

基本用法::

    from mcap_precheck import inspect, StatusCode

    report = inspect("data/rec.mcap", profile="strict")
    if report.status.is_rejected():
        print(report.failed_metrics)
"""

from __future__ import annotations

from logger import LogSettings, LogSink  # 通用日志工具，re-export 方便调用方

from .config import ConfigError, QcConfig, load_config
from .metrics.base import MetricResult, MetricStatus, Severity
from .pipeline import inspect, inspect_dir
from .qclog import QcLog
from .report import QcReport
from .verdict import StatusCode

__version__ = "0.1.0"

__all__ = [
    "ConfigError",
    "LogSettings",
    "LogSink",
    "MetricResult",
    "MetricStatus",
    "QcConfig",
    "QcLog",
    "QcReport",
    "Severity",
    "StatusCode",
    "__version__",
    "inspect",
    "inspect_dir",
    "load_config",
]
