"""集成 + 单元测试。无 pytest 时可直接 `python tests/test_precheck.py` 运行。"""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]          # apis/
SRC = ROOT / "src"                                   # src 布局：导入根是 src，不是 apis
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))                        # fixtures.py / rysen_apis.py 在 apis 下

import fixtures  # noqa: E402
from logger import LogSettings, LogSink  # noqa: E402
from mcap_precheck import StatusCode, inspect, inspect_dir  # noqa: E402
from mcap_precheck.config import ConfigError, load_config  # noqa: E402
from mcap_precheck.fields import select  # noqa: E402
from mcap_precheck.metrics.base import resolve_thresholds  # noqa: E402
from mcap_precheck.metrics.joint_metrics import JointMetric  # noqa: E402
from mcap_precheck.rules import Threshold, evaluate  # noqa: E402
from mcap_precheck.metrics.base import MetricStatus  # noqa: E402

import rysen_apis

DEFAULT_CONFIG = rysen_apis.resolve_config_path(config_name="mcap_precheck")


def make_config(directory: Path, name: str, mutate=None) -> Path:
    """基于业务配置派生一份测试配置。

    默认清掉 ``integrity.required_topics``——那是业务 topic，合成 fixture 里没有，
    否则每个用例都会在完整性阶段短路成 INCOMPLETE_STRUCTURE。需要校验它的用例在
    mutate 里自己设。
    """
    raw = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}
    profile = copy.deepcopy(raw["profiles"]["default"])
    profile.setdefault("integrity", {})["required_topics"] = []
    if mutate is not None:
        mutate(profile)
    raw["profiles"][name] = profile
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


# fixture 用例统一用这份「default」配置（已清掉业务 required_topics），
# 与 apis/config/mcap_precheck.yaml 的后续业务改动解耦。
_TEST_CONFIG_DIR = Path(tempfile.gettempdir()) / "mcap_precheck_tests"
TEST_CONFIG = make_config(_TEST_CONFIG_DIR, "default")


def relaxed_duration(profile):
    profile["global_metrics"]["thresholds"]["duration_s"] = {"min": 0.1, "severity": "error"}


def camera_profile(profile):
    relaxed_duration(profile)
    profile["categories"]["camera"] = {"enabled": True}
    profile["camera_metrics"]["topics"] = ["/camera/head"]
    profile["camera_metrics"]["sample_ratio"] = 1.0
    profile["camera_metrics"]["blur"] = {"resize_to": [64, 64], "min_laplacian_var": 100.0}


def joint_profile(profile):
    profile["categories"]["joint"] = {"enabled": True}
    profile["joint_metrics"]["topics"] = [
        {"name": "/joint_states", "position_field": "position", "velocity_field": "velocity"}
    ]
    profile["joint_metrics"]["joints"] = [
        {"index": 0, "name": "j0", "lower": -1.0, "upper": 1.0, "velocity_limit": 10.0},
        {"index": 1, "name": "j1", "lower": -1.0, "upper": 1.0, "velocity_limit": 10.0},
    ]


def joint_groups_profile(profile):
    """手/臂同段：topic 通过 group 引用各自的限位与阈值，验证 index 不再全局冲突。"""
    profile["categories"]["joint"] = {"enabled": True}
    profile["joint_metrics"]["topics"] = [
        {"name": "/hand/joint_states", "position_field": "position", "velocity_field": "velocity", "group": "hand"},
        {"name": "/arm/joint_states", "position_field": "position", "velocity_field": "velocity", "group": "arm"},
    ]
    profile["joint_metrics"]["joints"] = []
    profile["joint_metrics"]["defaults"] = {"lower": -9.0, "upper": 9.0}
    profile["joint_metrics"]["thresholds"] = {"out_of_range_count": {"max": 0, "severity": "error"}}
    profile["joint_metrics"]["groups"] = {
        "hand": {
            "defaults": {"lower": -1.0, "upper": 1.0, "velocity_limit": 10.0},
            "joints": [
                {"index": 0, "name": "hand_j0", "lower": -1.0, "upper": 1.0, "velocity_limit": 10.0}
            ],
            "thresholds": {
                # 故意与 velocity_limit 不一致，用于验证结构限速覆盖阈值 max
                "max_velocity": {"max": 5.0, "severity": "warn"},
                "velocity_p95": {"max": 3.0, "severity": "error"},
            },
        },
        "arm": {
            "defaults": {"lower": -5.0, "upper": 5.0},
            "joints": [{"index": 0, "name": "arm_j0", "lower": -5.0, "upper": 5.0}],
            "thresholds": {
                "max_velocity": {"max": 2.0, "severity": "warn"},
                "velocity_p95": {"max": 2.0, "severity": "error"},
            },
        },
    }


def topic_group_profile(profile):
    """自包含的 topic 分层配置，避免依赖业务 topic（业务 topic 会随录制设备变动）。"""
    profile["categories"]["topic"] = {"enabled": True}
    profile["topic_metrics"]["thresholds"] = {
        "rate_hz": {"min": 25, "max": 35, "severity": "error"},
        "max_interval_ms": {"max": 300.0, "severity": "error"},
        "message_count": {"min": 20, "severity": "error"},
    }
    profile["topic_metrics"]["groups"] = {
        "robot_state": {"thresholds": {"max_interval_ms": {"max": 30.0, "severity": "error"}}},
        "head_camera": {"thresholds": {"max_interval_ms": {"max": 50.0, "severity": "error"}}},
    }
    profile["topic_metrics"]["topics"] = {
        "/state": {
            "group": "robot_state",
            "thresholds": {"rate_hz": {"min": 90, "max": 110, "severity": "error"}},
        },
        "/camera": {"group": "head_camera"},
        "/other": {},
    }


def required_topic_profile(profile):
    profile["integrity"]["required_topics"] = ["/camera/head"]


def cmd_state_profile(profile):
    profile["categories"]["cmd_state"] = {"enabled": True}
    profile["cmd_state_metrics"]["pairs"] = [
        {
            "name": "arm",
            "command": {"topic": "/arm/command", "field": "position"},
            "state": {"topic": "/arm/state", "field": "position"},
            "match": "nearest",
            "max_time_delta_ms": 50.0,
        }
    ]


def test_good_file(tmp: Path) -> None:
    path = fixtures.good_mcap(tmp / "good.mcap")
    report = inspect(path, config=TEST_CONFIG)
    assert report.status is StatusCode.OK, report.to_json()
    assert report.metric_value("message_count") == 300
    assert report.metric_value("duration_s") > 9.9
    assert report.failed_metrics == ()


def test_truncated(tmp: Path) -> None:
    path = fixtures.truncated_mcap(tmp / "truncated.mcap")
    report = inspect(path, config=TEST_CONFIG)
    assert report.status is StatusCode.CORRUPTED, report.to_json()


def test_empty(tmp: Path) -> None:
    path = fixtures.empty_mcap(tmp / "empty.mcap")
    report = inspect(path, config=TEST_CONFIG)
    assert report.status is StatusCode.EMPTY, report.to_json()


def test_rollback(tmp: Path) -> None:
    path = fixtures.rollback_mcap(tmp / "rollback.mcap")
    report = inspect(path, config=TEST_CONFIG)
    assert report.status is StatusCode.REJECTED_METRIC, report.to_json()
    assert report.metric_value("timestamp_rollback_count") >= 1


def test_dropped(tmp: Path) -> None:
    path = fixtures.dropped_mcap(tmp / "dropped.mcap")
    report = inspect(path, config=TEST_CONFIG)
    assert report.status is StatusCode.REJECTED_METRIC, report.to_json()
    assert report.metric_value("dropped_frame_count") >= 1


def test_nan(tmp: Path) -> None:
    path = fixtures.nan_mcap(tmp / "nan.mcap")
    report = inspect(path, config=TEST_CONFIG)
    assert report.status is StatusCode.REJECTED_METRIC, report.to_json()
    assert report.metric_value("nan_inf_count") >= 2


def test_missing_topic(tmp: Path) -> None:
    path = fixtures.good_mcap(tmp / "missing_topic.mcap")
    config = make_config(tmp, "required_topic", required_topic_profile)
    report = inspect(path, config=config, profile="required_topic")
    assert report.status is StatusCode.INCOMPLETE_STRUCTURE, report.to_json()


def test_integrity_thresholds(tmp: Path) -> None:
    """完整性判定线来自 integrity.thresholds；require_summary 仍决定 summary_present 的 severity。"""
    truncated = fixtures.truncated_mcap(tmp / "t.mcap")
    assert inspect(truncated, config=TEST_CONFIG).status is StatusCode.CORRUPTED

    def downgrade(profile):
        # 三项都降为 warn：完整性整体不再产生短路状态，①~⑥ 应当照跑
        profile["integrity"]["thresholds"].update(
            {
                "not_truncated": {"equals": 1, "severity": "warn"},
                "summary_present": {"equals": 1, "severity": "warn"},
                "message_count": {"min": 1, "severity": "warn"},
            }
        )

    config = make_config(tmp, "integrity_warn", downgrade)
    report = inspect(truncated, config=config, profile="integrity_warn")
    assert report.status is not StatusCode.CORRUPTED, report.to_json()
    assert "integrity.not_truncated" in report.warn_metrics, report.to_json()
    assert any(m.category == "global" for m in report.metrics), "降为 warn 后不该再短路"

    # require_summary: false → summary_present 降为 warn；显式配置优先
    def no_summary(profile):
        profile["integrity"]["require_summary"] = False

    config = make_config(tmp, "no_summary", no_summary)
    good = fixtures.good_mcap(tmp / "good.mcap")
    summary = next(
        m for m in inspect(good, config=config, profile="no_summary").metrics
        if m.name == "summary_present"
    )
    assert summary.severity.value == "warn"

    def explicit_error(profile):
        no_summary(profile)
        profile["integrity"]["thresholds"]["summary_present"] = {"equals": 1, "severity": "error"}

    config = make_config(tmp, "explicit_error", explicit_error)
    summary = next(
        m for m in inspect(good, config=config, profile="explicit_error").metrics
        if m.name == "summary_present"
    )
    assert summary.severity.value == "error"

    # 未知键必须报错（此前是静默忽略）
    config = make_config(tmp, "integrity_typo", lambda p: p["integrity"].__setitem__("typo", 1))
    assert inspect(good, config=config, profile="integrity_typo").status is StatusCode.INVALID_INPUT


def test_joint_out_of_range(tmp: Path) -> None:
    path = fixtures.joint_oob_mcap(tmp / "joint_oob.mcap")
    config = make_config(tmp, "joint", joint_profile)
    report = inspect(path, config=config, profile="joint")
    assert report.status is StatusCode.REJECTED_METRIC, report.to_json()
    assert "joint.out_of_range_count[joint[j0]]" in report.failed_metrics

    good = inspect(fixtures.good_mcap(tmp / "good.mcap"), config=config, profile="joint")
    assert good.status is StatusCode.OK, good.to_json()


def test_joint_specs_are_topic_scoped(tmp: Path) -> None:
    """index 0 在手/臂两个 group 里含义不同，必须各自解析、互不覆盖。"""
    config = load_config(make_config(tmp, "joint_groups", joint_groups_profile), "joint_groups")
    metric = JointMetric(config)

    hand = metric._spec_source({"name": "/hand/joint_states", "group": "hand"})
    arm = metric._spec_source({"name": "/arm/joint_states", "group": "arm"})

    assert metric._joint_spec(0, 1, hand)["name"] == "hand_j0"
    assert metric._joint_spec(0, 1, arm)["name"] == "arm_j0"
    assert metric._joint_spec(0, 1, hand)["upper"] == 1.0
    assert metric._joint_spec(0, 1, arm)["upper"] == 5.0
    assert metric._threshold("velocity_p95", metric._joint_spec(0, 1, hand), hand["thresholds"]).maximum == 3.0
    assert metric._threshold("velocity_p95", metric._joint_spec(0, 1, arm), arm["thresholds"]).maximum == 2.0
    # 结构限速（velocity_limit，非指标名）非空时覆盖 max_velocity 指标的 max；为空则沿用阈值
    assert metric._threshold("max_velocity", metric._joint_spec(0, 1, hand), hand["thresholds"]).maximum == 10.0
    assert metric._threshold("max_velocity", metric._joint_spec(0, 1, arm), arm["thresholds"]).maximum == 2.0


def test_joint_unknown_group_rejected(tmp: Path) -> None:
    def bad_group(profile):
        joint_groups_profile(profile)
        profile["joint_metrics"]["topics"][1]["group"] = "typo"

    path = make_config(tmp, "joint_bad_group", bad_group)
    try:
        load_config(path, "joint_bad_group")
    except ConfigError as exc:
        assert "typo" in str(exc)
    else:
        raise AssertionError("unknown joint group should be rejected")


def test_cmd_state_pair_thresholds(tmp: Path) -> None:
    """pair 级 thresholds 按指标覆盖段落级；写错的指标名必须报错而非静默忽略。"""

    def per_pair(profile):
        cmd_state_profile(profile)
        profile["cmd_state_metrics"]["thresholds"] = {
            "unmatched_ratio": {"max": 0.0, "severity": "error"},
            "first_command_minus_state_p95_abs": {"max": 0.05, "severity": "error"},
        }
        profile["cmd_state_metrics"]["pairs"][0]["thresholds"] = {
            "unmatched_ratio": {"max": 0.5, "severity": "warn"}
        }

    config = load_config(make_config(tmp, "cmd_pair", per_pair), "cmd_pair")
    pair = config.section("cmd_state_metrics")["pairs"][0]
    thresholds = resolve_thresholds(config.section("cmd_state_metrics"), pair)
    assert thresholds["unmatched_ratio"] == {"max": 0.5, "severity": "warn"}                    # pair 覆盖
    assert thresholds["first_command_minus_state_p95_abs"] == {"max": 0.05, "severity": "error"}  # 段落级沿用

    def typo(profile):
        cmd_state_profile(profile)
        profile["cmd_state_metrics"]["pairs"][0]["thresholds"] = {"typo_metric": {"max": 1.0}}

    try:
        load_config(make_config(tmp, "cmd_typo", typo), "cmd_typo")
    except ConfigError as exc:
        assert "typo_metric" in str(exc)
    else:
        raise AssertionError("unknown metric in pairs[*].thresholds should be rejected")


def test_topic_group_thresholds(tmp: Path) -> None:
    """② topic 与 ⑤⑥ 共用同一套分层解析：段落 → group → 条目。"""
    config = load_config(make_config(tmp, "topic_group", topic_group_profile), "topic_group")
    section = config.section("topic_metrics")

    state = resolve_thresholds(section, section["topics"]["/state"])
    assert state["max_interval_ms"]["max"] == 30.0   # robot_state group
    assert state["rate_hz"]["max"] == 110            # 条目覆盖
    assert state["message_count"]["min"] == 20       # 段落兜底

    camera = resolve_thresholds(section, section["topics"]["/camera"])
    assert camera["max_interval_ms"]["max"] == 50.0  # head_camera group
    assert camera["rate_hz"]["min"] == 25            # 段落兜底

    other = resolve_thresholds(section, section["topics"]["/other"])
    assert other["max_interval_ms"]["max"] == 300.0  # 无 group，吃段落兜底


def test_single_hand_recording_is_accepted(tmp: Path) -> None:
    """只含左手的录制应通过：缺失的右手/机械臂 topic 记为 SKIPPED，不阻断检测。"""
    path = fixtures.write_mcap(
        tmp / "left_only.mcap",
        fixtures.joint_messages(
            "/recording/apexhand/left/joint_states",
            count=600,
            rate_hz=100.0,
            dims=21,
            transform=lambda index, values: [0.0] * len(values),
        ),
    )
    report = inspect(path, config=DEFAULT_CONFIG, profile="default", categories=["joint"])
    assert report.status is StatusCode.OK, report.to_json()

    skipped = {result.subject for result in report.metrics if result.status is MetricStatus.SKIPPED}
    assert "/recording/apexhand/right/joint_states" in skipped
    assert "/tianji/joint_states" in skipped


def test_camera_black(tmp: Path) -> None:
    path = fixtures.black_frames_mcap(tmp / "black.mcap")
    config = make_config(tmp, "camera", camera_profile)
    report = inspect(path, config=config, profile="camera")
    assert report.status is StatusCode.REJECTED_METRIC, report.to_json()
    assert any("black_ratio" in label for label in report.failed_metrics)


def test_camera_duplicate(tmp: Path) -> None:
    path = fixtures.duplicate_frames_mcap(tmp / "dup.mcap")
    config = make_config(tmp, "camera", camera_profile)
    report = inspect(path, config=config, profile="camera")
    assert any("duplicate_ratio" in label for label in report.failed_metrics), report.to_json()


def test_camera_blur_is_warning(tmp: Path) -> None:
    path = fixtures.blur_frames_mcap(tmp / "blur.mcap")
    config = make_config(tmp, "camera", camera_profile)
    report = inspect(path, config=config, profile="camera")
    assert report.status is StatusCode.ACCEPTED_WITH_WARNING, report.to_json()
    assert any("blur_ratio" in label for label in report.warn_metrics)


def test_camera_good(tmp: Path) -> None:
    path = fixtures.good_camera_mcap(tmp / "camera_good.mcap")
    config = make_config(tmp, "camera", camera_profile)
    report = inspect(path, config=config, profile="camera")
    assert report.status is StatusCode.OK, report.to_json()


def test_nonseekable_is_invalid_input(tmp: Path) -> None:
    data = fixtures.good_mcap(tmp / "stream.mcap").read_bytes()
    stream = fixtures.NonSeekable(data)
    report = inspect(stream, config=TEST_CONFIG)
    assert report.status is StatusCode.INVALID_INPUT, report.to_json()


def test_unknown_profile(tmp: Path) -> None:
    report = inspect(fixtures.good_mcap(tmp / "good.mcap"), config=TEST_CONFIG, profile="nope")
    assert report.status is StatusCode.INVALID_INPUT


def test_cmd_state(tmp: Path) -> None:
    config = make_config(tmp, "cmd_state", cmd_state_profile)
    good = inspect(fixtures.cmd_state_mcap(tmp / "cmd_good.mcap"), config=config, profile="cmd_state")
    assert good.status is StatusCode.OK, good.to_json()
    assert good.metric_value("unmatched_ratio") == 0.0

    bad = inspect(
        fixtures.cmd_state_mcap(tmp / "cmd_bad.mcap", bad=True), config=config, profile="cmd_state"
    )
    assert bad.status is StatusCode.REJECTED_METRIC, bad.to_json()
    assert any("first_command_minus_state_max_abs" in label for label in bad.failed_metrics)


def test_inspect_dir(tmp: Path) -> None:
    directory = tmp / "batch"
    fixtures.good_mcap(directory / "a.mcap")
    fixtures.nan_mcap(directory / "b.mcap")
    reports = inspect_dir(directory, config=TEST_CONFIG, max_workers=2)
    assert len(reports) == 2
    assert sorted(report.status for report in reports) == [StatusCode.OK, StatusCode.REJECTED_METRIC]


def test_categories_override(tmp: Path) -> None:
    path = fixtures.nan_mcap(tmp / "nan.mcap")
    report = inspect(path, config=TEST_CONFIG, categories=["global"])
    assert report.status is StatusCode.OK, report.to_json()
    assert not any(result.category == "numeric" for result in report.metrics)


def test_field_selector() -> None:
    message = SimpleNamespace(
        position=[1.0, 2.0, 3.0],
        joints=[SimpleNamespace(position=4.0), SimpleNamespace(position=5.0)],
        header=SimpleNamespace(stamp=SimpleNamespace(sec=7, nanosec=8)),
    )
    assert list(select(message, "position")) == [1.0, 2.0, 3.0]
    assert select(message, "joints[1].position") == 5.0
    assert select(message, "header.stamp.sec") == 7
    assert list(select(message, "position[0:2]")) == [1.0, 2.0]


def test_threshold_table() -> None:
    threshold = Threshold.from_spec({"min": 1, "max": 3, "severity": "error"})
    cases = [
        (0.9, MetricStatus.FAILED),
        (1.0, MetricStatus.PASSED),
        (3.0, MetricStatus.PASSED),
        (3.1, MetricStatus.FAILED),
        (None, MetricStatus.SKIPPED),
    ]
    for value, expected in cases:
        result = evaluate(threshold, value, category="unit", name="x")
        assert result.status is expected, (value, result.status)
    warn = Threshold.from_spec({"max": 10, "severity": "warn"})
    assert evaluate(warn, 11, category="unit", name="x").status is MetricStatus.WARN
    assert evaluate(None, 1, category="unit", name="x").status is MetricStatus.SKIPPED


def test_near_limit() -> None:
    """区间阈值按区间宽度判逼近；单侧阈值按 bound 比例判逼近。"""
    interval = Threshold.from_spec({"min": 90, "max": 110})
    assert interval.near_limit(100.0, 0.8) is None        # 区间中部不算逼近
    assert interval.near_limit(92.0, 0.8) == ("min", 90.0)
    assert interval.near_limit(108.0, 0.8) == ("max", 110.0)
    assert interval.near_limit(100.0, 0.8) is None

    one_sided = Threshold.from_spec({"max": 100.0})
    assert one_sided.near_limit(85.0, 0.8) == ("max", 100.0)   # 文档示例：85/100 触发预警
    assert one_sided.near_limit(70.0, 0.8) is None
    assert one_sided.near_limit(101.0, 0.8) == ("max", 100.0)  # 已越界，由 violated 处理

    assert Threshold().near_limit(1.0, 0.8) is None

def test_mcap_precheck_api() -> None:
    """遍历真实录制目录，对每个 .mcap 跑一遍 rysen_apis.precheck_mcap。

    logger 为 None 时 pipeline 用 NullLog（不落盘），要留痕必须显式传 LogSink。
    """

    mcap_files_path = ROOT / "data" / "mcap_files"
    files = sorted(mcap_files_path.rglob("*.mcap"))
    if not files:
        print(f"skip: no .mcap found under {mcap_files_path}")
        return

    reports = []
    for file in files:
        print(f"checking {file} ({file.stat().st_size / 1e6:.0f} MB) ...", flush=True)
        report = rysen_apis.api_mcap_precheck(path=file, profile="default", log_level="WARN")
        reports.append(report)
        print(
            f"  -> {report.code} {report.status.name} "
            f"elapsed={report.elapsed_ms / 1000:.1f}s failed={list(report.failed_metrics)}",
            flush=True,
        )

    assert len(reports) == len(files)
    for file, report in zip(files, reports):
        # 真实数据越界（10/20/30…）是正常结论；输入非法与内部异常说明代码或配置有问题
        assert report.status not in (StatusCode.INVALID_INPUT, StatusCode.INTERNAL_ERROR), (
            f"{file}: {report.status.name} errors={report.errors}"
        )




def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        tests = [
            # test_field_selector,
            # test_threshold_table,
            # test_near_limit,
            # lambda: test_good_file(tmp),
            # lambda: test_truncated(tmp),
            # lambda: test_empty(tmp),
            # lambda: test_rollback(tmp),
            # lambda: test_dropped(tmp),
            # lambda: test_nan(tmp),
            # lambda: test_missing_topic(tmp),
            # lambda: test_integrity_thresholds(tmp),
            # lambda: test_joint_out_of_range(tmp),
            # lambda: test_camera_black(tmp),
            # lambda: test_camera_duplicate(tmp),
            # lambda: test_camera_blur_is_warning(tmp),
            # lambda: test_camera_good(tmp),
            # lambda: test_cmd_state(tmp),
            # lambda: test_inspect_dir(tmp),
            # lambda: test_nonseekable_is_invalid_input(tmp),
            # lambda: test_unknown_profile(tmp),
            # lambda: test_categories_override(tmp),
            # lambda: test_joint_specs_are_topic_scoped(tmp),
            # lambda: test_joint_unknown_group_rejected(tmp),
            # lambda: test_single_hand_recording_is_accepted(tmp),
            # lambda: test_cmd_state_pair_thresholds(tmp),
            # lambda: test_topic_group_thresholds(tmp),
            test_mcap_precheck_api,
        ]
        for test in tests:
            try:
                test()
                print(f"PASS {test.__name__ if hasattr(test, '__name__') else test}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {test}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {test}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests)} tests, {failures} failed" if failures else f"\nall {len(tests)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
