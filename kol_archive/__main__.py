"""Command-line entry points for the local archive."""

from __future__ import annotations

import argparse
import logging
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
from kol_archive.models import ArchiveSettings
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
    result = create_verified_backup(
        args.path,
        args.backup_dir,
        retention_count=args.retention_count,
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
    result = export_archive(args.path, args.output_dir)
    LOGGER.info("credential-safe export created path=%s", result.bundle_dir)


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
    backup_parser.add_argument("path", type=Path, nargs="?", default=Path("data/kol.sqlite3"))
    backup_parser.add_argument("--backup-dir", type=Path, default=Path("data/backups"))
    backup_parser.add_argument("--retention-count", type=int, default=30)
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
    export_parser.add_argument("path", type=Path, nargs="?", default=Path("data/kol.sqlite3"))
    export_parser.add_argument("--output-dir", type=Path, default=Path("data/exports"))
    export_parser.set_defaults(handler=_export_command)
    args = parser.parse_args()

    args.handler(args)


if __name__ == "__main__":
    main()
