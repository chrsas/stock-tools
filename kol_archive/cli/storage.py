"""Storage maintenance commands: backup, verify, restore, and export."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from kol_archive.config import load_config
from kol_archive.maintenance import (
    create_verified_backup,
    export_archive,
    restore_backup,
    verify_backup,
)

from .common import backup_retention_count, configured_db_path, resolve_db_path, section

LOGGER = logging.getLogger(__name__)


def _backup_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    storage = section(config, "storage")
    result = create_verified_backup(
        resolve_db_path(args.path, config),
        args.backup_dir or Path(str(storage.get("backup_dir") or "data/backups")),
        retention_count=(
            backup_retention_count(storage)
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
    result = export_archive(configured_db_path(args.path, args.config_dir), args.output_dir)
    LOGGER.info("credential-safe export created path=%s", result.bundle_dir)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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
