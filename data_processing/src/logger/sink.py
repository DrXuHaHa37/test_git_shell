"""日志出口：三通道 + 轮转 + 队列 listener。

并发安全：统一走 ``QueueHandler`` + 单独 listener 线程。多线程/多进程直接写同一个文件
会互相覆盖，批量场景这是必选项。
"""

from __future__ import annotations

import json
import logging
import queue
import sys
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

from .common import _LOGGER_NAME, timestamp, to_level
from .run import RunLog
from .settings import LogSettings


class _NameFilter(logging.Filter):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == self._name


class LogSink:
    """持有日志文件与 listener 的日志出口；一次批量/CLI 生命周期一个实例。

    用法::

        with LogSink(LogSettings(dir=Path("logs"), level="INFO")) as sink:
            run = sink.run("7f3a2b91", source="/data/job_001.bin")
            run.event("inspect.started", profile="strict")
    """

    def __init__(self, settings: LogSettings) -> None:
        self.settings = settings
        self._handlers: list[logging.Handler] = []
        self._queue: queue.Queue = queue.Queue(-1)
        base_level = to_level(settings.level)

        self._logger = logging.getLogger(_LOGGER_NAME)
        # 总闸只放宽到 DEBUG：真正的按级过滤在本实例私有的三个 handler 上（均为
        # base_level）。不能让总闸直接卡 base_level —— logger 名是全局共享的，
        # 多个 LogSink 实例会互相覆盖 setLevel。
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)

        if settings.jsonl and settings.dir is not None:
            detail = _with_filter(
                self._rotating(
                    settings.dir / f"{settings.file_prefix}-{datetime.now().strftime('%Y%m%d')}.jsonl"
                ),
                f"{_LOGGER_NAME}.detail",
            )
            detail.setLevel(base_level)
            self._handlers.append(detail)

            summary_handler = _with_filter(
                self._rotating(settings.dir / settings.summary_file),
                f"{_LOGGER_NAME}.summary",
            )
            summary_handler.setLevel(base_level)
            self._handlers.append(summary_handler)

        if settings.console:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(logging.Formatter("%(message)s"))
            console.setLevel(base_level)
            self._handlers.append(_with_filter(console, f"{_LOGGER_NAME}.console"))

        self._queue_handler = QueueHandler(self._queue)
        self._logger.addHandler(self._queue_handler)
        self._listener = QueueListener(self._queue, *self._handlers, respect_handler_level=True)
        self._listener.start()

    def _rotating(self, path: Path) -> logging.Handler:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotation = self.settings.rotation or {}
        if str(rotation.get("by", "size")).lower() == "time":
            handler: logging.Handler = TimedRotatingFileHandler(
                path, when="midnight", backupCount=int(rotation.get("backup_count", 10))
            )
        else:
            handler = RotatingFileHandler(
                path,
                maxBytes=int(rotation.get("max_bytes", 50 * 1024 * 1024)),
                backupCount=int(rotation.get("backup_count", 10)),
            )
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def run(self, run_id: str, source: str = "") -> RunLog:
        """开一次「运行」的日志上下文（注入 run_id / source）。"""
        return RunLog(self, run_id, source)

    def write_summary(self, payload: dict) -> None:
        """写一行汇总 JSONL（自动补 ts）。"""
        self._write("summary", logging.INFO, {"ts": timestamp(), **payload})

    def _write(self, channel: str, level: int, payload: dict) -> None:
        logger = logging.getLogger(f"{_LOGGER_NAME}.{channel}")
        if channel == "console":
            logger.log(level, payload.get("text", ""))
            return
        logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))

    def close(self) -> None:
        try:
            self._listener.stop()
        except Exception:  # noqa: BLE001 - 关闭失败不应影响业务结果
            pass
        for handler in self._handlers:
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
        if self._queue_handler in self._logger.handlers:
            self._logger.removeHandler(self._queue_handler)

    def __enter__(self) -> "LogSink":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _with_filter(handler: logging.Handler, name: str) -> logging.Handler:
    handler.addFilter(_NameFilter(name))
    return handler
