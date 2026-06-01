from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from kol_archive.collector import CollectorSettings, XueqiuCollector
from kol_archive.database import connect_database, initialize_database
from kol_archive.models import (
    ArchiveSettings,
    ContentFidelity,
    FeedRun,
    IngestMode,
    LoginState,
    NormalizedPost,
    RunStatus,
)
from kol_archive.service import Archive

FIXTURES = Path(__file__).parent.parent / "probe" / "fixtures"
NOW = "2026-06-01T00:00:00+00:00"


def load_fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / name).read_text(encoding="utf-8")))


@pytest.fixture
def archive() -> Iterator[Archive]:
    connection = connect_database(":memory:")
    initialize_database(connection)
    service = Archive(connection, ArchiveSettings(absent_threshold_n=3))
    service.add_author("xueqiu", "100", NOW)
    try:
        yield service
    finally:
        connection.close()


def make_client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def rate_limited_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, request=request)


def connect_error_response(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("offline", request=request)


def seed_post(archive: Archive, platform_post_id: str = "999") -> int:
    post = NormalizedPost(
        platform_post_id=platform_post_id,
        author_id=1,
        observed_at=NOW,
        content_fidelity=ContentFidelity.FULL,
        content_text="seed",
        content_hash="hash-seed",
        posted_at_claimed="2026-05-20T00:00:00+00:00",
    )
    archive.record_feed_run(
        FeedRun(
            author_id=1,
            platform="xueqiu",
            started_at=NOW,
            finished_at=NOW,
            status=RunStatus.OK,
            login_state=LoginState.VALID,
            pages_fetched=1,
            pagination_complete=True,
            covered_from="2026-05-01T00:00:00+00:00",
            covered_to="2026-06-02T00:00:00+00:00",
            rate_limited=False,
            http_error_count=0,
            ingest_mode=IngestMode.LIVE,
            adapter_version="xueqiu-1",
        ),
        [post],
    )
    row = archive.connection.execute(
        "SELECT id FROM posts WHERE platform_post_id = ?", (platform_post_id,)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def test_guest_page_two_login_expiry_archives_page_one_and_blocks_negative_inference(
    archive: Archive,
) -> None:
    missing_id = seed_post(archive)
    waits: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(200, json=load_fixture("xueqiu_timeline_page.json"))
        return httpx.Response(400, json=load_fixture("xueqiu_error_login.json"))

    with make_client(httpx.MockTransport(handle)) as client:
        collector = XueqiuCollector(
            archive,
            client,
            CollectorSettings(0, 0),
            sleep=waits.append,
            clock=lambda: NOW,
        )
        run_id = collector.poll_feed(1, "100", "2026-05-01T00:00:00+00:00")

    row = archive.connection.execute(
        """
        SELECT status, login_state, pages_fetched, pagination_complete
        FROM fetch_runs WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == (RunStatus.PARTIAL, LoginState.EXPIRED, 1, 0)
    assert archive.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 4
    assert (
        archive.connection.execute(
            "SELECT absent_healthy_streak FROM posts WHERE id = ?", (missing_id,)
        ).fetchone()[0]
        == 0
    )
    assert waits == [0.0]


def test_normal_multi_page_feed_completion(archive: Archive) -> None:
    requested_pages: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        requested_pages.append(page)
        payload = load_fixture("xueqiu_timeline_page.json")
        if page == "2":
            payload["page"] = 2
            payload["statuses"] = []
        return httpx.Response(200, json=payload)

    with make_client(httpx.MockTransport(handle)) as client:
        collector = XueqiuCollector(
            archive,
            client,
            CollectorSettings(0, 0),
            sleep=lambda _: None,
            clock=lambda: NOW,
        )
        run_id = collector.poll_feed(1, "100", "2026-05-01T00:00:00+00:00")

    row = archive.connection.execute(
        """
        SELECT status, login_state, pages_fetched, pagination_complete
        FROM fetch_runs WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == (RunStatus.OK, LoginState.VALID, 2, 1)
    assert requested_pages == ["1", "2"]


def test_feed_max_pages_cap_archives_seen_posts_as_partial(archive: Archive) -> None:
    missing_id = seed_post(archive)
    requested_pages: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested_pages.append(request.url.params["page"])
        payload = load_fixture("xueqiu_timeline_page.json")
        payload["maxPage"] = 999
        return httpx.Response(200, json=payload)

    with make_client(httpx.MockTransport(handle)) as client:
        collector = XueqiuCollector(
            archive,
            client,
            CollectorSettings(0, 0, max_feed_pages=2),
            sleep=lambda _: None,
            clock=lambda: NOW,
        )
        run_id = collector.poll_feed(1, "100", "2026-05-01T00:00:00+00:00")

    row = archive.connection.execute(
        "SELECT status, pages_fetched, pagination_complete, notes FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == (RunStatus.PARTIAL, 2, 0, "max_feed_pages_reached")
    assert requested_pages == ["1", "2"]
    assert archive.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 4
    assert (
        archive.connection.execute(
            "SELECT absent_healthy_streak FROM posts WHERE id = ?", (missing_id,)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (
            rate_limited_response,
            (RunStatus.PARTIAL, LoginState.UNKNOWN, 1, 1, "http_429"),
        ),
        (
            connect_error_response,
            (RunStatus.FAILED, LoginState.UNKNOWN, 0, 1, "http_error"),
        ),
    ],
)
def test_feed_records_rate_limit_and_http_error(
    archive: Archive,
    handler: Any,
    expected: tuple[RunStatus, LoginState, int, int, str],
) -> None:
    with make_client(httpx.MockTransport(handler)) as client:
        collector = XueqiuCollector(
            archive,
            client,
            CollectorSettings(0, 0),
            sleep=lambda _: None,
            clock=lambda: NOW,
        )
        run_id = collector.poll_feed(1, "100", "2026-05-01T00:00:00+00:00")

    row = archive.connection.execute(
        "SELECT status, login_state, rate_limited, http_error_count, notes "
        "FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == expected


def test_due_probe_maps_not_found_to_unavailable(archive: Archive) -> None:
    post_id = seed_post(archive)
    archive.pin_post(post_id, "2026-06-01T01:00:00+00:00")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/statuses/show.json"
        return httpx.Response(400, json=load_fixture("xueqiu_error_not_found.json"))

    with make_client(httpx.MockTransport(handle)) as client:
        collector = XueqiuCollector(
            archive,
            client,
            CollectorSettings(0, 0),
            sleep=lambda _: None,
            clock=lambda: NOW,
        )
        run_ids = collector.probe_due_posts()

    assert len(run_ids) == 1
    row = archive.connection.execute(
        "SELECT source_state, source_checked_at FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("unavailable", NOW)


def test_due_probe_stops_after_rate_limit(archive: Archive) -> None:
    first_id = seed_post(archive, "998")
    second_id = seed_post(archive, "999")
    archive.pin_post(first_id, "2026-06-01T01:00:00+00:00")
    archive.pin_post(second_id, "2026-06-01T01:00:00+00:00")
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, request=request)

    with make_client(httpx.MockTransport(handle)) as client:
        collector = XueqiuCollector(
            archive,
            client,
            CollectorSettings(0, 0),
            sleep=lambda _: None,
            clock=lambda: NOW,
        )
        run_ids = collector.probe_due_posts()

    assert len(run_ids) == 1
    assert calls == 1
    row = archive.connection.execute(
        "SELECT status, rate_limited FROM probe_runs WHERE id = ?", (run_ids[0],)
    ).fetchone()
    assert row is not None
    assert tuple(row) == (RunStatus.PARTIAL, 1)
