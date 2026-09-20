"""CLI：退出码 == 状态码，可直接在 shell 里判断。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from logger import LogSettings, LogSink

from .config import SELECTABLE_CATEGORIES, DEFAULT_PROFILE, ConfigError, load_config
from .pipeline import LOG_FILE_PREFIX, inspect, inspect_dir
from .report import QcReport
from .verdict import StatusCode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcap_precheck", description="MCAP 文件前置质量检测")
    subparsers = parser.add_subparsers(dest="command", required=True)

    single = subparsers.add_parser("inspect", help="检测单个 MCAP 文件")
    single.add_argument("path", help="MCAP 文件路径")
    _add_common(single)
    single.set_defaults(handler=_run_single)

    batch = subparsers.add_parser("inspect-dir", help="批量检测目录")
    batch.add_argument("directory", help="目录")
    batch.add_argument("--pattern", default="*.mcap", help="文件匹配模式（默认 *.mcap）")
    batch.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    batch.add_argument("--max-workers", type=int, default=4, help="并发数（默认 4）")
    _add_common(batch)
    batch.set_defaults(handler=_run_batch)
    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="配置文件路径（默认包内 config/mcap_precheck.yaml）")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="profile 名（默认 default）")
    parser.add_argument(
        "--only", default=None, help=f"只运行这些类别，逗号分隔：{','.join(SELECTABLE_CATEGORIES)}"
    )
    parser.add_argument("--skip", default=None, help="跳过这些类别，逗号分隔")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--log-dir", default=None, help="日志目录（覆盖配置）")
    parser.add_argument("--log-level", default=None, help="日志级别（覆盖配置）")
    parser.add_argument("--no-console", action="store_true", help="关闭控制台表格输出")
    parser.add_argument("--fail-on-warning", action="store_true", help="warn 级越界也视为拒绝")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        categories = _resolve_categories(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return int(StatusCode.INVALID_INPUT)
    try:
        settings = load_config(args.config, args.profile)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return int(StatusCode.INVALID_INPUT)

    log_settings = LogSettings.from_config(settings.section("logging")).with_overrides(
        level=args.log_level,
        directory=Path(args.log_dir) if args.log_dir else None,
        console=False if (args.no_console or args.json) else None,
        prefix=LOG_FILE_PREFIX,
    )
    try:
        with LogSink(log_settings) as sink:
            reports = args.handler(args, categories=categories, sink=sink)
    except (ConfigError, FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(StatusCode.INVALID_INPUT)

    if args.json:
        print(_as_json(reports))
    return _exit_code(reports, fail_on_warning=args.fail_on_warning or settings.fail_on_warning)


def _run_single(args, *, categories, sink) -> list[QcReport]:
    return [
        inspect(
            args.path,
            config=args.config,
            profile=args.profile,
            categories=categories,
            logger=sink,
        )
    ]


def _run_batch(args, *, categories, sink) -> list[QcReport]:
    return inspect_dir(
        args.directory,
        pattern=args.pattern,
        recursive=not args.no_recursive,
        config=args.config,
        profile=args.profile,
        categories=categories,
        max_workers=args.max_workers,
        logger=sink,
    )


def _resolve_categories(args) -> list[str] | None:
    if args.only:
        return [item.strip() for item in args.only.split(",") if item.strip()]
    if args.skip:
        skipped = {item.strip() for item in args.skip.split(",") if item.strip()}
        return [name for name in SELECTABLE_CATEGORIES if name not in skipped]
    return None


def _as_json(reports: list[QcReport]) -> str:
    if len(reports) == 1:
        return reports[0].to_json()
    return "[" + ",\n ".join(report.to_json(indent=None) for report in reports) + "]"


def _exit_code(reports: list[QcReport], *, fail_on_warning: bool) -> int:
    """退出码 == 状态码；--fail-on-warning 时 ACCEPTED_WITH_WARNING 视为拒绝。"""
    codes = [report.status for report in reports] or [StatusCode.OK]
    worst = max(codes, key=int)
    if fail_on_warning and worst is StatusCode.ACCEPTED_WITH_WARNING:
        return int(StatusCode.REJECTED_METRIC)
    return int(worst)


if __name__ == "__main__":
    raise SystemExit(main())
