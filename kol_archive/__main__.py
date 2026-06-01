"""Command-line entry points for the local archive."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kol_archive.collector import CollectorSettings, XueqiuCollector, create_xueqiu_client
from kol_archive.config import load_config, resolve_cookie
from kol_archive.database import connect_database, initialize_database
from kol_archive.maintenance import (
    create_verified_backup,
    export_archive,
    restore_backup,
    verify_backup,
)
from kol_archive.models import ArchiveSettings, QueueReason
from kol_archive.presentation import build_evidence_card, list_timeline
from kol_archive.rewrite import load_rewrite_settings, request_rewrite
from kol_archive.service import Archive

LOGGER = logging.getLogger(__name__)


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(path)
    try:
        initialize_database(connection)
    finally:
        connection.close()


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _backup_retention_count(storage: dict[str, Any]) -> int:
    value = storage.get("backup_retention_count")
    return 30 if value is None else int(value)


def _db_path_from_config(config: dict[str, Any]) -> Path:
    storage = _section(config, "storage")
    return Path(str(storage.get("db_path") or "data/kol.sqlite3"))


def _resolve_db_path(path: Path | None, config: dict[str, Any]) -> Path:
    return _db_path_from_config(config) if path is None else path


def _configured_db_path(path: Path | None, config_dir: Path) -> Path:
    return path if path is not None else _db_path_from_config(load_config(config_dir))


def _connect_existing_archive(path: Path) -> tuple[sqlite3.Connection, Archive]:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite archive does not exist: {path}")
    connection = connect_database(path)
    return connection, Archive(connection)


def _print_json(payload: object) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        # CLI JSON stays UTF-8 for Windows consoles and downstream redirection.
        reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _current_version_id(archive: Archive, post_id: int, version_id: int | None) -> int:
    return archive.current_version_id(post_id) if version_id is None else version_id


def run_once(conf_dir: Path) -> None:
    config = load_config(conf_dir)
    storage = _section(config, "storage")
    monitoring = _section(config, "monitoring")
    polling = _section(config, "polling")
    db_path = Path(str(storage.get("db_path") or "data/kol.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(db_path)
    initialize_database(connection)
    archive = Archive(
        connection,
        ArchiveSettings(
            absent_threshold_n=int(monitoring.get("absent_threshold_n") or 3),
            recent_feed_absent_ttl_days=int(monitoring.get("recent_feed_absent_ttl_days") or 7),
        ),
    )
    cookie, cookie_source = resolve_cookie(config)
    logging.getLogger(__name__).info("credential source=%s present=%s", cookie_source, bool(cookie))
    client = None
    try:
        client = create_xueqiu_client(cookie)
        collector = XueqiuCollector(
            archive,
            client,
            CollectorSettings(
                request_min_interval_seconds=float(
                    polling.get("request_min_interval_seconds") or 2.5
                ),
                request_jitter_seconds=float(polling.get("request_jitter_seconds") or 1.5),
                max_feed_pages=int(polling.get("max_feed_pages", 20)),
            ),
        )
        now = datetime.now(tz=UTC)
        archive.expire_rechecks(now.isoformat())
        window_started_at = (
            now - timedelta(days=int(monitoring.get("window_days") or 30))
        ).isoformat()
        accounts = config.get("accounts") or []
        if not accounts:
            raise ValueError("at least one account must be configured")
        for account in accounts:
            uid = str(account.get("uid") or "").strip()
            if not uid:
                continue
            note = str(account.get("note") or "") or None
            author_id = archive.ensure_author("xueqiu", uid, now.isoformat(), note)
            collector.poll_feed(author_id, uid, window_started_at)
        collector.probe_due_posts()
    finally:
        if client is not None:
            client.close()
        connection.close()
    if bool(storage.get("backup_after_run", True)):
        result = create_verified_backup(
            db_path,
            Path(str(storage.get("backup_dir") or "data/backups")),
            retention_count=_backup_retention_count(storage),
        )
        LOGGER.info(
            "verified snapshot created path=%s removed_snapshots=%s",
            result.snapshot_path,
            len(result.removed_snapshots),
        )


def _init_db_command(args: argparse.Namespace) -> None:
    init_db(args.path)


def _run_once_command(args: argparse.Namespace) -> None:
    run_once(args.config_dir)


def _backup_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    storage = _section(config, "storage")
    result = create_verified_backup(
        _resolve_db_path(args.path, config),
        args.backup_dir or Path(str(storage.get("backup_dir") or "data/backups")),
        retention_count=(
            _backup_retention_count(storage)
            if args.retention_count is None
            else args.retention_count
        ),
    )
    LOGGER.info(
        "verified snapshot created path=%s removed_snapshots=%s",
        result.snapshot_path,
        len(result.removed_snapshots),
    )


def _verify_backup_command(args: argparse.Namespace) -> None:
    verify_backup(args.path)
    LOGGER.info("snapshot restore verification passed path=%s", args.path)


def _restore_backup_command(args: argparse.Namespace) -> None:
    restore_backup(args.snapshot_path, args.target_path)
    LOGGER.info("snapshot restored path=%s", args.target_path)


def _export_command(args: argparse.Namespace) -> None:
    result = export_archive(_configured_db_path(args.path, args.config_dir), args.output_dir)
    LOGGER.info("credential-safe export created path=%s", result.bundle_dir)


def _timeline_command(args: argparse.Namespace) -> None:
    connection, _ = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        _print_json(list_timeline(connection, limit=args.limit))
    finally:
        connection.close()


def _show_post_command(args: argparse.Namespace) -> None:
    connection, _ = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        _print_json(build_evidence_card(connection, args.post_id))
    finally:
        connection.close()


def _pin_command(args: argparse.Namespace) -> None:
    connection, archive = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        reason = None if args.confirm_reason is None else QueueReason(args.confirm_reason)
        archive.pin_post(args.post_id, datetime.now(tz=UTC).isoformat(), confirm_reason=reason)
        _print_json({"post_id": args.post_id, "watch_mode": "pinned"})
    finally:
        connection.close()


def _unpin_command(args: argparse.Namespace) -> None:
    connection, archive = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
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
        _print_json({"post_id": args.post_id, "watch_mode": row["watch_mode"]})
    finally:
        connection.close()


def _add_attention_command(args: argparse.Namespace) -> None:
    connection, archive = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        version_id = _current_version_id(archive, args.post_id, args.version_id)
        attention_id = archive.add_attention(
            args.post_id,
            version_id,
            datetime.now(tz=UTC).isoformat(),
            args.reason,
            args.expectation,
        )
        _print_json(
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
    connection, archive = _connect_existing_archive(_resolve_db_path(args.path, config))
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
        _print_json(
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
    connection, archive = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        archive.review_rewrite_exercise(args.exercise_id, args.verdict)
        _print_json({"rewrite_exercise_id": args.exercise_id, "my_verdict": args.verdict})
    finally:
        connection.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="KOL evidence archive")
    subparsers = parser.add_subparsers(required=True)
    init_parser = subparsers.add_parser("init-db", help="initialize a SQLite archive")
    init_parser.add_argument("path", type=Path)
    init_parser.set_defaults(handler=_init_db_command)
    run_parser = subparsers.add_parser("run-once", help="poll feeds and probe due posts")
    run_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    run_parser.set_defaults(handler=_run_once_command)
    backup_parser = subparsers.add_parser("backup", help="create and verify a SQLite snapshot")
    backup_parser.add_argument("--path", type=Path)
    backup_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    backup_parser.add_argument("--backup-dir", type=Path)
    backup_parser.add_argument("--retention-count", type=int)
    backup_parser.set_defaults(handler=_backup_command)
    verify_parser = subparsers.add_parser(
        "verify-backup", help="restore a snapshot temporarily and validate it"
    )
    verify_parser.add_argument("path", type=Path)
    verify_parser.set_defaults(handler=_verify_backup_command)
    restore_parser = subparsers.add_parser("restore-backup", help="restore a verified snapshot")
    restore_parser.add_argument("snapshot_path", type=Path)
    restore_parser.add_argument("target_path", type=Path)
    restore_parser.set_defaults(handler=_restore_backup_command)
    export_parser = subparsers.add_parser("export", help="export credential-safe JSON and CSV")
    export_parser.add_argument("--path", type=Path)
    export_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    export_parser.add_argument("--output-dir", type=Path, default=Path("data/exports"))
    export_parser.set_defaults(handler=_export_command)
    timeline_parser = subparsers.add_parser("timeline", help="show the raw observed timeline")
    timeline_parser.add_argument("--path", type=Path)
    timeline_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    timeline_parser.add_argument("--limit", type=int, default=50)
    timeline_parser.set_defaults(handler=_timeline_command)
    show_parser = subparsers.add_parser("show-post", help="show one post evidence card")
    show_parser.add_argument("post_id", type=int)
    show_parser.add_argument("--path", type=Path)
    show_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    show_parser.set_defaults(handler=_show_post_command)
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
    args = parser.parse_args()

    args.handler(args)


if __name__ == "__main__":
    main()
