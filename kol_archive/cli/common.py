"""Shared CLI plumbing: config sections, DB paths, archive connections, JSON output."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from kol_archive.config import load_config
from kol_archive.database import connect_database, initialize_database
from kol_archive.service import Archive


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(path)
    try:
        initialize_database(connection)
    finally:
        connection.close()


def section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def backup_retention_count(storage: dict[str, Any]) -> int:
    value = storage.get("backup_retention_count")
    return 30 if value is None else int(value)


def db_path_from_config(config: dict[str, Any]) -> Path:
    storage = section(config, "storage")
    return Path(str(storage.get("db_path") or "data/kol.sqlite3"))


def resolve_db_path(path: Path | None, config: dict[str, Any]) -> Path:
    return db_path_from_config(config) if path is None else path


def configured_db_path(path: Path | None, config_dir: Path) -> Path:
    return path if path is not None else db_path_from_config(load_config(config_dir))


def connect_existing_archive(path: Path) -> tuple[sqlite3.Connection, Archive]:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite archive does not exist: {path}")
    connection = connect_database(path)
    initialize_database(connection)
    return connection, Archive(connection)


def configure_stdout_utf8() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        # CLI JSON stays UTF-8 for Windows consoles and downstream redirection.
        reconfigure(encoding="utf-8")


def print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def enrich_prompt_version(config: dict[str, Any], override: str | None) -> str:
    if override:
        return override
    llm = config.get("llm") or {}
    if isinstance(llm, dict):
        configured = str(llm.get("enrich_prompt_version") or "").strip()
        if configured:
            return configured
    return "enrich-v2"
