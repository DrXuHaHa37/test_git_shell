"""通用日志配置。

与业务无关：任何需要「按次留痕 + 机器可读明细 + 人读控制台」的流程都能用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LogSettings:
    level: str = "INFO"                 # 三个通道（明细/汇总/控制台）共用的级别
    dir: Path | None = None             # 日志目录；None = 不落盘
    jsonl: bool = True                  # 明细 + 汇总
    console: bool = True
    rotation: dict = field(default_factory=dict)
    summary_file: str = "summary.jsonl"
    file_prefix: str = "run"            # 明细文件名前缀：<prefix>-YYYYMMDD.jsonl
    redact_paths: bool = False

    def __post_init__(self) -> None:
        # 放宽入参：str / Path 都接受，否则调用方要自己记得转 Path
        if self.dir is not None and not isinstance(self.dir, Path):
            object.__setattr__(self, "dir", Path(self.dir).expanduser())

    @classmethod
    def from_config(cls, section: dict | None) -> "LogSettings":
        section = section or {}
        directory = section.get("dir")
        return cls(
            level=str(section.get("level", "INFO")).upper(),
            dir=Path(directory).expanduser() if directory else None,
            jsonl=bool(section.get("jsonl", True)),
            console=bool(section.get("console", True)),
            rotation=dict(section.get("rotation") or {}),
            summary_file=str(section.get("summary_file", "summary.jsonl")),
            file_prefix=str(section.get("file_prefix", "run")),
            redact_paths=bool(section.get("redact_paths", False)),
        )

    def with_overrides(
        self,
        *,
        level: str | None = None,
        directory: Path | None = None,
        console: bool | None = None,
        prefix: str | None = None,
    ) -> "LogSettings":
        return LogSettings(
            level=level or self.level,
            dir=directory or self.dir,
            jsonl=self.jsonl,
            console=self.console if console is None else console,
            rotation=self.rotation,
            summary_file=self.summary_file,
            file_prefix=prefix or self.file_prefix,
            redact_paths=self.redact_paths,
        )
