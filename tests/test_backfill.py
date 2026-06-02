"""Backfill ingest mode, page/until budgets, and live continuity-gap guard."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
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

Handler = Callable[[httpx.Request], httpx.Response]

FIXTURES = Path(__file__).parent.parent / "probe" / "fixtures"
NOW = "2026-06-01T00:00:00+00:00"
# Fixture xueqiu_timeline_page.json: maxPage=2; datable posts span
# covered_from=2026-05-17T06:40:00 .. covered_to=2026-05-31T08:00:45.
COVERED_FROM = "2026-05-17T06:40:00+00:00"
WINDOW_START = "2026-05-01T00:00:00+00:00"


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


def make_collector(archive: Archive, handler: Any, **settings: Any) -> XueqiuCollector:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return XueqiuCollector(
        archive,
        client,
        CollectorSettings(0, 0, **settings),
        sleep=lambda _: None,
        clock=lambda: NOW,
    )


def seed_present_post(archive: Archive) -> int:
    """Seed a live, present post inside the fixture's covered range (posted 2026-05-20)."""
    post = NormalizedPost(
        platform_post_id="999",
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
        "SELECT id FROM posts WHERE platform_post_id = '999'"
    ).fetchone()
    assert row is not None
    return int(row["id"])


def two_page_handler(requested: list[str]) -> Any:
    """Page 1 = fixture (maxPage 2); page 2 = empty so pagination completes."""

    def handle(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        requested.append(page)
        payload = load_fixture("xueqiu_timeline_page.json")
        if page != "1":
            payload["page"] = int(page)
            payload["statuses"] = []
        return httpx.Response(200, json=payload)

    return handle


def test_backfill_records_backfill_mode_and_skips_negative_inference(archive: Archive) -> None:
    present_id = seed_present_post(archive)
    requested: list[str] = []

    collector = make_collector(archive, two_page_handler(requested))
    run_id = collector.backfill_feed(1, "100", max_pages=5)

    run = archive.connection.execute(
        "SELECT status, ingest_mode, pagination_complete FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert tuple(run) == (RunStatus.OK, IngestMode.BACKFILL, 1)
    # Full posts (111, 222) carry backfill provenance; preview post 333 builds no version.
    modes = [
        row["ingest_mode"]
        for row in archive.connection.execute(
            """
            SELECT v.ingest_mode FROM post_versions v
            JOIN posts p ON p.id = v.post_id
            WHERE p.platform_post_id IN ('111', '222')
            """
        ).fetchall()
    ]
    assert sorted(modes) == [IngestMode.BACKFILL, IngestMode.BACKFILL]
    # Rule 9: backfill never infers absence, even though the seeded present post sits inside
    # the covered range and was not seen this run.
    present = archive.connection.execute(
        "SELECT feed_state, absent_healthy_streak FROM posts WHERE id = ?", (present_id,)
    ).fetchone()
    assert tuple(present) == ("present", 0)
    absent_obs = archive.connection.execute(
        "SELECT COUNT(*) FROM post_observations WHERE post_id = ? AND present = 0", (present_id,)
    ).fetchone()[0]
    assert absent_obs == 0


def test_backfill_honors_page_budget(archive: Archive) -> None:
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params["page"])
        payload = load_fixture("xueqiu_timeline_page.json")
        payload["maxPage"] = 999
        return httpx.Response(200, json=payload)

    collector = make_collector(archive, handle)
    run_id = collector.backfill_feed(1, "100", max_pages=3)

    row = archive.connection.execute(
        "SELECT status, pages_fetched, pagination_complete, notes FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert tuple(row) == (RunStatus.PARTIAL, 3, 0, "backfill_pages_reached")
    assert requested == ["1", "2", "3"]


def test_backfill_until_stops_once_covered(archive: Archive) -> None:
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params["page"])
        payload = load_fixture("xueqiu_timeline_page.json")
        payload["maxPage"] = 999  # would otherwise keep paging
        return httpx.Response(200, json=payload)

    collector = make_collector(archive, handle)
    run_id = collector.backfill_feed(1, "100", max_pages=10, until="2026-12-01T00:00:00+00:00")

    row = archive.connection.execute(
        "SELECT pages_fetched, pagination_complete FROM fetch_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert tuple(row) == (1, 1)
    assert requested == ["1"]


def test_backfill_rejects_non_positive_pages(archive: Archive) -> None:
    collector = make_collector(archive, two_page_handler([]))
    with pytest.raises(ValueError, match="max_pages"):
        collector.backfill_feed(1, "100", max_pages=0)


def test_backfill_rejects_non_positive_start_page(archive: Archive) -> None:
    collector = make_collector(archive, two_page_handler([]))
    with pytest.raises(ValueError, match="start_page"):
        collector.backfill_feed(1, "100", max_pages=5, start_page=0)


def test_backfill_rejects_malformed_until(archive: Archive) -> None:
    """A bad --until must be rejected up front, even when the timeline ends first.

    maxPage=1 means page 1 satisfies ``page >= max_page`` before ``until`` is ever
    compared, so the validation has to happen at entry, not lazily in target_reached.
    """
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params["page"])
        payload = load_fixture("xueqiu_timeline_page.json")
        payload["maxPage"] = 1  # page 1 is the end; until would never be consulted
        return httpx.Response(200, json=payload)

    collector = make_collector(archive, handle)
    with pytest.raises(ValueError):
        collector.backfill_feed(1, "100", max_pages=5, until="not-a-timestamp")
    with pytest.raises(ValueError, match="timezone"):
        collector.backfill_feed(1, "100", max_pages=5, until="2026-05-01T00:00:00")
    assert requested == []  # rejected before any request was made


def test_backfill_start_page_resumes_past_live_window(archive: Archive) -> None:
    """start_page pages forward (deeper history), and max_pages stays a fetch count."""
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        requested.append(page)
        payload = load_fixture("xueqiu_timeline_page.json")
        payload["maxPage"] = 999  # never the natural stop; budget must cap
        payload["page"] = int(page)
        return httpx.Response(200, json=payload)

    collector = make_collector(archive, handle)
    run_id = collector.backfill_feed(1, "100", max_pages=3, start_page=4)

    assert requested == ["4", "5", "6"]
    row = archive.connection.execute(
        "SELECT pages_fetched, pagination_complete, notes FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert tuple(row) == (3, 0, "backfill_pages_reached")


def test_baseline_pending_until_planned_depth_reached(archive: Archive) -> None:
    # No backfill yet: pending so run-once will attempt it.
    assert archive.baseline_backfill_pending(1) is True

    # A budget-capped backfill is a planned stop -> baseline established.
    def capped(request: httpx.Request) -> httpx.Response:
        payload = load_fixture("xueqiu_timeline_page.json")
        payload["maxPage"] = 999
        return httpx.Response(200, json=payload)

    make_collector(archive, capped).backfill_feed(1, "100", max_pages=2)
    assert archive.baseline_backfill_pending(1) is False


def test_baseline_pending_when_pagination_completes(archive: Archive) -> None:
    make_collector(archive, two_page_handler([])).backfill_feed(1, "100", max_pages=5)
    assert archive.baseline_backfill_pending(1) is False


def test_baseline_stays_pending_after_rate_limited_backfill(archive: Archive) -> None:
    """A rate-limited (or otherwise failed) first attempt must not look established."""

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    run_id = make_collector(archive, rate_limited).backfill_feed(1, "100", max_pages=5)

    row = archive.connection.execute(
        "SELECT rate_limited, pagination_complete, notes FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert tuple(row) == (1, 0, "http_429")
    assert archive.baseline_backfill_pending(1) is True


def degraded_handler(max_page: int) -> Any:
    """Pages parse but each carries one un-parseable status (parse_failure_count > 0)."""

    def handle(request: httpx.Request) -> httpx.Response:
        payload = load_fixture("xueqiu_timeline_page.json")
        payload["maxPage"] = max_page
        payload["statuses"] = [*payload["statuses"], "not-a-status-object"]
        return httpx.Response(200, json=payload)

    return handle


def test_baseline_pending_when_pagination_end_has_parse_failures(archive: Archive) -> None:
    # maxPage=1 -> page 1 is the natural end, but it carried a degraded status.
    run_id = make_collector(archive, degraded_handler(1)).backfill_feed(1, "100", max_pages=5)
    row = archive.connection.execute(
        "SELECT pagination_complete, parse_failure_count FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert row["pagination_complete"] == 1 and row["parse_failure_count"] >= 1
    assert archive.baseline_backfill_pending(1) is True


def test_baseline_pending_when_page_budget_hit_with_parse_failures(archive: Archive) -> None:
    # maxPage=999 -> the run stops on the page budget, and the pages were degraded.
    run_id = make_collector(archive, degraded_handler(999)).backfill_feed(1, "100", max_pages=2)
    row = archive.connection.execute(
        "SELECT notes, parse_failure_count FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert row["notes"] == "backfill_pages_reached" and row["parse_failure_count"] >= 1
    assert archive.baseline_backfill_pending(1) is True


@pytest.mark.parametrize(
    ("kwargs", "blocked"),
    [
        ({}, False),
        ({"rate_limited": True, "status": RunStatus.PARTIAL}, True),
        ({"http_error_count": 1, "status": RunStatus.PARTIAL}, True),
        ({"login_state": LoginState.EXPIRED, "status": RunStatus.PARTIAL}, True),
        # A coverage gap is partial but the fetch itself worked -> not blocked.
        (
            {"status": RunStatus.PARTIAL, "pagination_complete": False, "notes": "coverage_gap"},
            False,
        ),
    ],
)
def test_feed_run_blocked_only_flags_session_obstruction(
    archive: Archive, kwargs: dict[str, Any], blocked: bool
) -> None:
    run = FeedRun(
        author_id=1,
        platform="xueqiu",
        started_at=NOW,
        finished_at=NOW,
        status=kwargs.get("status", RunStatus.OK),
        login_state=kwargs.get("login_state", LoginState.VALID),
        pages_fetched=1,
        pagination_complete=kwargs.get("pagination_complete", True),
        covered_from=WINDOW_START,
        covered_to=NOW,
        rate_limited=kwargs.get("rate_limited", False),
        http_error_count=kwargs.get("http_error_count", 0),
        ingest_mode=IngestMode.LIVE,
        adapter_version="xueqiu-1",
        notes=kwargs.get("notes"),
    )
    run_id = archive.record_feed_run(run, [])
    assert archive.feed_run_blocked(run_id) is blocked


def write_run_once_config(
    config_dir: Path,
    db_path: Path,
    uids: list[str],
    *,
    on_add_pages: int = 5,
    max_feed_pages: int | None = None,
) -> None:
    lines = [
        "storage:",
        f"  db_path: {str(db_path).replace(chr(92), '/')}",
        "  backup_after_run: false",
        "accounts:",
        *(f"  - uid: '{uid}'" for uid in uids),
        "backfill:",
        "  on_add_enabled: true",
        f"  on_add_pages: {on_add_pages}",
    ]
    if max_feed_pages is not None:
        lines += ["polling:", f"  max_feed_pages: {max_feed_pages}"]
    (config_dir / "config.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def seed_pinned_probe_target(db_path: Path, uids: list[str]) -> None:
    """Author rows + one pinned post, so a probe target exists if probes are not skipped."""
    connection = connect_database(db_path)
    initialize_database(connection)
    seed = Archive(connection)
    for uid in uids:
        seed.add_author("xueqiu", uid, NOW)
    seed.pin_post(seed_present_post(seed), NOW)
    connection.close()


def read_run_once_counts(db_path: Path) -> tuple[int, int, int]:
    """(backfill run count, probe run count, latest live run's rate_limited flag)."""
    connection = connect_database(db_path)
    try:
        backfill_runs = connection.execute(
            "SELECT COUNT(*) FROM fetch_runs WHERE ingest_mode = ?", (IngestMode.BACKFILL,)
        ).fetchone()[0]
        probe_runs = connection.execute("SELECT COUNT(*) FROM probe_runs").fetchone()[0]
        live_rate_limited = connection.execute(
            "SELECT rate_limited FROM fetch_runs WHERE ingest_mode = ? ORDER BY id DESC LIMIT 1",
            (IngestMode.LIVE,),
        ).fetchone()[0]
    finally:
        connection.close()
    return backfill_runs, probe_runs, live_rate_limited


def make_run_once_client(monkeypatch: pytest.MonkeyPatch, handle: Handler) -> list[str]:
    """Route run_once's collector client to ``handle`` and return a call log it appends to."""
    from kol_archive import __main__ as kol_main

    calls: list[str] = []

    def logged(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("user_timeline.json"):
            params = request.url.params
            calls.append(f"feed uid={params['user_id']} page={params['page']}")
        else:
            calls.append(f"probe {path}")
        return handle(request)

    client = httpx.Client(transport=httpx.MockTransport(logged))
    monkeypatch.setattr(kol_main, "_build_collector_client", lambda config: client)
    return calls


def test_run_once_live_429_stops_account_loop_and_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate-limited live poll ends the whole account loop — the session is blocked."""
    from kol_archive import __main__ as kol_main

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "kol.sqlite3"
    write_run_once_config(config_dir, db_path, ["100", "200"])
    seed_pinned_probe_target(db_path, ["100", "200"])

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("user_timeline.json"):
            return httpx.Response(429, json={})
        return httpx.Response(200, json={})

    calls = make_run_once_client(monkeypatch, handle)
    kol_main.run_once(config_dir)

    # Loop broke after the first account's 429: uid 200 is never polled, probes skipped.
    assert calls == ["feed uid=100 page=1"]
    backfill_runs, probe_runs, live_rate_limited = read_run_once_counts(db_path)
    assert (backfill_runs, probe_runs, live_rate_limited) == (0, 0, 1)


def test_run_once_backfill_429_stops_account_loop_and_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 during auto-backfill is also session-wide: stop the loop, skip probes."""
    from kol_archive import __main__ as kol_main

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "kol.sqlite3"
    # max_feed_pages=1 caps the live poll at page 1 without reaching the timeline end
    # (maxPage=999), so a backfill is attempted at page 2 — where it hits the wall.
    write_run_once_config(config_dir, db_path, ["100", "200"], max_feed_pages=1)
    seed_pinned_probe_target(db_path, ["100", "200"])

    def handle(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("user_timeline.json"):
            return httpx.Response(200, json={})
        if request.url.params["page"] == "1":
            return httpx.Response(200, json={"page": 1, "maxPage": 999, "total": 0, "statuses": []})
        return httpx.Response(429, json={})  # backfill (start_page=2) trips the wall

    calls = make_run_once_client(monkeypatch, handle)
    kol_main.run_once(config_dir)

    # uid 100: clean live page 1, then backfill page 2 hits 429 -> loop breaks before uid 200.
    assert calls == ["feed uid=100 page=1", "feed uid=100 page=2"]
    connection = connect_database(db_path)
    try:
        backfill_rate_limited = connection.execute(
            "SELECT rate_limited FROM fetch_runs WHERE ingest_mode = ? ORDER BY id DESC LIMIT 1",
            (IngestMode.BACKFILL,),
        ).fetchone()[0]
        probe_runs = connection.execute("SELECT COUNT(*) FROM probe_runs").fetchone()[0]
    finally:
        connection.close()
    assert backfill_rate_limited == 1
    assert probe_runs == 0


def test_run_once_skips_backfill_when_live_reaches_timeline_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short-timeline account: the live poll reaches the end, so no backfill is needed.

    Without the reached_timeline_end signal the auto-backfill would request the
    out-of-range next page forever and the baseline would stay pending.
    """
    from kol_archive import __main__ as kol_main

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "kol.sqlite3"
    write_run_once_config(config_dir, db_path, ["100"])

    def handle(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("user_timeline.json"):
            return httpx.Response(200, json={})
        payload = load_fixture("xueqiu_timeline_page.json")
        payload["maxPage"] = 1  # page 1 IS the end of the timeline
        payload["page"] = 1
        return httpx.Response(200, json=payload)

    calls = make_run_once_client(monkeypatch, handle)
    kol_main.run_once(config_dir)

    assert calls == ["feed uid=100 page=1"]  # no page-2 backfill request
    connection = connect_database(db_path)
    try:
        archive = Archive(connection)
        author_id = archive.get_author_id("xueqiu", "100")
        assert author_id is not None
        pending = archive.baseline_backfill_pending(author_id)
        backfill_runs = connection.execute(
            "SELECT COUNT(*) FROM fetch_runs WHERE ingest_mode = ?", (IngestMode.BACKFILL,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert backfill_runs == 0
    assert pending is False


def test_run_once_skips_backfill_when_live_last_page_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded live page-1 (timeline end) must not seed an out-of-range page-2 backfill.

    Page 1 is the end of the timeline (maxPage=1) but carries an un-parseable entry, so
    its page count cannot be trusted to locate the real end. The baseline stays pending
    for a later clean run instead of requesting the out-of-range page 2.
    """
    from kol_archive import __main__ as kol_main

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "kol.sqlite3"
    write_run_once_config(config_dir, db_path, ["100"])

    def handle(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("user_timeline.json"):
            return httpx.Response(200, json={})
        payload = load_fixture("xueqiu_timeline_page.json")
        payload["maxPage"] = 1  # page 1 IS the end of the timeline
        payload["page"] = 1
        payload["statuses"] = [*payload["statuses"], "not-a-status-object"]  # degraded entry
        return httpx.Response(200, json=payload)

    calls = make_run_once_client(monkeypatch, handle)
    kol_main.run_once(config_dir)

    assert calls == ["feed uid=100 page=1"]  # no page-2 backfill request
    connection = connect_database(db_path)
    try:
        archive = Archive(connection)
        author_id = archive.get_author_id("xueqiu", "100")
        assert author_id is not None
        pending = archive.baseline_backfill_pending(author_id)
        backfill_runs = connection.execute(
            "SELECT COUNT(*) FROM fetch_runs WHERE ingest_mode = ?", (IngestMode.BACKFILL,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert backfill_runs == 0
    assert pending is True  # left pending for a later clean retry


def test_run_once_honors_explicit_zero_on_add_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """on_add_pages: 0 must disable auto-backfill, not fall back to the default."""
    from kol_archive import __main__ as kol_main

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "kol.sqlite3"
    # Live caps at page 1 without reaching the end, so a backfill *would* run if enabled.
    write_run_once_config(config_dir, db_path, ["100"], on_add_pages=0, max_feed_pages=1)

    def handle(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("user_timeline.json"):
            return httpx.Response(200, json={})
        if request.url.params["page"] == "1":
            return httpx.Response(200, json={"page": 1, "maxPage": 999, "total": 0, "statuses": []})
        return httpx.Response(429, json={})  # a backfill page would land here

    calls = make_run_once_client(monkeypatch, handle)
    kol_main.run_once(config_dir)

    assert calls == ["feed uid=100 page=1"]  # no backfill page requested
    connection = connect_database(db_path)
    try:
        backfill_runs = connection.execute(
            "SELECT COUNT(*) FROM fetch_runs WHERE ingest_mode = ?", (IngestMode.BACKFILL,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert backfill_runs == 0


def test_run_once_rejects_negative_on_add_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A negative on_add_pages is a misconfiguration -> error, not a silent disable."""
    from kol_archive import __main__ as kol_main

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "kol.sqlite3"
    write_run_once_config(config_dir, db_path, ["100"], on_add_pages=-1)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    monkeypatch.setattr(kol_main, "_build_collector_client", lambda config: client)

    with pytest.raises(ValueError, match="on_add_pages must not be negative"):
        kol_main.run_once(config_dir)


def test_run_backfill_honors_explicit_zero_command_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """command_pages: 0 must be passed through (and rejected), not defaulted to 10."""
    from kol_archive import __main__ as kol_main

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "kol.sqlite3"
    (config_dir / "config.yml").write_text(
        "storage:\n"
        f"  db_path: {str(db_path).replace(chr(92), '/')}\n"
        "accounts:\n"
        "  - uid: '100'\n"
        "backfill:\n"
        "  command_pages: 0\n",
        encoding="utf-8",
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    monkeypatch.setattr(kol_main, "_build_collector_client", lambda config: client)

    with pytest.raises(ValueError, match="max_pages must be positive"):
        kol_main.run_backfill(config_dir, uid="100", pages=None, until=None)


def test_run_backfill_validates_inputs_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad inputs are rejected before the DB is created or the client is built."""
    from kol_archive import __main__ as kol_main

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "kol.sqlite3"
    write_run_once_config(config_dir, db_path, ["100"])

    def fail_if_built(config: Any) -> httpx.Client:
        raise AssertionError("collector client must not be built before input validation")

    monkeypatch.setattr(kol_main, "_build_collector_client", fail_if_built)

    with pytest.raises(ValueError, match="max_pages must be positive"):
        kol_main.run_backfill(config_dir, uid="100", pages=0, until=None)
    with pytest.raises(ValueError, match="timezone"):
        kol_main.run_backfill(config_dir, uid="100", pages=5, until="2026-05-01T00:00:00")
    # No side effect: the database file was never created.
    assert not db_path.exists()


def test_initialize_database_backfills_new_fetch_runs_columns(tmp_path: Path) -> None:
    """An archive created before the new columns gains them on the next initialize."""
    db_path = tmp_path / "old.sqlite3"
    connection = connect_database(db_path)
    # A pre-feature fetch_runs: no parse_failure_count / reached_timeline_end columns.
    connection.executescript(
        """
        CREATE TABLE authors (
            id INTEGER PRIMARY KEY,
            platform TEXT NOT NULL,
            platform_uid TEXT NOT NULL,
            live_monitoring_started_at TEXT NOT NULL,
            notes TEXT,
            UNIQUE(platform, platform_uid)
        );
        CREATE TABLE fetch_runs (
            id INTEGER PRIMARY KEY,
            author_id INTEGER NOT NULL REFERENCES authors(id),
            platform TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            login_state TEXT NOT NULL,
            pages_fetched INTEGER NOT NULL,
            pagination_complete INTEGER NOT NULL,
            covered_from TEXT,
            covered_to TEXT,
            rate_limited INTEGER NOT NULL,
            http_error_count INTEGER NOT NULL,
            ingest_mode TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            notes TEXT
        );
        """
    )
    connection.close()

    connection = connect_database(db_path)
    try:
        initialize_database(connection)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(fetch_runs)")}
        assert {"parse_failure_count", "reached_timeline_end"} <= columns
        # The migrated table accepts a full insert, and the new signal works end to end.
        archive = Archive(connection)
        archive.add_author("xueqiu", "100", NOW)
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
                covered_from=WINDOW_START,
                covered_to=NOW,
                rate_limited=False,
                http_error_count=0,
                ingest_mode=IngestMode.BACKFILL,
                adapter_version="xueqiu-1",
                reached_timeline_end=True,
            ),
            [],
        )
        assert archive.baseline_backfill_pending(1) is False
    finally:
        connection.close()


def test_feed_run_pages_reports_live_page_count(archive: Archive) -> None:
    requested: list[str] = []
    collector = make_collector(archive, two_page_handler(requested))
    run_id = collector.poll_feed(1, "100", WINDOW_START)
    assert archive.feed_run_pages(run_id) == 2


def test_first_add_detected_via_get_author_id(archive: Archive) -> None:
    assert archive.get_author_id("xueqiu", "200") is None
    new_id = archive.ensure_author("xueqiu", "200", NOW)
    assert archive.get_author_id("xueqiu", "200") == new_id


def test_live_poll_reconnects_and_infers_absence(archive: Archive) -> None:
    present_id = seed_present_post(archive)
    requested: list[str] = []

    collector = make_collector(archive, two_page_handler(requested))
    # Previous run's newest post is 2026-05-25, newer than this run's oldest (05-17):
    # the coverage reconnects, so the healthy live run may infer absence.
    run_id = collector.poll_feed(
        1, "100", WINDOW_START, previous_covered_to="2026-05-25T00:00:00+00:00"
    )

    run = archive.connection.execute(
        "SELECT status, pagination_complete, notes FROM fetch_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert tuple(run) == (RunStatus.OK, 1, None)
    streak = archive.connection.execute(
        "SELECT absent_healthy_streak FROM posts WHERE id = ?", (present_id,)
    ).fetchone()[0]
    assert streak == 1


def test_live_poll_coverage_gap_blocks_negative_inference(archive: Archive) -> None:
    present_id = seed_present_post(archive)
    requested: list[str] = []

    collector = make_collector(archive, two_page_handler(requested))
    # Previous run's newest post (05-10) is older than this run's oldest (05-17): there is a
    # hole between runs, so absence must not be inferred over it.
    run_id = collector.poll_feed(
        1, "100", WINDOW_START, previous_covered_to="2026-05-10T00:00:00+00:00"
    )

    run = archive.connection.execute(
        "SELECT status, pagination_complete, notes FROM fetch_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert tuple(run) == (RunStatus.PARTIAL, 0, "coverage_gap")
    streak = archive.connection.execute(
        "SELECT absent_healthy_streak FROM posts WHERE id = ?", (present_id,)
    ).fetchone()[0]
    assert streak == 0


def test_live_poll_empty_timeline_is_healthy_and_skips_inference(archive: Archive) -> None:
    """A brand-new/empty account: page 1 is the timeline end with no posts.

    The round is healthy (OK + complete) but covers no time range, so negative
    inference is skipped instead of crashing on the missing coverage. A seeded
    present post must therefore keep its state.
    """
    present_id = seed_present_post(archive)

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"page": 1, "maxPage": 1, "total": 0, "statuses": []})

    collector = make_collector(archive, empty)
    run_id = collector.poll_feed(1, "100", WINDOW_START)

    run = archive.connection.execute(
        "SELECT status, pagination_complete, covered_from, covered_to FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert tuple(run) == (RunStatus.OK, 1, None, None)
    present = archive.connection.execute(
        "SELECT feed_state, absent_healthy_streak FROM posts WHERE id = ?", (present_id,)
    ).fetchone()
    assert tuple(present) == ("present", 0)


def test_live_poll_without_previous_run_unchanged(archive: Archive) -> None:
    requested: list[str] = []
    collector = make_collector(archive, two_page_handler(requested))
    run_id = collector.poll_feed(1, "100", WINDOW_START)

    row = archive.connection.execute(
        "SELECT status, pages_fetched, pagination_complete FROM fetch_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert tuple(row) == (RunStatus.OK, 2, 1)
    assert requested == ["1", "2"]
    assert (
        COVERED_FROM
        == archive.connection.execute(
            "SELECT covered_from FROM fetch_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
    )
