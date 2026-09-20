"""一次「运行」的日志上下文：注入 run_id / source，提供三个通道的写入口。"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .common import timestamp, to_level

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from .sink import LogSink


class RunLog:
    """单次运行的日志入口。

    三个通道:
      * ``event()``  → 明细 JSONL（机器读，每行一个事件）
      * ``summary()``→ 汇总 JSONL（每次运行一行）
      * ``text()``   → 控制台（人读）
    """

    def __init__(self, sink: LogSink | None, run_id: str, source: str = "") -> None:
        self._sink = sink
        self.run_id = run_id
        self.source = source

    @property
    def min_level(self) -> int:
        """当前生效的日志级别（来自 ``LogSettings.level``）。

        渲染方据此决定是否输出某条内容——比如控制台表格只列达到该级别的指标行。
        """
        if self._sink is None:
            return logging.INFO
        return to_level(self._sink.settings.level)

    @property
    def display_source(self) -> str:
        if self._sink is None or not self._sink.settings.redact_paths:
            return self.source
        path = Path(self.source)
        digest = hashlib.sha256(str(path.parent).encode()).hexdigest()[:8]
        return f"{digest}/{path.name}"

    def event(self, name: str, /, level: int = logging.INFO, **fields: Any) -> None:
        # name 用仅位置参数：调用方常常要在 fields 里也带一个 name 字段
        if self._sink is None:
            return
        self._sink._write(
            "detail",
            level,
            {
                "ts": timestamp(),
                "level": logging.getLevelName(level),
                "run_id": self.run_id,
                "source": self.display_source,
                "event": name,
                **fields,
            },
        )

    def summary(self, **fields: Any) -> None:
        if self._sink is None:
            return
        self._sink.write_summary({"run_id": self.run_id, "source": self.display_source, **fields})

    def text(self, text: str, level: int = logging.INFO) -> None:
        if self._sink is None:
            return
        self._sink._write("console", level, {"text": text})

    def error(self, message: str) -> None:
        self.event("error", logging.ERROR, message=message)


class NullRunLog(RunLog):
    """不落盘的日志上下文（未配置日志时使用）。"""

    def __init__(self, run_id: str = "-", source: str = "") -> None:
        super().__init__(None, run_id, source)


class StdLoggerRunLog(RunLog):
    """把标准库 ``logging.Logger`` 适配成 RunLog 接口（只写明细，无落盘/汇总）。"""

    def __init__(self, logger: logging.Logger, run_id: str, source: str = "") -> None:
        super().__init__(None, run_id, source)
        self._logger = logger

    def event(self, name: str, /, level: int = logging.INFO, **fields: Any) -> None:
        rendered = " ".join(f"{key}={value}" for key, value in fields.items())
        self._logger.log(level, "run=%s %s %s", self.run_id, name, rendered)

    def text(self, text: str, level: int = logging.INFO) -> None:
        self._logger.log(level, "%s", text)

    def summary(self, **fields: Any) -> None:
        self.event("summary", logging.INFO, **fields)


def adapt(logger: Any, run_id: str, source: str = "") -> RunLog:
    """把 ``logger`` 参数归一化成 RunLog：None / LogSink / RunLog / logging.Logger。"""
    if logger is None:
        return NullRunLog(run_id, source)
    if isinstance(logger, RunLog):
        return logger
    from .sink import LogSink  # 延迟导入：sink 依赖 run，避免循环

    if isinstance(logger, LogSink):
        return logger.run(run_id, source)
    if isinstance(logger, logging.Logger):
        return StdLoggerRunLog(logger, run_id, source)
    raise TypeError(f"unsupported logger type: {type(logger).__name__}")
