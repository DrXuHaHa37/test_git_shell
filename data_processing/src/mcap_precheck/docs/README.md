# mcap_precheck

MCAP 文件的**通用前置质量检测**（质量门禁）：给定文件路径或数据流，判定「能不能用」，返回整型状态码。

- 与 `convert_rysen_multicam_mcap_to_lerobot.py` **完全解耦**：不 import `openpi` / `lerobot` / `openpi_client.rysen`。
- 不内置任何 Rysen 业务知识：topic 名、关节名、限位、字段路径全部由 YAML 提供。
- 阈值与代码分离：改阈值不改代码；6 类指标均可独立启停。

设计依据见 `../docs/mcap-qc-design.md`。

---

## 1. 快速上手

```bash
cd apis
# src 布局：导入根是 src，包内一律 `from logger import ...`（不要写 `from src.logger import ...`）
PYTHONPATH=src .venv/bin/python -m mcap_precheck inspect data/rec.mcap --profile strict
PYTHONPATH=src .venv/bin/python -m mcap_precheck inspect-dir /data/raw --only global,numeric --json
echo $?          # 退出码 == 状态码：0 可用，>=20 拒绝

# 或直接用目录入口（__main__.py 会自己把 src 加进 sys.path）
.venv/bin/python src/mcap_precheck inspect data/rec.mcap
```

```python
from mcap_precheck import inspect, StatusCode

report = inspect("data/rec.mcap", profile="strict")
report.code                       # 0
report.status.is_rejected()       # False
report.failed_metrics             # ("global.timestamp_rollback_count",)
report.to_dict()                  # 需要 JSON 时
```

业务侧封装（推荐）：`../rysen_apis.py`

```python
from rysen_apis import precheck_mcap
report = precheck_mcap("data/rec.mcap")     # 返回 QcReport，不抛业务异常
```

---

## 2. 代码架构

### 2.1 分层

```
输入适配层  source.py      str | Path | bytes | 流 → Source（探测 seekable）
     ↓
读取层      reader.py      summary + 单趟消息迭代 + 按需解码 + 相机帧 sink
     ↓
指标层      metrics/       ⓪完整性 ①全局 ②Topic ③相机 ④NaN/Inf ⑤关节 ⑥命令-状态
     ↓
规则层      rules.py       MetricResult = 实测值 × 阈值
     ↓
判定层      verdict.py     → StatusCode
     ↓
            report.py（QcReport）   qclog.py（渲染检测结果）→ logger 包（三通道日志）

编排：pipeline.py（inspect / inspect_dir）   CLI：cli.py + __main__.py
配置：config.py（加载 / profile / 自洽校验）
字段：fields.py（点号路径选择器）
```

### 2.2 模块职责

| 模块 | 职责 |
|---|---|
| `source.py` | 统一输入形态；`seekable` 决定走 strict 还是 streaming 模式 |
| `reader.py` | `scan(plan, sinks)` 单趟迭代：累计各 topic 时间戳、字段值；相机帧交给 sink 流式消费。解析异常**不抛出**，记入 `McapScan.errors` |
| `fields.py` | 字段选择器：`position`、`data[0:16]`、`joints[3].position`、`header.stamp.sec`；以及"扫描全部数值字段"的递归遍历 |
| `config.py` | YAML 加载、profile 选取、加载期自洽校验、`time_source` 分层解析 |
| `rules.py` | `Threshold`（min/max/equals/severity）与 `evaluate()`；含逼近阈值预警 |
| `verdict.py` | `StatusCode`、`Severity`、`MetricStatus` |
| `metrics/base.py` | `MetricResult`、`Metric` 基类、`stack_values` |
| `metrics/integrity.py` | 完整性检查（函数式，不是 Metric 子类，因为要短路） |
| `metrics/global_metrics.py` 等 | 6 个类别，各自产出 `MetricResult` 列表 |
| `report.py` | `QcReport` 与序列化 |
| `qclog.py` | `QcLog`：把 `MetricResult` / `QcReport` 渲染成日志事件与控制台表格（领域相关部分） |
| `pipeline.py` | 编排、短路、聚合判定；不抛业务异常 |
| `cli.py` | argparse；退出码 == 状态码 |

### 2.3 单次检测流程（`inspect()`）

1. 生成 `inspect_id`（8 位十六进制，串联全部日志）
2. 加载配置 → 失败返回 `INVALID_INPUT (60)`
3. 解析启用的类别（可被 `categories` 参数运行时覆盖）
4. 打开输入 → 失败返回 `INVALID_INPUT (60)`
5. **流式约束**：输入不可 seek 且配置要求结构类检查（summary / required_topics）→ `INVALID_INPUT (60)`，不静默跳过
6. 单趟扫描 → 完整性检查 → `fail_fast` 时短路返回
7. 逐类别算指标 → 规则比较 → 聚合判定 → 写日志 → 返回 `QcReport`

任何未预期异常兜底为 `INTERNAL_ERROR (90)`，仅此时记录 traceback。

### 2.4 关键实现约定

- **单趟扫描**：除相机解码外，所有指标在一次消息迭代中累计完成；只解码"被指标需要"的 topic。
- **回退检测用文件顺序**：`timestamp_rollback_*` 必须用**未排序**的 log_time 序列——排序后回退恒为 0；间隔/丢帧才用排序后的序列。
- **相机帧不驻留内存**：通过 `CameraSink` 流式消费，只累计计数与统计量；h264/h265 的解码器状态跨消息保留（P 帧依赖同 GOP 的 I 帧）。
- **指标只算值，阈值只在配置，比较只在 `rules.py`**。

---

## 3. 状态码

| 值 | 名称 | 含义 |
|---|---|---|
| 0 | `OK` | 全部启用指标在阈值内 |
| 10 | `ACCEPTED_WITH_WARNING` | 仅 warn 级越界，仍可用 |
| 20 | `REJECTED_METRIC` | error 级指标越界 |
| 30 | `INCOMPLETE_STRUCTURE` | 缺 topic / 无 summary |
| 40 | `CORRUPTED` | 解析失败 / 截断 |
| 50 | `EMPTY` | 无消息 |
| 60 | `INVALID_INPUT` | 输入或配置非法 |
| 90 | `INTERNAL_ERROR` | 未预期异常 |

- `code >= 20` 即拒绝；用 `status.is_rejected(fail_on_warning=...)` 判断，不要在业务代码里硬编码比较。
- `status.http_status`：0/10 → 200，20~60 → 400，90 → 500。
- CLI 退出码 == 状态码；`--fail-on-warning` 时 10 按 20 退出。

---

## 4. 指标类别

| # | 类别 | 模块 | 启用条件 | 指标 key |
|---|---|---|---|---|
| ⓪ | `integrity` | `integrity.py` | **恒启用** | `readable` `not_truncated` `summary_present` `message_count` `required_topics` |
| ① | `global` | `global_metrics.py` | `categories.global` | `duration_s` `timestamp_rollback_count` `timestamp_rollback_max_ns` `max_frame_interval_ms` `dropped_frame_count` `message_count` |
| ② | `topic` | `topic_metrics.py` | `catch_all` 或 `topics` 非空 | `rate_hz` `max_interval_ms` `message_count` `stamp_rollback_count`¹ |
| ③ | `camera` | `camera_metrics.py` | `camera.topics` 非空 | `skew_p95_ms` `skew_max_ms` `stutter_count` `duplicate_ratio` `black_ratio` `blur_ratio` `decode_failure_count` `log_minus_header_p95_ms` `log_minus_header_drift_ms` |
| ④ | `numeric` | `numeric_metrics.py` | 有阈值或 `scan_fields` 非空 | `nan_inf_count` |
| ⑤ | `joint` | `joint_metrics.py` | `joint.topics` 且 `thresholds` 非空 | `out_of_range_count` `max_velocity` `velocity_p95` |
| ⑥ | `cmd_state` | `cmd_state_metrics.py` | `cmd_state.pairs` 非空 | `first_command_minus_state_p95_abs` `first_command_minus_state_max_abs` `adjacent_command_change_max_abs` `unmatched_ratio` |

¹ `stamp_rollback_count` 由 `topic_metrics.check_stamp_rollback` 控制（默认关），用于单 topic 内的 publish_time 回退检查（多设备未同步时会有假阳性）。

各类的语义与算法细节见 `../docs/mcap-qc-design.md` 第 5 节。

---

## 5. YAML 配置规则

配置文件：`apis/config/mcap_precheck.yaml`（`version: 1`，内置 `default` / `strict` / `smoke` 三个 profile）。

### 5.1 profile 与继承

```yaml
profiles:
  default: &default       # 锚点
    ...
  strict:
    <<: *default          # 合并
    categories: { ... }   # 覆盖
```

> **浅合并陷阱**：YAML 的 `<<` 是**浅**合并——覆盖某个段落会**整体替换**该段落，不是逐键深合并。
> 所以 `strict` / `smoke` 里 `categories`、`global_metrics` 这类段落必须**写全**，否则未列出的类别会被丢掉。
> 想把某段复用给多个 profile，用锚点单独引用（配置里 `topic_metrics.topics: &topic_overrides` / `topics: *topic_overrides` 就是这么做的）。

### 5.2 三级启停

1. **粗**：`categories.<name>.enabled`
2. **中**：该类的 `topics` / `pairs` / `joints` 为空 → 整类跳过（不产生日志噪声）
3. **细**：单项 `enabled: false`、或不给阈值 → 该指标 `SKIPPED`

运行时还可用 `inspect(..., categories=["global","numeric"])` 或 CLI `--only` / `--skip` 覆盖。

被跳过的类别**不会静默消失**：控制台显示 `③ camera   跳过：camera.topics 为空…`，明细日志记 `event=metric.skipped`。常见原因就是"类别开关开了，但领域配置（topics / pairs / 限位）没填"——这是最容易误判成"跑了但没问题"的地方。

> `catch_all: true` 时，未在 `groups` / `topics` 里归类的 topic 一律吃 `thresholds` 兜底阈值。严格 profile 下 `thresholds.max_interval_ms` 通常比相机/状态的实际间隔紧，记得用 `topics.<topic>.group: head_camera|robot_state|...` 归类。

### 5.3 阈值 spec

```yaml
rate_hz: {min: 25, max: 35, severity: error}
```

- 算子：`min`（低于即越界）/ `max`（高于即越界）/ `equals`（不等即越界）；`min`+`max` 构成区间。
- `severity`：`error`（越界 → 拒绝）/ `warn`（越界 → 仍可用，状态码 10）/ `info`。
- 三个算子都缺省 → 该指标 `SKIPPED`（不判失败）。
- 回显：每个 `MetricResult.threshold` 会带上实际生效的 spec 写进日志，便于复盘。

### 5.4 各配置段速查

| 段 | 结构要点 |
|---|---|
| `integrity` | 输入键在段落根：`required_topics: []`（留空不校验）、`require_summary: true`（true/false 决定 `summary_present` 的 severity）；判定线在 `thresholds{readable, not_truncated, summary_present, message_count, required_topics}`。该段只接受这三类键，写其它键直接报错 |
| `time_source` | `default` / `per_category{camera,joint}` / `per_topic{}`，优先级从下往上 |
| `global_metrics` | `expected_rate_hz`（丢帧判定的基准，缺省则 `dropped_frame_count` 为 SKIPPED）、`drop_detect{mode: interval\|expected_count, factor}`、`thresholds{}` |
| `topic_metrics` | `catch_all`、`check_stamp_rollback`、`thresholds`（兜底）、`groups.<name>.thresholds`（按数据类别）、`topics.<topic>.thresholds`（逐 topic）；解析顺序 **段落 → group → 条目**，按指标逐项覆盖 |
| `camera_metrics` | `topics`（{topic: spec} 或列表）、`backend`（av / opencv）、`max_frames`、`sample_ratio`、`blur/black/duplicate/stutter` 算法参数（段落级）、`groups.<name>.thresholds`、`topics.<topic>.thresholds` |
| `numeric` | `scan_fields: [{topic, field}]`（**留空 = 扫描所有数值字段**）、`nan_inf_count` |
| `joint_metrics` | `topics[{name, position_field, velocity_field, group}]`、`groups.<name>{defaults, joints, thresholds}`（左右手/左右臂各一份）、`thresholds{}`（段落兜底） |
| `cmd_state_metrics` | `pairs[{name, command{topic,field}, state{topic,field}, match, max_time_delta_ms, thresholds}]`、`thresholds{}`（段落兜底；阈值须逐 pair 给） |
| 判定 | `fail_fast`（结构失败即短路）、`fail_on_warning` |
| `logging` | 见第 6 节 |

> **例外**：`numeric` 的阈值直接写在类别根节点（`numeric.nan_inf_count`），**没有** `thresholds` 子段；其余类别（global/camera/joint/cmd_state）都在 `thresholds` 里。

### 5.5 加载期自洽校验（`config.validate_config`）

| 规则 | 说明 |
|---|---|
| 未知指标 key | 直接报错，防止拼写错误被静默忽略（按类别各有白名单） |
| `min > max` | 报错 |
| 语义上非负的指标给了负值 | 报错（`duration_s` / `*_ms` / `*_count` / `*_ratio` / `rate_hz` 等） |
| **跨字段**：`topic_metrics.*.max_interval_ms` 不得宽于 `global_metrics.max_frame_interval_ms` | 报错。软阈值比硬上限还宽会导致硬上限静默失效 |
| `global_metrics.thresholds.message_count.min` 必须 > 0 | 报错 |
| `expected_rate_hz` 必须 > 0 | 报错 |
| profile 不存在 / YAML 非法 / version 不匹配 | 报错 → `INVALID_INPUT (60)` |

---

## 6. 日志规则

### 6.1 三个通道

日志的**通用实现**在顶层 `../logger` 包（三通道、轮转、队列 listener、路径脱敏），本包只依赖它、不含任何日志基础设施代码。本包内的 `qclog.py` 负责领域渲染：事件命名、`metric.result` 的字段布局、控制台的 ①②③ 表格。

```
QcLog（领域渲染）→ RunLog（通用，注入 inspect_id / source）
   ├─ ① 明细   <dir>/qc-YYYYMMDD.jsonl   每指标一行，机器读（JSONL）
   ├─ ② 汇总   <dir>/summary.jsonl       每次检测一行 + 批量汇总一行
   └─ ③ 控制台 stdout                    ✓/✗ 表格，人读
```

统一走 `QueueHandler` + 单独 listener 线程：多线程/多进程写同一文件会互相覆盖，批量场景这是**必选项**。因此日志由 `LogSink` 持有生命周期，用完必须 `close()`（或 `with LogSink(...)`）。

不传 `logger` 时用 `NullLog`，完全不落盘——**要留痕必须显式构造 `LogSink` 传入**。

### 6.2 明细 JSONL

固定 schema（便于建索引）：

`ts` `level` `inspect_id` `event` `source` `category` `metric` `subject` `value` `unit` `op` `threshold` `status` `severity` `detail`

```json
{"ts":"2026-09-15T08:13:52.776Z","level":"ERROR","inspect_id":"7f3a2b91","event":"metric.result","source":"/data/rec_001.mcap","category":"global","metric":"timestamp_rollback_count","subject":"-","value":3,"unit":"count","op":"max","threshold":0,"status":"FAILED","severity":"error","detail":{"bound":0}}
```

- `event` 枚举：`inspect.started` / `config.loaded` / `metric.result` / `metric.skipped` / `inspect.error` / `inspect.finished` / `batch.summary`
- 区间阈值（min~max）没有单一 bound，`threshold` 字段回显完整 spec。
- 越界时 `detail.bound` 与 `op` 指出是哪一侧越界；逼近阈值时 `detail.near_limit = {op, bound}`。

### 6.3 汇总 JSONL

```json
{"ts":"...","inspect_id":"...","source":"...","code":0,"status":"OK","profile":"default",
 "enabled_categories":["integrity","global","topic","numeric"],"duration_s":17.1,"message_count":11185,
 "elapsed_ms":1777,"failed_metrics":[],"warn_metrics":[],"near_limit_metrics":[]}
```

批量结束时追加一行：`{"event":"batch.summary","files":4,"rejected":1,"by_status":{"OK":3,"REJECTED_METRIC":1}}`

> `warn_metrics` 只收 **status == WARN**（warn 级阈值被越界）的指标；`near_limit_metrics` 收**通过但逼近阈值**的指标（趋势预警）。两者互不相干——看到明细里有 WARNING 而 `warn_metrics` 为空，通常就是 near_limit 预警。

### 6.4 级别约定

| 级别 | 用途 |
|---|---|
| DEBUG | 预留（当前无逐消息日志，避免大文件刷屏） |
| INFO | `inspect.started` / `config.loaded` / PASSED 指标 / `inspect.finished` / 汇总 / 控制台 |
| WARNING | warn 级越界；**逼近阈值预警**（`near_limit`） |
| ERROR | error 级越界、解析异常、最终判定拒绝 |
| CRITICAL | 不可恢复（未使用，留给运维侧） |

**`logging.level` 对三个通道统一生效**：`INFO` → 输出 INFO / WARNING / ERROR；`WARNING` → 只输出 WARNING / ERROR。

- **控制台表格按内容裁剪**：只列级别达到 `level` 的指标行（FAILED / WARN / 逼近阈值预警），通过的指标与 SKIPPED 行不再刷屏；整份报告无告警无错误时表格退回 INFO 级，WARNING 挡下完全不输出。
- **汇总行恒为 INFO 级**，因此 WARNING 挡下 `summary.jsonl` 不追加。

**逼近阈值预警的判据**（`rules.Threshold.near_limit`，`warn_ratio` 默认 0.8）：

- 双侧区间 `[min,max]`：按**区间宽度**判定——`[90,110]` 里的 100 处于中部，不算逼近；只有落在两端 20% 内才算。（若误用 bound 自身的比例，区间中部会被判成逼近，已修。）
- 单侧阈值：按 bound 的比例——100ms 阈值实测 85ms（0.85 ≥ 0.8）→ 预警。
- 预警**不影响判定码**，只用于提前发现趋势性劣化。

### 6.5 轮转、并发与安全

| 项 | 处理 |
|---|---|
| 轮转 | `rotation.by: size` → `RotatingFileHandler(max_bytes, backup_count)`；`by: time` → 按天 `TimedRotatingFileHandler` |
| 并发 | 所有 handler 挂在一个 `QueueListener` 上，`inspect_dir(max_workers=N)` 安全 |
| 大二进制 | 绝不写入日志；图像只记分辨率与帧号 |
| 路径脱敏 | `redact_paths: true` 时只记「父目录 sha256 前 8 位 / 文件名」 |
| 堆栈 | 只有 `INTERNAL_ERROR (90)` 记 traceback；业务拒绝只记原因 |

---

## 7. 依赖

| 依赖 | 必需性 | 用途 |
|---|---|---|
| `mcap` / `mcap-ros2-support` | 必需 | MCAP 读取与 ROS2 解码 |
| `numpy` | 必需 | 向量化统计 |
| `PyYAML` | 必需 | 配置加载 |
| `av` | ③ 相机类启用时 | h264/h265 解码（`uv sync --group camera`） |
| `pillow` | JPEG 相机 | 已在默认组 |
| `opencv-python-headless` | 可选 | `camera.backend: opencv` 时加速灰度/resize/拉普拉斯（默认 numpy 实现，零依赖） |

---

## 8. 已知待办

- ③ 相机的四类阈值（卡顿/重复/黑屏/模糊）仍为占位初值，需用真实样本标定。
- ⑤ 关节限位表（`joint.joints`）尚未填入真实限位。
- 一期不做 HTTP 服务；要做应改成「提交任务 → 轮询」的异步形态，复用本核心库。
