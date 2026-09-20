"""输入适配层：路径 / bytes / 二进制流 → 统一的 Source。"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Union

SourceInput = Union[str, Path, bytes, bytearray, memoryview, BinaryIO]


class SourceError(ValueError):
    """输入无法作为 MCAP 数据源打开。"""


@dataclass
class Source:
    stream: BinaryIO
    name: str
    seekable: bool
    _owns_stream: bool = False

    def __enter__(self) -> "Source":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_stream:
            self.stream.close()


def open_source(source: SourceInput) -> Source:
    if isinstance(source, (str, Path)):
        path = Path(source).expanduser()
        if not path.is_file():
            raise SourceError(f"file does not exist: {path}")
        stream = path.open("rb")
        return Source(stream=stream, name=str(path), seekable=True, _owns_stream=True)
    if isinstance(source, (bytes, bytearray, memoryview)):
        return Source(
            stream=io.BytesIO(bytes(source)),
            name="<bytes>",
            seekable=True,
            _owns_stream=True,
        )
    if hasattr(source, "read"):
        stream: Any = source
        seekable = bool(getattr(stream, "seekable", lambda: False)())
        return Source(
            stream=stream,
            name=str(getattr(stream, "name", "<stream>")),
            seekable=seekable,
            _owns_stream=False,
        )
    raise SourceError(f"unsupported source type: {type(source).__name__}")
