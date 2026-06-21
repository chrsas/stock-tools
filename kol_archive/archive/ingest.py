"""Atomic archive writes for feed polling and direct-link probes."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from kol_archive.market import extract_market_tickers
from kol_archive.models import (
    BACKFILL_PAGES_NOTE,
    REQUEST_BUDGET_EXHAUSTED_NOTE,
    TIMELINE_HEAD_DAILY_OBSERVED_NOTE,
    TIMELINE_HEAD_UNCHANGED_NOTE,
    TIMELINE_PARSE_FAILED_NOTE,
    ContentFidelity,
    EventDimension,
    FeedRun,
    FeedState,
    IngestMode,
    LoginState,
    NormalizedPost,
    PendingPositive,
    PendingProjection,
    ProbeResult,
    ProbeRun,
    ProbeTarget,
    QueueReason,
    QueueState,
    SourceState,
    WatchMode,
)
from kol_archive.time import parse_utc_timestamp, timestamp_in_closed_range

from .base import (
    LOGGER,
    ArchiveBase,
    _json,
    _plus_days,
    _required_lastrowid,
    is_healthy_feed_run,
    is_healthy_probe_run,
)


class IngestMixin(ArchiveBase):
    def last_live_covered_to(self, author_id: int) -> str | None:
        """Newest feed-observed post time from prior live runs, for continuity checks."""
        row = self.connection.execute(
            """
            SELECT MAX(covered_to) AS covered_to FROM fetch_runs
            WHERE author_id = ? AND ingest_mode = ? AND covered_to IS NOT NULL
            """,
            (author_id, IngestMode.LIVE),
        ).fetchone()
        return None if row is None or row["covered_to"] is None else str(row["covered_to"])

    def feed_run_pages(self, fetch_run_id: int) -> int:
        """Pages fetched by one recorded feed run (used to resume backfill past it)."""
        row = self.connection.execute(
            "SELECT pages_fetched FROM fetch_runs WHERE id = ?",
            (fetch_run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown fetch run id: {fetch_run_id}")
        return int(row["pages_fetched"])

    def feed_run_parse_clean(self, fetch_run_id: int) -> bool:
        """True if a feed run parsed every page cleanly (no degraded/un-parseable pages).

        A live run whose last page is degraded — some entries un-parseable
        (``parse_failure_count > 0``) or the whole page un-parseable
        (``notes = TIMELINE_PARSE_FAILED_NOTE``) — cannot be trusted to have located
        the real end of the timeline, so its page count must not seed an auto-backfill's
        start page. Such a run leaves the baseline pending for a later clean retry.
        """
        row = self.connection.execute(
            "SELECT parse_failure_count, notes FROM fetch_runs WHERE id = ?",
            (fetch_run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown fetch run id: {fetch_run_id}")
        return int(row["parse_failure_count"]) == 0 and row["notes"] != TIMELINE_PARSE_FAILED_NOTE

    def feed_run_note(self, fetch_run_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT notes FROM fetch_runs WHERE id = ?", (fetch_run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown fetch run id: {fetch_run_id}")
        return None if row["notes"] is None else str(row["notes"])

    def feed_run_head_unchanged(self, fetch_run_id: int) -> bool:
        return self.feed_run_note(fetch_run_id) in {
            TIMELINE_HEAD_DAILY_OBSERVED_NOTE,
            TIMELINE_HEAD_UNCHANGED_NOTE,
        }

    def feed_run_request_budget_exhausted(self, fetch_run_id: int) -> bool:
        return self.feed_run_note(fetch_run_id) == REQUEST_BUDGET_EXHAUSTED_NOTE

    def feed_run_blocked(self, fetch_run_id: int) -> bool:
        """True if a feed run hit rate limiting, login expiry, or transport errors.

        These mean the session/endpoint pushed back; a clean coverage gap or a
        page-budget cap (``status=partial`` with no errors) is *not* blocked. Used
        to decide whether to pile more requests (auto-backfill, direct-link probes)
        onto the same wall this run.
        """
        row = self.connection.execute(
            "SELECT rate_limited, http_error_count, login_state FROM fetch_runs WHERE id = ?",
            (fetch_run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown fetch run id: {fetch_run_id}")
        return bool(
            row["rate_limited"]
            or int(row["http_error_count"]) > 0
            or str(row["login_state"]) != LoginState.VALID
        )

    def baseline_backfill_pending(self, author_id: int) -> bool:
        """True until the history baseline is established for this author.

        The baseline counts as established by a clean run (no parse failures) that is
        one of:

        * a backfill that made a planned stop — paged to the end of the timeline or
          stopped at its configured page budget (``notes = BACKFILL_PAGES_NOTE``); or
        * any feed run (live or backfill) that paged to the actual end of the
          timeline (``reached_timeline_end``). A short-timeline account whose live
          poll already reaches the end has no older history to backfill, so we must
          not keep requesting out-of-range pages forever.

        Runs that ended on a collection failure — rate limiting, HTTP/network errors,
        login expiry — or that returned degraded (un-parseable) pages leave the
        baseline pending so a later run retries instead of skipping on partial data.
        """
        row = self.connection.execute(
            """
            SELECT 1 FROM fetch_runs
            WHERE author_id = ? AND parse_failure_count = 0
              AND (
                  reached_timeline_end = 1
                  OR (ingest_mode = ? AND (pagination_complete = 1 OR notes = ?))
              )
            LIMIT 1
            """,
            (author_id, IngestMode.BACKFILL, BACKFILL_PAGES_NOTE),
        ).fetchone()
        return row is None

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

    def should_observe_feed_post(
        self, platform: str, author_id: int, platform_post_id: str, observed_at: str
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT id, feed_state, absent_healthy_streak
            FROM posts
            WHERE platform = ? AND author_id = ? AND platform_post_id = ?
            """,
            (platform, author_id, platform_post_id),
        ).fetchone()
        if row is None:
            return True
        if (
            int(row["absent_healthy_streak"]) > 0
            or FeedState(str(row["feed_state"])) is not FeedState.PRESENT
        ):
            return True
        post_id = int(row["id"])
        count_row = self.connection.execute(
            "SELECT COUNT(*) FROM post_observations WHERE post_id = ? AND present = 1",
            (post_id,),
        ).fetchone()
        if int(count_row[0]) >= self.settings.positive_observation_max_count:
            return False
        last_row = self.connection.execute(
            """
            SELECT MAX(observed_at) AS observed_at
            FROM post_observations
            WHERE post_id = ? AND present = 1
            """,
            (post_id,),
        ).fetchone()
        if last_row is None or last_row["observed_at"] is None:
            return True
        return parse_utc_timestamp(observed_at) - parse_utc_timestamp(
            str(last_row["observed_at"])
        ) >= timedelta(days=self.settings.positive_observation_interval_days)

    def record_feed_run(
        self,
        run: FeedRun,
        posts: list[NormalizedPost],
        *,
        seen_platform_post_ids: Iterable[str] | None = None,
        crash_after_evidence: bool = False,
    ) -> int:
        self._validate_feed_posts(run, posts)
        effective_run = run.with_effective_status(posts)
        with self._transaction():
            fetch_run_id = self._insert_fetch_run(effective_run)
            post_ids = {
                post.platform_post_id: self._ensure_post(run.platform, post) for post in posts
            }
            seen_post_ids = self._known_post_ids(
                run.platform, run.author_id, seen_platform_post_ids or ()
            ) | set(post_ids.values())

            positives = [
                self._prepare_positive_observation(post, post_ids[post.platform_post_id])
                for post in posts
            ]
            archived_positives = [positive for positive in positives if positive.record_observation]
            for positive in archived_positives:
                self._insert_positive_observation(fetch_run_id, positive)
            for positive in archived_positives:
                self._insert_positive_events(fetch_run_id, positive)

            projections: list[PendingProjection] = []
            # Backfill is historical archival only: it never drives absence/out_of_scope
            # inference (charter rule 9 separates backfill from live monitoring). An empty
            # timeline (a brand-new or fully-cleared account) is a healthy round but covers
            # no time range, so there is nothing to infer absence over — skip inference
            # rather than treating the missing coverage as an error.
            if (
                effective_run.ingest_mode is IngestMode.LIVE
                and is_healthy_feed_run(effective_run)
                and effective_run.covered_from is not None
                and effective_run.covered_to is not None
            ):
                projections = self._prepare_negative_inferences(
                    fetch_run_id, effective_run, seen_post_ids
                )

            if crash_after_evidence:
                raise RuntimeError("injected crash after feed evidence")

            for positive in archived_positives:
                self._apply_positive_projection(positive)
            for projection in projections:
                self._apply_negative_projection(projection)
        LOGGER.info(
            "feed_run archived run_id=%s author_id=%s status=%s healthy=%s "
            "pages_fetched=%s covered_from=%s covered_to=%s rate_limited=%s "
            "http_error_count=%s seen_posts=%s archived_present_observations=%s",
            fetch_run_id,
            effective_run.author_id,
            effective_run.status,
            is_healthy_feed_run(effective_run),
            effective_run.pages_fetched,
            effective_run.covered_from,
            effective_run.covered_to,
            effective_run.rate_limited,
            effective_run.http_error_count,
            len(seen_post_ids),
            len(archived_positives),
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
                    run.post_id,
                    prior_version_id,
                    row["current_content_hash"],
                    row["current_image_manifest_hash"],
                    observed_post,
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

    def _known_post_ids(
        self, platform: str, author_id: int, platform_post_ids: Iterable[str]
    ) -> set[int]:
        ids = sorted({str(value) for value in platform_post_ids if str(value)})
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"""
            SELECT id FROM posts
            WHERE platform = ? AND author_id = ? AND platform_post_id IN ({placeholders})
            """,
            (platform, author_id, *ids),
        ).fetchall()
        return {int(row["id"]) for row in rows}

    def _insert_fetch_run(self, run: FeedRun) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO fetch_runs(
                author_id, platform, started_at, finished_at, status, login_state,
                pages_fetched, pagination_complete, covered_from, covered_to,
                rate_limited, http_error_count, ingest_mode, adapter_version,
                parse_failure_count, reached_timeline_end, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                run.parse_failure_count,
                run.reached_timeline_end,
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
                post_id,
                prior_version_id,
                row["current_content_hash"],
                row["current_image_manifest_hash"],
                post,
            )
        prior_feed_state = FeedState(str(row["feed_state"]))
        record_observation = self._should_record_positive_observation(
            post,
            post_id,
            row,
            content_changed=changed,
            prior_feed_state=prior_feed_state,
        )
        return PendingPositive(
            post=post,
            post_id=post_id,
            prior_feed_state=prior_feed_state,
            prior_version_id=prior_version_id,
            version_id=version_id,
            content_changed=changed,
            record_observation=record_observation,
        )

    def _should_record_positive_observation(
        self,
        post: NormalizedPost,
        post_id: int,
        row: Any,
        *,
        content_changed: bool,
        prior_feed_state: FeedState,
    ) -> bool:
        if (
            post.content_fidelity is ContentFidelity.NA
            or content_changed
            or int(row["absent_healthy_streak"]) > 0
            or prior_feed_state is not FeedState.PRESENT
        ):
            return True
        observation_count = int(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM post_observations
                WHERE post_id = ? AND present = 1
                """,
                (post_id,),
            ).fetchone()[0]
        )
        if observation_count >= self.settings.positive_observation_max_count:
            return False
        observed_at = parse_utc_timestamp(post.observed_at)
        last_row = self.connection.execute(
            """
            SELECT MAX(observed_at) AS observed_at
            FROM post_observations
            WHERE post_id = ? AND present = 1
            """,
            (post_id,),
        ).fetchone()
        if last_row is None or last_row["observed_at"] is None:
            return True
        last_observed_at = parse_utc_timestamp(str(last_row["observed_at"]))
        return observed_at - last_observed_at >= timedelta(
            days=self.settings.positive_observation_interval_days
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
                    current_version_id = ?, current_content_hash = ?,
                    current_image_manifest_hash = ?, posted_at_claimed = ?,
                    url = ?, raw_meta = ?
                WHERE id = ?
                """,
                (
                    post.observed_at,
                    FeedState.PRESENT,
                    positive.version_id,
                    post.content_hash,
                    post.image_manifest_hash,
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
        prior_image_manifest_hash: object,
        post: NormalizedPost,
    ) -> tuple[int, bool]:
        if post.content_text is None or post.content_hash is None:
            raise ValueError("full content is required to append a version")
        text_changed = prior_content_hash != post.content_hash
        # NULL-tolerant: a prior manifest of NULL is a pre-feature version we never
        # back-filled, so it is not comparable — only a genuine difference between
        # two known manifests forks a version. This is why a post edited to only
        # swap/add/remove an image (text untouched) still produces a new version,
        # while the first poll after the migration does not.
        manifest_changed = (
            prior_image_manifest_hash is not None
            and post.image_manifest_hash is not None
            and prior_image_manifest_hash != post.image_manifest_hash
        )
        if not text_changed and not manifest_changed:
            if prior_version_id is None:
                raise RuntimeError("content hash exists without current version")
            return prior_version_id, False
        cursor = self.connection.execute(
            """
            INSERT INTO post_versions(
                post_id, content_text, content_hash, image_manifest_hash,
                first_observed_at, ingest_mode, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                post.content_text,
                post.content_hash,
                post.image_manifest_hash,
                post.observed_at,
                post.ingest_mode,
                _json(post.raw_payload),
            ),
        )
        version_id = _required_lastrowid(cursor)
        self.connection.executemany(
            "INSERT INTO version_tickers(version_id, ticker) VALUES (?, ?)",
            (
                (version_id, ticker)
                for ticker in sorted(
                    extract_market_tickers(post.content_text, _json(post.raw_payload))
                )
            ),
        )
        self.connection.execute(
            "INSERT INTO version_ticker_scans(version_id) VALUES (?)",
            (version_id,),
        )
        return version_id, True

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
                    current_version_id = ?, current_content_hash = ?,
                    current_image_manifest_hash = ?
                WHERE id = ?
                """,
                (
                    source_state,
                    run.observed_at,
                    version_id,
                    observed_post.content_hash,
                    observed_post.image_manifest_hash,
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
