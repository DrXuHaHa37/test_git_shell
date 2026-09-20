"""状态码定义。

分段编号（0/10/20/…/90）留扩展位，插入新码不破坏既有值。
``code >= 20`` 即拒绝，``code < 20`` 即可用；上层请用 :meth:`StatusCode.is_rejected`
而不是硬编码比较。
"""

from __future__ import annotations

import logging
from enum import Enum, IntEnum


class StatusCode(IntEnum):
    OK = 0                     # 全部启用指标在阈值内
    ACCEPTED_WITH_WARNING = 10 # 仅 warn 级越界，仍可用
    REJECTED_METRIC = 20       # error 级指标越界 → 拒绝
    INCOMPLETE_STRUCTURE = 30  # 结构不完整（缺 topic / schema / 截断）
    CORRUPTED = 40             # 解析失败
    EMPTY = 50                 # 无消息
    INVALID_INPUT = 60         # 输入或配置非法
    INTERNAL_ERROR = 90        # 未预期异常

    def is_rejected(self, *, fail_on_warning: bool = False) -> bool:
        if fail_on_warning:
            return int(self) >= int(StatusCode.ACCEPTED_WITH_WARNING)
        return int(self) >= int(StatusCode.REJECTED_METRIC)

    @property
    def http_status(self) -> int:
        """数值兼容 HTTP 语义：0/10 → 2xx，20~60 → 4xx，90 → 5xx。"""
        if int(self) < int(StatusCode.REJECTED_METRIC):
            return 200
        if int(self) >= int(StatusCode.INTERNAL_ERROR):
            return 500
        return 400

    def __str__(self) -> str:
        return f"{self.name} ({int(self)})"


REJECTED_THRESHOLD = int(StatusCode.REJECTED_METRIC)


class Severity(str, Enum):
    """越界严重级别：error → 拒绝，warn → 仍可用但记 WARNING。"""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"

    @classmethod
    def parse(cls, value: object, *, default: "Severity | str" = ERROR) -> "Severity":
        if value is None:
            return cls(default) if isinstance(default, str) else default
        text = str(value).strip().lower()
        aliases = {
            "info": cls.INFO,
            "warn": cls.WARN,
            "warning": cls.WARN,
            "error": cls.ERROR,
            "fatal": cls.ERROR,
        }
        if text not in aliases:
            raise ValueError(f"unknown severity {value!r}, expected one of {sorted(aliases)}")
        return aliases[text]

    @property
    def log_level(self) -> int:
        return {Severity.INFO: logging.INFO, Severity.WARN: logging.WARNING}.get(self, logging.ERROR)


class MetricStatus(str, Enum):
    PASSED = "PASSED"
    WARN = "WARN"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
