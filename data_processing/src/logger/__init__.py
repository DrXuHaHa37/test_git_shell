"""通用日志工具：三通道（明细 JSONL / 汇总 JSONL / 控制台）+ 轮转 + 并发安全。

与任何业务无关。典型用法::

    from logger import LogSettings, LogSink

    with LogSink(LogSettings(dir=Path("logs"), level="INFO", file_prefix="job")) as sink:
        run = sink.run("7f3a2b91", source="/data/job_001.bin")
        run.event("job.started", profile="strict")
        run.event("step.result", logging.INFO, name="rate_hz", value=29.8)
        run.summary(code=0, status="OK")
        run.text("人读的控制台输出")

不传 logger（或用 ``NullRunLog``）时完全不落盘。
"""

from .common import CHANNELS, timestamp, to_level
from .run import NullRunLog, RunLog, StdLoggerRunLog, adapt
from .settings import LogSettings
from .sink import LogSink

__version__ = "0.1.0"

__all__ = [
    "CHANNELS",
    "LogSettings",
    "LogSink",
    "NullRunLog",
    "RunLog",
    "StdLoggerRunLog",
    "__version__",
    "adapt",
    "timestamp",
    "to_level",
]
