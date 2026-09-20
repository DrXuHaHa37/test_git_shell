"""字段选择器：把 ROS2 动态消息上的点号路径取出数值向量。

支持点号路径 + 末尾切片，例如 ``position``、``data[0:16]``、``joints[3].position``、
``header.stamp.sec``。
"""

from __future__ import annotations

import re
from typing import Any, Iterator

import numpy as np

# 自动扫描全部数值字段时跳过的超大 uint8 数组（图像像素），避免把相机数据也扫一遍。
MAX_AUTO_ELEMENTS = 1024
_MAX_DEPTH = 6

_SEGMENT = re.compile(r"^([A-Za-z_]\w*)\s*(?:\[\s*(-?\d*)\s*(?::\s*(-?\d*)\s*)?\])?$")


class FieldError(ValueError):
    """字段路径无法在消息上解析。"""


def select(obj: Any, path: str) -> Any:
    """按点号路径取值，失败抛 :class:`FieldError`。"""
    current = obj
    for raw in path.split("."):
        segment = raw.strip()
        if not segment:
            raise FieldError(f"empty segment in path {path!r}")
        match = _SEGMENT.match(segment)
        if match is None:
            raise FieldError(f"invalid path segment {segment!r} in {path!r}")
        name, index, slice_end = match.groups()
        current = _getattr(current, name, path)
        if index is not None or slice_end is not None:
            if slice_end is not None or index == "":
                start = int(index) if index else None
                end = int(slice_end) if slice_end else None
                current = current[start:end]
            else:
                current = current[int(index)]
    return current


def _getattr(obj: Any, name: str, path: str) -> Any:
    if isinstance(obj, dict):
        if name not in obj:
            raise FieldError(f"field {name!r} not found (path {path!r})")
        return obj[name]
    try:
        return getattr(obj, name)
    except AttributeError as exc:
        raise FieldError(f"field {name!r} not found (path {path!r})") from exc


def numeric_vector(value: Any) -> np.ndarray | None:
    """把任意字段值转成一维 float64 数组；不可转成数值时返回 ``None``。"""
    if isinstance(value, (bool, str, bytes, bytearray, memoryview)):
        return None
    if isinstance(value, np.ndarray):
        if value.dtype.kind in "fiu":
            return value.astype(np.float64, copy=False).reshape(-1)
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        return np.array([float(value)], dtype=np.float64)
    if isinstance(value, (list, tuple)):
        try:
            array = np.asarray(value)
        except (TypeError, ValueError):
            return None
        if array.dtype.kind in "fiu":
            return array.astype(np.float64, copy=False).reshape(-1)
    return None


def iter_numeric_fields(obj: Any, *, prefix: str = "") -> Iterator[tuple[str, np.ndarray]]:
    """递归遍历消息中的数值叶子字段，产出 ``(路径, 一维数组)``。"""
    yield from _walk(obj, prefix, 0)


def _walk(obj: Any, prefix: str, depth: int) -> Iterator[tuple[str, np.ndarray]]:
    if depth > _MAX_DEPTH or obj is None:
        return
    if isinstance(obj, (str, bytes, bytearray, memoryview)):
        return
    vector = numeric_vector(obj)
    if vector is not None:
        if vector.size > MAX_AUTO_ELEMENTS:
            return  # 图像像素之类的大块数据，跳过
        yield (prefix or "value"), vector
        return
    for name, child in _members(obj):
        yield from _walk(child, f"{prefix}.{name}" if prefix else name, depth + 1)


def _members(obj: Any) -> list[tuple[str, Any]]:
    if isinstance(obj, dict):
        return [(str(key), value) for key, value in obj.items()]
    names: list[str] = []
    slots = getattr(type(obj), "__slots__", None)
    if isinstance(slots, (tuple, list)):
        names.extend(str(slot) for slot in slots if not str(slot).startswith("_"))
    state = getattr(obj, "__dict__", None)
    if isinstance(state, dict):
        names.extend(name for name in state if not name.startswith("_"))
    if not names:
        names = [name for name in dir(obj) if not name.startswith("_")]
    members: list[tuple[str, Any]] = []
    for name in dict.fromkeys(names):
        try:
            members.append((name, getattr(obj, name)))
        except Exception:  # noqa: BLE001 - 动态消息可能有无用属性
            continue
    return members
