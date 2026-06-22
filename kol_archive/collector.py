"""Single-process Xueqiu feed polling and direct-link rechecks."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from kol_archive.adapters.xueqiu import (
    ADAPTER_VERSION,
    LOGIN_EXPIRED_CODE,
    FeedPage,
    parse_feed_page,
    parse_probe_response,
    response_failure_note,
)
from kol_archive.models import (
    BACKFILL_PAGES_NOTE,
    REQUEST_BUDGET_EXHAUSTED_NOTE,
    TIMELINE_HEAD_DAILY_OBSERVED_NOTE,
    TIMELINE_HEAD_UNCHANGED_NOTE,
    TIMELINE_PARSE_FAILED_NOTE,
    FeedRun,
    IngestMode,
    LoginState,
    NormalizedPost,
    ProbeRun,
    RunStatus,
)
from kol_archive.obs import http_client
from kol_archive.service import Archive
from kol_archive.time import parse_utc_timestamp, timestamp_at_or_before

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://xueqiu.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": f"{BASE_URL}/",
}


@dataclass(frozen=True)
class CollectorSettings:
    request_min_interval_seconds: float = 2.5
    request_jitter_seconds: float = 1.5
    max_feed_pages: int = 20
    max_feed_requests_per_run: int | None = None
    # Per-run cap on direct rechecks. The recheck lifecycle can make a large backlog of
    # posts due at once (e.g. the first run after enabling it), and each probe is a slow
    # browser round-trip; this bounds a run's duration and lets the backlog drain across
    # runs, most-overdue first. None = unlimited (used in tests).
    max_probes_per_run: int | None = None

    def __post_init__(self) -> None:
        if self.request_min_interval_seconds < 0:
            raise ValueError("request_min_interval_seconds must not be negative")
        if self.request_jitter_seconds < 0:
            raise ValueError("request_jitter_seconds must not be negative")
        if self.max_feed_pages < 1:
            raise ValueError("max_feed_pages must be positive")
        if self.max_feed_requests_per_run is not None and self.max_feed_requests_per_run < 1:
            raise ValueError("max_feed_requests_per_run must be positive")
        if self.max_probes_per_run is not None and self.max_probes_per_run < 1:
            raise ValueError("max_probes_per_run must be positive")


@dataclass(frozen=True)
class _FeedFetch:
    """Raw outcome of paging a feed, before ingest-mode-specific run assembly."""

    started_at: str
    finished_at: str
    status: RunStatus
    login_state: LoginState
    rate_limited: bool
    http_error_count: int
    parse_failure_count: int
    pages_fetched: int
    pagination_complete: bool
    covered_from: str | None
    covered_to: str | None
    notes: str | None
    reached_timeline_end: bool = False
    posts: list[NormalizedPost] = field(default_factory=list)
    seen_platform_post_ids: list[str] = field(default_factory=list)
    head_platform_post_id: str | None = None
    head_posted_at: str | None = None
    head_unchanged: bool = False
    head_observed: bool = False


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _json_payload(response: httpx.Response) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = response.json()
    except ValueError:
        return None, "response_not_json"
    if not isinstance(payload, dict):
        return None, "response_json_not_object"
    return cast(dict[str, Any], payload), None


def create_xueqiu_client(cookie: str | None, *, timeout_seconds: float = 20.0) -> httpx.Client:
    client = http_client(headers=HEADERS, timeout=timeout_seconds, follow_redirects=False)
    try:
        client.get(f"{BASE_URL}/")
    except BaseException:
        client.close()
        raise
    if cookie:
        for part in cookie.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            client.cookies.set(key.strip(), value.strip(), domain=".xueqiu.com")
    return client


class XueqiuCollector:
    def __init__(
        self,
        archive: Archive,
        client: httpx.Client,
        settings: CollectorSettings | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.archive = archive
        self.client = client
        self.settings = settings or CollectorSettings()
        self.sleep = sleep
        self.clock = clock
        self._feed_requests_made = 0

    def poll_feed(
        self,
        author_id: int,
        platform_uid: str,
        window_started_at: str,
        *,
        previous_covered_to: str | None = None,
        known_head: tuple[str | None, str | None] = (None, None),
        observe_unchanged_head: bool = False,
    ) -> int:
        """Live feed poll: page back only until it reconnects with known content.

        A live poll is incremental: it stops at the first page that reaches back to
        the newest post the prior live run already archived (``previous_covered_to``),
        so a routine 15-minute poll fetches one page instead of re-sweeping a fixed
        window. Deletion and edit detection no longer ride on feed depth; per-post
        direct rechecks (Track B) own that now.

        ``window_started_at`` is only the seed bound for the *first* poll of a brand
        new author, when there is nothing to reconnect with yet.

        If coverage does not page back far enough to reconnect with
        ``previous_covered_to`` (a gap left by a long interval or a prolific author),
        the run is downgraded to ``partial`` so the health gate suppresses any
        negative inference over the hole.
        """
        overlap_anchor = (
            previous_covered_to if previous_covered_to is not None else window_started_at
        )
        fetch = self._fetch_feed(
            author_id,
            platform_uid,
            ingest_mode=IngestMode.LIVE,
            page_budget=self.settings.max_feed_pages,
            target_reached=lambda page: page.covers_window(overlap_anchor),
            cap_note="max_feed_pages_reached",
            known_head=known_head,
            observe_unchanged_head=observe_unchanged_head,
        )
        status = fetch.status
        pagination_complete = fetch.pagination_complete
        notes = fetch.notes
        if (
            previous_covered_to is not None
            and fetch.covered_from is not None
            and not timestamp_at_or_before(fetch.covered_from, previous_covered_to)
        ):
            status = RunStatus.PARTIAL
            pagination_complete = False
            notes = notes or "coverage_gap"
        return self._record_feed_run(
            author_id, fetch, IngestMode.LIVE, status, pagination_complete, notes
        )

    def backfill_feed(
        self,
        author_id: int,
        platform_uid: str,
        *,
        max_pages: int,
        until: str | None = None,
        start_page: int = 1,
    ) -> int:
        """Historical backfill: archive up to ``max_pages`` (or back to ``until``).

        Paging begins at ``start_page`` so an auto-backfill can resume *past* the
        pages a live poll already covered, reaching genuinely older posts instead
        of re-requesting the recent window. Runs as ``ingest_mode=backfill`` so the
        archive records only the positive history and never infers
        absence/out_of_scope from it (charter rule 9).
        """
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        if start_page < 1:
            raise ValueError("start_page must be positive")
        # Validate ``until`` here, not lazily inside target_reached: a short timeline that
        # ends via ``page >= max_page`` never compares against it, so a malformed bound
        # would otherwise be silently accepted.
        if until is not None:
            parse_utc_timestamp(until)

        def target_reached(page: FeedPage) -> bool:
            if page.page >= page.max_page:
                return True
            if until is not None and page.covered_from is not None:
                return timestamp_at_or_before(page.covered_from, until)
            return False

        fetch = self._fetch_feed(
            author_id,
            platform_uid,
            ingest_mode=IngestMode.BACKFILL,
            page_budget=max_pages,
            target_reached=target_reached,
            cap_note=BACKFILL_PAGES_NOTE,
            start_page=start_page,
        )
        return self._record_feed_run(
            author_id,
            fetch,
            IngestMode.BACKFILL,
            fetch.status,
            fetch.pagination_complete,
            fetch.notes,
        )

    def _fetch_feed(
        self,
        author_id: int,
        platform_uid: str,
        *,
        ingest_mode: IngestMode,
        page_budget: int,
        target_reached: Callable[[FeedPage], bool],
        cap_note: str,
        start_page: int = 1,
        known_head: tuple[str | None, str | None] = (None, None),
        observe_unchanged_head: bool = False,
    ) -> _FeedFetch:
        started_at = self.clock()
        observed_at = started_at
        posts_by_id: dict[str, NormalizedPost] = {}
        parsed_pages: list[FeedPage] = []
        parse_failure_count = 0
        status = RunStatus.OK
        login_state = LoginState.VALID
        rate_limited = False
        http_error_count = 0
        notes: str | None = None
        page_number = start_page
        pagination_complete = False
        reached_timeline_end = False
        seen_platform_post_ids: set[str] = set()
        head_platform_post_id: str | None = None
        head_posted_at: str | None = None
        head_unchanged = False
        head_observed = False
        while True:
            if self.feed_request_budget_exhausted():
                status = RunStatus.PARTIAL
                notes = REQUEST_BUDGET_EXHAUSTED_NOTE
                break
            try:
                self._feed_requests_made += 1
                response = self.client.get(
                    f"{BASE_URL}/v4/statuses/user_timeline.json",
                    params={"user_id": platform_uid, "page": page_number},
                )
            except httpx.HTTPError:
                status = RunStatus.FAILED if not parsed_pages else RunStatus.PARTIAL
                login_state = LoginState.UNKNOWN
                http_error_count += 1
                notes = "http_error"
                break
            payload, payload_issue = _json_payload(response)
            error_code = None if payload is None else str(payload.get("error_code") or "")
            if response.status_code == 429:
                status = RunStatus.PARTIAL
                login_state = LoginState.UNKNOWN
                rate_limited = True
                http_error_count += 1
                notes = "http_429"
                break
            if error_code == LOGIN_EXPIRED_CODE:
                status = RunStatus.PARTIAL
                login_state = LoginState.EXPIRED
                http_error_count += 1
                notes = f"error_code={LOGIN_EXPIRED_CODE}"
                break
            if response.status_code != 200 or payload is None:
                status = RunStatus.FAILED if not parsed_pages else RunStatus.PARTIAL
                login_state = LoginState.UNKNOWN
                http_error_count += 1
                notes = response_failure_note(response.status_code, payload_issue)
                break
            try:
                if (
                    ingest_mode is IngestMode.LIVE
                    and page_number == 1
                    and start_page == 1
                    and known_head[0] is not None
                ):
                    preview = parse_feed_page(
                        payload,
                        author_id=author_id,
                        observed_at=observed_at,
                        ingest_mode=ingest_mode,
                        should_observe=lambda _post_id: False,
                    )
                    head_platform_post_id = preview.head_platform_post_id
                    head_posted_at = preview.head_posted_at
                    if self._head_matches_known(preview, known_head):
                        if not observe_unchanged_head:
                            parsed_pages.append(preview)
                            seen_platform_post_ids.update(preview.seen_platform_post_ids)
                            parse_failure_count += preview.parse_failure_count
                            status = RunStatus.OK
                            pagination_complete = False
                            reached_timeline_end = preview.page >= preview.max_page
                            notes = TIMELINE_HEAD_UNCHANGED_NOTE
                            head_unchanged = True
                            break
                        parsed = parse_feed_page(
                            payload,
                            author_id=author_id,
                            observed_at=observed_at,
                            ingest_mode=ingest_mode,
                            should_observe=lambda post_id: self.archive.should_observe_feed_post(
                                "xueqiu", author_id, post_id, observed_at
                            ),
                        )
                        parsed_pages.append(parsed)
                        seen_platform_post_ids.update(parsed.seen_platform_post_ids)
                        posts_by_id.update((post.platform_post_id, post) for post in parsed.posts)
                        parse_failure_count += parsed.parse_failure_count
                        status = RunStatus.OK
                        pagination_complete = False
                        reached_timeline_end = parsed.page >= parsed.max_page
                        notes = TIMELINE_HEAD_DAILY_OBSERVED_NOTE
                        head_unchanged = True
                        head_observed = True
                        break
                parsed = parse_feed_page(
                    payload,
                    author_id=author_id,
                    observed_at=observed_at,
                    ingest_mode=ingest_mode,
                    should_observe=lambda post_id: self.archive.should_observe_feed_post(
                        "xueqiu", author_id, post_id, observed_at
                    ),
                )
            except ValueError:
                status = RunStatus.PARTIAL
                notes = TIMELINE_PARSE_FAILED_NOTE
                break
            parsed_pages.append(parsed)
            if ingest_mode is IngestMode.LIVE and page_number == 1 and start_page == 1:
                head_observed = True
            seen_platform_post_ids.update(parsed.seen_platform_post_ids)
            head_platform_post_id = head_platform_post_id or parsed.head_platform_post_id
            head_posted_at = head_posted_at or parsed.head_posted_at
            posts_by_id.update((post.platform_post_id, post) for post in parsed.posts)
            parse_failure_count += parsed.parse_failure_count
            if target_reached(parsed):
                pagination_complete = True
                # Distinguish "paged to the actual end of the timeline" from "covered the
                # recent window / hit the until bound": only the former means there is no
                # older history left to backfill.
                reached_timeline_end = parsed.page >= parsed.max_page
                break
            if len(parsed_pages) >= page_budget:
                status = RunStatus.PARTIAL
                notes = cap_note
                break
            page_number += 1
            self._wait()

        finished_at = self.clock()
        covered_from = min(
            (page.covered_from for page in parsed_pages if page.covered_from is not None),
            default=None,
        )
        covered_to = max(
            (page.covered_to for page in parsed_pages if page.covered_to is not None),
            default=None,
        )
        return _FeedFetch(
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            login_state=login_state,
            rate_limited=rate_limited,
            http_error_count=http_error_count,
            parse_failure_count=parse_failure_count,
            pages_fetched=len(parsed_pages),
            pagination_complete=pagination_complete,
            reached_timeline_end=reached_timeline_end,
            covered_from=covered_from,
            covered_to=covered_to,
            notes=notes,
            posts=list(posts_by_id.values()),
            seen_platform_post_ids=sorted(seen_platform_post_ids),
            head_platform_post_id=head_platform_post_id,
            head_posted_at=head_posted_at,
            head_unchanged=head_unchanged,
            head_observed=head_observed,
        )

    def feed_request_budget_exhausted(self) -> bool:
        budget = self.settings.max_feed_requests_per_run
        return budget is not None and self._feed_requests_made >= budget

    @staticmethod
    def _head_matches_known(page: FeedPage, known_head: tuple[str | None, str | None]) -> bool:
        known_id, known_posted_at = known_head
        if known_id is None or page.head_platform_post_id != known_id:
            return False
        return known_posted_at is None or page.head_posted_at == known_posted_at

    def _record_feed_run(
        self,
        author_id: int,
        fetch: _FeedFetch,
        ingest_mode: IngestMode,
        status: RunStatus,
        pagination_complete: bool,
        notes: str | None,
    ) -> int:
        run_id = self.archive.record_feed_run(
            FeedRun(
                author_id=author_id,
                platform="xueqiu",
                started_at=fetch.started_at,
                finished_at=fetch.finished_at,
                status=status,
                login_state=fetch.login_state,
                pages_fetched=fetch.pages_fetched,
                pagination_complete=pagination_complete,
                covered_from=fetch.covered_from,
                covered_to=fetch.covered_to,
                rate_limited=fetch.rate_limited,
                http_error_count=fetch.http_error_count,
                ingest_mode=ingest_mode,
                adapter_version=ADAPTER_VERSION,
                parse_failure_count=fetch.parse_failure_count,
                reached_timeline_end=fetch.reached_timeline_end,
                notes=notes,
            ),
            fetch.posts,
            seen_platform_post_ids=fetch.seen_platform_post_ids,
        )
        if ingest_mode is IngestMode.LIVE and fetch.head_platform_post_id is not None:
            self.archive.record_author_feed_head(
                author_id, fetch.head_platform_post_id, fetch.head_posted_at
            )
            if fetch.head_observed:
                self.archive.mark_author_head_observed(author_id, fetch.finished_at)
        return run_id

    def probe_due_posts(self) -> list[int]:
        run_ids: list[int] = []
        for target in self.archive.probe_targets(
            self.clock(), limit=self.settings.max_probes_per_run
        ):
            started_at = self.clock()
            payload: dict[str, Any] | None = None
            try:
                response = self.client.get(
                    f"{BASE_URL}/statuses/show.json",
                    params={"id": target.platform_post_id},
                )
                payload, payload_issue = _json_payload(response)
                parsed = parse_probe_response(
                    response.status_code,
                    payload,
                    author_id=target.author_id,
                    observed_at=started_at,
                    payload_issue=payload_issue,
                )
                http_status = response.status_code
            except httpx.HTTPError:
                parsed = parse_probe_response(
                    0,
                    None,
                    author_id=target.author_id,
                    observed_at=started_at,
                )
                http_status = None
            finished_at = self.clock()
            run_ids.append(
                self.archive.record_probe_run(
                    ProbeRun(
                        post_id=target.post_id,
                        started_at=started_at,
                        finished_at=finished_at,
                        observed_at=started_at,
                        status=parsed.status,
                        http_status=http_status,
                        login_state=parsed.login_state,
                        rate_limited=parsed.rate_limited,
                        result=parsed.result,
                        content_fidelity=parsed.content_fidelity,
                        ingest_mode=IngestMode.LIVE,
                        adapter_version=ADAPTER_VERSION,
                        notes=parsed.notes,
                    ),
                    parsed.observed_post,
                )
            )
            if parsed.rate_limited:
                break
            self._wait()
        return run_ids

    def _wait(self) -> None:
        delay = self.settings.request_min_interval_seconds + random.uniform(
            0, self.settings.request_jitter_seconds
        )
        self.sleep(delay)
