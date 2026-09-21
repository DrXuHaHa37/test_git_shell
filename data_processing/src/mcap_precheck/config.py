"""配置加载、profile 合并与自洽校验。

校验规则（设计文档 5.7.4）：
  * 未知指标 key → 报错（防拼写错误被静默忽略）；
  * ``min > max`` → 报错；
  * ② Topic 的软阈值不得宽于 ① 全局硬上限 ``max_frame_interval_ms``；
  * ``message_count.min`` 必须 > 0；``expected_rate_hz`` 必须为正。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import yaml

from .verdict import Severity

CONFIG_VERSION = 1
DEFAULT_PROFILE = "default"


def _default_config_path() -> Path:
    """向上逐级找 ``config/mcap_precheck.yaml``。

    兼容两种目录布局：``<root>/mcap_precheck`` 与 src 布局 ``<root>/src/mcap_precheck``
    （配置文件仍在仓库根的 ``config/`` 下，不随包进 src）。
    """
    here = Path(__file__).resolve()
    candidates = [parent / "config" / "mcap_precheck.yaml" for parent in here.parents[:4]]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


DEFAULT_CONFIG_PATH = _default_config_path()

CATEGORIES = (
    "integrity",
    "global",
    "topic",
    "camera",
    "numeric",
    "joint",
    "cmd_state",
)
# integrity 恒启用，不参与 categories 开关
SELECTABLE_CATEGORIES = tuple(name for name in CATEGORIES if name != "integrity")

KNOWN_METRICS: dict[str, frozenset[str]] = {
    # ⓪ 完整性的 5 项指标过去硬编码在 integrity.py 里、YAML 中不可见。
    # 现统一收到 integrity.thresholds 下（缺省值见 INTEGRITY_DEFAULT_THRESHOLDS），
    # 这样"阈值"这个唯一可调的东西全部集中在 YAML，且能吃到加载期校验与日志回显。
    "integrity": frozenset(
        {
            "readable",
            "not_truncated",
            "summary_present",
            "message_count",
            "missing_topics_count",
        }
    ),
    "global": frozenset(
        {
            "duration_s",
            "timestamp_rollback_count",
            "timestamp_rollback_max_ns",
            "max_frame_interval_ms",
            "dropped_frame_count",
            "message_count",
        }
    ),
    "topic": frozenset({"rate_hz", "max_interval_ms", "message_count"}),
    "camera": frozenset(
        {
            "skew_p95_ms",
            "skew_max_ms",
            "stutter_count",
            "duplicate_ratio",
            "black_ratio",
            "blur_ratio",
            "decode_failure_count",
            "log_minus_header_p95_ms",
            "log_minus_header_drift_ms",
        }
    ),
    "numeric": frozenset({"nan_inf_count"}),
    "joint": frozenset({"out_of_range_count", "max_velocity", "velocity_p95"}),
    "cmd_state": frozenset(
        {
            "first_command_minus_state_p95_abs",
            "first_command_minus_state_max_abs",
            "adjacent_command_change_max_abs",
            "unmatched_ratio",
        }
    ),
}

# integrity 段允许出现的键。写其它键（含拼错）一律报错——此前这里是静默忽略的。
INTEGRITY_SECTION_KEYS = frozenset({"required_topics", "require_summary", "thresholds"})

# integrity 指标的缺省阈值。YAML 里 integrity.thresholds 未列某项时用这里的值，
# 两者语义完全一致，改 YAML 即改判定线。
INTEGRITY_DEFAULT_THRESHOLDS: dict[str, dict] = {
    "readable": {"equals": 1, "severity": "error"},
    "not_truncated": {"equals": 1, "severity": "error"},
    "summary_present": {"equals": 1, "severity": "error"},
    "message_count": {"min": 1, "severity": "error"},
    "missing_topics_count": {"max": 0, "severity": "error"},
}

# 「多对象」类别的统一骨架：(类别, 段落键, 条目键)
#
# 这三类都是「一个类别作用于多个对象（topic / pair）」，因此共用同一套分层结构：
#   thresholds  —— 指标阈值，按指标逐项覆盖
#   defaults    —— 非阈值的类别参数（如关节限位），按 key 逐项覆盖
#   groups.<name> —— 可复用的命名配置片段，由条目用 group 引用
#   <条目键>     —— 作用对象列表（或 topic→spec 映射），条目内可再覆盖上面两项
# 解析顺序固定为 段落 → group → 条目，见 metrics/base.py 的 resolve_*。
LAYERED_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("topic", "topic_metrics", "topics"),
    ("camera", "camera_metrics", "topics"),
    ("joint", "joint_metrics", "topics"),
    ("cmd_state", "cmd_state_metrics", "pairs"),
)

# 语义上不可能为负的指标，用于阈值自洽校验
NON_NEGATIVE_METRICS = frozenset(
    {
        "readable",
        "not_truncated",
        "summary_present",
        "duration_s",
        "timestamp_rollback_count",
        "timestamp_rollback_max_ns",
        "max_frame_interval_ms",
        "dropped_frame_count",
        "message_count",
        "rate_hz",
        "max_interval_ms",
        "skew_p95_ms",
        "skew_max_ms",
        "stutter_count",
        "duplicate_ratio",
        "black_ratio",
        "blur_ratio",
        "decode_failure_count",
        "nan_inf_count",
        "out_of_range_count",
        "max_velocity",
        "velocity_p95",
        "first_command_minus_state_p95_abs",
        "first_command_minus_state_max_abs",
        "adjacent_command_change_max_abs",
        "unmatched_ratio",
    }
)


class ConfigError(ValueError):
    """配置文件缺失、profile 不存在或内容非法。"""


class TimeSource(str, Enum):
    LOG_TIME = "log_time"        # MCAP 记录器时钟（写入顺序）
    HEADER_STAMP = "header_stamp"  # 消息自带采集时间戳

    @classmethod
    def parse(cls, value: Any, *, default: "TimeSource | None" = None) -> "TimeSource":
        text = str(value or "").strip().lower()
        aliases = {
            "log_time": cls.LOG_TIME,
            "mcap_log_time_ns": cls.LOG_TIME,
            "log": cls.LOG_TIME,
            "header_stamp": cls.HEADER_STAMP,
            "publish_time": cls.HEADER_STAMP,
            "ros_header_stamp": cls.HEADER_STAMP,
            "header": cls.HEADER_STAMP,
        }
        if text in aliases:
            return aliases[text]
        if default is not None:
            return default
        raise ConfigError(f"unknown time_source {value!r}, expected one of {sorted(aliases)}")


@dataclass(frozen=True)
class QcConfig:
    path: Path | None
    profile: str
    data: dict[str, Any]

    @property
    def warn_ratio(self) -> float:
        value = (self.section("logging") or {}).get("warn_ratio", 0.8)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.8

    @property
    def fail_fast(self) -> bool:
        return bool(self.data.get("fail_fast", True))

    @property
    def fail_on_warning(self) -> bool:
        return bool(self.data.get("fail_on_warning", False))

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def category_enabled(self, category: str) -> bool:
        if category == "integrity":
            return True
        return bool((self.section("categories").get(category) or {}).get("enabled", False))

    def enabled_categories(self, override: Iterable[str] | None = None) -> tuple[str, ...]:
        if override is not None:
            selected = [name for name in override if name in SELECTABLE_CATEGORIES]
            unknown = [name for name in override if name not in SELECTABLE_CATEGORIES]
            if unknown:
                raise ConfigError(
                    f"unknown categories {unknown}, expected a subset of {list(SELECTABLE_CATEGORIES)}"
                )
        else:
            selected = [name for name in SELECTABLE_CATEGORIES if self.category_enabled(name)]
        return ("integrity", *selected)

    def time_source_for(self, *, category: str | None = None, topic: str | None = None) -> TimeSource:
        """按 per_topic > per_category > default 的优先级解析时间源。"""
        block = self.section("time_source")
        per_topic = block.get("per_topic") or {}
        if topic is not None and topic in per_topic:
            return TimeSource.parse(per_topic[topic])
        per_category = block.get("per_category") or {}
        if category is not None and category in per_category:
            return TimeSource.parse(per_category[category])
        return TimeSource.parse(block.get("default"), default=TimeSource.LOG_TIME)


def load_config(
    path: str | Path | None = None,
    profile: str = DEFAULT_PROFILE,
    *,
    validate: bool = True,
) -> QcConfig:
    config_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping: {config_path}")

    version = raw.get("version")
    if version is not None and int(version) != CONFIG_VERSION:
        raise ConfigError(f"unsupported config version {version!r}, expected {CONFIG_VERSION}")

    profiles = raw.get("profiles") or {}
    if profile not in profiles:
        raise ConfigError(f"profile {profile!r} not found in {config_path}; available: {sorted(profiles)}")
    data = profiles[profile]
    if not isinstance(data, dict):
        raise ConfigError(f"profile {profile!r} must be a mapping")

    config = QcConfig(path=config_path, profile=profile, data=data)
    if validate:
        validate_config(config)
    return config


def validate_config(config: QcConfig) -> None:
    _check_integrity_keys(config)
    _check_known_metrics(config)
    _check_thresholds(config)
    _check_group_references(config)
    _check_defaults_not_metrics(config)


def _check_integrity_keys(config: QcConfig) -> None:
    """integrity 段不接受白名单外的键。

    此前写错键（或误以为可在这里配阈值）会被静默忽略，看起来"配了却不生效"。
    """
    section = config.section("integrity")
    unknown = sorted(str(key) for key in section if str(key) not in INTEGRITY_SECTION_KEYS)
    if unknown:
        raise ConfigError(
            f"integrity: unknown keys {unknown}; allowed: {sorted(INTEGRITY_SECTION_KEYS)}"
        )


def integrity_thresholds(config: QcConfig) -> dict[str, dict]:
    """完整性指标的生效阈值：YAML 的 integrity.thresholds 覆盖内置缺省。"""
    merged = {name: dict(spec) for name, spec in INTEGRITY_DEFAULT_THRESHOLDS.items()}
    for name, spec in (config.section("integrity").get("thresholds") or {}).items():
        if isinstance(spec, dict):
            merged[str(name)] = dict(spec)
    return merged


def _check_known_metrics(config: QcConfig) -> None:
    sections = {
        "integrity": integrity_thresholds(config),
        "global": (config.section("global_metrics") or {}).get("thresholds") or {},
        "numeric": {"nan_inf_count": (config.section("numeric") or {}).get("nan_inf_count")},
    }
    for category, thresholds in sections.items():
        known = KNOWN_METRICS[category]
        for key in thresholds:
            if key not in known:
                raise ConfigError(
                    f"unknown metric {key!r} in category {category!r}; known: {sorted(known)}"
                )

    for category, section_key, entries_key in LAYERED_SECTIONS:
        known = KNOWN_METRICS[category]
        for where, name, _spec in _iter_layered_thresholds(config.section(section_key), entries_key):
            if name not in known:
                raise ConfigError(
                    f"unknown metric {name!r} in {section_key}.{where}; known: {sorted(known)}"
                )


def iter_entries(section: dict[str, Any], entries_key: str) -> Iterable[tuple[str, dict[str, Any]]]:
    """产出 ``(条目标签, 条目 spec)``。

    条目可以是 {名字: spec} 映射（``topic_metrics.topics`` / ``camera.topics``），
    也可以是 spec 列表（``joint.topics`` / ``cmd_state.pairs``）；
    列表里若有 ``name`` 就用它当标签，否则用下标。
    """
    entries = section.get(entries_key) or {}
    if isinstance(entries, dict):
        for name, spec in entries.items():
            if isinstance(spec, dict):
                yield str(name), spec
        return
    for index, spec in enumerate(entries):
        if not isinstance(spec, dict):
            yield str(spec), {}
        else:
            yield str(spec.get("name") or index), spec


def _iter_layered_thresholds(
    section: dict[str, Any], entries_key: str
) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """统一骨架下阈值可能出现的所有位置：段落 → groups.* → 条目。"""
    for name, spec in (section.get("thresholds") or {}).items():
        if isinstance(spec, dict):
            yield "thresholds", str(name), spec
    for group, group_spec in (section.get("groups") or {}).items():
        if not isinstance(group_spec, dict):
            continue
        for name, spec in (group_spec.get("thresholds") or {}).items():
            if isinstance(spec, dict):
                yield f"groups.{group}.thresholds", str(name), spec
    for label, entry in iter_entries(section, entries_key):
        for name, spec in (entry.get("thresholds") or {}).items():
            if isinstance(spec, dict):
                yield f"{entries_key}[{label}].thresholds", str(name), spec


def _check_defaults_not_metrics(config: QcConfig) -> None:
    """``defaults`` 只放非阈值参数；出现指标名说明写错了地方（阈值须在 ``thresholds``）。

    例如 ``joint.defaults.max_velocity`` 曾与指标 ``max_velocity`` 同名，
    且会静默覆盖 ``thresholds.max_velocity.max``——现已改名为 ``velocity_limit``，
    这里从机制上堵死复发。
    """
    for category, section_key, entries_key in LAYERED_SECTIONS:
        known = KNOWN_METRICS[category]
        section = config.section(section_key)
        places = [("defaults", section.get("defaults"))]
        for group, group_spec in (section.get("groups") or {}).items():
            if isinstance(group_spec, dict):
                places.append((f"groups.{group}.defaults", group_spec.get("defaults")))
        for label, entry in iter_entries(section, entries_key):
            places.append((f"{entries_key}[{label}].defaults", entry.get("defaults")))
        for place, defaults in places:
            if not isinstance(defaults, dict):
                continue
            for key in defaults:
                if str(key) in known:
                    raise ConfigError(
                        f"{section_key}.{place}.{key} 是指标名，阈值必须写在 thresholds 下；"
                        f"defaults 只放非阈值参数（如 lower / upper / velocity_limit）"
                    )


def _check_group_references(config: QcConfig) -> None:
    """条目引用的 group 必须存在，否则限位/阈值会被静默忽略。"""
    for category, section_key, entries_key in LAYERED_SECTIONS:
        section = config.section(section_key)
        groups = section.get("groups") or {}
        for label, entry in iter_entries(section, entries_key):
            name = entry.get("group")
            if name and str(name) not in groups:
                raise ConfigError(
                    f"{section_key}.{entries_key}[{label}].group={name!r} 未定义于 "
                    f"{section_key}.groups；可用：{sorted(groups)}"
                )


def _check_thresholds(config: QcConfig) -> None:
    for category, thresholds in (
        ("integrity", integrity_thresholds(config)),
        ("global", (config.section("global_metrics") or {}).get("thresholds") or {}),
    ):
        for name, spec in thresholds.items():
            _check_one(category, name, spec)

    for category, section_key, entries_key in LAYERED_SECTIONS:
        for where, name, spec in _iter_layered_thresholds(config.section(section_key), entries_key):
            _check_one(category, name, spec, where=f"{section_key}.{where}.{name}")

    numeric_threshold = (config.section("numeric") or {}).get("nan_inf_count")
    if isinstance(numeric_threshold, dict):
        _check_one("numeric", "nan_inf_count", numeric_threshold)

    _check_cross_field(config)


def _check_one(category: str, name: str, spec: Any, *, where: str | None = None) -> None:
    if not isinstance(spec, dict):
        raise ConfigError(f"threshold {category}.{name} must be a mapping, got {type(spec).__name__}")
    location = where or f"{category}.{name}"
    minimum = _as_float(spec.get("min"))
    maximum = _as_float(spec.get("max"))
    equals = _as_float(spec.get("equals", spec.get("eq")))
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ConfigError(f"{location}: min {minimum} > max {maximum}")
    for bound in (minimum, maximum):
        if bound is not None and bound < 0 and name in NON_NEGATIVE_METRICS:
            raise ConfigError(f"{location}: {name} must be >= 0, got {bound}")
    if equals is not None and equals < 0 and name in NON_NEGATIVE_METRICS:
        raise ConfigError(f"{location}: {name} must be >= 0, got {equals}")
    try:
        Severity.parse(spec.get("severity"), default=Severity.ERROR)
    except ValueError as exc:
        raise ConfigError(f"{location}: {exc}") from exc


def _check_cross_field(config: QcConfig) -> None:
    global_thresholds = (config.section("global_metrics") or {}).get("thresholds") or {}
    hard_limit = _as_float((global_thresholds.get("max_frame_interval_ms") or {}).get("max"))
    topic_section = config.section("topic_metrics")
    for where, name, spec in _iter_layered_thresholds(topic_section, "topics"):
        if name != "max_interval_ms":
            continue
        soft = _as_float(spec.get("max"))
        if hard_limit is not None and soft is not None and soft > hard_limit:
            raise ConfigError(
                f"topic_metrics.{where}.max_interval_ms={soft} is wider than the hard limit "
                f"global_metrics.max_frame_interval_ms={hard_limit}"
            )
    min_messages = _as_float((global_thresholds.get("message_count") or {}).get("min"))
    if min_messages is not None and min_messages <= 0:
        raise ConfigError(f"global_metrics.thresholds.message_count.min must be > 0, got {min_messages}")
    rate = _as_float((config.section("global_metrics") or {}).get("expected_rate_hz"))
    if rate is not None and rate <= 0:
        raise ConfigError(f"global_metrics.expected_rate_hz must be > 0, got {rate}")


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
