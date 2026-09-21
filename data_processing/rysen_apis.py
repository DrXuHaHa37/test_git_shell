"""Rysen 对外接口。

当前只有一个入口：给定 MCAP 文件路径，跑一遍 mcap_precheck 质量检测。

用法::

    from rysen_apis import precheck_mcap

    report = precheck_mcap("data/record_0.mcap", profile="strict")
    if report.status.is_rejected():
        print(report.code, report.failed_metrics)
    payload = report.to_dict()      # 需要 JSON 时
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import shutil
import sys

__all__ = ["StatusCode", "precheck_mcap", "resolve_config_path", "restore_config"]

ROOT = Path(__file__).resolve().parents[0]      # apis/
sys.path.insert(0, str(ROOT / "src"))           # src 布局：导入根是 src

from logger import LogSink, LogSettings  # noqa: E402
from mcap_precheck import QcReport, StatusCode, inspect  # noqa: E402
from mcap_precheck.pipeline import LOG_FILE_PREFIX  # noqa: E402

CONFIG_DIR = ROOT / "config"


def _restore_config(config_name: str = "mcap_precheck") -> Path:
    """从 ``.bak`` 备份恢复 ``{config_name}.yaml``（备份缺失则提示 pull 最新代码）。"""
    default_path = CONFIG_DIR / f"{config_name}.yaml"
    backup_path = CONFIG_DIR / f"{config_name}.yaml.bak"
    if not backup_path.is_file():
        raise ValueError(f"config file not found: {default_path}, please pull newest code")
    shutil.copyfile(backup_path, default_path)
    print(f"[rysen_apis] restored config {default_path} from backup {backup_path}")
    return default_path


def resolve_config_path(config_path: str | Path | None = None, *, config_name: str = "mcap_precheck") -> Path:
    """解析配置文件路径。

    config_path 非空时直接返回；为 None 时回退到 CONFIG_DIR 下的
    {config_name}.yaml，缺失则自动从 .bak 备份恢复，
    两者皆缺才报错提示 pull 最新代码。

    后续新增 api 只需传各自的 config_name。
    """
    if config_path is not None:
        return Path(config_path)

    default_path = CONFIG_DIR / f"{config_name}.yaml"
    if default_path.is_file():
        return default_path
    if (CONFIG_DIR / f"{config_name}.yaml.bak").is_file():
        return _restore_config(config_name)
    raise ValueError(f"config file not found: {default_path}, please pull newest code")


def api_mcap_precheck(
    path: str | Path,
    *,
    profile: str = "default",
    categories: Iterable[str] | None = None,
    config_path: str | Path | None = None,
    log_level: str = "INFO",
    log_dir: str | Path | None = None,
) -> QcReport:
    """对一个 MCAP 文件做前置质量检测。

    Args:
        path: MCAP 文件路径。
        profile: 配置 profile（``default`` / ``strict`` / ``smoke``）。
        categories: 运行时覆盖类别启停，如 ``["global", "numeric"]``；None 用配置。
        config: 配置文件路径；None 时回退到包内 ``config/mcap_precheck.yaml``，
            缺失则自动从 ``.bak`` 备份恢复，两者皆缺才提示 pull 最新代码。
        log_level: 日志级别; 默认 ``INFO``
        log_dir: 日志目录；None 用包内 ``logs``。

    Returns:
        QcReport。不抛业务异常——可预期错误转成状态码写入 ``report.errors``。
        判定：``code == 0`` 可用；``10`` 有 warn；``>= 20`` 拒绝（见 StatusCode）。
    """
    config_path = resolve_config_path(config_path, config_name="mcap_precheck")

    if log_dir is None:
        log_dir = ROOT / "logs"

    with LogSink(
        LogSettings(dir=log_dir, level=log_level, console=True, file_prefix=LOG_FILE_PREFIX)
    ) as sink:
        return inspect(
            path,
            config=config_path,
            profile=profile,
            categories=list(categories) if categories is not None else None,
            logger=sink,
        )
