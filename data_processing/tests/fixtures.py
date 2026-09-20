"""合成 MCAP fixture：用 mcap 库自写"坏文件"，供集成测试使用。"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from mcap_ros2.writer import Writer

NS_PER_SECOND = 1_000_000_000

VEC_SCHEMA = "qc/msg/Vec"
VEC_TEXT = "float64[] position\nfloat64[] velocity\n"

IMAGE_SCHEMA = "qc/msg/RawImage"
IMAGE_TEXT = "uint32 height\nuint32 width\nuint32 step\nstring encoding\nuint8[] data\n"


@dataclass
class Message:
    topic: str
    log_time_ns: int
    payload: object
    schema: str = VEC_SCHEMA
    publish_time_ns: int | None = None


def write_mcap(
    path: Path,
    messages: list[Message],
    *,
    schemas: dict[str, str] | None = None,
    truncate_bytes: int = 0,
) -> Path:
    """写出一个 MCAP 文件；``truncate_bytes`` 用于构造截断文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    definitions = {VEC_SCHEMA: VEC_TEXT, IMAGE_SCHEMA: IMAGE_TEXT}
    definitions.update(schemas or {})
    with path.open("wb") as stream:
        writer = Writer(stream)
        registered: dict[str, object] = {}
        for message in messages:
            if message.schema not in registered:
                if message.schema not in definitions:
                    raise ValueError(f"unknown schema {message.schema}")
                registered[message.schema] = writer.register_msgdef(
                    message.schema, definitions[message.schema]
                )
            writer.write_message(
                message.topic,
                registered[message.schema],  # type: ignore[arg-type]
                message.payload,
                log_time=message.log_time_ns,
                publish_time=message.publish_time_ns,
            )
        writer.finish()
    if truncate_bytes:
        data = path.read_bytes()
        path.write_bytes(data[:-truncate_bytes])
    return path


def joint_messages(
    topic: str,
    *,
    count: int = 300,
    rate_hz: float = 30.0,
    dims: int = 2,
    start_ns: int = 1_700_000_000 * NS_PER_SECOND,
    transform=None,
) -> list[Message]:
    """等间隔的关节消息序列；``transform(i, values)`` 可注入异常。"""
    period = int(round(NS_PER_SECOND / rate_hz))
    messages: list[Message] = []
    for index in range(count):
        phase = index / rate_hz
        values = [0.1 * np.sin(phase), 0.2 * np.cos(phase)][:dims]
        values = values + [0.05] * max(0, dims - len(values))
        if transform is not None:
            values = transform(index, values)
        messages.append(
            Message(
                topic=topic,
                log_time_ns=start_ns + index * period,
                publish_time_ns=start_ns + index * period - 1_000_000,
                payload=SimpleNamespace(
                    position=list(values),
                    velocity=[0.0] * len(values),
                ),
            )
        )
    return messages


def image_messages(
    topic: str,
    frames: list[np.ndarray],
    *,
    start_ns: int = 1_700_000_000 * NS_PER_SECOND,
    period_ns: int = NS_PER_SECOND // 30,
    header_offset_ns: int = -2_000_000,
) -> list[Message]:
    messages: list[Message] = []
    for index, frame in enumerate(frames):
        height, width = frame.shape[:2]
        messages.append(
            Message(
                topic=topic,
                schema=IMAGE_SCHEMA,
                log_time_ns=start_ns + index * period_ns,
                publish_time_ns=start_ns + index * period_ns + header_offset_ns,
                payload=SimpleNamespace(
                    height=int(height),
                    width=int(width),
                    step=int(width * 3),
                    encoding="rgb8",
                    data=frame.tobytes(),
                ),
            )
        )
    return messages


def good_mcap(path: Path, **kwargs) -> Path:
    return write_mcap(path, joint_messages("/joint_states", **kwargs))


def empty_mcap(path: Path) -> Path:
    return write_mcap(path, [])


def truncated_mcap(path: Path) -> Path:
    return write_mcap(path, joint_messages("/joint_states"), truncate_bytes=400)


def rollback_mcap(path: Path) -> Path:
    messages = joint_messages("/joint_states")
    messages[150] = Message(
        topic="/joint_states",
        log_time_ns=messages[100].log_time_ns,
        publish_time_ns=messages[100].publish_time_ns,
        payload=messages[150].payload,
    )
    return write_mcap(path, messages)


def dropped_mcap(path: Path) -> Path:
    messages = joint_messages("/joint_states")
    del messages[100:130]  # 1 秒空洞
    return write_mcap(path, messages)


def nan_mcap(path: Path) -> Path:
    def inject(index: int, values: list[float]) -> list[float]:
        if index == 42:
            values[0] = float("nan")
        if index == 43:
            values[1] = float("inf")
        return values

    return write_mcap(path, joint_messages("/joint_states", transform=inject))


def joint_oob_mcap(path: Path) -> Path:
    def inject(index: int, values: list[float]) -> list[float]:
        if index == 10:
            values[0] = 99.0
        return values

    return write_mcap(path, joint_messages("/joint_states", transform=inject))


def cmd_state_mcap(path: Path, *, bad: bool = False) -> Path:
    """一对 command/state 话题；``bad=True`` 时某帧 command 大幅偏离 state。"""
    state = joint_messages("/arm/state")
    command = joint_messages("/arm/command")
    if bad:
        command[50] = Message(
            topic="/arm/command",
            log_time_ns=command[50].log_time_ns,
            publish_time_ns=command[50].publish_time_ns,
            payload=SimpleNamespace(position=[5.0, 5.0], velocity=[0.0, 0.0]),
        )
    # 真实录制里两个话题按到达顺序交错写入，全局 log_time 才是单调的
    return write_mcap(path, sorted([*state, *command], key=lambda message: message.log_time_ns))


def frame(value: int, size: int = 64) -> np.ndarray:
    return np.full((size, size, 3), value, dtype=np.uint8)


def noisy_frame(seed: int, size: int = 64) -> np.ndarray:
    generator = np.random.default_rng(seed)
    return generator.integers(0, 255, size=(size, size, 3), dtype=np.uint8).astype(np.uint8)


def black_frames_mcap(path: Path) -> Path:
    return write_mcap(path, image_messages("/camera/head", [frame(0) for _ in range(30)]))


def duplicate_frames_mcap(path: Path) -> Path:
    frames = [noisy_frame(0)] * 30
    return write_mcap(path, image_messages("/camera/head", frames))


def blur_frames_mcap(path: Path) -> Path:
    """纯色帧（拉普拉斯方差为 0）；相邻帧亮度差 3 > pixel_diff_eps，故不判重复。"""
    frames = [frame(128 + 3 * (index % 2)) for index in range(30)]
    return write_mcap(path, image_messages("/camera/head", frames))


def good_camera_mcap(path: Path) -> Path:
    frames = [noisy_frame(seed) for seed in range(30)]
    return write_mcap(path, image_messages("/camera/head", frames))


class NonSeekable(io.RawIOBase):
    """包一层不可 seek 的流，用于验证 streaming 降级。"""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        return self._buffer.read(size)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False
