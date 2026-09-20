"""⑥ 命令-状态偏差：复用 convert 脚本 ``_action_quality()`` 的三个量。

范数用「逐维绝对值 + 按关节组分组」，不用欧氏距离。

阈值可逐 pair 覆盖：手与臂的跟随特性差异很大，共用一套阈值没有意义
（段落级 ``thresholds`` 作兜底，``pairs[*].thresholds`` 按指标覆盖）。
"""

from __future__ import annotations

import numpy as np

from ..reader import FieldRequest, McapScan, ScanPlan
from .base import Metric, MetricResult, resolve_thresholds, skipped, stack_values

CATEGORY = "cmd_state"


class CmdStateMetric(Metric):
    category = CATEGORY
    section_key = "cmd_state_metrics"

    def is_active(self) -> bool:
        return bool(self.section.get("pairs"))

    def skip_reason(self) -> str | None:
        if self.is_active():
            return None
        return "cmd_state.pairs 为空：需配置 command/state 的 topic 与字段配对"

    def plan(self) -> ScanPlan:
        requests: list[FieldRequest] = []
        for pair in self.section.get("pairs") or ():
            for side in ("command", "state"):
                reference = pair.get(side) or {}
                if reference.get("topic"):
                    requests.append(
                        FieldRequest(
                            topic=str(reference["topic"]),
                            path=str(reference.get("field", "position")),
                            store=True,
                        )
                    )
        return ScanPlan(fields=tuple(requests))

    def run(self, scan: McapScan) -> list[MetricResult]:
        results: list[MetricResult] = []
        for pair in self.section.get("pairs") or ():
            results.extend(self._pair_results(scan, pair))
        return results

    def _pair_results(self, scan: McapScan, pair: dict) -> list[MetricResult]:
        command = pair.get("command") or {}
        state = pair.get("state") or {}
        name = str(pair.get("name") or f"{command.get('topic')}->{state.get('topic')}")
        command_data = self._track(scan, command)
        state_data = self._track(scan, state)
        if command_data is None or state_data is None:
            return [
                skipped(CATEGORY, "unmatched_ratio", subject=name, reason="command/state topic missing")
            ]

        command_times, command_values = command_data
        state_times, state_values = state_data
        dims = min(command_values.shape[1], state_values.shape[1])
        command_values = command_values[:, :dims]
        state_values = state_values[:, :dims]
        if command_values.shape[0] == 0 or state_values.shape[0] == 0:
            return [
                skipped(CATEGORY, "unmatched_ratio", subject=name, reason="no command/state samples")
            ]

        max_delta_ns = float(pair.get("max_time_delta_ms", 50.0)) * 1e6
        match = str(pair.get("match", "nearest")).lower()
        index, delta_ns = _match(command_times, state_times, match)
        matched = delta_ns <= max_delta_ns

        thresholds = resolve_thresholds(self.section, pair)
        diffs = np.abs(command_values[matched] - state_values[index[matched]])
        adjacent = np.abs(np.diff(command_values, axis=0))
        unmatched_ratio = float(np.count_nonzero(~matched) / command_values.shape[0])

        return [
            self.evaluate(
                "first_command_minus_state_p95_abs",
                float(np.percentile(diffs, 95)) if diffs.size else None,
                subject=name,
                unit="rad",
                threshold=self.pick_threshold(thresholds, "first_command_minus_state_p95_abs"),
            ),
            self.evaluate(
                "first_command_minus_state_max_abs",
                float(diffs.max()) if diffs.size else None,
                subject=name,
                unit="rad",
                threshold=self.pick_threshold(thresholds, "first_command_minus_state_max_abs"),
            ),
            self.evaluate(
                "adjacent_command_change_max_abs",
                float(adjacent.max()) if adjacent.size else 0.0,
                subject=name,
                unit="rad",
                threshold=self.pick_threshold(thresholds, "adjacent_command_change_max_abs"),
            ),
            self.evaluate(
                "unmatched_ratio",
                unmatched_ratio,
                subject=name,
                unit="ratio",
                threshold=self.pick_threshold(thresholds, "unmatched_ratio"),
            ),
        ]

    # 阈值解析统一走 base.resolve_thresholds：段落 thresholds → group → 条目 thresholds。

    def _track(self, scan: McapScan, reference: dict) -> tuple[np.ndarray, np.ndarray] | None:
        topic = reference.get("topic")
        if not topic:
            return None
        series = scan.topics.get(str(topic))
        if series is None or series.count == 0:
            return None
        source = self.config.time_source_for(topic=str(topic))
        order = series.order_for(source)
        times = np.asarray(series.time_ns(source), dtype=np.int64)[order]
        rows = scan.values.get((str(topic), str(reference.get("field", "position"))))
        if not rows:
            return None
        values, _ = stack_values([rows[i] for i in order.tolist()])
        if values.size == 0:
            return None
        if values.shape[0] != times.size:
            times = times[: values.shape[0]]  # 长度不一致的行已被丢弃
        return times, values


def _match(
    command_times: np.ndarray, state_times: np.ndarray, mode: str
) -> tuple[np.ndarray, np.ndarray]:
    """把每条 command 配到一条 state，返回 (state 下标, |Δt| ns)。"""
    if mode == "exact":
        lookup = {int(value): position for position, value in enumerate(state_times)}
        index = np.array([lookup.get(int(value), -1) for value in command_times], dtype=np.int64)
        delta = np.array(
            [
                abs(int(command_times[i]) - int(state_times[index[i]])) if index[i] >= 0 else -1
                for i in range(command_times.size)
            ],
            dtype=np.int64,
        )
        return np.where(index >= 0, index, 0), np.where(index >= 0, delta, np.int64(2**62))

    position = np.searchsorted(state_times, command_times)
    right = np.clip(position, 0, state_times.size - 1)
    left = np.clip(position - 1, 0, state_times.size - 1)
    delta_right = np.abs(command_times - state_times[right])
    delta_left = np.abs(command_times - state_times[left])
    choose_left = delta_left < delta_right
    index = np.where(choose_left, left, right)
    return index, np.minimum(delta_left, delta_right)
