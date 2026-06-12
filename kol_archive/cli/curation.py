"""Curation commands: pin/unpin, attention log, and rewrite exercises."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kol_archive.config import load_config
from kol_archive.models import QueueReason
from kol_archive.rewrite import load_rewrite_settings, request_rewrite
from kol_archive.service import Archive

from .common import configured_db_path, connect_existing_archive, print_json, resolve_db_path


def _current_version_id(archive: Archive, post_id: int, version_id: int | None) -> int:
    return archive.current_version_id(post_id) if version_id is None else version_id


def _pin_command(args: argparse.Namespace) -> None:
    connection, archive = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        reason = None if args.confirm_reason is None else QueueReason(args.confirm_reason)
        archive.pin_post(args.post_id, datetime.now(tz=UTC).isoformat(), confirm_reason=reason)
        print_json({"post_id": args.post_id, "watch_mode": "pinned"})
    finally:
        connection.close()


def _unpin_command(args: argparse.Namespace) -> None:
    connection, archive = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        if args.window_days < 1:
            raise ValueError("window days must be positive")
        now = datetime.now(tz=UTC)
        archive.unpin_post_for_window(
            args.post_id,
            now.isoformat(),
            (now - timedelta(days=args.window_days)).isoformat(),
        )
        row = connection.execute(
            "SELECT watch_mode FROM posts WHERE id = ?",
            (args.post_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown post id: {args.post_id}")
        print_json({"post_id": args.post_id, "watch_mode": row["watch_mode"]})
    finally:
        connection.close()


def _add_attention_command(args: argparse.Namespace) -> None:
    connection, archive = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        version_id = _current_version_id(archive, args.post_id, args.version_id)
        attention_id = archive.add_attention(
            args.post_id,
            version_id,
            datetime.now(tz=UTC).isoformat(),
            args.reason,
            args.expectation,
        )
        print_json(
            {
                "attention_id": attention_id,
                "post_id": args.post_id,
                "version_id": version_id,
                "watch_mode": "pinned",
            }
        )
    finally:
        connection.close()


def _rewrite_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    settings = load_rewrite_settings(config)
    connection, archive = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        version_id = _current_version_id(archive, args.post_id, args.version_id)
        source = archive.rewrite_source(args.post_id, version_id)
        suggestion = request_rewrite(settings, source.original_text)
        exercise_id = archive.add_rewrite_exercise(
            source,
            suggestion.rewritten_claim,
            suggestion.rationale,
            settings.model,
            settings.prompt_version,
            datetime.now(tz=UTC).isoformat(),
        )
        print_json(
            {
                "rewrite_exercise_id": exercise_id,
                "post_id": args.post_id,
                "version_id": version_id,
                "llm_rewritten_claim": suggestion.rewritten_claim,
                "llm_rationale": suggestion.rationale,
                "watch_mode": "pinned",
            }
        )
    finally:
        connection.close()


def _review_rewrite_command(args: argparse.Namespace) -> None:
    connection, archive = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        archive.review_rewrite_exercise(args.exercise_id, args.verdict)
        print_json({"rewrite_exercise_id": args.exercise_id, "my_verdict": args.verdict})
    finally:
        connection.close()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    pin_parser = subparsers.add_parser("pin", help="pin one archived post")
    pin_parser.add_argument("post_id", type=int)
    pin_parser.add_argument("--path", type=Path)
    pin_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    pin_parser.add_argument("--confirm-reason", choices=[reason.value for reason in QueueReason])
    pin_parser.set_defaults(handler=_pin_command)
    unpin_parser = subparsers.add_parser("unpin", help="unpin one archived post")
    unpin_parser.add_argument("post_id", type=int)
    unpin_parser.add_argument("--path", type=Path)
    unpin_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    unpin_parser.add_argument("--window-days", type=int, default=30)
    unpin_parser.set_defaults(handler=_unpin_command)
    attention_parser = subparsers.add_parser(
        "add-attention", help="record a reason and pin the selected observed version"
    )
    attention_parser.add_argument("post_id", type=int)
    attention_parser.add_argument("--reason", required=True)
    attention_parser.add_argument("--expectation")
    attention_parser.add_argument("--version-id", type=int)
    attention_parser.add_argument("--path", type=Path)
    attention_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    attention_parser.set_defaults(handler=_add_attention_command)
    rewrite_parser = subparsers.add_parser(
        "rewrite", help="request one LLM rewrite exercise and pin the selected observed version"
    )
    rewrite_parser.add_argument("post_id", type=int)
    rewrite_parser.add_argument("--version-id", type=int)
    rewrite_parser.add_argument("--path", type=Path)
    rewrite_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    rewrite_parser.set_defaults(handler=_rewrite_command)
    review_parser = subparsers.add_parser(
        "review-rewrite", help="record a rewrite exercise verdict"
    )
    review_parser.add_argument("exercise_id", type=int)
    review_parser.add_argument("--verdict", choices=["valid", "too_vague", "wrong"], required=True)
    review_parser.add_argument("--path", type=Path)
    review_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    review_parser.set_defaults(handler=_review_rewrite_command)
