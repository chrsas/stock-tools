"""Retrospective topic recall commands: deterministic evidence retrieval.

Phase 11. ``recall --no-llm`` is the free, hallucination-free base: it runs the
pure-SQL grouped retrieval and prints the matching versions with coverage and
selection counts. LLM query-expansion and brief synthesis are layered on later;
this command stands alone so recall quality can be verified without a model.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from kol_archive.config import load_config
from kol_archive.recall import RetrievalQuery, TermGroup, retrieve

from .common import (
    configured_db_path,
    connect_existing_archive,
    enrich_prompt_version,
    print_json,
    section,
)


def _parse_group(value: str) -> TermGroup:
    label, _, terms = value.partition("=")
    label = label.strip()
    if not label or not _:
        raise argparse.ArgumentTypeError(f"--group must be 'label=词1,词2' (got: {value!r})")
    parsed = tuple(term.strip() for term in terms.split(",") if term.strip())
    if not parsed:
        raise argparse.ArgumentTypeError(f"--group '{label}' has no terms")
    return TermGroup(label=label, terms=parsed)


def _split_tickers(values: list[str] | None) -> tuple[str, ...]:
    tickers: list[str] = []
    for value in values or []:
        tickers.extend(part.strip().upper() for part in value.split(",") if part.strip())
    return tuple(dict.fromkeys(tickers))


def _recall_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    if not args.group:
        # LLM query-expansion (turning a bare question into groups) lands in the
        # next step; until then a deterministic run needs explicit groups.
        raise SystemExit(
            "本步骤需显式分组检索词:请用 --group event=美伊,伊朗 --group market=油价,原油"
            "（扩词功能将在下一步加入）。"
        )
    query = RetrievalQuery(
        groups=tuple(args.group),
        date_from=args.date_from,
        date_to=args.date_to,
        tickers=_split_tickers(args.ticker),
        require_all_groups=not args.any_group,
        limit=args.limit,
        question=args.question,
    )
    benchmark = str((section(config, "prices")).get("benchmark_ticker") or "SH000300").upper()
    prompt_version = enrich_prompt_version(config, None)
    connection, _ = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        print_json(
            retrieve(
                connection,
                query,
                prompt_version=prompt_version,
                benchmark_ticker=benchmark,
            )
        )
    finally:
        connection.close()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    recall_parser = subparsers.add_parser(
        "recall",
        help="按主题分组词 + 时间窗回溯检索当时发言（确定性，不调用 LLM）",
    )
    recall_parser.add_argument("question", help="主题问题（仅作记录，检索由 --group 决定）")
    recall_parser.add_argument(
        "--group",
        action="append",
        type=_parse_group,
        metavar="label=词1,词2",
        help="一组 OR 检索词，可重复；组间默认 AND。如 --group market=油价,原油,布油",
    )
    recall_parser.add_argument(
        "--ticker", action="append", help="可选标的过滤（与分组 AND），逗号分隔，可重复"
    )
    recall_parser.add_argument(
        "--from", dest="date_from", required=True, help="起始日期（北京时间，YYYY-MM-DD）"
    )
    recall_parser.add_argument(
        "--to", dest="date_to", required=True, help="结束日期（北京时间，含当日）"
    )
    recall_parser.add_argument(
        "--any-group",
        action="store_true",
        help="组间改为 OR（放宽召回；默认 AND 提高精度）",
    )
    recall_parser.add_argument(
        "--no-llm",
        action="store_true",
        help="只做确定性检索（当前唯一模式；扩词/简报后续加入）",
    )
    recall_parser.add_argument("--limit", type=int, default=200, help="最多返回命中版本数")
    recall_parser.add_argument("--path", type=Path)
    recall_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    recall_parser.set_defaults(handler=_recall_command)
