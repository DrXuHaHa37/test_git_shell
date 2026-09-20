"""logger 包内的公共小工具（不依赖 sink/run，避免循环导入）。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

_LOGGER_NAME = "logger"
CHANNELS = ("detail", "summary", "console")


def timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def to_level(value: str) -> int:
    return getattr(logging, str(value).upper(), logging.INFO)
