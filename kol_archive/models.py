"""Typed contracts shared by adapters and the archive state machine."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class LoginState(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class IngestMode(StrEnum):
    LIVE = "live"
    BACKFILL = "backfill"


class ContentFidelity(StrEnum):
    FULL = "full"
    PREVIEW = "preview"
    NA = "na"


class FeedState(StrEnum):
    PRESENT = "present"
    ABSENT_CONFIRMED = "absent_confirmed"
    OUT_OF_SCOPE = "out_of_scope"
    UNKNOWN = "unknown"


class SourceState(StrEnum):
    REACHABLE = "reachable"
    GONE_CONFIRMED = "gone_confirmed"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class WatchMode(StrEnum):
    RECENT_WINDOW = "recent_window"
    PINNED = "pinned"
    INACTIVE = "inactive"


class ProbeResult(StrEnum):
    REACHABLE = "reachable"
    EXPLICITLY_REMOVED = "explicitly_removed"
    RESTRICTED = "restricted"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


# fetch_runs.notes sentinel: a backfill stopped because it hit its page budget.
# This is a *planned* stop (the baseline reached its configured depth), distinct
# from a collection failure (rate limiting / HTTP / parse errors). The archive
# uses it to tell "baseline established" from "retry needed". Shared here so the
# collector (writer) and the service (reader) agree without a circular import.
BACKFILL_PAGES_NOTE = "backfill_pages_reached"

# A page that could not be parsed at all (the adapter raised), as opposed to a
# page that parsed with some un-parseable entries (counted in parse_failure_count).
# Shared so the collector (writer) and the service (reader) agree on the note text.
TIMELINE_PARSE_FAILED_NOTE = "timeline_parse_failed"


class EventDimension(StrEnum):
    FEED_STATE = "feed_state"
    SOURCE_STATE = "source_state"
    WATCH_MODE = "watch_mode"
    CONTENT = "content"


class QueueReason(StrEnum):
    LLM_CANDIDATE = "llm_candidate"
    RECENT_FEED_ABSENT = "recent_feed_absent"


class QueueState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class NormalizedPost:
    """Platform-neutral post content returned by an adapter."""

    platform_post_id: str
    author_id: int
    observed_at: str
    content_fidelity: ContentFidelity
    content_text: str | None = None
    content_hash: str | None = None
    posted_at_claimed: str | None = None
    url: str | None = None
    ingest_mode: IngestMode = IngestMode.LIVE
    raw_payload: dict[str, Any] | None = None
    raw_meta: dict[str, Any] | None = None

    def validate(self) -> None:
        if not self.platform_post_id:
            raise ValueError("platform_post_id must not be empty")
        if self.content_fidelity is ContentFidelity.FULL:
            if self.content_text is None or self.content_hash is None:
                raise ValueError("full fidelity posts require content_text and content_hash")
        elif self.content_hash is not None:
            raise ValueError("content_hash is only valid for full fidelity posts")


@dataclass(frozen=True)
class FeedRun:
    author_id: int
    platform: str
    started_at: str
    finished_at: str
    status: RunStatus
    login_state: LoginState
    pages_fetched: int
    pagination_complete: bool
    covered_from: str | None
    covered_to: str | None
    rate_limited: bool
    http_error_count: int
    ingest_mode: IngestMode
    adapter_version: str
    parse_failure_count: int = 0
    reached_timeline_end: bool = False
    notes: str | None = None

    def with_effective_status(self, posts: list[NormalizedPost]) -> FeedRun:
        has_degraded_post = any(post.content_fidelity is ContentFidelity.NA for post in posts)
        if self.status is RunStatus.OK and (self.parse_failure_count or has_degraded_post):
            return replace(self, status=RunStatus.PARTIAL)
        return self


@dataclass(frozen=True)
class ProbeRun:
    post_id: int
    started_at: str
    finished_at: str
    observed_at: str
    status: RunStatus
    http_status: int | None
    login_state: LoginState
    rate_limited: bool
    result: ProbeResult
    content_fidelity: ContentFidelity
    ingest_mode: IngestMode
    adapter_version: str
    notes: str | None = None


@dataclass(frozen=True)
class ArchiveSettings:
    absent_threshold_n: int = 3
    recent_feed_absent_ttl_days: int = 7

    def __post_init__(self) -> None:
        if self.absent_threshold_n < 3:
            raise ValueError("absent_threshold_n must be at least 3")
        if self.recent_feed_absent_ttl_days < 1:
            raise ValueError("recent_feed_absent_ttl_days must be positive")


@dataclass(frozen=True)
class PendingPositive:
    post: NormalizedPost
    post_id: int
    prior_feed_state: FeedState
    prior_version_id: int | None
    version_id: int | None
    content_changed: bool


@dataclass(frozen=True)
class PendingProjection:
    post_id: int
    feed_state: FeedState
    absent_healthy_streak: int
    watch_mode: WatchMode | None = None
    events: list[tuple[EventDimension, str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ProbeTarget:
    post_id: int
    author_id: int
    platform_post_id: str


@dataclass(frozen=True)
class RewriteSource:
    post_id: int
    version_id: int
    original_text: str
