"""最小 MCAP 读取示例：看清一个 bag 到底是怎么组织的。

    .venv/bin/python src/mcap_precheck/reader_test.py [文件路径] [-n 消息条数] [--decode] [-s 秒数]

同一个文件分五层看，其中 4/5 两层是给 mcap_precheck.yaml 配置用的：
  1) layout   —— 磁盘上的字节序列：record = [opcode:1B][length:8B][payload]
  2) summary  —— 文件尾部的索引区，不读任何消息就能知道有哪些 topic、多少条
  3) messages —— 消息流本身，(schema, channel, message) 三元组 + 可选 ROS2 解码
  4) joints   —— 解码 JointState 首帧拿关节名/维度 -> 填 joint.topics / joint.joints
  5) pairs    —— 同维度 topic 两两试配 -> 填 cmd_state.pairs

一句话版用法：
    from mcap.reader import make_reader
    for schema, channel, message in make_reader(open(path, "rb")).iter_messages():
        print(channel.topic, message.log_time, message.data)
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

import numpy as np
from mcap.reader import make_reader

try:
    from mcap_ros2.decoder import DecoderFactory
except ImportError:  # 没装 mcap-ros2-support 时，第 3~5 层的解码部分自动跳过
    DecoderFactory = None

DEFAULT_MCAP = (
    Path(__file__).resolve().parents[2]
    / "data/mcap_files/record_v4l2_2026-09-09_14-28-04"
    / "record_v4l2_2026-09-09_14-28-04_0.mcap"
)
MAGIC = b"\x89MCAP0\r\n"
NS_PER_SECOND = 1_000_000_000

OPCODES = {
    0x01: "Header",
    0x02: "Footer",
    0x03: "Schema",
    0x04: "Channel",
    0x05: "Message",
    0x06: "Chunk",
    0x07: "MessageIndex",
    0x08: "ChunkIndex",
    0x09: "Attachment",
    0x0A: "AttachmentIndex",
    0x0B: "Statistics",
    0x0C: "Metadata",
    0x0D: "MetadataIndex",
    0x0E: "SummaryOffset",
    0x0F: "DataEnd",
}


def show_layout(path: Path, limit: int = 12) -> None:
    """第 1 层：手写解析器，跳过数据区只读前几条记录的元信息。"""
    print("\n=== 1. 物理布局 ===")
    size = path.stat().st_size
    with path.open("rb") as stream:
        head, tail = stream.read(len(MAGIC)), None
        stream.seek(-len(MAGIC), 2)
        tail = stream.read(len(MAGIC))
    print(f"文件 {path.name}  {size / 2**20:.1f} MiB")
    print(f"头部 magic {head!r}  -> {'合法 MCAP' if head == MAGIC else '不是 MCAP'}")
    print(f"尾部 magic {tail!r}  -> {'完整未截断' if tail == MAGIC else '被截断（无 Footer）'}")

    print(f"前 {limit} 条记录（payload 用 seek 跳过，所以 1GB 文件也是秒出）：")
    with path.open("rb") as stream:
        stream.seek(len(MAGIC))
        for index in range(limit):
            header = stream.read(9)
            if len(header) < 9:
                print(f"  [{index}] 文件结束")
                break
            opcode, length = struct.unpack("<BQ", header)
            print(
                f"  [{index}] {OPCODES.get(opcode, f'0x{opcode:02x}'):<15}"
                f" offset={stream.tell() - 9:<12} payload={length}"
            )
            stream.seek(length, 1)


def show_summary(path: Path) -> None:
    """第 2 层：Summary = Footer 指向的尾部索引，读它不需要扫全文。"""
    print("\n=== 2. Summary（尾部索引）===")
    with path.open("rb") as stream:
        reader = make_reader(stream)
        header = reader.get_header()
        summary = reader.get_summary()

    print(f"profile={header.profile}  library={header.library}")
    stats = summary.statistics
    duration_s = (stats.message_end_time - stats.message_start_time) / NS_PER_SECOND
    print(
        f"message_count={stats.message_count}  channel_count={stats.channel_count}  "
        f"schema_count={stats.schema_count}  chunk_count={stats.chunk_count}  "
        f"attachment_count={stats.attachment_count}  duration={duration_s:.3f}s"
    )
    print(f"metadata_count={stats.metadata_count}  -> metadata.yaml 里那份 ROS2 bag 信息")
    for index in summary.metadata_indexes:
        print(f"  metadata[{index.name}] @offset={index.offset} len={index.length}")

    print("\ntopic 列表（channel 是「topic↔schema」的绑定关系）：")
    counts = stats.channel_message_counts
    for channel in sorted(summary.channels.values(), key=lambda item: item.id):
        schema = summary.schemas.get(channel.schema_id)
        print(
            f"  id={channel.id:<3} n={counts.get(channel.id, 0):<6} "
            f"{channel.topic:<52} {channel.message_encoding:<6} {schema.name if schema else '<no schema>'}"
        )

    print("\nschema 列表（真正描述了消息二进制怎么解）：")
    for schema in summary.schemas.values():
        print(f"  id={schema.id} encoding={schema.encoding} name={schema.name} data={len(schema.data)}B")

    print(f"\nchunk_indexes={len(summary.chunk_indexes)} 条（每个 chunk 一段压缩的消息簇），前 3 条：")
    for chunk in summary.chunk_indexes[:3]:
        span_s = (chunk.message_end_time - chunk.message_start_time) / NS_PER_SECOND
        print(
            f"  offset={chunk.chunk_start_offset} len={chunk.chunk_length} "
            f"compression={chunk.compression or 'none'} "
            f"{chunk.compressed_size}B <- {chunk.uncompressed_size}B  span={span_s:.3f}s"
        )


def show_messages(path: Path, limit: int = 3, decode: bool = False) -> None:
    """第 3 层：真正的消息流。chunk 解压、按 channel 找 schema 都由 reader 代劳。"""
    print(f"\n=== 3. 消息流（前 {limit} 条，decode={decode}）===")
    if decode and DecoderFactory is None:
        print("未安装 mcap-ros2-support，跳过解码")
        decode = False
    factories = [DecoderFactory()] if decode else []
    if decode:
        print("加了一个 DecoderFactory：cdr 字节 -> ROS2 消息对象")

    decoders: dict[int, object | None] = {}
    shown = 0
    start_ns: int | None = None
    with path.open("rb") as stream:
        for schema, channel, message in make_reader(stream, decoder_factories=factories).iter_messages():
            if start_ns is None:
                start_ns = message.log_time
            topic = channel.topic
            text = ""
            if decode:
                callback = decoders.get(channel.id, False)
                if callback is False:
                    callback = factories[0].decoder_for(channel.message_encoding, schema)
                    decoders[channel.id] = callback
                text = f" -> {callback(message.data)!s:.120}"
            print(
                f"  [{shown}] t={(message.log_time - start_ns) / NS_PER_SECOND:.6f}s"
                f" log_time={message.log_time} topic={topic}"
                f"\n      schema={schema.name if schema else None} encoding={channel.message_encoding}"
                f" channel_id={channel.id} data={len(message.data)}B{text}"
            )
            shown += 1
            if shown >= limit:
                break


def joint_state_topics(path: Path) -> list[str]:
    """不看消息体，先从 summary 筛出消息类型是 JointState 的 topic。"""
    with path.open("rb") as stream:
        summary = make_reader(stream).get_summary()
    topics: list[str] = []
    for channel in summary.channels.values():
        schema = summary.schemas.get(channel.schema_id)
        if schema is not None and schema.name.endswith("msg/JointState"):
            topics.append(channel.topic)
    return sorted(topics)


def _first_frames(path: Path, topics: list[str]) -> dict[str, dict]:
    """每个 JointState topic 的首帧解码结果 —— name[] 只在消息体里，summary 里没有。"""
    factory = DecoderFactory()
    frames: dict[str, dict] = {}
    with path.open("rb") as stream:
        for schema, channel, message in make_reader(stream, decoder_factories=[factory]).iter_messages():
            topic = channel.topic
            if topic not in topics or topic in frames:
                continue
            sample = factory.decoder_for(channel.message_encoding, schema)(message.data)
            frames[topic] = {
                "names": list(sample.name),
                "dim": len(sample.position),
                "has_velocity": len(sample.velocity) > 0,
                "has_effort": len(sample.effort) > 0,
                "frame_id": sample.header.frame_id,
            }
            if len(frames) == len(topics):
                break
    return frames


def _decode_tracks(
    path: Path, topics: list[str], seconds: float
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """按 topic 收集开头 seconds 秒的 (log_time, position)，各 topic 取满即提前退出。"""
    factory = DecoderFactory()
    raw: dict[str, dict[str, list]] = {topic: {"t": [], "v": []} for topic in topics}
    decoders: dict[int, object] = {}
    finished: set[str] = set()
    start_ns: int | None = None
    with path.open("rb") as stream:
        for schema, channel, message in make_reader(stream, decoder_factories=[factory]).iter_messages():
            topic = channel.topic
            if topic not in raw or topic in finished:
                continue
            if start_ns is None:
                start_ns = message.log_time
            if (message.log_time - start_ns) / NS_PER_SECOND <= seconds:
                callback = decoders.setdefault(
                    channel.id, factory.decoder_for(channel.message_encoding, schema)
                )
                raw[topic]["t"].append(int(message.log_time))
                raw[topic]["v"].append(list(callback(message.data).position))
            else:
                finished.add(topic)
            if len(finished) == len(topics):
                break
    tracks: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for topic, data in raw.items():
        order = np.argsort(data["t"])
        tracks[topic] = (
            np.asarray(data["t"], dtype=np.int64)[order],
            np.asarray(data["v"], dtype=np.float64)[order],
        )
    return tracks


def show_joint_topics(path: Path) -> None:
    """第 4 层：JointState 首帧结构 —— joint.topics / joint.joints 的填表依据。

    关节名不在 summary 里：JointState 的 name[] 是**每帧消息的数据**，只有解码才拿得到。
    joint.joints 的 index 就是这里 name/position 的数组下标。
    """
    print("\n=== 4. JointState 首帧（joint.topics / joint.joints 依据）===")
    if DecoderFactory is None:
        print("  未安装 mcap-ros2-support，无法解码")
        return
    topics = joint_state_topics(path)
    if not topics:
        print("  没有 JointState 类型的 topic")
        return

    for topic, frame in _first_frames(path, topics).items():
        print(f"\n  {topic}")
        print(
            f"    position={frame['dim']}维  velocity={'有' if frame['has_velocity'] else '空'}"
            f"  effort={'有' if frame['has_effort'] else '空'}  frame_id={frame['frame_id']!r}"
        )
        cells = [f"[{index:>2}] {name:<12}" for index, name in enumerate(frame["names"])]
        for row in range(0, len(cells), 4):
            print("    " + "".join(cells[row : row + 4]))
        hint = "{name: " + topic + ", position_field: position"
        hint += ", velocity_field: velocity}" if frame["has_velocity"] else "}"
        print(f"    -> joint.topics 里写：{hint}")
        print(f"    -> joint.joints 的 index 0..{frame['dim'] - 1} 顺序如上")


def show_cmd_state_candidates(path: Path, seconds: float = 4.0) -> None:
    """第 5 层：command/state 两两试配 —— cmd_state.pairs 的填表依据。

    维度相同的 topic 全部组合算一遍：差值小、相关系数高、&& 时间接近的那组才是真配对。
    """
    print(f"\n=== 5. 命令-状态配对试验（前 {seconds}s，cmd_state.pairs 依据）===")
    if DecoderFactory is None:
        print("  未安装 mcap-ros2-support，无法解码")
        return
    topics = joint_state_topics(path)
    tracks = _decode_tracks(path, topics, seconds=seconds)
    commands = [topic for topic in topics if "command" in topic.lower()]
    states = [topic for topic in topics if topic not in commands]
    if not commands or not states:
        print("  找不到 command/state 两类 topic（判定依据：topic 名是否含 command）")
        return

    rows: list[tuple[float, dict]] = []
    for command_topic in commands:
        command_times, command_values = tracks[command_topic]
        if command_values.size == 0:
            continue
        for state_topic in states:
            state_times, state_values = tracks[state_topic]
            if state_values.size == 0 or state_values.shape[1] != command_values.shape[1]:
                continue
            rows.append(_compare_pair(command_times, command_values, state_times, state_values,
                                      command_topic, state_topic))

    rows.sort(key=lambda item: item[0])
    print(f"  {'command':<36}{'state':<36}{'dim':>4}{'n':>5}{'mean|r|':>8}{'p95|diff|':>10}{'dt_p95':>9}")
    for _, row in rows:
        print(
            f"  {_short(row['command']):<36}{_short(row['state']):<36}{row['dim']:>4}{row['n']:>5}"
            f"{row['mean_abs_r']:>8.3f}{row['p95_abs_diff']:>10.4f}{row['dt_p95_ms']:>8.1f}ms"
        )
    if rows:
        best = rows[0][1]
        print(
            f"\n  最接近的一对 -> {best['command']} <=> {best['state']}"
            f"（p95|diff|={best['p95_abs_diff']:.4f} rad, dt_p95={best['dt_p95_ms']:.1f}ms）"
        )
        print(
            "  -> cmd_state.pairs 里写："
            "{name: <起个名>, command: {topic: "
            + best["command"]
            + ", field: position}, state: {topic: "
            + best["state"]
            + ", field: position}, match: nearest, max_time_delta_ms: 50}"
        )
    print("  注：command(通常较低频) 与 state 采样率不同，匹配用 log_time 最近邻，dt 才是能否配的硬指标。")


def _short(topic: str, width: int = 36) -> str:
    return topic if len(topic) <= width else "..." + topic[-(width - 3) :]


def _compare_pair(
    command_times: np.ndarray,
    command_values: np.ndarray,
    state_times: np.ndarray,
    state_values: np.ndarray,
    command_topic: str,
    state_topic: str,
) -> tuple[float, dict]:
    """command 每条消息配到最近的 state：算相关系数、差值分位数、时间差。"""
    right = np.clip(np.searchsorted(state_times, command_times), 0, state_times.size - 1)
    left = np.clip(right - 1, 0, state_times.size - 1)
    delta_left = np.abs(command_times - state_times[left])
    delta_right = np.abs(command_times - state_times[right])
    choose_left = delta_left < delta_right
    index = np.where(choose_left, left, right)
    delta_ns = np.minimum(delta_left, delta_right)

    # 相关系数用插值（长度不同不能逐点比），差值也用插值后的 pair 更稳
    interpolated = np.column_stack(
        [np.interp(command_times, state_times, state_values[:, j]) for j in range(state_values.shape[1])]
    )
    diffs = command_values - interpolated
    correlations = [
        float(np.corrcoef(command_values[:, j], interpolated[:, j])[0, 1])
        if command_values[:, j].std() > 1e-9 and interpolated[:, j].std() > 1e-9
        else 0.0
        for j in range(command_values.shape[1])
    ]
    p95 = float(np.percentile(np.abs(diffs), 95))
    row = {
        "command": command_topic,
        "state": state_topic,
        "dim": command_values.shape[1],
        "n": int(command_values.shape[0]),
        "mean_abs_r": float(np.mean(np.abs(correlations))),
        "p95_abs_diff": p95,
        "dt_p95_ms": float(np.percentile(delta_ns, 95) / 1e6),
    }
    return p95, row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mcap", nargs="?", type=Path, default=DEFAULT_MCAP)
    parser.add_argument("-n", "--messages", type=int, default=3, help="打印的消息条数")
    parser.add_argument("--decode", action="store_true", help="用 mcap_ros2 解码 cdr 负载")
    parser.add_argument("-s", "--seconds", type=float, default=4.0, help="第 5 层配对试验的采样窗口秒数")
    args = parser.parse_args()

    show_layout(args.mcap)
    show_summary(args.mcap)
    show_messages(args.mcap, args.messages, args.decode)
    show_joint_topics(args.mcap)
    show_cmd_state_candidates(args.mcap, args.seconds)


if __name__ == "__main__":
    main()
