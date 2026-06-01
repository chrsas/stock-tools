"""Atomic archive writes for feed polling and direct-link probes."""

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
    ContentFidelity,
    EventDimension,
    FeedRun,
    FeedState,
    LoginState,
    NormalizedPost,
    PendingPositive,
    PendingProjection,
    ProbeResult,
    ProbeRun,
    ProbeTarget,
    QueueReason,
    QueueState,
    RunStatus,
    SourceState,
    WatchMode,
)
from kol_archive.time import parse_utc_timestamp, timestamp_in_closed_range

LOGGER = logging.getLogger(__name__)


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


class Archive:
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

    def probe_targets(self) -> list[ProbeTarget]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT p.id, p.author_id, p.platform_post_id
            FROM posts p
            LEFT JOIN recheck_queue q ON q.post_id = p.id AND q.state = ?
            WHERE p.watch_mode = ? OR q.id IS NOT NULL
            ORDER BY p.id
            """,
            (QueueState.PENDING, WatchMode.PINNED),
        ).fetchall()
        return [
            ProbeTarget(
                post_id=int(row["id"]),
                author_id=int(row["author_id"]),
                platform_post_id=str(row["platform_post_id"]),
            )
            for row in rows
        ]

    def record_feed_run(
        self,
        run: FeedRun,
        posts: list[NormalizedPost],
        *,
        crash_after_evidence: bool = False,
    ) -> int:
        self._validate_feed_posts(run, posts)
        effective_run = run.with_effective_status(posts)
        with self._transaction():
            fetch_run_id = self._insert_fetch_run(effective_run)
            post_ids = {
                post.platform_post_id: self._ensure_post(run.platform, post) for post in posts
            }

            positives = [
                self._prepare_positive_observation(post, post_ids[post.platform_post_id])
                for post in posts
            ]
            for positive in positives:
                self._insert_positive_observation(fetch_run_id, positive)
            for positive in positives:
                self._insert_positive_events(fetch_run_id, positive)

            projections: list[PendingProjection] = []
            if is_healthy_feed_run(effective_run):
                projections = self._prepare_negative_inferences(
                    fetch_run_id, effective_run, set(post_ids.values())
                )

            if crash_after_evidence:
                raise RuntimeError("injected crash after feed evidence")

            for positive in positives:
                self._apply_positive_projection(positive)
            for projection in projections:
                self._apply_negative_projection(projection)
        LOGGER.info(
            "feed_run archived run_id=%s author_id=%s status=%s healthy=%s "
            "pages_fetched=%s covered_from=%s covered_to=%s rate_limited=%s "
            "http_error_count=%s observed_posts=%s",
            fetch_run_id,
            effective_run.author_id,
            effective_run.status,
            is_healthy_feed_run(effective_run),
            effective_run.pages_fetched,
            effective_run.covered_from,
            effective_run.covered_to,
            effective_run.rate_limited,
            effective_run.http_error_count,
            len(posts),
        )
        return fetch_run_id

    def record_probe_run(
        self,
        run: ProbeRun,
        observed_post: NormalizedPost | None = None,
        *,
        crash_after_evidence: bool = False,
    ) -> int:
        if observed_post is not None:
            observed_post.validate()
            if observed_post.content_fidelity is not run.content_fidelity:
                raise ValueError("probe run fidelity must match observed post fidelity")
        healthy = is_healthy_probe_run(run)
        with self._transaction():
            row = self._get_post(run.post_id)
            if observed_post is not None and (
                observed_post.author_id != int(row["author_id"])
                or observed_post.platform_post_id != str(row["platform_post_id"])
            ):
                raise ValueError("probe observation identity must match archived post")
            prior_version_id = self._optional_int(row["current_version_id"])
            version_id: int | None = None
            content_changed = False
            if (
                healthy
                and run.result is ProbeResult.REACHABLE
                and run.content_fidelity is ContentFidelity.FULL
            ):
                if observed_post is None:
                    raise ValueError("healthy reachable full probe requires observed post")
                version_id, content_changed = self._append_version_if_changed(
                    run.post_id, prior_version_id, row["current_content_hash"], observed_post
                )
            probe_run_id = self._insert_probe_run(run, version_id)
            source_state = SourceState(str(row["source_state"]))
            next_source_state = source_state
            if healthy:
                next_source_state = self._next_source_state(source_state, run.result)
                if next_source_state is not source_state:
                    self._insert_event(
                        run.post_id,
                        EventDimension.SOURCE_STATE,
                        source_state,
                        next_source_state,
                        run.observed_at,
                        evidence_probe_run_id=probe_run_id,
                    )
                if content_changed:
                    self._insert_event(
                        run.post_id,
                        EventDimension.CONTENT,
                        prior_version_id,
                        version_id,
                        run.observed_at,
                        evidence_probe_run_id=probe_run_id,
                        from_version_id=prior_version_id,
                        to_version_id=version_id,
                    )
            if crash_after_evidence:
                raise RuntimeError("injected crash after probe evidence")
            if healthy:
                self._apply_probe_projection(
                    run,
                    next_source_state,
                    version_id,
                    observed_post if content_changed else None,
                )
        LOGGER.info(
            "probe_run archived run_id=%s post_id=%s status=%s healthy=%s "
            "result=%s fidelity=%s rate_limited=%s",
            probe_run_id,
            run.post_id,
            run.status,
            healthy,
            run.result,
            run.content_fidelity,
            run.rate_limited,
        )
        return probe_run_id

    def enqueue_recheck(
        self,
        post_id: int,
        reason: QueueReason,
        enqueued_at: str,
        expires_at: str,
    ) -> None:
        with self._transaction():
            self._enqueue_recheck(post_id, reason, enqueued_at, expires_at)

    def pin_post(
        self,
        post_id: int,
        detected_at: str,
        *,
        confirm_reason: QueueReason | None = None,
    ) -> None:
        with self._transaction():
            row = self._get_post(post_id)
            prior = WatchMode(str(row["watch_mode"]))
            if prior is not WatchMode.PINNED:
                self._insert_event(
                    post_id,
                    EventDimension.WATCH_MODE,
                    prior,
                    WatchMode.PINNED,
                    detected_at,
                )
                self.connection.execute(
                    "UPDATE posts SET watch_mode = ? WHERE id = ?",
                    (WatchMode.PINNED, post_id),
                )
            if confirm_reason is not None:
                self.connection.execute(
                    """
                    UPDATE recheck_queue SET state = ?
                    WHERE post_id = ? AND reason = ? AND state = ?
                    """,
                    (QueueState.CONFIRMED, post_id, confirm_reason, QueueState.PENDING),
                )

    def unpin_post(self, post_id: int, detected_at: str, *, within_recent_window: bool) -> None:
        next_mode = WatchMode.RECENT_WINDOW if within_recent_window else WatchMode.INACTIVE
        with self._transaction():
            row = self._get_post(post_id)
            prior = WatchMode(str(row["watch_mode"]))
            if prior is next_mode:
                return
            self._insert_event(
                post_id,
                EventDimension.WATCH_MODE,
                prior,
                next_mode,
                detected_at,
            )
            self.connection.execute(
                "UPDATE posts SET watch_mode = ? WHERE id = ?",
                (next_mode, post_id),
            )

    def expire_rechecks(self, as_of: str) -> int:
        with self._transaction():
            cursor = self.connection.execute(
                """
                UPDATE recheck_queue SET state = ?
                WHERE state = ? AND expires_at <= ?
                """,
                (QueueState.EXPIRED, QueueState.PENDING, as_of),
            )
        return cursor.rowcount

    def _validate_feed_posts(self, run: FeedRun, posts: list[NormalizedPost]) -> None:
        post_ids: set[str] = set()
        for post in posts:
            post.validate()
            if post.author_id != run.author_id:
                raise ValueError("feed post author_id must match feed run author_id")
            if post.platform_post_id in post_ids:
                raise ValueError("feed run contains duplicate platform_post_id")
            post_ids.add(post.platform_post_id)

    def _insert_fetch_run(self, run: FeedRun) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO fetch_runs(
                author_id, platform, started_at, finished_at, status, login_state,
                pages_fetched, pagination_complete, covered_from, covered_to,
                rate_limited, http_error_count, ingest_mode, adapter_version, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.author_id,
                run.platform,
                run.started_at,
                run.finished_at,
                run.status,
                run.login_state,
                run.pages_fetched,
                run.pagination_complete,
                run.covered_from,
                run.covered_to,
                run.rate_limited,
                run.http_error_count,
                run.ingest_mode,
                run.adapter_version,
                run.notes,
            ),
        )
        return _required_lastrowid(cursor)

    def _ensure_post(self, platform: str, post: NormalizedPost) -> int:
        self.connection.execute(
            """
            INSERT INTO posts(
                author_id, platform, platform_post_id, first_seen_at, last_present_at,
                absent_healthy_streak, feed_state, source_state, watch_mode,
                posted_at_claimed, url, ingest_mode, raw_meta
            ) VALUES (?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, platform_post_id) DO NOTHING
            """,
            (
                post.author_id,
                platform,
                post.platform_post_id,
                post.observed_at,
                FeedState.UNKNOWN,
                SourceState.UNKNOWN,
                WatchMode.RECENT_WINDOW,
                post.posted_at_claimed,
                post.url,
                post.ingest_mode,
                _json(post.raw_meta),
            ),
        )
        row = self.connection.execute(
            "SELECT id, author_id FROM posts WHERE platform = ? AND platform_post_id = ?",
            (platform, post.platform_post_id),
        ).fetchone()
        if row is None or int(row["author_id"]) != post.author_id:
            raise ValueError("platform post identity belongs to another author")
        return int(row["id"])

    def _prepare_positive_observation(self, post: NormalizedPost, post_id: int) -> PendingPositive:
        row = self._get_post(post_id)
        prior_version_id = self._optional_int(row["current_version_id"])
        version_id: int | None = None
        changed = False
        if post.content_fidelity is ContentFidelity.FULL:
            version_id, changed = self._append_version_if_changed(
                post_id, prior_version_id, row["current_content_hash"], post
            )
        return PendingPositive(
            post=post,
            post_id=post_id,
            prior_feed_state=FeedState(str(row["feed_state"])),
            prior_version_id=prior_version_id,
            version_id=version_id,
            content_changed=changed,
        )

    def _insert_positive_observation(self, fetch_run_id: int, positive: PendingPositive) -> None:
        self.connection.execute(
            """
            INSERT INTO post_observations(
                fetch_run_id, post_id, observed_at, present, content_hash,
                content_fidelity, version_id
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                fetch_run_id,
                positive.post_id,
                positive.post.observed_at,
                positive.post.content_hash,
                positive.post.content_fidelity,
                positive.version_id,
            ),
        )

    def _insert_positive_events(self, fetch_run_id: int, positive: PendingPositive) -> None:
        if positive.content_changed:
            self._insert_event(
                positive.post_id,
                EventDimension.CONTENT,
                positive.prior_version_id,
                positive.version_id,
                positive.post.observed_at,
                evidence_fetch_run_id=fetch_run_id,
                from_version_id=positive.prior_version_id,
                to_version_id=positive.version_id,
            )
        if positive.prior_feed_state is not FeedState.PRESENT:
            self._insert_event(
                positive.post_id,
                EventDimension.FEED_STATE,
                positive.prior_feed_state,
                FeedState.PRESENT,
                positive.post.observed_at,
                evidence_fetch_run_id=fetch_run_id,
            )

    def _prepare_negative_inferences(
        self,
        fetch_run_id: int,
        run: FeedRun,
        seen_post_ids: set[int],
    ) -> list[PendingProjection]:
        if run.covered_from is None or run.covered_to is None:
            raise ValueError("healthy feed run requires covered_from and covered_to")
        candidates = self.connection.execute(
            """
            SELECT id, posted_at_claimed, feed_state, absent_healthy_streak, watch_mode
            FROM posts WHERE author_id = ? AND platform = ? AND watch_mode != ?
            """,
            (run.author_id, run.platform, WatchMode.INACTIVE),
        ).fetchall()
        projections: list[PendingProjection] = []
        for row in candidates:
            post_id = int(row["id"])
            posted_at = row["posted_at_claimed"]
            if post_id in seen_post_ids or posted_at is None:
                continue
            prior_state = FeedState(str(row["feed_state"]))
            prior_watch = WatchMode(str(row["watch_mode"]))
            if timestamp_in_closed_range(str(posted_at), run.covered_from, run.covered_to):
                streak = int(row["absent_healthy_streak"]) + 1
                next_state = (
                    FeedState.ABSENT_CONFIRMED
                    if streak >= self.settings.absent_threshold_n
                    else prior_state
                )
                self.connection.execute(
                    """
                    INSERT INTO post_observations(
                        fetch_run_id, post_id, observed_at, present, content_hash,
                        content_fidelity, version_id
                    ) VALUES (?, ?, ?, 0, NULL, ?, NULL)
                    """,
                    (fetch_run_id, post_id, run.finished_at, ContentFidelity.NA),
                )
                events: list[tuple[EventDimension, str, str]] = []
                if next_state is not prior_state:
                    events.append((EventDimension.FEED_STATE, prior_state, next_state))
                    self._insert_event(
                        post_id,
                        EventDimension.FEED_STATE,
                        prior_state,
                        next_state,
                        run.finished_at,
                        evidence_fetch_run_id=fetch_run_id,
                    )
                if next_state is FeedState.ABSENT_CONFIRMED:
                    self._enqueue_recheck(
                        post_id,
                        QueueReason.RECENT_FEED_ABSENT,
                        run.finished_at,
                        _plus_days(run.finished_at, self.settings.recent_feed_absent_ttl_days),
                    )
                projections.append(
                    PendingProjection(
                        post_id=post_id,
                        feed_state=next_state,
                        absent_healthy_streak=streak,
                        events=events,
                    )
                )
            elif parse_utc_timestamp(str(posted_at)) < parse_utc_timestamp(run.covered_from):
                events = []
                if prior_state is not FeedState.OUT_OF_SCOPE:
                    events.append((EventDimension.FEED_STATE, prior_state, FeedState.OUT_OF_SCOPE))
                    self._insert_event(
                        post_id,
                        EventDimension.FEED_STATE,
                        prior_state,
                        FeedState.OUT_OF_SCOPE,
                        run.finished_at,
                        evidence_fetch_run_id=fetch_run_id,
                    )
                next_watch = prior_watch
                if prior_watch is not WatchMode.PINNED:
                    next_watch = WatchMode.INACTIVE
                    events.append((EventDimension.WATCH_MODE, prior_watch, next_watch))
                    self._insert_event(
                        post_id,
                        EventDimension.WATCH_MODE,
                        prior_watch,
                        next_watch,
                        run.finished_at,
                        evidence_fetch_run_id=fetch_run_id,
                    )
                projections.append(
                    PendingProjection(
                        post_id=post_id,
                        feed_state=FeedState.OUT_OF_SCOPE,
                        absent_healthy_streak=int(row["absent_healthy_streak"]),
                        watch_mode=next_watch,
                        events=events,
                    )
                )
        return projections

    def _apply_positive_projection(self, positive: PendingPositive) -> None:
        post = positive.post
        if post.content_fidelity is ContentFidelity.FULL:
            self.connection.execute(
                """
                UPDATE posts SET last_present_at = ?, feed_state = ?, absent_healthy_streak = 0,
                    current_version_id = ?, current_content_hash = ?, posted_at_claimed = ?,
                    url = ?, raw_meta = ?
                WHERE id = ?
                """,
                (
                    post.observed_at,
                    FeedState.PRESENT,
                    positive.version_id,
                    post.content_hash,
                    post.posted_at_claimed,
                    post.url,
                    _json(post.raw_meta),
                    positive.post_id,
                ),
            )
        else:
            self.connection.execute(
                """
                UPDATE posts SET last_present_at = ?, feed_state = ?, absent_healthy_streak = 0,
                    posted_at_claimed = COALESCE(?, posted_at_claimed),
                    url = COALESCE(?, url), raw_meta = COALESCE(?, raw_meta)
                WHERE id = ?
                """,
                (
                    post.observed_at,
                    FeedState.PRESENT,
                    post.posted_at_claimed,
                    post.url,
                    _json(post.raw_meta),
                    positive.post_id,
                ),
            )

    def _apply_negative_projection(self, projection: PendingProjection) -> None:
        if projection.watch_mode is None:
            self.connection.execute(
                """
                UPDATE posts SET feed_state = ?, absent_healthy_streak = ?
                WHERE id = ?
                """,
                (
                    projection.feed_state,
                    projection.absent_healthy_streak,
                    projection.post_id,
                ),
            )
        else:
            self.connection.execute(
                """
                UPDATE posts SET feed_state = ?, absent_healthy_streak = ?, watch_mode = ?
                WHERE id = ?
                """,
                (
                    projection.feed_state,
                    projection.absent_healthy_streak,
                    projection.watch_mode,
                    projection.post_id,
                ),
            )

    def _append_version_if_changed(
        self,
        post_id: int,
        prior_version_id: int | None,
        prior_content_hash: object,
        post: NormalizedPost,
    ) -> tuple[int, bool]:
        if post.content_text is None or post.content_hash is None:
            raise ValueError("full content is required to append a version")
        if prior_content_hash == post.content_hash:
            if prior_version_id is None:
                raise RuntimeError("content hash exists without current version")
            return prior_version_id, False
        cursor = self.connection.execute(
            """
            INSERT INTO post_versions(
                post_id, content_text, content_hash, first_observed_at, ingest_mode, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                post.content_text,
                post.content_hash,
                post.observed_at,
                post.ingest_mode,
                _json(post.raw_payload),
            ),
        )
        return _required_lastrowid(cursor), True

    def _insert_probe_run(self, run: ProbeRun, version_id: int | None) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO probe_runs(
                post_id, started_at, finished_at, observed_at, status, http_status,
                login_state, rate_limited, result, content_fidelity, observed_version_id,
                ingest_mode, adapter_version, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.post_id,
                run.started_at,
                run.finished_at,
                run.observed_at,
                run.status,
                run.http_status,
                run.login_state,
                run.rate_limited,
                run.result,
                run.content_fidelity,
                version_id,
                run.ingest_mode,
                run.adapter_version,
                run.notes,
            ),
        )
        return _required_lastrowid(cursor)

    def _apply_probe_projection(
        self,
        run: ProbeRun,
        source_state: SourceState,
        version_id: int | None,
        observed_post: NormalizedPost | None,
    ) -> None:
        if observed_post is not None:
            self.connection.execute(
                """
                UPDATE posts SET source_state = ?, source_checked_at = ?,
                    current_version_id = ?, current_content_hash = ?
                WHERE id = ?
                """,
                (
                    source_state,
                    run.observed_at,
                    version_id,
                    observed_post.content_hash,
                    run.post_id,
                ),
            )
        else:
            self.connection.execute(
                "UPDATE posts SET source_state = ?, source_checked_at = ? WHERE id = ?",
                (source_state, run.observed_at, run.post_id),
            )

    def _next_source_state(self, current: SourceState, result: ProbeResult) -> SourceState:
        if result is ProbeResult.REACHABLE:
            return SourceState.REACHABLE
        if result is ProbeResult.EXPLICITLY_REMOVED:
            return SourceState.GONE_CONFIRMED
        if result in (ProbeResult.RESTRICTED, ProbeResult.NOT_FOUND):
            if current is SourceState.GONE_CONFIRMED:
                return current
            return SourceState.UNAVAILABLE
        return current

    def _enqueue_recheck(
        self,
        post_id: int,
        reason: QueueReason,
        enqueued_at: str,
        expires_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO recheck_queue(post_id, reason, enqueued_at, expires_at, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, reason, enqueued_at, expires_at, QueueState.PENDING),
        )

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

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return None if value is None else int(str(value))
