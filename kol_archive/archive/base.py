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

    def author_feed_due(self, author_id: int, now: str) -> bool:
        row = self.connection.execute(
            "SELECT next_feed_due_at FROM authors WHERE id = ?", (author_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown author id: {author_id}")
        due_at = row["next_feed_due_at"]
        return due_at is None or parse_utc_timestamp(str(due_at)) <= parse_utc_timestamp(now)

    def author_feed_head(self, author_id: int) -> tuple[str | None, str | None]:
        row = self.connection.execute(
            """
            SELECT last_timeline_head_id, last_timeline_head_posted_at
            FROM authors WHERE id = ?
            """,
            (author_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown author id: {author_id}")
        return (
            None if row["last_timeline_head_id"] is None else str(row["last_timeline_head_id"]),
            None
            if row["last_timeline_head_posted_at"] is None
            else str(row["last_timeline_head_posted_at"]),
        )

    def author_head_observation_due(self, author_id: int, now: str) -> bool:
        row = self.connection.execute(
            "SELECT last_timeline_head_observed_at FROM authors WHERE id = ?", (author_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown author id: {author_id}")
        last_observed_at = row["last_timeline_head_observed_at"]
        if last_observed_at is None:
            return True
        return parse_utc_timestamp(str(last_observed_at)).date() < parse_utc_timestamp(now).date()

    def mark_author_head_observed(self, author_id: int, observed_at: str) -> None:
        with self._transaction():
            self.connection.execute(
                "UPDATE authors SET last_timeline_head_observed_at = ? WHERE id = ?",
                (observed_at, author_id),
            )

    def record_author_feed_head(
        self, author_id: int, platform_post_id: str | None, posted_at: str | None
    ) -> None:
        with self._transaction():
            self.connection.execute(
                """
                UPDATE authors
                SET last_timeline_head_id = ?, last_timeline_head_posted_at = ?
                WHERE id = ?
                """,
                (platform_post_id, posted_at, author_id),
            )

    def schedule_author_feed_poll(self, author_id: int, polled_at: str, next_due_at: str) -> None:
        with self._transaction():
            self.connection.execute(
                """
                UPDATE authors SET last_feed_polled_at = ?, next_feed_due_at = ?
                WHERE id = ?
                """,
                (polled_at, next_due_at, author_id),
            )

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
