"""Manual curation: pinning, attention log, and rewrite exercises."""

from __future__ import annotations

from kol_archive.models import EventDimension, QueueReason, QueueState, RewriteSource, WatchMode
from kol_archive.time import parse_utc_timestamp

from .base import ArchiveBase, _required_lastrowid


class CurationMixin(ArchiveBase):
    def pin_post(
        self,
        post_id: int,
        detected_at: str,
        *,
        confirm_reason: QueueReason | None = None,
    ) -> None:
        with self._transaction():
            self._pin_post(post_id, detected_at, confirm_reason=confirm_reason)

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

    def unpin_post_for_window(
        self,
        post_id: int,
        detected_at: str,
        window_started_at: str,
    ) -> None:
        row = self._get_post(post_id)
        posted_at = row["posted_at_claimed"]
        within_recent_window = posted_at is not None and parse_utc_timestamp(
            str(posted_at)
        ) >= parse_utc_timestamp(window_started_at)
        self.unpin_post(post_id, detected_at, within_recent_window=within_recent_window)

    def add_attention(
        self,
        post_id: int,
        version_id: int,
        triggered_at: str,
        my_reason: str,
        my_expectation: str | None = None,
    ) -> int:
        if not my_reason.strip():
            raise ValueError("attention reason must not be empty")
        with self._transaction():
            post = self._get_post(post_id)
            self._get_post_version(post_id, version_id)
            cursor = self.connection.execute(
                """
                INSERT INTO attention_log(
                    author_id, post_id, version_id, triggered_at, my_reason, my_expectation
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(post["author_id"]),
                    post_id,
                    version_id,
                    triggered_at,
                    my_reason.strip(),
                    None if my_expectation is None else my_expectation.strip() or None,
                ),
            )
            self._pin_post(post_id, triggered_at)
        return _required_lastrowid(cursor)

    def add_rewrite_exercise(
        self,
        source: RewriteSource,
        llm_rewritten_claim: str,
        llm_rationale: str,
        model: str,
        prompt_version: str,
        created_at: str,
    ) -> int:
        values = {
            "rewritten claim": llm_rewritten_claim,
            "rationale": llm_rationale,
            "model": model,
            "prompt version": prompt_version,
        }
        for label, value in values.items():
            if not value.strip():
                raise ValueError(f"{label} must not be empty")
        with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO rewrite_exercises(
                    post_id, version_id, original_text, llm_rewritten_claim, llm_rationale,
                    model, prompt_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.post_id,
                    source.version_id,
                    source.original_text,
                    llm_rewritten_claim.strip(),
                    llm_rationale.strip(),
                    model.strip(),
                    prompt_version.strip(),
                    created_at,
                ),
            )
            self._pin_post(source.post_id, created_at)
        return _required_lastrowid(cursor)

    def review_rewrite_exercise(self, exercise_id: int, verdict: str) -> None:
        if verdict not in {"valid", "too_vague", "wrong"}:
            raise ValueError("rewrite verdict must be valid, too_vague, or wrong")
        with self._transaction():
            cursor = self.connection.execute(
                "UPDATE rewrite_exercises SET my_verdict = ? WHERE id = ?",
                (verdict, exercise_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"unknown rewrite exercise id: {exercise_id}")

    def current_version_id(self, post_id: int) -> int:
        row = self._get_post(post_id)
        version_id = self._optional_int(row["current_version_id"])
        if version_id is None:
            raise ValueError(f"post has no full-content version: {post_id}")
        return version_id

    def rewrite_source(self, post_id: int, version_id: int) -> RewriteSource:
        version = self._get_post_version(post_id, version_id)
        return RewriteSource(
            post_id=post_id,
            version_id=version_id,
            original_text=str(version["content_text"]),
        )

    def _pin_post(
        self,
        post_id: int,
        detected_at: str,
        *,
        confirm_reason: QueueReason | None = None,
    ) -> None:
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
