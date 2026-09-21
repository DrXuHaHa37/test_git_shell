# logger

通用日志工具：三通道（明细 JSONL / 汇总 JSONL / 控制台）+ 轮转 + 并发安全。

**与业务无关**——`mcap_precheck` 只是它的一个使用方；任何需要「按次留痕 + 机器可读明细 + 人读控制台」的流程都可以直接用。实现只依赖标准库 `logging`。

## 快速上手

```python
from pathlib import Path
from logger import LogSettings, LogSink

with LogSink(LogSettings(dir=Path("logs"), level="INFO", file_prefix="job")) as sink:
    run = sink.run("7f3a2b91", source="/data/job_001.bin")
    run.event("job.started", profile="default")
    run.event("step.result", name="rate_hz", value=29.8, status="PASSED")
    run.summary(code=0, status="OK")
    run.text("人读的控制台输出")
```

`LogSink` 必须在流程结束时 `close()`（或用 `with`），否则队列里的日志可能没落盘。

## 三个通道

| 通道 | 出口 | 内容 |
|---|---|---|
| detail | `<dir>/<file_prefix>-YYYYMMDD.jsonl` | 每个事件一行 JSON，机器读 |
| summary | `<dir>/<summary_file>` | 每次运行一行 JSON，便于聚合统计 |
| console | stdout | 纯文本，人读 |

- 明细行的固定字段：`ts` `level` `run_id` `source` `event`，其余由 `event(**fields)` 自由追加。
- 汇总行自动补 `ts` / `run_id` / `source`；`LogSink.write_summary()` 可写与某次运行无关的汇总（如批量统计）。
- 不传 logger（或 `NullRunLog`）时完全不落盘。

## 并发

所有 handler 挂在一个 `QueueListener` 上：多线程/多进程直接写同一文件会互相覆盖，所以**必须**走 `QueueHandler` + 单独的 listener 线程。一个进程/批量任务持有一个 `LogSink` 实例即可。

## 级别

`LogSettings.level` 对**三个通道统一生效**：`INFO` → 输出 INFO / WARNING / ERROR；`WARNING` → 只输出 WARNING / ERROR。

- 汇总行以 INFO 级写入，level 高于 INFO 时 `summary.jsonl` 不再追加。
- 控制台：整块文本是一次 `run.text()`，级别由渲染方给出。`RunLog.min_level` 暴露当前档位，
  渲染方据此裁剪内容（如「只列告警条目」），并在无可报内容时退回 INFO——于是高挡位下
  正常流程完全不占控制台。

## 配置字段（`LogSettings`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `level` | `INFO` | 三个通道共用的级别 |
| `dir` | `None` | 日志目录；None = 不写文件 |
| `jsonl` | `True` | 明细 + 汇总 |
| `console` | `True` | 控制台 |
| `rotation` | `{by: size, max_bytes: 52428800, backup_count: 10}` | `by: size`（RotatingFileHandler）或 `time`（按天） |
| `summary_file` | `summary.jsonl` | 汇总文件名 |
| `file_prefix` | `run` | 明细文件名前缀 |
| `redact_paths` | `False` | 为真时 `source` 只记「父目录 sha256 前 8 位 / 文件名」 |

`LogSettings.from_config(section)` 可直接从任意配置字典读取这些键（键名与字段同名）。

## 适配已有 logger

`logger.adapt(logger, run_id, source)` 把入参归一化成 `RunLog`：

| 传入 | 结果 |
|---|---|
| `None` | `NullRunLog`（不落盘） |
| `LogSink` | `sink.run(run_id, source)` |
| `RunLog` | 原样返回 |
| `logging.Logger` | `StdLoggerRunLog`（事件降级为一行文本，交给该 logger） |
