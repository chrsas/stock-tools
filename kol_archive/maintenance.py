"""Verified SQLite snapshots and credential-safe archive exports."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

EXPORT_QUERIES = {
    "authors": "SELECT * FROM authors",
    "fetch_runs": "SELECT * FROM fetch_runs",
    "posts": "SELECT * FROM posts",
    "post_versions": "SELECT * FROM post_versions",
    "probe_runs": "SELECT * FROM probe_runs",
    "post_observations": "SELECT * FROM post_observations",
    "post_events": "SELECT * FROM post_events",
    "recheck_queue": "SELECT * FROM recheck_queue",
    "attention_log": "SELECT * FROM attention_log",
    "rewrite_exercises": "SELECT * FROM rewrite_exercises",
    "enrichments": "SELECT * FROM enrichments",
    "claims": "SELECT * FROM claims",
    "claim_outcomes": "SELECT * FROM claim_outcomes",
    "prices": "SELECT * FROM prices",
    "version_sightings": "SELECT * FROM version_sightings",
}
JSON_COLUMNS = {
    ("posts", "raw_meta"),
    ("post_versions", "raw_payload"),
}
TEXT_REDACTION_COLUMNS = {"notes"}
_SNAPSHOT_NAME_RE = re.compile(r"^kol-(\d{8}T\d{12}Z)(?:-(\d+))?\.sqlite3$")
_SENSITIVE_KEY_PARTS = (
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "xqat",
)
_COOKIE_TEXT_RE = re.compile(r"(?i)\bcookie\s*[:=]\s*[^\r\n]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:api[_-]?key|authorization|cookie|password|secret|token|xqat)"
    r"[a-z0-9_.-]*)\s*[:=]\s*([^;\s,]+)"
)


@dataclass(frozen=True)
class BackupResult:
    snapshot_path: Path
    removed_snapshots: tuple[Path, ...]


@dataclass(frozen=True)
class ExportResult:
    bundle_dir: Path
    json_path: Path
    csv_dir: Path


def _timestamp_slug(now: datetime | None = None) -> str:
    current = now or datetime.now(tz=UTC)
    return current.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _require_database(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite archive does not exist: {path}")


def _copy_database(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def validate_database(path: Path) -> None:
    _require_database(path)
    connection = sqlite3.connect(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"SQLite foreign key check failed: {violations}")
    finally:
        connection.close()


def verify_backup(snapshot_path: Path) -> None:
    _require_database(snapshot_path)
    with TemporaryDirectory(prefix="kol-archive-restore-") as temp_dir:
        restored_path = Path(temp_dir) / "restored.sqlite3"
        _copy_database(snapshot_path, restored_path)
        validate_database(restored_path)


def restore_backup(snapshot_path: Path, target_path: Path) -> None:
    _require_database(snapshot_path)
    if target_path.exists():
        raise FileExistsError(f"restore target already exists: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
    try:
        _copy_database(snapshot_path, temporary_path)
        validate_database(temporary_path)
        temporary_path.replace(target_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def _snapshot_sort_key(path: Path) -> tuple[str, int]:
    match = _SNAPSHOT_NAME_RE.fullmatch(path.name)
    if match is None:
        return path.name, 0
    return match.group(1), int(match.group(2) or 0)


def prune_snapshots(backup_dir: Path, retention_count: int) -> tuple[Path, ...]:
    if retention_count < 1:
        raise ValueError("backup retention count must be positive")
    snapshots = sorted(backup_dir.glob("kol-*.sqlite3"), key=_snapshot_sort_key)
    stale = snapshots[:-retention_count]
    for path in stale:
        path.unlink()
    return tuple(stale)


def create_verified_backup(
    source_path: Path,
    backup_dir: Path,
    *,
    retention_count: int = 30,
    now: datetime | None = None,
) -> BackupResult:
    _require_database(source_path)
    if retention_count < 1:
        raise ValueError("backup retention count must be positive")
    backup_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = _unique_path(backup_dir, f"kol-{_timestamp_slug(now)}", ".sqlite3")
    temporary_path = snapshot_path.with_name(f".{snapshot_path.name}.{uuid4().hex}.tmp")
    try:
        _copy_database(source_path, temporary_path)
        verify_backup(temporary_path)
        temporary_path.replace(snapshot_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    removed = prune_snapshots(backup_dir, retention_count)
    return BackupResult(snapshot_path=snapshot_path, removed_snapshots=removed)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_text(value: str) -> str:
    redacted = _COOKIE_TEXT_RE.sub("cookie=[REDACTED]", value)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return _SECRET_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", redacted)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(str(key)) else _sanitize_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _export_value(relation: str, column: str, value: Any) -> Any:
    if value is None:
        return None
    if (relation, column) in JSON_COLUMNS:
        return _sanitize_value(json.loads(str(value)))
    if column in TEXT_REDACTION_COLUMNS:
        return _redact_text(str(value))
    return value


def _read_relation(
    connection: sqlite3.Connection,
    relation: str,
    query: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    cursor = connection.execute(query)
    columns = [str(item[0]) for item in cursor.description or ()]
    rows = [
        {
            column: _export_value(relation, column, value)
            for column, value in zip(columns, row, strict=True)
        }
        for row in cursor.fetchall()
    ]
    return columns, rows


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def export_archive(
    source_path: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> ExportResult:
    _require_database(source_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = _unique_path(output_dir, f"export-{_timestamp_slug(now)}", "")
    bundle_dir.mkdir()
    csv_dir = bundle_dir / "csv"
    csv_dir.mkdir()
    json_path = bundle_dir / "archive.json"

    connection = sqlite3.connect(source_path)
    try:
        exports = {
            relation: _read_relation(connection, relation, query)
            for relation, query in EXPORT_QUERIES.items()
        }
    finally:
        connection.close()
    data = {relation: rows for relation, (_, rows) in exports.items()}

    payload = {
        "format_version": 1,
        "credential_redaction_attempted": True,
        "credential_redaction_mode": "heuristic_notes_and_raw_json",
        "relations": data,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    for relation, (fieldnames, rows) in exports.items():
        csv_path = csv_dir / f"{relation}.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows({key: _csv_value(value) for key, value in row.items()} for row in rows)
    return ExportResult(bundle_dir=bundle_dir, json_path=json_path, csv_dir=csv_dir)
