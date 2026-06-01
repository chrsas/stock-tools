"""Single-process Xueqiu feed polling and direct-link rechecks."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
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
    FeedRun,
    IngestMode,
    LoginState,
    NormalizedPost,
    ProbeRun,
    RunStatus,
)
from kol_archive.service import Archive

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

    def __post_init__(self) -> None:
        if self.request_min_interval_seconds < 0:
            raise ValueError("request_min_interval_seconds must not be negative")
        if self.request_jitter_seconds < 0:
            raise ValueError("request_jitter_seconds must not be negative")
        if self.max_feed_pages < 1:
            raise ValueError("max_feed_pages must be positive")


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
    client = httpx.Client(headers=HEADERS, timeout=timeout_seconds, follow_redirects=False)
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

    def poll_feed(self, author_id: int, platform_uid: str, window_started_at: str) -> int:
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
        page_number = 1
        pagination_complete = False
        while True:
            try:
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
                parsed = parse_feed_page(
                    payload,
                    author_id=author_id,
                    observed_at=observed_at,
                )
            except ValueError:
                status = RunStatus.PARTIAL
                notes = "timeline_parse_failed"
                break
            parsed_pages.append(parsed)
            posts_by_id.update((post.platform_post_id, post) for post in parsed.posts)
            parse_failure_count += parsed.parse_failure_count
            if parsed.covers_window(window_started_at):
                pagination_complete = True
                break
            if page_number >= self.settings.max_feed_pages:
                status = RunStatus.PARTIAL
                notes = "max_feed_pages_reached"
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
        return self.archive.record_feed_run(
            FeedRun(
                author_id=author_id,
                platform="xueqiu",
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                login_state=login_state,
                pages_fetched=len(parsed_pages),
                pagination_complete=pagination_complete,
                covered_from=covered_from,
                covered_to=covered_to,
                rate_limited=rate_limited,
                http_error_count=http_error_count,
                ingest_mode=IngestMode.LIVE,
                adapter_version=ADAPTER_VERSION,
                parse_failure_count=parse_failure_count,
                notes=notes,
            ),
            list(posts_by_id.values()),
        )

    def probe_due_posts(self) -> list[int]:
        run_ids: list[int] = []
        for target in self.archive.probe_targets():
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
