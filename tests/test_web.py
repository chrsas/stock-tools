from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import pytest

from kol_archive.database import connect_database, initialize_database
from kol_archive.models import (
    ContentFidelity,
    EnrichmentResult,
    FeedRun,
    IngestMode,
    LoginState,
    NormalizedPost,
    RewriteSource,
    RunStatus,
    WatchMode,
)
from kol_archive.rewrite import RewriteSuggestion
from kol_archive.service import Archive
from kol_archive.web import (
    ArchiveHttpServer,
    ArchiveRequestHandler,
    WebSettings,
    _load_automation_settings,
    _start_auto_enrichment,
    create_server,
    load_web_settings,
)

CSRF_TOKEN = "test-csrf-token"
BASE_TIME = "2026-06-01T00:00:00+00:00"


def _make_feed_run() -> FeedRun:
    return FeedRun(
        author_id=1,
        platform="xueqiu",
        started_at=BASE_TIME,
        finished_at=BASE_TIME,
        status=RunStatus.OK,
        login_state=LoginState.VALID,
        pages_fetched=1,
        pagination_complete=True,
        covered_from="2026-05-01T00:00:00+00:00",
        covered_to="2026-06-02T00:00:00+00:00",
        rate_limited=False,
        http_error_count=0,
        ingest_mode=IngestMode.LIVE,
        adapter_version="xueqiu-2",
        notes="cookie=feed-secret",
    )


def _make_post() -> NormalizedPost:
    return NormalizedPost(
        platform_post_id="post-1 & qa",
        author_id=1,
        observed_at=BASE_TIME,
        content_fidelity=ContentFidelity.FULL,
        content_text="原始正文 A",
        content_hash="hash-a",
        posted_at_claimed=datetime.now(tz=UTC).isoformat(),
        url="https://xueqiu.com/100/post-1",
        raw_meta={"cookie": "meta-secret"},
        raw_payload={
            "token": "payload-secret",
            "stockCorrelation": ["SH000001"],
            "user": {
                "screen_name": "测试作者 & QA",
                "profile_image_url": "community/avatar.jpg!50x50.png",
                "description": "作者简介",
            },
        },
    )


@pytest.fixture
def web_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ArchiveHttpServer]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "archive.sqlite3"
    config_dir.joinpath("config.yml").write_text(
        f"""
storage:
  db_path: {db_path.as_posix()}
monitoring:
  window_days: 30
llm:
  provider: openai_compatible
  base_url: https://llm.example/v1
  model: test-model
  api_key_env: TEST_LLM_KEY
  prompt_version: rewrite-v1
web:
  enrich_prompt_version: enrich-v1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_LLM_KEY", "local-test-key")
    connection = connect_database(db_path)
    initialize_database(connection)
    archive = Archive(connection)
    archive.add_author("xueqiu", "100", BASE_TIME)
    archive.record_feed_run(_make_feed_run(), [_make_post()])
    connection.close()

    server = create_server(
        db_path,
        config_dir,
        WebSettings(port=0, enrich_prompt_version="enrich-v1"),
        csrf_token=CSRF_TOKEN,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    server: ArchiveHttpServer,
    method: str,
    path: str,
    form: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    host, port = cast(tuple[str, int], server.server_address)
    connection = http.client.HTTPConnection(host, port, timeout=5)
    body = None if form is None else urlencode(form)
    headers = {} if form is None else {"Content-Type": "application/x-www-form-urlencoded"}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    content = response.read().decode("utf-8")
    response_headers = {key: value for key, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, content


def _request_bytes(server: ArchiveHttpServer, path: str) -> tuple[int, dict[str, str], bytes]:
    host, port = cast(tuple[str, int], server.server_address)
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request("GET", path)
    response = connection.getresponse()
    content = response.read()
    response_headers = {key: value for key, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, content


def _read_post_row(server: ArchiveHttpServer) -> tuple[str, int]:
    connection = connect_database(server.db_path)
    try:
        row = connection.execute(
            "SELECT watch_mode, current_version_id FROM posts WHERE id = 1"
        ).fetchone()
        assert row is not None
        return str(row["watch_mode"]), int(row["current_version_id"])
    finally:
        connection.close()


def _get_json(server: ArchiveHttpServer, path: str) -> dict[str, object]:
    status, headers, content = _request(server, "GET", path)
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    return cast(dict[str, object], json.loads(content))


def test_decision_web_flow_and_csrf(web_server: ArchiveHttpServer) -> None:
    payload = _get_json(web_server, "/api/home?view=decisions")
    assert payload["view"] == "decisions"
    assert cast(dict[str, int], payload["counts"])["open"] == 0

    status, _, _ = _request(
        web_server,
        "POST",
        "/decisions/add",
        {
            "csrf_token": CSRF_TOKEN,
            "ticker": "SH688303",
            "direction": "neutral",
            "thesis": "观察论点",
            "invalidation": "证伪条件",
            "horizon_days": "7",
        },
    )
    assert status == 303
    payload = _get_json(web_server, "/api/home?view=decisions")
    item = cast(list[dict[str, object]], payload["items"])[0]
    assert item["ticker"] == "SH688303"
    assert item["status"] == "open"

    status, _, _ = _request(
        web_server,
        "POST",
        f"/decisions/{item['id']}/close",
        {"csrf_token": CSRF_TOKEN, "status": "closed"},
    )
    assert status == 303
    status, _, _ = _request(
        web_server,
        "POST",
        f"/decisions/{item['id']}/review",
        {"csrf_token": CSRF_TOKEN, "retro": "复盘原文", "lesson": "经验"},
    )
    assert status == 303
    payload = _get_json(web_server, "/api/home?view=decisions")
    item = cast(list[dict[str, object]], payload["items"])[0]
    assert item["status"] == "closed"
    assert cast(list[dict[str, object]], item["reviews"])[0]["retro_text"] == "复盘原文"

    status, _, _ = _request(
        web_server,
        "POST",
        "/decisions/add",
        {"ticker": "SH688303", "direction": "neutral", "thesis": "x", "invalidation": "y"},
    )
    assert status == 403


def test_add_account_web_flow(web_server: ArchiveHttpServer) -> None:
    status, headers, content = _request(
        web_server,
        "POST",
        "/accounts/add",
        {"csrf_token": CSRF_TOKEN, "account": "https://xueqiu.com/u/1234567890", "note": "测试"},
    )
    assert status == 303
    assert headers["Location"] == "/"

    config_dir = web_server.config_dir
    managed = config_dir.joinpath("accounts.local.yml").read_text(encoding="utf-8")
    assert "1234567890" in managed

    # CSRF is enforced like every other mutation.
    status, _, _ = _request(
        web_server,
        "POST",
        "/accounts/add",
        {"account": "https://xueqiu.com/u/1234567890"},
    )
    assert status == 403

    # An unparseable input is rejected with a 400 rather than writing a garbage entry.
    status, _, _ = _request(
        web_server,
        "POST",
        "/accounts/add",
        {"csrf_token": CSRF_TOKEN, "account": "https://example.com/not-xueqiu"},
    )
    assert status == 400


def test_collect_run_once_web_flow(
    web_server: ArchiveHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kol_archive.cli.collect as collect_module

    calls: list[Path] = []
    auto_enrich_calls: list[tuple[ArchiveHttpServer, str]] = []

    def fake_run(
        config_dir: Path, *, progress: Callable[[str], None] | None = None
    ) -> collect_module.RunOnceResult:
        calls.append(config_dir)
        if progress is not None:
            progress("正在检查采集结果")
        return collect_module.RunOnceResult(healthy=True, reason=None)

    def fake_auto_enrich(server: ArchiveHttpServer, observed_since: str) -> bool:
        auto_enrich_calls.append((server, observed_since))
        return True

    monkeypatch.setattr(collect_module, "execute_run_once", fake_run)
    monkeypatch.setattr("kol_archive.web._start_auto_enrichment", fake_auto_enrich)
    status, _, content = _request(
        web_server, "POST", "/collect/run-once", {"csrf_token": CSRF_TOKEN}
    )
    assert status == 200
    payload = json.loads(content)
    assert payload["ok"] is True
    assert payload["healthy"] is True
    assert payload["message"] == "采集完成。"
    assert payload["auto_enrich_started"] is True
    assert calls == [web_server.config_dir]
    assert len(auto_enrich_calls) == 1
    assert auto_enrich_calls[0][0] is web_server
    assert datetime.fromisoformat(auto_enrich_calls[0][1]).tzinfo is not None
    status_payload = _get_json(web_server, "/api/collect/status")
    assert status_payload["running"] is False
    assert status_payload["phase"] == "采集完成。"
    assert status_payload["healthy"] is True
    collection_logs = cast(list[dict[str, object]], status_payload["logs"])
    assert [item["message"] for item in collection_logs] == [
        "正在启动采集",
        "正在检查采集结果",
        "采集完成。",
    ]

    # A completed-but-degraded pass surfaces the reason instead of claiming success.
    def degraded_run(
        config_dir: Path, *, progress: Callable[[str], None] | None = None
    ) -> collect_module.RunOnceResult:
        return collect_module.RunOnceResult(healthy=False, reason="run-once 连续失败")

    monkeypatch.setattr(collect_module, "execute_run_once", degraded_run)
    status, _, content = _request(
        web_server, "POST", "/collect/run-once", {"csrf_token": CSRF_TOKEN}
    )
    payload = json.loads(content)
    assert payload["healthy"] is False
    assert "run-once 连续失败" in payload["message"]


def test_collect_run_once_reports_browser_not_ready(
    web_server: ArchiveHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kol_archive.cli.collect as collect_module
    from kol_archive.browser import BrowserError

    def boom(
        config_dir: Path, *, progress: Callable[[str], None] | None = None
    ) -> collect_module.RunOnceResult:
        raise BrowserError("no cdp endpoint")

    monkeypatch.setattr(collect_module, "execute_run_once", boom)
    status, _, content = _request(
        web_server, "POST", "/collect/run-once", {"csrf_token": CSRF_TOKEN}
    )
    assert status == 503
    assert "浏览器" in content
    # The lock is released after a failed pass so the next click can still collect.
    assert web_server.collect_lock.acquire(blocking=False)
    web_server.collect_lock.release()


def test_collect_run_once_reports_cross_process_conflict(
    web_server: ArchiveHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kol_archive.cli.collect as collect_module

    def busy(
        config_dir: Path, *, progress: Callable[[str], None] | None = None
    ) -> collect_module.RunOnceResult:
        raise collect_module.RunLockError("已被其他进程占用")

    monkeypatch.setattr(collect_module, "execute_run_once", busy)
    status, _, content = _request(
        web_server, "POST", "/collect/run-once", {"csrf_token": CSRF_TOKEN}
    )
    assert status == 409
    assert "其他进程" in content
    # The in-process lock is freed even though the run never started.
    assert web_server.collect_lock.acquire(blocking=False)
    web_server.collect_lock.release()


def test_collect_run_once_guards_csrf_method_and_concurrency(
    web_server: ArchiveHttpServer,
) -> None:
    assert web_server.collect_lock is web_server.enrichment_lock
    status, _, _ = _request(web_server, "POST", "/collect/run-once", {})
    assert status == 403
    status, _, _ = _request(web_server, "GET", "/collect/run-once")
    assert status == 405

    # A run already in flight is rejected, not queued behind the active one.
    assert web_server.collect_lock.acquire(blocking=False)
    try:
        status, _, content = _request(
            web_server, "POST", "/collect/run-once", {"csrf_token": CSRF_TOKEN}
        )
    finally:
        web_server.collect_lock.release()
    assert status == 409
    assert "采集正在进行中" in content

    assert web_server.collect_lock.acquire(blocking=False)
    try:
        status, _, content = _request(
            web_server, "POST", "/authors/100/enrich", {"csrf_token": CSRF_TOKEN}
        )
    finally:
        web_server.collect_lock.release()
    assert status == 409
    assert "富化正在进行中" in content


def test_collect_status_reports_live_phase(
    web_server: ArchiveHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kol_archive.cli.collect as collect_module

    phase_reported = threading.Event()
    finish_run = threading.Event()
    response: list[tuple[int, dict[str, str], str]] = []

    def wait_in_phase(
        config_dir: Path, *, progress: Callable[[str], None] | None = None
    ) -> collect_module.RunOnceResult:
        assert progress is not None
        progress("正在采集博主 2/3 的近期发言")
        phase_reported.set()
        assert finish_run.wait(timeout=5)
        return collect_module.RunOnceResult(healthy=True, reason=None)

    monkeypatch.setattr(collect_module, "execute_run_once", wait_in_phase)
    request_thread = threading.Thread(
        target=lambda: response.append(
            _request(web_server, "POST", "/collect/run-once", {"csrf_token": CSRF_TOKEN})
        )
    )
    request_thread.start()
    assert phase_reported.wait(timeout=5)

    payload = _get_json(web_server, "/api/collect/status")
    assert payload["running"] is True
    assert payload["phase"] == "正在采集博主 2/3 的近期发言"
    assert isinstance(payload["elapsed_seconds"], int)
    assert payload["elapsed_seconds"] >= 0
    live_logs = cast(list[dict[str, object]], payload["logs"])
    assert live_logs[-1]["message"] == "正在采集博主 2/3 的近期发言"

    finish_run.set()
    request_thread.join(timeout=5)
    assert response[0][0] == 200


def test_enrich_author_web_flow(
    web_server: ArchiveHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_enrichment(
        settings: object, original_text: str, *, client: object
    ) -> EnrichmentResult:
        calls.append(original_text)
        return EnrichmentResult(
            post_type="观点",
            label_first_hand_info=False,
            label_transferable_framework=False,
            label_reasoned_non_consensus=False,
            rationale="测试富化",
            evidence_snippet="",
            stance_summary="",
        )

    monkeypatch.setattr("kol_archive.web.request_enrichment", fake_enrichment)
    status, _, content = _request(
        web_server,
        "POST",
        "/authors/100/enrich",
        {"csrf_token": CSRF_TOKEN},
    )

    assert status == 200
    payload = json.loads(content)
    assert payload["prompt_version"] == "enrich-v1"
    assert payload["candidates"] == 1
    assert payload["enriched"] == 1
    assert payload["failed"] == 0
    assert payload["details"] == [
        {
            "post_id": 1,
            "version_id": 1,
            "status": "success",
            "excerpt": "原始正文 A",
        }
    ]
    assert calls == ["原始正文 A"]
    status_payload = _get_json(web_server, "/api/enrich/status")
    assert status_payload["running"] is False
    assert status_payload["author_uid"] == "100"
    assert status_payload["processed"] == 1
    assert status_payload["enriched"] == 1
    assert status_payload["details"] == payload["details"]
    enrichment_logs = cast(list[dict[str, object]], status_payload["logs"])
    assert enrichment_logs[0]["message"] == "正在准备富化"
    assert enrichment_logs[1]["message"] == "准备富化 1 条发言"
    assert enrichment_logs[-1]["message"] == "富化完成，成功 1 条，失败 0 条"
    authors = cast(list[dict[str, object]], _get_json(web_server, "/api/home")["authors"])
    assert authors[0]["pending_enrichment_count"] == 0


def test_operations_home_lists_authors(web_server: ArchiveHttpServer) -> None:
    payload = _get_json(web_server, "/api/home?view=operations")
    authors = cast(list[dict[str, object]], payload["authors"])

    assert payload["view"] == "operations"
    assert authors[0]["author_platform_uid"] == "100"
    assert authors[0]["pending_enrichment_count"] == 1


def test_operations_status_combines_task_and_automation_state(
    web_server: ArchiveHttpServer,
) -> None:
    payload = _get_json(web_server, "/api/operations/status")
    collection = cast(dict[str, object], payload["collection"])
    enrichment = cast(dict[str, object], payload["enrichment"])
    automation = cast(dict[str, object], payload["automation"])

    assert collection["phase"] == "尚未开始采集"
    assert enrichment["phase"] == "尚未开始富化"
    assert automation["collection_interval_minutes"] == 180


def test_automation_settings_web_flow(web_server: ArchiveHttpServer) -> None:
    initial = _get_json(web_server, "/api/automation/settings")
    assert initial["collection_enabled"] is False
    assert initial["collection_interval_minutes"] == 180
    assert initial["auto_enrich"] is True

    status, _, content = _request(
        web_server,
        "POST",
        "/automation/settings",
        {
            "csrf_token": CSRF_TOKEN,
            "collection_enabled": "true",
            "collection_interval_minutes": "45",
            "auto_enrich": "false",
        },
    )
    assert status == 200
    payload = json.loads(content)
    assert payload["collection_enabled"] is True
    assert payload["collection_interval_minutes"] == 45
    assert payload["auto_enrich"] is False
    assert payload["next_collection_at"] is not None
    saved = json.loads(
        (web_server.db_path.parent / "web-automation.json").read_text(encoding="utf-8")
    )
    assert saved == {
        "collection_enabled": True,
        "collection_interval_minutes": 45,
        "auto_enrich": False,
    }

    status, _, content = _request(
        web_server,
        "POST",
        "/automation/settings",
        {
            "csrf_token": CSRF_TOKEN,
            "collection_enabled": "true",
            "collection_interval_minutes": "1",
            "auto_enrich": "true",
        },
    )
    assert status == 400
    assert "5 至 10080" in content


@pytest.mark.parametrize("content", ["null", "[]", "42", '"text"'])
def test_load_automation_settings_ignores_non_object_json(tmp_path: Path, content: str) -> None:
    db_path = tmp_path / "archive.sqlite3"
    (tmp_path / "web-automation.json").write_text(content, encoding="utf-8")

    settings = _load_automation_settings(db_path)

    assert settings.collection_enabled is False
    assert settings.collection_interval_minutes == 180
    assert settings.auto_enrich is True


def test_auto_enrichment_encodes_uid_and_passes_collection_boundary(
    web_server: ArchiveHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect_database(web_server.db_path)
    try:
        connection.execute(
            "UPDATE authors SET platform_uid = ? WHERE platform_uid = '100'",
            ("a/b %",),
        )
        connection.commit()
    finally:
        connection.close()
    web_server.automation_active = True
    calls: list[tuple[str, dict[str, str] | None]] = []
    completed = threading.Event()

    def fake_local_post(
        server: ArchiveHttpServer, path: str, values: dict[str, str] | None = None
    ) -> None:
        assert server is web_server
        calls.append((path, values))
        completed.set()

    monkeypatch.setattr("kol_archive.web._local_post", fake_local_post)

    assert _start_auto_enrichment(web_server, BASE_TIME) is True
    assert completed.wait(timeout=5)
    assert calls == [
        (
            "/authors/a%2Fb%20%25/enrich",
            {"observed_since": BASE_TIME},
        )
    ]


def test_enrich_author_observed_since_excludes_old_backlog(
    web_server: ArchiveHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_enrichment(
        settings: object, original_text: str, *, client: object
    ) -> EnrichmentResult:
        raise AssertionError("old backlog must not be enriched")

    monkeypatch.setattr("kol_archive.web.request_enrichment", unexpected_enrichment)
    status, _, content = _request(
        web_server,
        "POST",
        "/authors/100/enrich",
        {
            "csrf_token": CSRF_TOKEN,
            "observed_since": "2026-06-02T00:00:00+00:00",
        },
    )

    assert status == 200
    payload = json.loads(content)
    assert payload["candidates"] == 0
    assert payload["enriched"] == 0


def test_enrich_author_counts_database_failure_and_keeps_batch_status(
    web_server: ArchiveHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_enrichment(
        settings: object, original_text: str, *, client: object
    ) -> EnrichmentResult:
        return EnrichmentResult(
            post_type="观点",
            label_first_hand_info=False,
            label_transferable_framework=False,
            label_reasoned_non_consensus=False,
            rationale="测试富化",
            evidence_snippet="",
            stance_summary="",
        )

    def locked(*args: object, **kwargs: object) -> int | None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("kol_archive.web.request_enrichment", fake_enrichment)
    monkeypatch.setattr(Archive, "add_enrichment", locked)
    status, _, content = _request(
        web_server,
        "POST",
        "/authors/100/enrich",
        {"csrf_token": CSRF_TOKEN},
    )

    assert status == 200
    payload = json.loads(content)
    assert payload["candidates"] == 1
    assert payload["enriched"] == 0
    assert payload["failed"] == 1
    assert payload["details"] == [
        {
            "post_id": 1,
            "version_id": 1,
            "status": "failed",
            "excerpt": "原始正文 A",
            "error_type": "OperationalError",
            "error": "database is locked",
        }
    ]
    status_payload = _get_json(web_server, "/api/enrich/status")
    assert status_payload["running"] is False
    assert status_payload["author_uid"] == "100"
    assert status_payload["processed"] == 1
    assert status_payload["total"] == 1
    assert status_payload["failed"] == 1
    assert status_payload["details"] == payload["details"]


def test_watchlist_web_flow_and_csrf(web_server: ArchiveHttpServer) -> None:
    payload = _get_json(web_server, "/api/home?view=watchlist")
    assert payload["view"] == "watchlist"
    assert payload["items"] == []

    status, _, _ = _request(
        web_server,
        "POST",
        "/watchlist/add",
        {
            "csrf_token": CSRF_TOKEN,
            "ticker": "SH688303",
            "name": "大全能源",
            "note": "观察",
        },
    )
    assert status == 303
    payload = _get_json(web_server, "/api/home?view=watchlist")
    [item] = cast(list[dict[str, object]], payload["items"])
    assert item["ticker"] == "SH688303"
    assert item["name"] == "大全能源"

    status, _, _ = _request(
        web_server,
        "POST",
        "/watchlist/remove",
        {"csrf_token": CSRF_TOKEN, "ticker": "SH688303"},
    )
    assert status == 303
    assert _get_json(web_server, "/api/home?view=watchlist")["items"] == []

    status, _, _ = _request(web_server, "POST", "/watchlist/add", {"ticker": "SH688303"})
    assert status == 403


def test_claim_proposal_web_review_creates_claim(web_server: ArchiveHttpServer) -> None:
    _enrich_post_one(web_server)
    connection = connect_database(web_server.db_path)
    try:
        archive = Archive(connection)
        [target] = archive.claim_proposal_targets("claim-v1")
        cursor = connection.execute(
            """
            INSERT INTO claim_proposals(
                version_id, ticker, direction, evidence_snippet, model, prompt_version, created_at
            ) VALUES (?, 'SH000001', 'neutral', '原始正文 A', 'test-model', 'claim-v1', ?)
            """,
            (target.version_id, BASE_TIME),
        )
        assert cursor.lastrowid is not None
        proposal_id = int(cursor.lastrowid)
    finally:
        connection.close()

    payload = _get_json(web_server, "/api/home?view=claims&state=pending")
    assert cast(dict[str, int], payload["counts"])["pending"] == 1
    [item] = cast(list[dict[str, object]], payload["items"])
    assert item["id"] == proposal_id
    status, _, _ = _request(
        web_server,
        "POST",
        f"/claim-proposals/{proposal_id}/review",
        {"csrf_token": CSRF_TOKEN, "review_state": "accepted"},
    )
    assert status == 303
    payload = _get_json(web_server, "/api/home?view=claims&state=accepted")
    [item] = cast(list[dict[str, object]], payload["items"])
    assert item["claim_id"] is not None


def _enrich_post_one(server: ArchiveHttpServer, **labels: bool) -> None:
    connection = connect_database(server.db_path)
    try:
        archive = Archive(connection)
        [target] = archive.enrichment_targets("enrich-v1")
        archive.add_enrichment(
            target,
            EnrichmentResult(
                post_type="观点",
                label_first_hand_info=labels.get("first_hand", False),
                label_transferable_framework=labels.get("framework", False),
                label_reasoned_non_consensus=labels.get("non_consensus", False),
                rationale="理由",
                evidence_snippet="可证伪片段",
            ),
            "test-model",
            "enrich-v1",
            BASE_TIME,
        )
    finally:
        connection.close()


def test_layout_offers_persistent_light_dark_and_system_themes(
    web_server: ArchiveHttpServer,
) -> None:
    status, _, html = _request(web_server, "GET", "/")
    assert status == 200
    assert '<div id="app"></div>' in html
    asset_paths = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
    assert '<link rel="icon" type="image/png" href="/favicon.png">' in html
    assert '<link rel="apple-touch-icon" href="/app-icon.png">' in html
    favicon_response = _request_bytes(web_server, "/favicon.png")
    app_icon_response = _request_bytes(web_server, "/app-icon.png")
    assert favicon_response[0] == app_icon_response[0] == 200
    assert favicon_response[1]["Content-Type"] == "image/png"
    assert app_icon_response[1]["Content-Type"] == "image/png"
    assert 'localStorage.getItem("kol-theme")' in html
    assert html.index('localStorage.getItem("kol-theme")') < html.index('<div id="app"></div>')
    asset_responses = [_request(web_server, "GET", path) for path in asset_paths]
    assets = "".join(response[2] for response in asset_responses)
    assert all(
        response[1]["Cache-Control"] == "public, max-age=31536000, immutable"
        for response in asset_responses
    )
    assert ":root[data-theme=light]{color-scheme:light" in assets
    assert ":root[data-theme=dark]{color-scheme:dark" in assets
    for label in ("跟随系统", "浅色", "暗色", "kol-theme", "prefers-color-scheme: dark"):
        assert label in assets


def test_queue_view_keeps_label_guide(web_server: ArchiveHttpServer) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    payload = _get_json(web_server, "/api/home?view=queue")
    assert payload["view"] == "queue"
    assert payload["csrf_token"] == CSRF_TOKEN
    [item] = cast(list[dict[str, object]], payload["items"])
    assert item["label_reasoned_non_consensus"] == 1
    assert item["enrichment_evidence_snippet"] == "可证伪片段"
    assert item["platform_post_id"] == "post-1 & qa"


def test_queue_dequeues_after_pin(web_server: ArchiveHttpServer) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    before = _get_json(web_server, "/api/home?view=queue")
    assert len(cast(list[object], before["items"])) == 1

    status, _, _ = _request(web_server, "POST", "/posts/1/pin", {"csrf_token": CSRF_TOKEN})
    assert status == 303
    after = _get_json(web_server, "/api/home?view=queue")
    assert cast(list[object], after["items"]) == []


def test_pinned_view_lists_pinned_versions_and_offers_unpin(
    web_server: ArchiveHttpServer,
) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    # Before pinning: nothing pinned, toolbar count is zero, list is empty.
    before = _get_json(web_server, "/api/home?view=pinned")
    assert cast(dict[str, int], before["counts"])["pinned"] == 0
    assert cast(list[object], before["items"]) == []

    status, _, _ = _request(web_server, "POST", "/posts/1/pin", {"csrf_token": CSRF_TOKEN})
    assert status == 303

    # The home toolbar now exposes a clickable 已钉住 filter, and the count rose.
    home = _get_json(web_server, "/api/home?view=queue")
    assert cast(dict[str, int], home["counts"])["pinned"] == 1

    # The pinned view surfaces the dispositioned post with an unpin action.
    pinned = _get_json(web_server, "/api/home?view=pinned")
    [item] = cast(list[dict[str, object]], pinned["items"])
    assert item["platform_post_id"] == "post-1 & qa"


def test_pinned_list_includes_preview_only_post_matching_toolbar_count(
    web_server: ArchiveHttpServer,
) -> None:
    # A preview-only sighting never creates a full version (current_version_id
    # stays NULL), yet it can still be pinned from the evidence card. The list
    # must not silently drop it, or it would disagree with the 已钉住 count.
    connection = connect_database(web_server.db_path)
    try:
        archive = Archive(connection)
        archive.record_feed_run(
            replace(
                _make_feed_run(),
                started_at="2026-06-01T02:00:00+00:00",
                finished_at="2026-06-01T02:00:00+00:00",
            ),
            [
                NormalizedPost(
                    platform_post_id="post-preview",
                    author_id=1,
                    observed_at="2026-06-01T02:00:00+00:00",
                    content_fidelity=ContentFidelity.PREVIEW,
                    content_text=None,
                    content_hash=None,
                    posted_at_claimed=datetime.now(tz=UTC).isoformat(),
                    url="https://xueqiu.com/100/post-preview",
                    raw_payload={"text": "preview"},
                )
            ],
        )
        row = connection.execute(
            "SELECT id, current_version_id FROM posts WHERE platform_post_id = 'post-preview'"
        ).fetchone()
        assert row is not None
        assert row["current_version_id"] is None  # preview created no version
        preview_id = int(row["id"])
    finally:
        connection.close()

    status, _, _ = _request(
        web_server, "POST", f"/posts/{preview_id}/pin", {"csrf_token": CSRF_TOKEN}
    )
    assert status == 303

    pinned = _get_json(web_server, "/api/home?view=pinned")
    assert cast(dict[str, int], pinned["counts"])["pinned"] == 1
    [item] = cast(list[dict[str, object]], pinned["items"])
    assert item["platform_post_id"] == "post-preview"
    assert item["post_id"] == preview_id
    assert item["current_text"] is None


def test_authors_view_renders_recent_viewpoints_without_ranking(
    web_server: ArchiveHttpServer,
) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    payload = _get_json(web_server, "/api/home")
    [author] = cast(list[dict[str, object]], payload["authors"])
    [cluster] = cast(list[dict[str, object]], payload["clusters"])
    assert author["viewpoint_count"] == 1
    assert author["evaluated_viewpoint_count"] == 0
    assert author["latest_post_at"] == author["latest_viewpoint_at"]
    assert author["pending_enrichment_count"] == 0
    assert author["latest_enrichment_at"] is not None
    assert cluster["statement_count"] == 1
    assert (
        cast(list[dict[str, object]], cluster["viewpoints"])[0]["enrichment_evidence_snippet"]
        == "可证伪片段"
    )


def test_author_selector_only_renders_selected_author_viewpoints(
    web_server: ArchiveHttpServer,
) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    connection = connect_database(web_server.db_path)
    try:
        archive = Archive(connection)
        archive.add_author("xueqiu", "200", BASE_TIME, notes="第二位博主")
        second_post = replace(
            _make_post(),
            platform_post_id="post-2",
            author_id=2,
            content_text="第二位博主的观点",
            content_hash="hash-b",
            raw_payload={
                "stockCorrelation": ["SH000001"],
                "user": {"screen_name": "第二位博主"},
            },
        )
        archive.record_feed_run(replace(_make_feed_run(), author_id=2), [second_post])
        target = next(item for item in archive.enrichment_targets("enrich-v1") if item.post_id == 2)
        archive.add_enrichment(
            target,
            EnrichmentResult(
                post_type="观点",
                label_first_hand_info=False,
                label_transferable_framework=True,
                label_reasoned_non_consensus=False,
                rationale="理由",
                evidence_snippet="第二位博主的观点",
            ),
            "test-model",
            "enrich-v1",
            BASE_TIME,
        )
    finally:
        connection.close()

    first = _get_json(web_server, "/api/home")
    assert len(cast(list[object], first["authors"])) == 2
    first_text = json.dumps(first["clusters"], ensure_ascii=False)
    assert "第二位博主的观点" not in first_text

    second = _get_json(web_server, "/api/home?author=200")
    second_text = json.dumps(second["clusters"], ensure_ascii=False)
    assert "第二位博主的观点" in second_text
    assert "原始正文 A" not in second_text


def test_author_viewpoint_shows_recorded_market_relationship(
    web_server: ArchiveHttpServer,
) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    connection = connect_database(web_server.db_path)
    try:
        version_id = int(
            connection.execute("SELECT current_version_id FROM posts WHERE id = 1").fetchone()[0]
        )
        claim_id = connection.execute(
            """
            INSERT INTO claims(
                post_id, version_id, author_id, ticker, direction, horizon_days,
                target_price, confidence_phrasing, claim_made_at, ingest_mode, status, created_at
            ) VALUES (?, ?, 1, 'SH000300', 'long', 10, NULL, '看多', ?, 'live', 'resolved', ?)
            """,
            (1, version_id, BASE_TIME, BASE_TIME),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO claim_outcomes(
                claim_id, resolved_at, raw_return, benchmark_return, excess_return, notes
            ) VALUES (?, ?, 0.12, 0.03, 0.09, 'test')
            """,
            (claim_id, BASE_TIME),
        )
        second_claim_id = connection.execute(
            """
            INSERT INTO claims(
                post_id, version_id, author_id, ticker, direction, horizon_days,
                target_price, confidence_phrasing, claim_made_at, ingest_mode, status, created_at
            ) VALUES (?, ?, 1, 'SH000905', 'long', 10, NULL, '看多', ?, 'live', 'resolved', ?)
            """,
            (1, version_id, BASE_TIME, BASE_TIME),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO claim_outcomes(
                claim_id, resolved_at, raw_return, benchmark_return, excess_return, notes
            ) VALUES (?, ?, -0.10, -0.03, -0.07, 'test-2')
            """,
            (second_claim_id, BASE_TIME),
        )
        connection.commit()
    finally:
        connection.close()

    author = _get_json(web_server, "/api/authors/100")
    profile = cast(dict[str, object], author["profile"])
    author_text = json.dumps(profile["viewpoint_clusters"], ensure_ascii=False)
    for expected in ("SH000300", "SH000905", "0.12", "0.03", "0.09", "-0.07"):
        assert expected in author_text

    overview = _get_json(web_server, "/api/home")
    [summary] = cast(list[dict[str, object]], overview["authors"])
    assert summary["evaluated_viewpoint_count"] == 1


def test_recall_view_returns_form_without_groups(web_server: ArchiveHttpServer) -> None:
    payload = _get_json(web_server, "/api/home?view=recall")
    assert payload["view"] == "recall"
    assert payload["has_results"] is False
    assert cast(dict[str, object], payload["form"])["groups"] == []
    assert payload["csrf_token"] == CSRF_TOKEN


def test_recall_view_runs_deterministic_retrieval(web_server: ArchiveHttpServer) -> None:
    # The seeded post ("原始正文 A") falls inside a wide window regardless of run date.
    query = urlencode(
        {"view": "recall", "group": "text=原始", "from": "2026-01-01", "to": "2030-12-31"}
    )
    payload = _get_json(web_server, f"/api/home?view=recall&{query}")
    assert payload["has_results"] is True
    coverage = cast(dict[str, int], payload["coverage"])
    assert coverage["version_count"] == 1
    [hit] = cast(list[dict[str, object]], payload["hits"])
    assert hit["author_platform_uid"] == "100"
    assert hit["content_text"] == "原始正文 A"


def test_recall_expand_web_flow_and_guards(
    web_server: ArchiveHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kol_archive.recall_expand import ExpandedGroup, ExpandedQuery

    captured: list[str] = []

    def fake_expand(settings: object, question: str, **kwargs: object) -> ExpandedQuery:
        captured.append(question)
        return ExpandedQuery(
            groups=(
                ExpandedGroup("event", ("美伊", "伊朗")),
                ExpandedGroup("market", ("油价", "原油")),
            ),
            date_from="2025-06-10",
            date_to="2025-06-30",
            tickers=("SH601857",),
            notes="拆成事件与标的两组",
        )

    monkeypatch.setattr("kol_archive.web.expand_query", fake_expand)
    status, _, content = _request(
        web_server,
        "POST",
        "/recall/expand",
        {"csrf_token": CSRF_TOKEN, "question": "美伊冲突那阵油价怎么看"},
    )
    assert status == 200
    payload = json.loads(content)
    assert payload["ok"] is True
    assert payload["prompt_version"] == "expand-v1"
    assert payload["groups"] == [
        {"label": "event", "terms": ["美伊", "伊朗"]},
        {"label": "market", "terms": ["油价", "原油"]},
    ]
    assert payload["date_from"] == "2025-06-10"
    assert payload["tickers"] == ["SH601857"]
    assert captured == ["美伊冲突那阵油价怎么看"]

    # Token-spending expansion is POST + CSRF only; GET is rejected as a mutation path.
    status, _, _ = _request(web_server, "GET", "/recall/expand")
    assert status == 405
    status, _, _ = _request(web_server, "POST", "/recall/expand", {"question": "x"})
    assert status == 403


def test_web_settings_default_to_loopback_and_reject_wildcard_addresses() -> None:
    assert load_web_settings({}) == WebSettings()
    assert load_web_settings(
        {
            "web": {"bind_host": "100.64.0.8", "port": 9000},
            "prices": {"benchmark_ticker": "SZ399300"},
        }
    ) == WebSettings(bind_host="100.64.0.8", port=9000, market_benchmark_ticker="SZ399300")

    for host in ("0.0.0.0", "::", "[::]"):
        with pytest.raises(ValueError, match="explicit tailnet address"):
            load_web_settings({"web": {"bind_host": host}})
    with pytest.raises(ValueError, match="A-share ticker"):
        load_web_settings({"prices": {"benchmark_ticker": "SPY"}})
    with pytest.raises(ValueError, match="cluster_window_days"):
        load_web_settings({"web": {"viewpoint_cluster_window_days": 0}})


def test_web_settings_can_read_old_enrichment_during_prompt_migration() -> None:
    settings = load_web_settings(
        {
            "llm": {"enrich_prompt_version": "enrich-v2"},
            "web": {"enrich_prompt_version": "enrich-v1"},
        }
    )

    assert settings.enrich_prompt_version == "enrich-v1"


def test_read_routes_return_redacted_timeline_and_evidence_card(
    web_server: ArchiveHttpServer,
) -> None:
    timeline = _get_json(web_server, "/api/home?view=raw")
    [item] = cast(list[dict[str, object]], timeline["items"])
    assert item["author_display_name"] == "测试作者 & QA"
    assert item["current_text"] == "原始正文 A"
    assert cast(dict[str, str], item["status"])["human_label"] == (
        "feed：在场；来源：未复查；监控：近期窗口"
    )
    assert item["url"] == "https://xueqiu.com/100/post-1"

    payload = _get_json(web_server, "/api/posts/1")
    card = cast(dict[str, object], payload["card"])
    card_text = json.dumps(card, ensure_ascii=False)
    assert cast(dict[str, object], card["post"])["platform_post_id"] == "post-1 & qa"
    assert "测试作者 & QA" in card_text
    assert "cookie=[REDACTED]" in card_text
    ticker_history = cast(dict[str, object], card["ticker_history"])
    assert ticker_history["tickers"] == ["SH000001"]
    assert len(cast(list[object], ticker_history["items"])) == 1
    assert payload["csrf_token"] == CSRF_TOKEN
    for secret in ("feed-secret", "meta-secret", "payload-secret", "raw_meta", "raw_payload"):
        assert secret not in card_text

    status, _, _ = _request(web_server, "GET", "/api/posts/999")
    assert status == 404

    author = _get_json(web_server, "/api/authors/100")
    profile = cast(dict[str, object], author["profile"])
    assert cast(dict[str, object], profile["author"])["author_display_name"] == "测试作者 & QA"
    assert cast(dict[str, object], profile["author"])["author_description"] == "作者简介"
    assert cast(list[object], profile["viewpoint_clusters"]) == []
    assert len(cast(list[object], profile["posts"])) == 1

    analysis = _get_json(web_server, "/api/home?view=analysis")
    assert analysis["view"] == "analysis"
    assert analysis["selective_deletion"] == []
    assert analysis["crowding_events"] == []


def test_author_route_decodes_encoded_uid_segment() -> None:
    assert ArchiveRequestHandler._author_uid("/authors/user%2Fname%20A") == "user/name A"


def test_mutations_only_accept_post_with_valid_csrf(web_server: ArchiveHttpServer) -> None:
    status, _, _ = _request(web_server, "GET", "/posts/1/pin")
    assert status == 405

    status, _, _ = _request(web_server, "POST", "/posts/1/pin", {})
    assert status == 403

    status, _, _ = _request(
        web_server,
        "POST",
        "/posts/1/pin",
        {"csrf_token": "é"},
    )
    assert status == 403

    status, headers, _ = _request(
        web_server,
        "POST",
        "/posts/1/pin",
        {"csrf_token": CSRF_TOKEN},
    )
    assert status == 303
    assert headers["Location"] == "/posts/1"
    assert _read_post_row(web_server)[0] == WatchMode.PINNED

    status, _, _ = _request(
        web_server,
        "POST",
        "/posts/1/unpin",
        {"csrf_token": CSRF_TOKEN},
    )
    assert status == 303
    assert _read_post_row(web_server)[0] == WatchMode.RECENT_WINDOW


def test_attention_and_verdict_routes_reuse_archive_writes(web_server: ArchiveHttpServer) -> None:
    _, version_id = _read_post_row(web_server)
    status, _, _ = _request(
        web_server,
        "POST",
        "/posts/1/attention",
        {
            "csrf_token": CSRF_TOKEN,
            "version_id": str(version_id),
            "reason": "继续跟踪 <script>alert(1)</script>",
            "expectation": "观察后续兑现",
        },
    )
    assert status == 303

    connection = connect_database(web_server.db_path)
    try:
        attention = connection.execute(
            "SELECT version_id, my_reason, my_expectation FROM attention_log"
        ).fetchone()
        assert attention is not None
        assert tuple(attention) == (
            version_id,
            "继续跟踪 <script>alert(1)</script>",
            "观察后续兑现",
        )
        archive = Archive(connection)
        exercise_id = archive.add_rewrite_exercise(
            RewriteSource(post_id=1, version_id=version_id, original_text="原始正文 A"),
            "训练命题",
            "保留原文边界",
            "test-model",
            "rewrite-v1",
            BASE_TIME,
        )
    finally:
        connection.close()

    payload = _get_json(web_server, "/api/posts/1")
    card = cast(dict[str, object], payload["card"])
    [attention_item] = cast(list[dict[str, object]], card["attention_log"])
    assert attention_item["my_reason"] == "继续跟踪 <script>alert(1)</script>"

    status, _, _ = _request(
        web_server,
        "POST",
        f"/rewrite-exercises/{exercise_id}/verdict",
        {"csrf_token": CSRF_TOKEN, "post_id": "1", "verdict": "valid"},
    )
    assert status == 303
    connection = connect_database(web_server.db_path)
    try:
        row = connection.execute(
            "SELECT my_verdict FROM rewrite_exercises WHERE id = ?",
            (exercise_id,),
        ).fetchone()
        assert row is not None
        assert row["my_verdict"] == "valid"
    finally:
        connection.close()


def test_rewrite_route_locks_version_and_pins_post(
    web_server: ArchiveHttpServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, version_id = _read_post_row(web_server)
    monkeypatch.setattr(
        "kol_archive.web.request_rewrite",
        lambda settings, original_text: RewriteSuggestion(
            rewritten_claim=f"命题：{original_text}",
            rationale=f"模型：{settings.model}",
        ),
    )

    status, _, _ = _request(
        web_server,
        "POST",
        "/posts/1/rewrite",
        {"csrf_token": CSRF_TOKEN, "version_id": str(version_id)},
    )
    assert status == 303

    connection = connect_database(web_server.db_path)
    try:
        row = connection.execute(
            """
            SELECT r.version_id, r.original_text, r.llm_rewritten_claim, r.prompt_version,
                   p.watch_mode
            FROM rewrite_exercises r JOIN posts p ON p.id = r.post_id
            """
        ).fetchone()
        assert row is not None
        assert tuple(row) == (
            version_id,
            "原始正文 A",
            "命题：原始正文 A",
            "rewrite-v1",
            WatchMode.PINNED,
        )
    finally:
        connection.close()
