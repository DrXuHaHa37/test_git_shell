# mcap_precheck 配置参考

> **本文件以 `config/mcap_precheck.yaml.bak` 为准**，逐字段说明每个配置项的含义、取值与生效方式。
> 两者不一致时改 YAML，不改本文件；改了 YAML 请同步更新这里。
>
> 实现位置：`src/mcap_precheck/config.py`（加载与校验）、`src/mcap_precheck/metrics/`
> （指标计算与阈值解析）、`src/mcap_precheck/verdict.py`（状态码）。

---
# 概览
**初次拉取代码, config下只有mcap_precheck.yaml.bak文件, 调用接口时, 会自动拷贝一份mcap_precheck.yaml到config下, 并重命名为mcap_precheck.yaml;**

配置使用接口: rysen_apis.api_mcap_precheck
  默认调用config/mcap_precheck.yaml;
  如果用户需要自定义配置, 可以传入config_path字段的值, 指定配置文件路径, 此文件需要拷贝自.bak;

文档架构
- 0. 读法与状态码
- 1. 顶层结构
- 2. 通用机制
- 3. 逐段字段说明


## 0. 读法与状态码

### 0.1 状态码

| 码 | 名称 | 触发 |
|---|---|---|
| 0 | `OK` | 全部启用指标在阈值内 |
| 10 | `ACCEPTED_WITH_WARNING` | 仅 `warn` 级指标越界，仍可用 |
| 20 | `REJECTED_METRIC` | 任一 `error` 级指标越界 |
| 30 | `INCOMPLETE_STRUCTURE` | 缺 summary / 缺必需 topic |
| 40 | `CORRUPTED` | 打不开、header 不可读、文件被截断 |
| 50 | `EMPTY` | 消息数为 0 |
| 60 | `INVALID_INPUT` | 输入非法、配置非法、或不可 seek 却要求结构类检查 |
| 90 | `INTERNAL_ERROR` | 未预期异常 |

- `code >= 20` 即拒绝；业务侧请用 `status.is_rejected()`，不要硬编码比较。
- CLI 退出码 == 状态码；`--fail-on-warning` 时 10 按 20 退出（报告里的 `code` 仍是 10）。
- **指标级越界只有两种结果**：`severity: error` → 20；`severity: warn` → 10。
  唯一例外是 ⓪ 完整性（下表），它的指标直接映射到 40 / 30 / 50。

### 0.2 判定顺序（`pipeline._run`）

```
1. 配置非法                        → 60
2. 输入非法 / 流式却要求结构检查     → 60
3. 完整性：readable / not_truncated → 40
4. 完整性：summary / required_topics→ 30
5. 完整性：message_count == 0       → 50
   ↑ fail_fast: true 时到这里就短路返回，不再算 ①~⑥
6. ①~⑥ 任一 error 越界             → 20
7. ①~⑥ 有 warn 越界                → 10（fail_on_warning=true 时为 20）
8. 否则                             → 0
```

---

## 1. 顶层结构

```yaml
version: 1              # 配置版本号；不为 1 直接报 INVALID_INPUT (60)
profiles:               # 命名配置集，运行时用 --profile / profile= 指定
  default: &default     # 锚点，供其它 profile 用 <<: *default 合并
  strict:  {<<: *default, ...}
  smoke:   {<<: *default, ...}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 是 | 当前只支持 `1` |
| `profiles` | 是 | 至少要有 `default`；不存在的 profile → 60 |

### 1.1 profile 继承的两个坑

1. **`<<` 是浅合并**：覆盖某个段落会**整体替换**该段落，不是逐键深合并。
   所以 `strict` / `smoke` 里 `categories`、`global_metrics`、`topic_metrics` 都写全了；
   只写一半会把没写的子键丢掉。
2. **被顶掉的段落要用锚点引用**：`strict.topic_metrics.topics: *topic_overrides`
   就是为了不被 `strict` 里自己的 `topics` 覆盖掉 `default` 的逐 topic 配置。

### 1.2 三级启停

| 层级 | 手段 | 例子 |
|---|---|---|
| 粗 | `categories.<name>.enabled` | `camera: {enabled: false}` |
| 中 | 该类的作用对象为空 → 整类跳过（控制台打印跳过原因） | `camera_metrics.topics: []` |
| 细 | 某项不给阈值 → 该指标 `SKIPPED`（不判失败） | 删掉 `thresholds.duration_s` |

运行时还可用 `inspect(..., categories=[...])` 或 CLI `--only` / `--skip` 覆盖。
`integrity` 恒启用，不参与 `categories` 开关。

---

## 2. 通用机制

### 2.1 阈值 spec

```yaml
<指标名>: {min: 25, max: 35, severity: error}
```

| 算子 | 越界条件 | 说明 |
|---|---|---|
| `min` | `value < min` | 下界 |
| `max` | `value > max` | 上界 |
| `equals` | `value != equals` | 相等判定（`eq` 亦可） |

- `min` + `max` 同时给 → 区间；三者都不给 → 该指标 `SKIPPED`，**不判失败**（不是 0 分被拒）。
- `severity`：`error`（越界 → 20）/ `warn`（越界 → 10）/ `info`。缺省 `error`。
- 实际生效的 spec 会回显在 `MetricResult.threshold` 与日志里，便于复盘。

### 2.2 分层解析：段落 → group → 条目

② topic / ③ camera / ⑤ joint / ⑥ cmd_state 都是「一个类别作用于多个对象」，共用同一套骨架
（见 `config.LAYERED_SECTIONS`）：

```yaml
<类别>:
  thresholds: {指标: spec}        # ① 段落级兜底
  defaults:   {...}               # 非阈值的类别参数（如关节限位）
  groups:                         # ② 可复用的命名片段
    <name>: {defaults: {...}, thresholds: {...}, joints: [...]}
  <条目键>:                        # ③ 作用对象（topic 映射或列表）
    <条目>: {group: <name>, thresholds: {指标: spec}}
```

| 类别 | 段落键 | 条目键 |
|---|---|---|
| ② topic | `topic_metrics` | `topics`（topic → spec 映射） |
| ③ camera | `camera_metrics` | `topics`（映射或列表） |
| ⑤ joint | `joint_metrics` | `topics`（列表，每项需 `name`） |
| ⑥ cmd_state | `cmd_state_metrics` | `pairs`（列表，每项可给 `name`） |

- `thresholds` / `defaults`：**按 key 逐项覆盖**（段落 → group → 条目）。
- `joints` 这类"整体生效"的键：**取最具体的一层**（条目 > group > 段落），不做合并。
- ⚠ **`thresholds` 与 `defaults` 的 key 不得重叠**：`defaults` 里出现指标名会直接报错
  （`config._check_defaults_not_metrics`）。关节的结构限速因此叫 `velocity_limit` 而不是 `max_velocity`。

### 2.3 加载期校验（任一条不满足 → `INVALID_INPUT (60)`）

| 规则 | 说明 |
|---|---|
| 未知指标 key | 按类别有白名单（`config.KNOWN_METRICS`），防拼写错误被静默忽略 |
| `min > max` | 报错 |
| 语义上非负的指标给了负值 | 报错（如 `duration_s` / `*_ms` / `*_count` / `*_ratio` / `rate_hz`） |
| `defaults` 里出现指标名 | 报错（见 2.2） |
| 条目 `group` 未定义于 `groups` | 报错（否则限位/阈值被静默忽略） |
| **跨字段**：`topic_metrics.*.max_interval_ms` 不得宽于 `global_metrics.max_frame_interval_ms` | 报错。软阈值比硬上限还宽会让硬上限静默失效 |
| `global_metrics.message_count.min` 必须 > 0、`expected_rate_hz` 必须 > 0 | 报错 |
| profile 不存在 / YAML 非法 / `version` 不匹配 | 报错 |

### 2.4 作用对象缺失 → `SKIPPED`

topic / joint / cmd_state 里配置的 topic 若文件里没有（子集录制，如只有左手），
对应指标一律 `SKIPPED`，**不会**拿阈值去比 `message_count = 0` 而误杀。

### 2.5 逼近阈值预警（不影响判定码）

通过但离阈值很近时记 WARNING，明细日志里带 `detail.near_limit`，汇总里进
`near_limit_metrics`（**不是** `warn_metrics`）。判据（`rules.Threshold.near_limit`，`warn_ratio` 默认 0.8）：

- 双侧区间 `[min,max]`：按**区间宽度**——`[90,110]` 里的 100 不算逼近；
- 单侧阈值：按 bound 的比例——100 ms 阈值实测 85 ms（0.85 ≥ 0.8）触发。

---

## 3. 逐段字段说明

### 3.1 `categories` —— 类别总开关

```yaml
categories:
  global:    {enabled: true}
  topic:     {enabled: true}
  camera:    {enabled: true}     # 需解码，慢
  numeric:   {enabled: true}
  joint:     {enabled: true}
  cmd_state: {enabled: true}
```

| 键 | 说明 |
|---|---|
| `global` | ① 全局时间戳/丢帧统计 |
| `topic` | ② 逐 topic 频率与间隔 |
| `camera` | ③ 相机（**需解码**，`camera_metrics.topics` 非空才真跑） |
| `numeric` | ④ NaN / Inf |
| `joint` | ⑤ 关节限位与速度（`joint_metrics.topics` 非空且有阈值才跑） |
| `cmd_state` | ⑥ 命令-状态偏差（`cmd_state_metrics.pairs` 非空才跑） |

`integrity`（⓪）恒启用，不写在这里。

---

### 3.2 `integrity` —— ⓪ 完整性（恒启用）

```yaml
integrity:
  required_topics: []      # 留空 = 不校验具体 topic
  require_summary: true
```

> **这一段的键分两类，不要混放**：
>
> | 位置 | 放什么 | 例子 |
> |---|---|---|
> | 段落根 | **判据输入**（给指标提供"查什么"） | `required_topics`、`require_summary` |
> | `thresholds` | **判定线**（指标达到什么值算越界、越界多严重） | `readable`、`not_truncated`、… |
>
> 之所以不把指标平铺到段落根：`required_topics` 既是"要查的 topic 列表"（输入）
> 又是"缺失个数"（指标），平铺会抢同一个键名。这与 `global_metrics` 用 `thresholds` 保持一致，
> 后续新增完整性检查项也照此办理——输入放根，判定线放 `thresholds`。

```yaml
integrity:
  required_topics: []          # 输入：必须存在的 topic
  require_summary: true        # 输入：是否要求 summary
  thresholds:                  # 判定线：下列值即内置缺省（config.INTEGRITY_DEFAULT_THRESHOLDS）
    readable:        {equals: 1, severity: error}   # 可打开且 header 可读  → 40
    not_truncated:   {equals: 1, severity: error}   # 文件尾 magic 完整     → 40
    message_count:   {min: 1,    severity: error}   # 消息数 > 0            → 50
    required_topics: {max: 0,    severity: error}   # 缺失的必需 topic 个数 → 30
    # summary_present 的 severity 默认跟随 require_summary（true → error/30；false → warn/10），
    # 想固定住就显式写（会覆盖上面这条规则）：
    # summary_present: {equals: 1, severity: error}
```

> `summary_present` 是唯一**默认不写在 thresholds 里**的项：它的 severity 与输入键
> `require_summary` 绑定，写死会让 `require_summary: false` 失效（这就是它俩必须二选一的原因）。
> 优先级：`thresholds.summary_present` 显式配置 > `require_summary` 推导。

**输入字段**

| 字段 | 默认 | 说明 |
|---|---|---|
| `required_topics` | `[]` | 必须存在的 topic 列表；缺任一个 → `required_topics` 指标计缺失个数 → **30** |
| `require_summary` | `true` | 是否要求 summary。**未显式配置 `thresholds.summary_present` 时**，用它决定该指标的 severity：`true` → error（**30**）；`false` → warn（**10**）。显式写了则以 `thresholds` 为准。另有第二个作用：不可 seek 的输入 + `require_summary: true` → `INVALID_INPUT (60)`（见 §0.2 第 2 步） |

**指标（判定线在 `thresholds` 下，可改）**

| 指标 | 算法 | 默认阈值 | 越界状态码 |
|---|---|---|---|
| `readable` | 能打开且 header 可读 → 1 | `equals: 1` error | **40** |
| `not_truncated` | 文件尾 8 字节是 MCAP magic、迭代无异常 → 1；不可 seek 时 `None`（SKIPPED） | `equals: 1` error | **40** |
| `summary_present` | summary 与 statistics 均存在 → 1 | `equals: 1` error（severity 可随 `require_summary` 变） | **30** / 10 |
| `message_count` | 消息总数 > 0 | `min: 1` error | **50** |
| `required_topics` | `required_topics` 里缺失的个数 | `max: 0` error | **30** |

> ⚠ **降为 warn 的后果**：完整性指标不再是 `FAILED` → `outcome.status` 为 `None`
> → **`fail_fast` 不再短路**，带病数据会继续跑 ①~⑥（同样是残缺数据上的残缺结果）。
> 实测：截断文件默认 40；把 `not_truncated` / `summary_present` / `message_count` 全降为 warn 后，
> 七个类别全部执行，最终 20。除非明确要"看看到底坏在哪"，否则别动这几项。

> ✅ 白名单校验：`integrity` 段只接受 `required_topics` / `require_summary` / `thresholds`，
> 写其它键（含拼错）直接报 `INVALID_INPUT (60)`。此前这里是**静默忽略**的——
> 写了 `integrity.thresholds` 之外的键不会报错也不生效，已修。

---

### 3.3 `time_source` —— 时间源（分层）

```yaml
time_source:
  default: log_time
  per_category: {camera: header_stamp, joint: log_time}
  per_topic: {}
```

| 字段 | 说明 |
|---|---|
| `default` | 兜底时间源 |
| `per_category` | 按类别覆盖；当前支持 `camera` / `joint` 两个键 |
| `per_topic` | 最高优先级，按 topic 名精确覆盖 |

取值：`log_time`（MCAP 记录器时钟，写入顺序，天然单调）或 `header_stamp`
（消息自带 `header.stamp`，别名 `publish_time` / `header` / `ros_header_stamp`）。

优先级：**`per_topic` > `per_category` > `default`**。
② topic 的时间源由 topic 归属决定：名字命中 `camera_metrics.topics` → 按 `camera` 取；
命中 `joint_metrics.topics[].name` → 按 `joint` 取；否则按 `default`。

> 跨 topic 混合序列只有 `log_time` 有单调性；① 全局的回退/间隔统计必须用 `log_time`。
> 相机内容的时间真实性看 `header_stamp`。

---

### 3.4 `global_metrics` —— ① 全局

```yaml
global_metrics:
  expected_rate_hz: 30.0
  drop_detect: {mode: interval, factor: 1.5}
  thresholds:
    duration_s:                {min: 5.0,   severity: error}
    timestamp_rollback_count:  {max: 0,     severity: error}
    timestamp_rollback_max_ns: {max: 0,     severity: error}
    max_frame_interval_ms:     {max: 300.0, severity: error}
    dropped_frame_count:       {max: 0,     severity: error}
    message_count:             {min: 20,    severity: error}
```

**非阈值字段**

| 字段 | 默认 | 说明 |
|---|---|---|
| `expected_rate_hz` | `30.0` | 期望帧率，仅用于 `dropped_frame_count`；**缺省则该指标 SKIPPED** |
| `drop_detect.mode` | `interval` | `interval`：逐间隔判定，能定位丢帧位置；`expected_count`：只算总量 |
| `drop_detect.factor` | `1.5` | `interval` 模式下，间隔 `g > factor × (1/rate)` 才计入丢帧 |

**指标**

| 指标 key | 单位 | 算法 | 当前阈值 (default) | 越界状态码 |
|---|---|---|---|---|
| `duration_s` | s | `(t_max - t_min) / 1e9`（跨 topic 合并后排序） | `min: 5.0`，error | **20** |
| `timestamp_rollback_count` | count | 文件顺序下 `t[i] < t[i-1]` 的次数 | `max: 0`，error | **20** |
| `timestamp_rollback_max_ns` | ns | 最大单次回退幅度 | `max: 0`，error | **20** |
| `max_frame_interval_ms` | ms | `max(diff(t))`（硬上限，见 2.3 跨字段校验） | `max: 300.0`，error | **20** |
| `dropped_frame_count` | count | `interval`：`Σ(round(g/T) - 1)`；`expected_count`：`max(0, round(duration×rate) - n)` | `max: 0`，error | **20** |
| `message_count` | count | 消息总数 | `min: 20`，error | **20** |

> 回退检测用**文件顺序**（排序后回退恒为 0）；间隔/丢帧用排序后的序列。

---

### 3.5 `topic_metrics` —— ② Topic

```yaml
topic_metrics:
  catch_all: true
  check_stamp_rollback: false
  thresholds: {rate_hz: {...}, max_interval_ms: {...}, message_count: {...}}
  groups: {head_camera: {thresholds: {...}}, ...}
  topics:
    "/tianji/joint_states": {group: robot_state, thresholds: {rate_hz: {...}}}
```

**非阈值字段**

| 字段 | 默认 | 说明 |
|---|---|---|
| `catch_all` | `true` | `true` 检查文件里所有 topic；`false` 只检查 `topics` 里显式列出的 |
| `check_stamp_rollback` | `false` | 单 topic 内按 `header_stamp` 检查回退（多设备未同步时会有假阳性，故默认关） |

**指标**

| 指标 key | 单位 | 算法 | 当前阈值 (default 段落级) | 越界状态码 |
|---|---|---|---|---|
| `rate_hz` | Hz | `(n - 1) / duration` | `min: 25, max: 35`，error | **20** |
| `max_interval_ms` | ms | `max(diff(t))` | `max: 300.0`，error | **20** |
| `message_count` | count | 该 topic 消息条数 | `min: 20`，error | **20** |
| `stamp_rollback_count` | count | 单 topic 内 `header_stamp` 递减次数 | 内置兜底 `{max: 0, warn}` | **10** |

> ⚠ `stamp_rollback_count` 目前**不可在 YAML 里配置**：它不在 `KNOWN_METRICS["topic"]`
> 白名单内，写进 `thresholds` 会被校验拒绝。开启 `check_stamp_rollback` 后固定用内置兜底。

**`groups`（按数据类别细分，数值来自 convert 脚本）**

| group | 覆盖 | 当前值 |
|---|---|---|
| `head_camera` | `max_interval_ms` | `max: 50.0`，error（← `--max-head-age-ms`） |
| `wrist_camera` | `max_interval_ms` | `max: 70.0`，error（← `--max-wrist-age-ms`） |
| `robot_state` | `max_interval_ms` | `max: 30.0`，error（← `--max-state-age-ms`） |
| `command` | `max_interval_ms` | `max: 50.0`，error（← `--max-command-age-ms`） |

**`topics`（逐 topic：归类 + 覆盖）**

| topic | group | 额外覆盖 |
|---|---|---|
| `/tianji/joint_states` | `robot_state` | `rate_hz: {min: 90, max: 110}` |
| `/teleoperation/joint_command` | `command` | `rate_hz: {min: 90, max: 110}` |
| `/recording/apexhand/left/joint_states` | `robot_state` | `rate_hz: {min: 90, max: 110}` |
| `/recording/apexhand/right/joint_states` | `robot_state` | `rate_hz: {min: 90, max: 110}` |
| `/rysen/apexhand/ip_192_168_0_102/move_j_position_follow_command` | `command` | `rate_hz: {min: 55, max: 80}`，**warn** |
| `/rysen/apexhand/ip_192_168_0_103/move_j_position_follow_command` | `command` | `rate_hz: {min: 55, max: 80}`，**warn** |
| `/quad_tile/compressed` | `wrist_camera` | — |
| `/camera/camera/color/image_raw` | `head_camera` | — |
| `/camera/camera/aligned_depth_to_color/image_raw` | `head_camera` | — |

未列出的 topic 走段落级兜底（`rate_hz` 25~35）。

---

### 3.6 `camera_metrics` —— ③ 相机（需解码）

```yaml
camera_metrics:
  topics: ["/quad_tile/compressed", "/camera/camera/color/image_raw"]
  backend: av
  max_frames: null
  sample_ratio: 1.0
  blur:      {resize_to: [224, 224], min_laplacian_var: 100.0}
  black:     {max_mean_luma: 10.0, max_std_luma: 5.0}
  duplicate: {method: pixel_diff, pixel_diff_eps: 1.0}
  stutter:   {interval_factor: 2.0}
  groups: {}
  thresholds: {...}
```

**非阈值字段**

| 字段 | 当前值 | 说明 |
|---|---|---|
| `topics` | 2 个 topic | 要解码检查的 topic；支持 `{topic: spec}` 映射或字符串列表。为空 → 整类跳过 |
| `backend` | `av` | `av`：h264/h265 走 PyAV，默认 numpy 做灰度/resize/拉普拉斯；`opencv` 时启用 cv2 加速 |
| `max_frames` | `null` | 每个 topic 最多解多少帧；`null` = 全解（冒烟用） |
| `sample_ratio` | `1.0` | 抽样比例，`0.1` = 每 10 帧取 1（**生产建议 0.1**，全解 GB 级文件是分钟级） |
| `blur.resize_to` | `[224, 224]` | 拉普拉斯方差与分辨率强相关，检测前统一 resize，保证阈值可移植 |
| `blur.min_laplacian_var` | `100.0` | 低于此值判为模糊（待标定） |
| `black.max_mean_luma` | `10.0` | 灰度均值上限 |
| `black.max_std_luma` | `5.0` | 灰度标准差上限；**与均值双条件同时满足才判黑屏**（防漏过曝、防误判纯色背景） |
| `duplicate.method` | `pixel_diff` | `pixel_diff`：相邻帧 `mean(|I_t - I_{t-1}|) < eps`；`hash`：md5 比较（仅无损/逐帧 JPEG 可靠） |
| `duplicate.pixel_diff_eps` | `1.0` | 像素差阈值 |
| `stutter.interval_factor` | `2.0` | 帧间隔 `g > factor × median(intervals)` 记一次卡顿 |
| `groups` | 空 | 与 ②⑤⑥ 同构；`topics.<t>.group` 可引用（当前未启用） |

**指标**（前 7 项逐 topic 产出，后 2 项跨相机产出）

| 指标 key | 单位 | 算法 | 当前阈值 (default) | 越界状态码 |
|---|---|---|---|---|
| `decode_failure_count` | count | 解码抛异常或返回空帧的条数 | `max: 0`，error | **20** |
| `duplicate_ratio` | ratio | 重复帧数 / (分析帧数 - 1) | `max: 0.02`，error | **20** |
| `black_ratio` | ratio | 黑屏帧数 / 分析帧数 | `max: 0.01`，error | **20** |
| `blur_ratio` | ratio | 模糊帧数 / 分析帧数 | `max: 0.05`，**warn** | **10** |
| `stutter_count` | count | 间隔 > `factor × 中位数` 的次数 | `max: 5`，error | **20** |
| `log_minus_header_p95_ms` | ms | `log_time - header_stamp` 的 P95（时钟偏移） | `min: -100, max: 100`，warn | **10** |
| `log_minus_header_drift_ms` | ms | 上述偏移的极差（漂移） | `max: 50.0`，warn | **10** |
| `skew_p95_ms` | ms | 相机间采集时间戳偏差的 P95（无需解码） | `max: 50.0`，error | **20** |
| `skew_max_ms` | ms | 同上取最大 | `max: 100.0`，warn | **10** |

> `skew_*` 只有 ≥ 2 个相机 topic 且有 `header_stamp` 时才产出。
> 相机卡顿是**相对中位数**的自适应判定，与 ① 的绝对阈值 `max_frame_interval_ms` 互补，可同时启用。

> ⚠ 待清理：当前 YAML 的 `camera_metrics` 段里 `topics` 出现了两次（第 84 行与第 104 行），
> PyYAML 保留后者。两处内容相同，行为无差异，但建议删掉重复的一处。

---

### 3.7 `numeric` —— ④ 非法数值

```yaml
numeric:
  scan_fields: []                          # 留空 = 扫描所有数值字段
  nan_inf_count: {max: 0, severity: error}
```

| 字段 | 当前值 | 说明 |
|---|---|---|
| `scan_fields` | `[]` | `{topic, field}` 列表；**留空 = 扫所有数值字段**（会解码全部消息，大文件较慢） |
| `nan_inf_count` | `{max: 0, error}` | 阈值。⚠ **例外**：`numeric` 没有 `thresholds` 子段，阈值直接写在类别根节点 |

| 指标 key | 单位 | 算法 | 当前阈值 | 越界状态码 |
|---|---|---|---|---|
| `nan_inf_count` | count | `isnan(v) | isinf(v)` 的计数；按 topic/字段分别统计并汇总一条总计 | `max: 0`，error | **20** |

字段选择器支持点号路径 + 切片：`position`、`data[0:16]`、`joints[3].position`、`header.stamp.sec`。
自动扫描时会跳过超大的 uint8 数组（图像像素）。

---

### 3.8 `joint_metrics` —— ⑤ 关节（ApexHand 手 21 DOF + Tianji 臂 14 DOF）

```yaml
joint_metrics:
  topics:
    - {name: "/recording/apexhand/left/joint_states",  position_field: position, velocity_field: velocity, group: apexhand}
    - {name: "/recording/apexhand/right/joint_states", position_field: position, velocity_field: velocity, group: apexhand}
    - {name: "/tianji/joint_states",                   position_field: position, velocity_field: velocity, group: tianji_arm}
  thresholds: {out_of_range_count: {max: 0, severity: error}}
  groups:
    apexhand:   {defaults: {...}, thresholds: {...}, joints: [...]}
    tianji_arm: {defaults: {...}, thresholds: {...}, joints: [...]}
```

**`topics[*]` 字段**

| 字段 | 当前值 | 说明 |
|---|---|---|
| `name` | 三个 topic | 关节状态 topic |
| `position_field` | `position` | 位置字段名 |
| `velocity_field` | `velocity` | 速度字段名；**不填则从 position 差分**求速度 |
| `group` | `apexhand` / `tianji_arm` | 引用 `groups.<name>` 复用限位与阈值（左右手、左右臂各自共用一份） |
| `time_source` | 未配置 | 可选，缺省按 `time_source` 分层解析的 `joint` 类别值（`log_time`） |

**`groups.<name>.defaults`（非阈值参数，兜底）**

| 字段 | apexhand | tianji_arm | 说明 |
|---|---|---|---|
| `lower` | `-3.14` | `-3.1067` | 未在 `joints` 里列出的关节的限位下界 |
| `upper` | `3.14` | `3.1067` | 限位上界 |
| `velocity_limit` | `8.7266` | `null` | **结构限速**（URDF/MJCF 物理上限，非判定线）。非空时覆盖 `thresholds.max_velocity.max`；`null` = 无结构限速 |

**`groups.<name>.joints[*]`（逐关节，与数组下标一一对应）**

| 字段 | 说明 |
|---|---|
| `index` | 位置数组下标 |
| `name` | 关节名，用作日志/报告的 subject（`joint[thumb_j0]`）；未列出的 index 显示 `idx<index>` |
| `lower` / `upper` | 该关节限位（rad）；缺省用 `defaults` |
| `velocity_limit` | 该关节结构限速（rad/s）；缺省用 `defaults` |

- apexhand：21 项（thumb 5 + index/middle/ring/pinky 各 4），来源 URDF。
- tianji_arm：14 项（左臂 `Joint1_L..Joint7_L` = 0-6，右臂 `Joint1_R..Joint7_R` = 7-13），
  来源 MJCF（`marvin_L.xml` / `marvin_R.xml`，左右臂一致）。注意 `Joint4_*` 是单侧区间
  `-2.5307 ~ -0.1745`，不含 0。

**指标**（逐 topic、逐关节产出）

| 指标 key | 单位 | 算法 | 当前阈值 | 越界状态码 |
|---|---|---|---|---|
| `out_of_range_count` | count | 逐帧 `q < lower` 或 `q > upper` 的计数（段落级：`max: 0`，error） | error | **20** |
| `max_velocity` | rad/s | 有 `velocity_field` 取其绝对值最大；否则 `|Δq| / Δt` 的最大值 | apexhand `max: 8.7266` **warn**；tianji_arm `max: 3.0` **warn** | **10** |
| `velocity_p95` | rad/s | 同上取 95 分位（抑制单帧噪声误杀） | apexhand `max: 3.0` error；tianji_arm `max: 2.0` error | **20** |

速度计算要点：`Δt` 优先用消息时间戳差，缺失时退化为 `1 / expected_rate_hz`；
差分前先按时间排序；`Δt == 0` 的帧跳过，避免除零污染 `max_velocity`。

---

### 3.9 `cmd_state_metrics` —— ⑥ 命令-状态偏差

```yaml
cmd_state_metrics:
  pairs:
    - name: tianji_arm
      command: {topic: "/teleoperation/joint_command", field: position}
      state:   {topic: "/tianji/joint_states", field: position}
      match: nearest
      max_time_delta_ms: 50.0
      thresholds: {...}
  thresholds: {...}          # 段落级兜底
```

**`pairs[*]` 字段**

| 字段 | 当前值 | 说明 |
|---|---|---|
| `name` | `tianji_arm` / `apexhand_left` / `apexhand_right` | 配对名，用作 subject |
| `command.topic` / `command.field` | 见上 | 指令侧 topic 与字段（缺省字段 `position`） |
| `state.topic` / `state.field` | 见上 | 状态侧 topic 与字段 |
| `match` | `nearest` | `nearest`：时间戳最近邻（`np.searchsorted` 向量化）；`exact`：时间戳精确相等 |
| `max_time_delta_ms` | `50.0` | 最近邻时差超过此值记为**未匹配**（与 `--max-command-age-ms` 同量级） |
| `thresholds` | 逐 pair | 覆盖段落级兜底 |

**指标**（逐 pair 产出，单位 rad）

| 指标 key | 算法 | 当前阈值 | 越界状态码 |
|---|---|---|---|
| `first_command_minus_state_p95_abs` | 每条 command 与其最近邻 state 之差（逐维）展平后取 P95 绝对值 | 臂 `max: 0.05` error / 左手 `max: 0.25` warn / 右手 `max: 1.0` warn | **20** / **10** |
| `first_command_minus_state_max_abs` | 同上取最大值 | 臂 `max: 0.2` error / 左手 `max: 1.5` warn / 右手 `max: 2.5` warn | **20** / **10** |
| `adjacent_command_change_max_abs` | 相邻 command 变化（`diff`）的绝对值最大 | `max: 0.5`，warn | **10** |
| `unmatched_ratio` | 未匹配 command 数 / command 总数 | `max: 0.01`，warn | **10** |

段落级兜底（pair 未列某项时沿用）：`0.05` error / `0.2` error / `0.5` warn / `0.01` warn。

> ⚠ 命名与实现的差异：`first_command_minus_state_*` 实际对**每一条** command 求差后展平取
> P95 / max，并非只取首条 command（名字沿用自 convert 脚本的 `_action_quality()`）。
> 因此单关节异常会被整体分布稀释，定位具体关节需另算。
>
> 匹配本身按时间戳最近邻，两侧频率不同（如手命令 68 Hz / 状态 100 Hz）不影响对齐；
> `unmatched_ratio` 才是频率敏感项。

---

### 3.10 判定

| 字段 | 当前值 | 说明 |
|---|---|---|
| `fail_fast` | `true` | 完整性（40/30/50）失败即短路返回，不再算 ①~⑥ |
| `fail_on_warning` | `false` | `true` 时 warn 越界也判拒绝（`_decide` 直接返回 20） |

### 3.11 `logging`

```yaml
logging:
  level: INFO
  dir: logs/mcap_precheck
  jsonl: true
  console: true
  rotation: {by: size, max_bytes: 52428800, backup_count: 10}
  summary_file: summary.jsonl
  warn_ratio: 0.8
  redact_paths: false
```

| 字段 | 当前值 | 说明 |
|---|---|---|
| `level` | `INFO` | **只作用于明细通道**；汇总与控制台恒为 INFO（否则调高 level 会把汇总和控制台一起关掉） |
| `dir` | `logs/mcap_precheck` | 日志目录（相对当前工作目录）；`null` = 不落盘 |
| `jsonl` | `true` | 明细 + 汇总两个 JSONL 文件 |
| `console` | `true` | 控制台表格 |
| `rotation.by` | `size` | `size` → `RotatingFileHandler`；`time` → 按天 `TimedRotatingFileHandler` |
| `rotation.max_bytes` | `52428800`（50 MB） | `size` 模式下单文件上限 |
| `rotation.backup_count` | `10` | 保留的历史文件数 |
| `summary_file` | `summary.jsonl` | 汇总文件名（明细文件名固定 `<前缀>-YYYYMMDD.jsonl`，mcap 前缀为 `qc`） |
| `warn_ratio` | `0.8` | 逼近阈值预警比例，见 2.5 |
| `redact_paths` | `false` | `true` 时日志里的 source 只记「父目录 sha256 前 8 位 / 文件名」 |

三个通道：明细（每指标一行 JSON）/ 汇总（每次检测一行）/ 控制台（表格）。
统一走 `QueueHandler` + listener 线程，批量并发安全。
**未传 `logger` 时用 `NullRunLog`，完全不落盘**——要留痕必须显式构造 `LogSink` 传入。

---

## 4. profile 差异

| 段落 | default | strict（入库前终检） | smoke（采集现场初筛） |
|---|---|---|---|
| `categories.topic` | `true` | **`false`** | **`false`** |
| `categories.camera` | `true` | `true` | `false` |
| `categories.joint` / `cmd_state` | `true` | `true` | `false` |
| `topic_metrics.check_stamp_rollback` | `false` | **`true`** | `false` |
| `topic_metrics.thresholds.rate_hz` | `25~35` error | `28~32` error | 同 default |
| `topic_metrics.thresholds.max_interval_ms` | `300` error | `70.0` error（=`max_frame_interval_ms`） | 同 default |
| `global_metrics.drop_detect.mode` | `interval` | `interval` | **`expected_count`** |
| `global_metrics.thresholds.duration_s` | `min 5.0` error | `min 10.0` error | `min 1.0` **warn** |
| `global_metrics.thresholds.timestamp_rollback_count` | `max 0` error | `max 0` error | `max 5` **warn** |
| `global_metrics.thresholds.max_frame_interval_ms` | `max 300` error | `max 70.0` error | `max 1000` **warn** |
| `global_metrics.thresholds.dropped_frame_count` | `max 0` error | `max 0` error | `max 0` **warn** |
| `global_metrics.thresholds.message_count` | `min 20` error | `min 20` error | `min 1` **warn** |
| `topic_metrics.topics` | 9 条归类 + 覆盖 | 引用 `*topic_overrides`（同 default） | 同 default |

> `strict` 把 `max_frame_interval_ms` 压到 70 ms 是有约束的：跨字段校验要求
> topic 的软阈值不得宽于这个硬上限，而 `wrist_camera` 是 70 ms，所以硬上限不能再低。

---

## 5. 指标 → 状态码速查

| 类别 | 指标 | severity（default） | 越界状态码 |
|---|---|---|---|
| ⓪ integrity | `readable` / `not_truncated` | error（`integrity.thresholds` 可改） | **40** |
| ⓪ integrity | `summary_present` / `required_topics` | error（`require_summary: false` 时为 warn） | **30** / 10 |
| ⓪ integrity | `message_count` | error（`integrity.thresholds` 可改） | **50** |
| ① global | `duration_s` | error | **20** |
| ① global | `timestamp_rollback_count` / `timestamp_rollback_max_ns` | error | **20** |
| ① global | `max_frame_interval_ms` | error | **20** |
| ① global | `dropped_frame_count` | error（无 `expected_rate_hz` 时 SKIPPED） | **20** |
| ① global | `message_count` | error | **20** |
| ② topic | `rate_hz` | error（手命令 topic 为 warn） | **20** / 10 |
| ② topic | `max_interval_ms` | error | **20** |
| ② topic | `message_count` | error | **20** |
| ② topic | `stamp_rollback_count` | warn（内置兜底，仅 strict 开启） | **10** |
| ③ camera | `decode_failure_count` / `duplicate_ratio` / `black_ratio` / `stutter_count` / `skew_p95_ms` | error | **20** |
| ③ camera | `blur_ratio` / `skew_max_ms` / `log_minus_header_p95_ms` / `log_minus_header_drift_ms` | warn | **10** |
| ④ numeric | `nan_inf_count` | error | **20** |
| ⑤ joint | `out_of_range_count` | error | **20** |
| ⑤ joint | `max_velocity` | warn | **10** |
| ⑤ joint | `velocity_p95` | error | **20** |
| ⑥ cmd_state | `first_command_minus_state_p95_abs` / `_max_abs` | 臂 error；左右手 warn | **20** / 10 |
| ⑥ cmd_state | `adjacent_command_change_max_abs` / `unmatched_ratio` | warn | **10** |
