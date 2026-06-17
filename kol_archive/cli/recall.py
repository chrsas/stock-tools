"""Retrospective topic recall commands: deterministic evidence retrieval.

Phase 11. ``recall --no-llm`` is the free, hallucination-free base: it runs the
pure-SQL grouped retrieval and prints the matching versions with coverage and
selection counts — no model, no prose. ``recall-expand`` is the separate,
token-spending helper that turns a natural-language question into *editable*
keyword groups + a suggested window; its output is printed for the user to review
and feed back into ``recall --group``. Keeping expansion in its own command
preserves ``recall``'s zero-token, auditable guarantee.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from kol_archive.config import load_config
from kol_archive.recall import (
    RetrievalQuery,
    TermGroup,
    append_topic_brief,
    parse_term_group,
    retrieve,
)
from kol_archive.recall_brief import load_brief_settings, synthesize_brief
from kol_archive.recall_expand import expand_query, load_expand_settings

from .common import (
    configured_db_path,
    connect_existing_archive,
    enrich_prompt_version,
    print_json,
    section,
)

_NO_GROUPS_HINT = (
    "本命令为确定性检索，需显式分组检索词，如 "
    "--group event=美伊,伊朗 --group market=油价,原油。"
    '可先运行 recall-expand "<问题>" 获取建议分组词与时间窗。'
)


def _parse_group(value: str) -> TermGroup:
    try:
        return parse_term_group(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _split_tickers(values: list[str] | None) -> tuple[str, ...]:
    tickers: list[str] = []
    for value in values or []:
        tickers.extend(part.strip().upper() for part in value.split(",") if part.strip())
    return tuple(dict.fromkeys(tickers))


def _query_from_args(args: argparse.Namespace) -> RetrievalQuery:
    return RetrievalQuery(
        groups=tuple(args.group),
        date_from=args.date_from,
        date_to=args.date_to,
        tickers=_split_tickers(args.ticker),
        require_all_groups=not args.any_group,
        limit=args.limit,
        question=args.question,
    )


def _recall_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    if not args.group:
        # Deterministic recall needs explicit groups by design (no model in the
        # loop). Use `recall-expand "<问题>"` to get suggested groups to paste here.
        raise SystemExit(_NO_GROUPS_HINT)
    query = _query_from_args(args)
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


def _recall_brief_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    if not args.group:
        raise SystemExit(_NO_GROUPS_HINT)
    if not args.question.strip():
        raise SystemExit("简报需要一个非空主题问题作为可追溯标题。")
    query = _query_from_args(args)
    benchmark = str((section(config, "prices")).get("benchmark_ticker") or "SH000300").upper()
    prompt_version = enrich_prompt_version(config, None)
    settings = load_brief_settings(config)
    connection, _ = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        result = retrieve(
            connection, query, prompt_version=prompt_version, benchmark_ticker=benchmark
        )
        coverage = result["coverage"]
        assert isinstance(coverage, dict)
        if int(coverage["version_count"]) == 0:
            raise SystemExit("窗内没有命中发言，无法生成简报。")
        brief = synthesize_brief(settings, result)
        brief_id = append_topic_brief(
            connection,
            query=query,
            coverage=result["coverage"],
            selection=result["selection"],
            cited_version_ids=brief.cited_version_ids,
            brief_text=brief.brief_text,
            model=settings.model,
            prompt_version=settings.prompt_version,
            created_at=datetime.now(tz=UTC).isoformat(),
        )
        print_json(
            {
                "brief_id": brief_id,
                "question": args.question,
                "prompt_version": settings.prompt_version,
                "coverage": result["coverage"],
                "selection": result["selection"],
                **brief.to_payload(),
            }
        )
    finally:
        connection.close()


def _recall_expand_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    settings = load_expand_settings(config)
    expansion = expand_query(settings, args.question, today=args.today)
    print_json(
        {
            "question": args.question,
            "prompt_version": settings.prompt_version,
            **expansion.to_payload(),
        }
    )


def _add_retrieval_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("question", help="主题问题（检索由 --group 决定；简报会用作可追溯标题）")
    parser.add_argument(
        "--group",
        action="append",
        type=_parse_group,
        metavar="label=词1,词2",
        help="一组 OR 检索词，可重复；组间默认 AND。如 --group market=油价,原油,布油",
    )
    parser.add_argument(
        "--ticker", action="append", help="可选标的过滤（与分组 AND），逗号分隔，可重复"
    )
    parser.add_argument(
        "--from", dest="date_from", required=True, help="起始日期（北京时间，YYYY-MM-DD）"
    )
    parser.add_argument("--to", dest="date_to", required=True, help="结束日期（北京时间，含当日）")
    parser.add_argument(
        "--any-group",
        action="store_true",
        help="组间改为 OR（放宽召回；默认 AND 提高精度）",
    )
    parser.add_argument("--limit", type=int, default=200, help="最多返回命中版本数")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--config-dir", type=Path, default=Path("config"))


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    recall_parser = subparsers.add_parser(
        "recall",
        help="按主题分组词 + 时间窗回溯检索当时发言（确定性，不调用 LLM）",
    )
    _add_retrieval_args(recall_parser)
    recall_parser.add_argument(
        "--no-llm",
        action="store_true",
        help="只做确定性检索（不合成简报；简报见 recall-brief）",
    )
    recall_parser.set_defaults(handler=_recall_command)

    brief_parser = subparsers.add_parser(
        "recall-brief",
        help="在确定性检索结果上合成主题简报（固定四块，每条带 version_id 引用）",
    )
    _add_retrieval_args(brief_parser)
    brief_parser.set_defaults(handler=_recall_brief_command)

    expand_parser = subparsers.add_parser(
        "recall-expand",
        help="把一个中文问题扩成可改的分组检索词 + 建议时间窗（调用 LLM，仅产出检索辅助）",
    )
    expand_parser.add_argument("question", help="自然语言主题问题，如「美伊冲突那阵怎么看油价」")
    expand_parser.add_argument(
        "--today", help="锚定时间窗推断的「今天」（北京时间 YYYY-MM-DD，默认当天）"
    )
    expand_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    expand_parser.set_defaults(handler=_recall_expand_command)
