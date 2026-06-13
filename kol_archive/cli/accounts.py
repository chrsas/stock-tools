"""Account registration: add a tracked Xueqiu blogger by profile URL or numeric uid."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from kol_archive.accounts import add_account

LOGGER = logging.getLogger(__name__)


def _add_account_command(args: argparse.Namespace) -> None:
    result = add_account(args.config_dir, args.account, note=args.note)
    if result.status == "added":
        print(f"# 已登记博主 uid={result.uid}")
        if result.note:
            print(f"- 备注：{result.note}")
        print("- 下次运行 `python -m kol_archive run-once` 起开始采集（首轮自动回填基线）。")
    else:
        print(f"# 博主 uid={result.uid} 已在追踪列表中，未重复添加。")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "add-account", help="register a tracked blogger by profile URL or numeric uid"
    )
    parser.add_argument("account", help="雪球主页 URL（含 /u/<uid>）或数字 uid")
    parser.add_argument("--note", help="备注（可选）")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.set_defaults(handler=_add_account_command)
