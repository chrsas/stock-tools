"""Background automation: collection scheduling and the polling loop."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kol_archive.config import load_config
from kol_archive.database import connect_database

from . import jobs
from .settings import AutomationSettings

if TYPE_CHECKING:
    from .settings import ArchiveHttpServer

LOGGER = logging.getLogger("kol_archive.web")
AUTO_COLLECTION_RETRY_DELAY_MINUTES = 1


def _automation_path(db_path: Path) -> Path:
    return db_path.parent / "web-automation.json"


def _load_automation_settings(db_path: Path) -> AutomationSettings:
    path = _automation_path(db_path)
    if not path.is_file():
        return AutomationSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("automation settings must be an object")
        interval = int(raw.get("collection_interval_minutes", 180))
        if not 5 <= interval <= 10080:
            raise ValueError("collection interval out of range")
        return AutomationSettings(
            collection_enabled=bool(raw.get("collection_enabled", False)),
            collection_interval_minutes=interval,
            auto_enrich=bool(raw.get("auto_enrich", True)),
        )
    except OSError, ValueError, TypeError, json.JSONDecodeError:
        LOGGER.warning("invalid web automation settings ignored path=%s", path)
        return AutomationSettings()


def _save_automation_settings(server: ArchiveHttpServer) -> None:
    path = _automation_path(server.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with server.automation_settings_lock:
        settings = server.automation_settings
        payload = {
            "collection_enabled": settings.collection_enabled,
            "collection_interval_minutes": settings.collection_interval_minutes,
            "auto_enrich": settings.auto_enrich,
        }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _schedule_next_collection(settings: AutomationSettings, *, immediate: bool = False) -> None:
    delay = 0 if immediate else settings.collection_interval_minutes
    settings.next_collection_at = (datetime.now(tz=UTC) + timedelta(minutes=delay)).isoformat()


def _schedule_collection_retry(settings: AutomationSettings) -> None:
    settings.next_collection_at = (
        datetime.now(tz=UTC) + timedelta(minutes=AUTO_COLLECTION_RETRY_DELAY_MINUTES)
    ).isoformat()


def _configured_account_uids(config: dict[str, Any]) -> set[str]:
    return {
        uid
        for account in (config.get("accounts") or [])
        if (uid := str((account or {}).get("uid") or "").strip())
    }


def _startup_collection_due_at(
    db_path: Path,
    interval_minutes: int,
    *,
    active_author_uids: set[str] | None = None,
    now: datetime | None = None,
) -> datetime:
    current = now or datetime.now(tz=UTC)
    fallback_due_at = current
    if not db_path.is_file():
        return fallback_due_at
    query_parameters: tuple[str, ...] = ()
    author_filter = "WHERE platform = 'xueqiu'"
    if active_author_uids is not None:
        if not active_author_uids:
            return fallback_due_at
        query_parameters = tuple(sorted(active_author_uids))
        placeholders = ",".join("?" for _ in query_parameters)
        author_filter = f"WHERE platform = 'xueqiu' AND platform_uid IN ({placeholders})"
    connection = connect_database(db_path)
    try:
        rows = connection.execute(
            f"""
            WITH active_authors AS (
                SELECT id
                FROM authors
                {author_filter}
            ),
            latest_author_runs AS (
                SELECT f.author_id, MAX(f.started_at) AS latest_started_at
                FROM fetch_runs f
                JOIN active_authors a ON a.id = f.author_id
                WHERE f.ingest_mode = 'live'
                GROUP BY f.author_id
            )
            SELECT f.started_at, f.finished_at, f.status, f.login_state,
                   f.rate_limited, f.http_error_count
            FROM active_authors a
            LEFT JOIN latest_author_runs latest ON latest.author_id = a.id
            LEFT JOIN fetch_runs f
              ON f.author_id = a.id
             AND f.started_at = latest.latest_started_at
             AND f.ingest_mode = 'live'
            ORDER BY f.started_at DESC, f.id DESC
            """,
            query_parameters,
        ).fetchall()
    finally:
        connection.close()
    if not rows or (active_author_uids is not None and len(rows) < len(active_author_uids)):
        return fallback_due_at
    for row in rows:
        failed = (
            row["started_at"] is None
            or row["finished_at"] is None
            or row["status"] == "failed"
            or row["login_state"] != "valid"
            or bool(row["rate_limited"])
            or int(row["http_error_count"] or 0) > 0
        )
        if failed:
            return fallback_due_at
    try:
        latest_finished_at = max(datetime.fromisoformat(str(row["finished_at"])) for row in rows)
    except ValueError:
        return fallback_due_at
    if latest_finished_at.tzinfo is None:
        latest_finished_at = latest_finished_at.replace(tzinfo=UTC)
    next_due_at = latest_finished_at + timedelta(minutes=interval_minutes)
    if next_due_at <= current:
        return fallback_due_at
    return next_due_at


def _prime_startup_collection_schedule(server: ArchiveHttpServer) -> None:
    with server.automation_settings_lock:
        enabled = server.automation_settings.collection_enabled
        interval = server.automation_settings.collection_interval_minutes
    if not enabled:
        with server.automation_settings_lock:
            server.automation_settings.next_collection_at = None
        return
    active_author_uids = _configured_account_uids(load_config(server.config_dir))
    next_collection_at = _startup_collection_due_at(
        server.db_path, interval, active_author_uids=active_author_uids
    ).isoformat()
    with server.automation_settings_lock:
        if server.automation_settings.collection_enabled:
            server.automation_settings.next_collection_at = next_collection_at


def _schedule_after_automatic_collection(
    server: ArchiveHttpServer, failure_response: tuple[HTTPStatus, str] | None
) -> None:
    with server.automation_settings_lock:
        if not server.automation_settings.collection_enabled:
            return
        if failure_response is not None and failure_response[0] == HTTPStatus.CONFLICT:
            _schedule_collection_retry(server.automation_settings)
        else:
            _schedule_next_collection(server.automation_settings)


def _automation_loop(server: ArchiveHttpServer) -> None:
    while not server.automation_stop.wait(1):
        with server.automation_settings_lock:
            settings = server.automation_settings
            if not settings.collection_enabled:
                settings.next_collection_at = None
                continue
            if settings.next_collection_at is None:
                _schedule_next_collection(settings)
                continue
            due = datetime.now(tz=UTC) >= datetime.fromisoformat(settings.next_collection_at)
        if not due:
            continue
        try:
            _, failure_response = jobs._execute_collection(server)
            if failure_response is not None:
                LOGGER.warning("automatic web collection failed status=%s", failure_response[0])
            _schedule_after_automatic_collection(server, failure_response)
        except Exception:
            LOGGER.warning("automatic web collection failed")
            _schedule_after_automatic_collection(
                server, (HTTPStatus.INTERNAL_SERVER_ERROR, "automatic collection failed")
            )
