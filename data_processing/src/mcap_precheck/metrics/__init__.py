"""6 类指标 + 恒启用的完整性检查。

类别顺序即报告/日志中的展示顺序。
"""

from ..config import QcConfig
from .base import Metric
from .cmd_state_metrics import CmdStateMetric
from .global_metrics import GlobalMetric
from .joint_metrics import JointMetric
from .numeric_metrics import NumericMetric
from .topic_metrics import TopicMetric

# camera 需解码且依赖 av/PIL，延迟导入，避免核心路径 import 失败
METRIC_CLASSES: dict[str, type[Metric]] = {
    "global": GlobalMetric,
    "topic": TopicMetric,
    "numeric": NumericMetric,
    "joint": JointMetric,
    "cmd_state": CmdStateMetric,
}


def build(category: str, config: QcConfig, *, warn_ratio: float = 0.8) -> Metric:
    if category == "camera":
        from .camera_metrics import CameraMetric

        return CameraMetric(config, warn_ratio=warn_ratio)
    metric_class = METRIC_CLASSES.get(category)
    if metric_class is None:
        raise KeyError(f"unknown category {category!r}")
    return metric_class(config, warn_ratio=warn_ratio)


__all__ = [
    "CmdStateMetric",
    "GlobalMetric",
    "JointMetric",
    "METRIC_CLASSES",
    "Metric",
    "NumericMetric",
    "TopicMetric",
    "build",
]
