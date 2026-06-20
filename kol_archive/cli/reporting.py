"""Read-side commands: digest, timeline, queue, scorecards, evidence cards,
analysis, and the local web UI."""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from kol_archive.analysis import (
    list_crowding_events,
    load_analysis_settings,
    selective_deletion_analysis,
    stage_crowding_events,
)
from kol_archive.config import load_config
from kol_archive.digest import generate_digest
from kol_archive.notifications import (
    NotificationPayload,
    load_notification_settings,
    send_notification,
)
from kol_archive.obs import configure_logging
from kol_archive.presentation import (
    author_scorecards,
    build_evidence_card,
    list_attention_queue,
    list_filtered_timeline,
    list_timeline,
)
from kol_archive.web import load_web_settings, serve_archive

from .common import (
    configured_db_path,
    connect_existing_archive,
    enrich_prompt_version,
    print_json,
    resolve_db_path,
    section,
)

LOGGER = logging.getLogger(__name__)


def digest_settings(config: dict[str, Any], output_dir: Path | None) -> tuple[Path, int]:
    digest = section(config, "digest")
    configured_output_dir = digest.get("output_dir")
    resolved_output_dir = (
        output_dir
        if output_dir is not None
        else Path("data/digests" if configured_output_dir is None else str(configured_output_dir))
    )
    configured_wave_min_accounts = digest.get("wave_min_accounts")
    wave_min_accounts = (
        3 if configured_wave_min_accounts is None else int(configured_wave_min_accounts)
    )
    return resolved_output_dir, wave_min_accounts


def _digest_command(args: argparse.Namespace) -> None:
    if args.days < 1:
        raise ValueError("days must be positive")
    config = load_config(args.config_dir)
    output_dir, wave_min_accounts = digest_settings(config, args.output_dir)
    prices = section(config, "prices")
    benchmark_ticker = str(prices.get("benchmark_ticker") or "SH000300")
    prompt_version = enrich_prompt_version(config, None)
    end = datetime.now(tz=UTC)
    start = end - timedelta(days=args.days)
    connection, _ = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        result = generate_digest(
            connection,
            start.isoformat(),
            end.isoformat(),
            output_dir,
            wave_min_accounts=wave_min_accounts,
            prompt_version=prompt_version,
            benchmark_ticker=benchmark_ticker,
        )
        try:
            notification_settings = load_notification_settings(config)
            send_notification(
                notification_settings,
                NotificationPayload(
                    title=result.title,
                    count=len(result.events),
                    link=notification_settings.private_base_url,
                ),
            )
        except Exception:
            LOGGER.warning("digest notification failed")
        print_json(
            {
                "title": result.title,
                "markdown_path": str(result.markdown_path),
                "html_path": str(result.html_path),
                "deletion_count": result.deletion_count,
                "edit_count": result.edit_count,
                "image_change_count": result.image_change_count,
                "deletion_wave": result.deletion_wave,
            }
        )
    finally:
        connection.close()


def _timeline_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        if args.filtered:
            prompt_version = enrich_prompt_version(config, args.prompt_version)
            timeline = list_filtered_timeline(connection, prompt_version, limit=args.limit)
        else:
            timeline = list_timeline(connection, limit=args.limit)
        print_json(timeline)
    finally:
        connection.close()


def _queue_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        prompt_version = enrich_prompt_version(config, args.prompt_version)
        queue = list_attention_queue(connection, prompt_version, limit=args.limit)
        if args.tier3_only:
            queue = [item for item in queue if int(cast(int, item.get("tier") or 0)) >= 3]
        print_json(queue)
    finally:
        connection.close()


def _scorecards_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        prompt_version = enrich_prompt_version(config, args.prompt_version)
        print_json(author_scorecards(connection, prompt_version))
    finally:
        connection.close()


def _show_post_command(args: argparse.Namespace) -> None:
    connection, _ = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        print_json(build_evidence_card(connection, args.post_id))
    finally:
        connection.close()


def _analyze_command(args: argparse.Namespace) -> None:
    if args.limit < 1:
        raise ValueError("limit must be positive")
    config = load_config(args.config_dir)
    settings = load_analysis_settings(config)
    connection, _ = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        staged = stage_crowding_events(
            connection,
            settings,
            datetime.now(tz=UTC).isoformat(),
        )
        print_json(
            {
                "staged_crowding_events": staged,
                "selective_deletion": selective_deletion_analysis(
                    connection, settings.min_group_samples
                ),
                "crowding_events": list_crowding_events(connection, limit=args.limit),
            }
        )
    finally:
        connection.close()


def _serve_command(args: argparse.Namespace) -> None:
    if args.verbose:
        configure_logging(verbose=True)
    config = load_config(args.config_dir)
    serve_archive(
        resolve_db_path(args.path, config),
        args.config_dir,
        load_web_settings(config, bind_host=args.host, port=args.port),
    )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    digest_parser = subparsers.add_parser("digest", help="write a neutral change digest")
    digest_parser.add_argument("--path", type=Path)
    digest_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    digest_parser.add_argument("--days", type=int, default=7)
    digest_parser.add_argument("--output-dir", type=Path)
    digest_parser.set_defaults(handler=_digest_command)
    timeline_parser = subparsers.add_parser("timeline", help="show the raw observed timeline")
    timeline_parser.add_argument("--path", type=Path)
    timeline_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    timeline_parser.add_argument("--limit", type=int, default=50)
    timeline_parser.add_argument(
        "--filtered",
        action="store_true",
        help="show only posts whose current version hit an enrichment label",
    )
    timeline_parser.add_argument(
        "--prompt-version", help="enrichment prompt version for --filtered (default from config)"
    )
    timeline_parser.set_defaults(handler=_timeline_command)
    queue_parser = subparsers.add_parser(
        "queue", help="show the pending-attention queue (label hits not yet pinned/reasoned)"
    )
    queue_parser.add_argument("--path", type=Path)
    queue_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    queue_parser.add_argument("--limit", type=int, default=50)
    queue_parser.add_argument(
        "--tier3-only", action="store_true", help="only versions hitting all three labels"
    )
    queue_parser.add_argument(
        "--prompt-version", help="enrichment prompt version (default from config)"
    )
    queue_parser.set_defaults(handler=_queue_command)
    scorecards_parser = subparsers.add_parser(
        "scorecards",
        help="per-author label counts + genre mix (diagnostic summary; no hit-rate, no ranking)",
    )
    scorecards_parser.add_argument("--path", type=Path)
    scorecards_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    scorecards_parser.add_argument(
        "--prompt-version", help="enrichment prompt version (default from config)"
    )
    scorecards_parser.set_defaults(handler=_scorecards_command)
    show_parser = subparsers.add_parser("show-post", help="show one post evidence card")
    show_parser.add_argument("post_id", type=int)
    show_parser.add_argument("--path", type=Path)
    show_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    show_parser.set_defaults(handler=_show_post_command)
    analyze_parser = subparsers.add_parser(
        "analyze", help="stage crowding events and print neutral distribution analysis"
    )
    analyze_parser.add_argument("--limit", type=int, default=50)
    analyze_parser.add_argument("--path", type=Path)
    analyze_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    analyze_parser.set_defaults(handler=_analyze_command)
    serve_parser = subparsers.add_parser("serve", help="serve the local web archive")
    serve_parser.add_argument("--path", type=Path)
    serve_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)
    serve_parser.add_argument(
        "--verbose",
        action="store_true",
        help="log every SQL statement and third-party request/response body (DEBUG)",
    )
    serve_parser.set_defaults(handler=_serve_command)
