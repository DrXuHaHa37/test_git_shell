# MCAP 文件前置检测接口 —— 技术方案

> 版本：v2（按 6 类指标重构）
> 日期：2026-09-15
> 定位：**通用** MCAP 预检接口。在使用 MCAP 文件之前先做一道质量门禁。
> **与 `convert_rysen_multicam_mcap_to_lerobot.py` 完全解耦**，不依赖 openpi / lerobot / rysen 任何业务模块。
> 状态：待评审

---

## 1. 目标与非目标

### 1.1 目标

| 编号 | 目标 |
|---|---|
| G1 | 通用接口：输入 MCAP **文件路径**或**数据流** |
| G2 | 判定文件**完整性**（能否解析、结构是否齐全、是否截断） |
| G3 | 按 **6 大类指标**做检查，每类**可独立启停** |
| G4 | 全部阈值写在**配置文件**，支持多 profile，改阈值不改代码 |
| G5 | 所有启用指标在阈值内 → **可用**；任一 error 级越界 → **拒绝** |
| G6 | 返回**整型状态码**，可直接用作 CLI 退出码 / 上层编排判断 |
| G7 | 完整日志：每次检测可追溯、每指标留痕、可长期留存检索 |

### 1.2 非目标

- 不做数据集转换、不做数据修复/插值
- **不依赖** `openpi` / `lerobot` / `openpi_client.rysen` 等任何业务模块
- 不内置 Rysen 专用 topic 名、关节名、限位——**全部由配置提供**
- 一期不做 HTTP 服务（仅定义形态，见 4.4）

### 1.3 通用性约定

接口本身**不认识**"这是灵巧手数据还是机械臂数据"。所有领域知识外置到配置：

| 领域知识 | 来源 |
|---|---|
| 哪些 topic 要检查 | `topic_metrics.topics` |
| 哪些 topic 是相机 | `camera.topics` |
| 关节字段在哪、限位多少 | `joint.*` |
| command / state 怎么配对 | `cmd_state.pairs` |
| NaN/Inf 扫哪些字段 | `numeric.scan_fields` |

---

## 2. 总体架构

### 2.1 分层

```
┌──────────────────────────────────────────────────────────────┐
│ 输入适配层  SourceAdapter                                     │
│   str | Path | bytes | BinaryIO  →  可 seek 的二进制流         │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────────────┐
│ 读取层  McapReader                                            │
│   summary（结构/统计） + 单趟消息迭代 + 按需解码               │
│   依赖：mcap、mcap_ros2                                       │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────────────┐
│ 指标层  Metrics —— 6 个类别，各自可独立启停                    │
│   ①全局 ②Topic ③相机 ④非法数值 ⑤关节 ⑥命令-状态                │
│   每类产出 MetricResult 列表                                   │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────────────┐
│ 规则层  RuleEngine：MetricResult × 阈值 → passed/severity      │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
┌──────────────────────────────────────────────────────────────┐
│ 判定层  Verdict → StatusCode                                  │
└──────────────────────┬───────────────────────────────────────┘
                       ↓
        ┌──────────────┴───────────────┐
        ↓                              ↓
┌────────────────┐          ┌──────────────────────────┐
│ QcReport(dict) │          │ 日志：JSONL 明细+汇总+控制台│
└────────────────┘          └──────────────────────────┘
```

### 2.2 设计原则

| 原则 | 说明 |
|---|---|
| **类别正交** | 6 类互不依赖，可任意组合；关闭的类完全不执行、不产生日志噪声 |
| **单趟扫描** | 除相机解码外，所有指标在一次消息迭代中累计完成 |
| **指标/阈值分离** | 指标只算值，阈值只在配置，规则引擎负责比较 |
| **按需解码** | 相机类默认关闭；开启时受 `max_frames` / `sample_ratio` 限制 |
| **快速失败** | 结构不完整 → 直接短路，不做后续逐消息扫描 |
| **依赖可裁** | 核心只需 `mcap` + `mcap_ros2` + `numpy` + `PyYAML`；相机类才需要 `av` |

### 2.3 目录结构

```
apis/
└── mcap_precheck/                 # 包名沿用你已建的 mcap_pre_check 语义
    ├── __init__.py                # 导出 inspect / inspect_dir / StatusCode
    ├── source.py                  # 输入适配（路径 / bytes / 流）
    ├── reader.py                  # summary + 单趟迭代 + 帧解码
    ├── fields.py                  # 字段选择器（topic → 数值向量）
    ├── config.py                  # 配置加载 / profile 合并 / schema 校验
    ├── rules.py                   # 阈值比较与严重级别
    ├── verdict.py                 # StatusCode + 聚合
    ├── report.py                  # QcReport 与序列化
    ├── qclog.py                   # 日志的领域渲染（通用实现在顶层 logger 包）
    ├── cli.py
    └── metrics/
        ├── base.py                # MetricResult / Metric 基类
        ├── integrity.py           # 0 类：文件完整性（恒启用）
        ├── global_metrics.py      # ① 全局
        ├── topic_metrics.py       # ② Topic
        ├── camera_metrics.py      # ③ 相机（需解码）
        ├── numeric_metrics.py     # ④ NaN / Inf
        ├── joint_metrics.py       # ⑤ 关节
        └── cmd_state_metrics.py   # ⑥ 命令-状态
└── config/
    ├── mcap_precheck.yaml
```

---

## 3. 状态码

```python
class StatusCode(IntEnum):
    OK                    = 0     # 全部启用指标在阈值内
    ACCEPTED_WITH_WARNING = 10    # 仅 warn 级越界，仍可用
    REJECTED_METRIC       = 20    # error 级指标越界 → 拒绝
    INCOMPLETE_STRUCTURE  = 30    # 结构不完整（缺 topic / schema / 截断）
    CORRUPTED             = 40    # 解析失败
    EMPTY                 = 50    # 无消息
    INVALID_INPUT         = 60    # 输入或配置非法
    INTERNAL_ERROR        = 90    # 未预期异常
```

- **分段编号**（0/10/20/…/90）留扩展位，插入新码不破坏既有值。
- `code >= 20` 即**拒绝**；`code < 20` 即**可用**。提供 `is_rejected()`，上层不硬编码比较。
- 数值兼容 HTTP 语义（0/10→2xx，20~60→4xx，90→5xx）。
- CLI 退出码 == 状态码，`shell` 里直接 `if [ $? -eq 0 ]`。

---

## 4. 接口设计

### 4.1 数据结构

```python
@dataclass(frozen=True)
class MetricResult:
    category: str                  # "global" | "topic" | "camera" | ...
    name: str                      # 指标键名，对应配置 key
    subject: str                   # 作用对象："/camera/head" 或 "joint[index_mcp]" 或 "-"
    value: float | int | None      # 实测值；不可计算为 None
    unit: str
    status: MetricStatus           # PASSED / WARN / FAILED / SKIPPED
    threshold: dict | None         # 回显生效阈值
    severity: Severity             # INFO / WARN / ERROR
    detail: dict = field(default_factory=dict)

@dataclass(frozen=True)
class QcReport:
    inspect_id: str
    source: str
    status: StatusCode
    metrics: tuple[MetricResult, ...]
    started_at: str
    elapsed_ms: float
    profile: str
    enabled_categories: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def code(self) -> int: return int(self.status)
```

### 4.2 Python API

```python
def inspect(source, *, config=None, profile="default",
            categories: Iterable[str] | None = None,   # 运行时覆盖启停，None=用配置
            logger=None) -> QcReport: ...

def inspect_dir(directory, *, pattern="*.mcap", recursive=True,
                config=None, profile="default", max_workers=4) -> list[QcReport]: ...
```

`inspect()` **不抛业务异常**，可预期错误转成状态码写入 `errors`。

### 4.3 CLI

```bash
python -m mcap_precheck inspect data/rec.mcap --config config/mcap_precheck.yaml --profile strict
python -m mcap_precheck inspect-dir /data/raw --only global,numeric --json
echo $?        # 退出码 = 状态码
```

| 参数 | 说明 |
|---|---|
| `--config` / `--profile` | 配置文件与 profile |
| `--only` / `--skip` | 运行时选择**类别**（如 `--only global,numeric`） |
| `--json` | 输出 JSON 报告 |
| `--log-dir` / `--log-level` | 日志 |
| `--fail-on-warning` | warn 也视为拒绝 |

### 4.4 HTTP（一期不实现，仅定形态）

```
POST /api/v1/mcap/inspect      # multipart 上传 或 {"path": "..."}
→ {"inspect_id": "...", "code": 0, "status": "OK", "failed_metrics": [], ...}
```

一期不做的理由：MCAP 动辄数百 MB～GB，同步请求长时间占连接；且当前无队列/对象存储。要做应改为"提交任务 → 轮询"异步形态，复用本核心库。

### 4.5 重要约束：流式 vs summary

> **MCAP 的 summary 段在文件尾部。**

| 模式 | 前提 | 可用类别 | 说明 |
|---|---|---|---|
| `strict`（默认） | 输入**可 seek** | 全部 | 先读 summary 快速失败，再单趟迭代 |
| `streaming` | 输入**不可 seek** | ① ② ④ ⑤ ⑥（**不含结构类、不含相机**） | 无 summary → 无法校验 topic 完整性、无法预知消息数 |

流式模式下若配置要求结构类检查 → 返回 `INVALID_INPUT`（配置与输入形态不匹配），**不静默跳过**。

---

## 5. 指标体系（6 类，均可独立启停）

> 除 6 类外，恒启用 **0 类：文件完整性**（可解析 / 未截断 / 非空）。
> 详细内容见/config/*.md

---

## 6. 配置文件
> config/mcap_precheck.yaml

---

## 7. 判定逻辑

```
1. 输入/配置非法                       → INVALID_INPUT (60)
2. 解析失败 / 截断                      → CORRUPTED (40)
3. 结构不完整（缺 topic/schema/summary） → INCOMPLETE_STRUCTURE (30)
4. 消息数为 0                           → EMPTY (50)
5. 任一启用指标 error 级越界             → REJECTED_METRIC (20)
6. 有 warn 级越界                        → ACCEPTED_WITH_WARNING (10)
7. 否则                                  → OK (0)
8. 未预期异常                            → INTERNAL_ERROR (90)
```

`fail_fast: true` 时第 2/3 步短路返回，不执行后续指标计算（也是性能优化）。
`fail_on_warning: true` 时第 6 步也判为拒绝。

---

## 8. 日志方案

### 8.1 需求

| 编号 | 需求 |
|---|---|
| L1 | 每次检测唯一 ID，串联全部日志 |
| L2 | 每个指标单独留痕（名称、实测值、阈值、是否通过） |
| L3 | 越界指标醒目，能直接筛出"为什么被拒" |
| L4 | 批量可聚合统计（通过/拒绝数、按原因分布） |
| L5 | 轮转保留，不撑爆磁盘 |
| L6 | 机器可读 + 人类可读 |
| L7 | 不引入重型依赖 |

### 8.2 选型：标准库 `logging`

不引入 `structlog` / `loguru`。理由：零新增依赖；`LoggerAdapter` 天然支持上下文注入；
`RotatingFileHandler` 满足轮转；日后接 ELK 只需换 Formatter。

### 8.3 三个通道

```
                    ┌─→ ① 明细  qc-YYYYMMDD.jsonl   （每指标一行，机器读）
LoggerAdapter ──────┼─→ ② 汇总  summary.jsonl       （每次检测一行）
  (注入上下文)       └─→ ③ 控制台 stdout             （✓/✗ 表格，人读）
```

**① 明细（JSONL）**

```json
{"ts":"2026-09-15T14:30:12.881Z","level":"INFO","inspect_id":"7f3a2b91","event":"metric.result","source":"/data/rec_001.mcap","category":"global","metric":"duration_s","subject":"-","value":12.34,"unit":"s","op":"min","threshold":5.0,"status":"PASSED","severity":"error"}
{"ts":"...","level":"ERROR","inspect_id":"7f3a2b91","event":"metric.result","source":"/data/rec_001.mcap","category":"global","metric":"timestamp_rollback_count","subject":"-","value":3,"unit":"count","op":"max","threshold":0,"status":"FAILED","severity":"error","detail":{"first_index":812,"max_delta_ns":-15000000}}
{"ts":"...","level":"INFO","inspect_id":"7f3a2b91","event":"inspect.finished","source":"/data/rec_001.mcap","code":20,"status":"REJECTED_METRIC","elapsed_ms":129,"failed":["global.timestamp_rollback_count"]}
```

**固定 schema**（便于建索引）：`ts` / `level` / `inspect_id` / `event` / `source` / `category` / `metric` / `subject` / `value` / `unit` / `op` / `threshold` / `status` / `severity` / `detail`

`event` 枚举：`inspect.started` / `config.loaded` / `metric.result` / `metric.skipped` / `inspect.finished` / `batch.summary`

**② 汇总（JSONL）**

```json
{"ts":"...","inspect_id":"7f3a2b91","source":"/data/rec_001.mcap","code":20,"status":"REJECTED_METRIC","profile":"strict","enabled_categories":["global","topic","camera"],"duration_s":12.34,"message_count":12345,"elapsed_ms":129,"failed_metrics":["global.timestamp_rollback_count"],"warn_metrics":[]}
```

**③ 控制台**

```
[7f3a2b91] /data/rec_001.mcap   profile=strict
  ① global
    ✓ duration_s                  12.34 s      (min 5.0)
    ✗ timestamp_rollback_count         3       (max 0)      ← 拒绝原因
    ✓ max_frame_interval_ms       42.1  ms     (max 100.0)
  ② topic
    ✓ /camera/head  rate_hz       29.8  Hz     (25~35)
    ✓ /joint_states rate_hz       99.2  Hz     (90~110)
  ─────────────────────────────────────────────────────
  判定: REJECTED_METRIC (20)   耗时 129 ms
```

### 8.4 级别约定

| 级别 | 用途 |
|---|---|
| DEBUG | 逐消息采样（默认关闭，`debug_sample_rate` 控制，防大文件刷屏） |
| INFO | 检测开始/结束、每个 PASSED 指标、配置加载 |
| WARNING | warn 级越界；**预警**：指标达阈值 `warn_ratio`（默认 0.8） |
| ERROR | error 级越界、解析异常、最终判定拒绝 |
| CRITICAL | 不可恢复（磁盘满、配置不可读） |

> **预警机制**：`max_frame_interval_ms` 阈值 100ms，实测 85ms 虽通过但已达 80%，
> 打 WARNING。可在真正越界前发现趋势性劣化。

### 8.5 轮转与保留

`RotatingFileHandler(maxBytes=50MB, backupCount=10)`（按大小，默认）或 `TimedRotatingFileHandler`（按天）。
单日上限约 500MB，超出由运维侧 `logrotate` 清理。

### 8.6 上下文注入

```python
adapter = logging.LoggerAdapter(logger, {"inspect_id": inspect_id, "source": source})
```

保证任何一处日志自动带 `inspect_id`，核心库只接收 `logger` 参数、不感知上层上下文。

### 8.7 安全与体积

| 项 | 处理 |
|---|---|
| 大二进制 | 绝不写入；图像只记分辨率与帧号 |
| 路径脱敏 | `redact_paths: true` 时只记文件名 + 父目录哈希 |
| 堆栈 | 仅 `INTERNAL_ERROR (90)` 记完整 traceback；业务拒绝只记原因 |
| 刷屏 | 每指标一条，天然有限；逐消息日志默认关闭 |

### 8.8 并发安全

批量 `inspect_dir(max_workers=N)` 时，日志**必须**用 `QueueHandler` + 单独 listener 线程，
否则多进程/线程写同一文件会互相覆盖。这是批量场景的**必选项**。

---

## 9. 性能

| 措施 | 说明 |
|---|---|
| 单趟扫描 | 除相机外，所有指标一次迭代累计 |
| 快速失败 | 结构类失败即短路 |
| summary 优先 | 消息数/时长/topic 校验走 summary，O(1) |
| 向量化 | 时间戳差分、分位数、cmd-state 匹配用 numpy / `searchsorted` |
| 相机抽样 | `sample_ratio` / `max_frames` 限制解码量 |
| 预分配 | 时间戳用 `np.empty` 预分配 + 事后截断，而非 list.append |

**量级估计**（单文件，纯元数据扫描）：100 万条消息约 3–8 秒；开启相机全量解码则升至分钟级。

---

## 10. 测试策略

### 10.1 合成 fixture（用 `mcap` 库自写"坏文件"）

| fixture | 构造 | 期望 |
|---|---|---|
| `good.mcap` | 正常 | 0 |
| `truncated.mcap` | 写一半截断 | 40 |
| `rollback.mcap` | 把第 N 条时间戳改小 | 20 |
| `dropped.mcap` | 抽掉中间若干帧 | 20 |
| `missing_topic.mcap` | 少写一个必需 topic | 30 |
| `empty.mcap` | 只写 header | 50 |
| `nan.mcap` | 写入 NaN | 20 |
| `dup_frame.mcap` | 连续写相同图像 | 20 |
| `black.mcap` | 全黑帧 | 20 |
| `blur.mcap` | 高斯模糊帧 | 10/20（warn/error 可配） |
| `joint_oob.mcap` | 关节值超限位 | 20 |
| `nonseekable` | 不可 seek 的流 | 降级 streaming 模式 |

### 10.2 层次

| 层次 | 内容 |
|---|---|
| 单元 | 每类指标的纯函数（给定序列 → 期望值）；RuleEngine 边界值表驱动 |
| 配置 | profile 继承、未知 key 报错、区间矛盾、类别启停组合 |
| 集成 | `inspect()` 对各 fixture 的状态码断言 |
| 日志 | `caplog` 断言每指标 1 条 `metric.result`；拒绝时有 ERROR 级 |
| CLI | 退出码 == 状态码 |

---

## 11. 实施计划

| 阶段 | 内容 | 预估 |
|---|---|---|
| M1 | 骨架：`source` / `reader` / `config` / `rules` / `verdict` / `report` + **① 全局** | 1.5 天 |
| M2 | **② Topic** + **④ 非法数值** + **0 完整性** | 1 天 |
| M3 | 日志模块（三通道 + 轮转 + 上下文） | 1 天 |
| M4 | **⑤ 关节** + **⑥ 命令-状态** | 1.5 天 |
| M5 | **③ 相机**（解码 + 卡顿/重复/黑屏/模糊） | 2 天 |
| M6 | CLI + 批量并发 + fixture 生成器 + 测试 | 1.5 天 |

**建议**：M1–M3 先交付即可覆盖 ①②④ 与日志；③⑤⑥ 按优先级续做。

---

## 12. 待确认

| # | 问题 | 影响 |
|---|---|---|
| # | 问题 | 影响 | 状态 |
|---|---|---|---|
| ~~Q1~~ | 全局 `time_source` 默认值 | ① 全局回退定义 | ✅ **已由脚本回答**（5.7.2）：相机用 `header_stamp`、状态/命令用 `log_time`，配置改为**分层指定** |
| ~~Q5~~ | ⑥ 偏差用什么范数 | ⑥ 算法 | ✅ **已由脚本回答**（`_action_quality` L700-712）：**逐维绝对值 + 按关节组分组**，取 p95/max |
| Q2 | **丢帧**的 `expected_rate_hz` 是全局统一还是逐 topic？不同相机/关节频率不同 | ① ② 配置结构 | 待定（倾向逐 topic） |
| Q3 | ③ 相机阈值初值谁定？**脚本只给了 skew=50ms、resize=224**，卡顿/重复/黑屏/模糊四个数仍缺 | 相机类可用性 | **待标定**：需要真实样本 |
| Q4 | ⑤ 关节限位表从哪来？（URDF？现成 yaml？） | ⑤ 落地前提 | 待提供 |
| Q6 | 相机类是否接受抽样（`sample_ratio`）？还是必须全量？ | M5 性能方案 | 待定 |
| Q7 | 运行环境：容器 Python 3.10（当前无 mcap）还是新建 3.11 环境？ | 部署方式 | 待定 |
| Q8 | ⑥ 的 `max_time_delta_ms` 用 50ms（与 `--max-command-age-ms` 同量级）是否合适？ | ⑥ 配对窗口 | 待定 |

---

## 13. 依赖清单

| 依赖 | 必需性 | 用途 |
|---|---|---|
| `mcap` | **必需** | MCAP 读取 |
| `mcap-ros2` | **必需** | ROS2 消息解码 |
| `numpy` | **必需** | 向量化统计 |
| `PyYAML` | **必需** | 配置加载 |
| `av` | 相机类启用时 | h264/h265 解码 |
| `opencv-python-headless` | 可选 | 加速拉普拉斯/resize（默认 numpy 实现） |

> 当前开发容器（Python 3.10.12）**尚未安装**以上任何一个，需按前文依赖梳理决策：
> 独立 venv（推荐）或装进容器。
