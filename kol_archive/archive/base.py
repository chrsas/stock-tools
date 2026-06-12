"""Shared base for the archive: connection, transactions, and common row access."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import cast

from kol_archive.models import (
    ArchiveSettings,
    EventDimension,
    FeedRun,
    LoginState,
    ProbeRun,
    RunStatus,
)
from kol_archive.time import parse_utc_timestamp

LOGGER = logging.getLogger("kol_archive.archive")


def is_healthy_feed_run(run: FeedRun) -> bool:
    return (
        run.status is RunStatus.OK
        and run.login_state is LoginState.VALID
        and run.pagination_complete
        and not run.rate_limited
    )


def is_healthy_probe_run(run: ProbeRun) -> bool:
    return (
        run.status is RunStatus.OK and run.login_state is LoginState.VALID and not run.rate_limited
    )


def _json(value: dict[str, object] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _plus_days(timestamp: str, days: int) -> str:
    return (parse_utc_timestamp(timestamp) + timedelta(days=days)).isoformat()


def _required_lastrowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("insert did not return a row id")
    return cursor.lastrowid


class ArchiveBase:
    def __init__(
        self,
        connection: sqlite3.Connection,
        settings: ArchiveSettings | None = None,
    ) -> None:
        self.connection = connection
        self.settings = settings or ArchiveSettings()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def add_author(
        self,
        platform: str,
        platform_uid: str,
        live_monitoring_started_at: str,
        notes: str | None = None,
    ) -> int:
        with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO authors(platform, platform_uid, live_monitoring_started_at, notes)
                VALUES (?, ?, ?, ?)
                """,
                (platform, platform_uid, live_monitoring_started_at, notes),
            )
        return _required_lastrowid(cursor)

    def ensure_author(
        self,
        platform: str,
        platform_uid: str,
        live_monitoring_started_at: str,
        notes: str | None = None,
    ) -> int:
        row = self.connection.execute(
            "SELECT id FROM authors WHERE platform = ? AND platform_uid = ?",
            (platform, platform_uid),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        return self.add_author(platform, platform_uid, live_monitoring_started_at, notes)

    def get_author_id(self, platform: str, platform_uid: str) -> int | None:
        row = self.connection.execute(
            "SELECT id FROM authors WHERE platform = ? AND platform_uid = ?",
            (platform, platform_uid),
        ).fetchone()
        return None if row is None else int(row["id"])

    def _insert_event(
        self,
        post_id: int,
        dimension: EventDimension,
        from_value: object,
        to_value: object,
        detected_at: str,
        *,
        evidence_fetch_run_id: int | None = None,
        evidence_probe_run_id: int | None = None,
        from_version_id: int | None = None,
        to_version_id: int | None = None,
        notes: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO post_events(
                post_id, dimension, from_value, to_value, detected_at,
                evidence_fetch_run_id, evidence_probe_run_id,
                from_version_id, to_version_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                dimension,
                None if from_value is None else str(from_value),
                str(to_value),
                detected_at,
                evidence_fetch_run_id,
                evidence_probe_run_id,
                from_version_id,
                to_version_id,
                notes,
            ),
        )

    def _get_post(self, post_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown post id: {post_id}")
        return cast(sqlite3.Row, row)

    def _get_post_version(self, post_id: int, version_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM post_versions WHERE id = ? AND post_id = ?",
            (version_id, post_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"version {version_id} does not belong to post {post_id}")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return None if value is None else int(str(value))
