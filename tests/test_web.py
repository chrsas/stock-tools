from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import pytest

from kol_archive.database import connect_database, initialize_database
from kol_archive.models import (
    ContentFidelity,
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
from kol_archive.web import ArchiveHttpServer, WebSettings, create_server, load_web_settings

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
        platform_post_id="post-1",
        author_id=1,
        observed_at=BASE_TIME,
        content_fidelity=ContentFidelity.FULL,
        content_text="原始正文 A",
        content_hash="hash-a",
        posted_at_claimed=datetime.now(tz=UTC).isoformat(),
        url="https://xueqiu.com/100/post-1",
        raw_meta={"cookie": "meta-secret"},
        raw_payload={"token": "payload-secret"},
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
        WebSettings(port=0),
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


def test_web_settings_default_to_loopback_and_reject_wildcard_addresses() -> None:
    assert load_web_settings({}) == WebSettings()
    assert load_web_settings({"web": {"bind_host": "100.64.0.8", "port": 9000}}) == WebSettings(
        bind_host="100.64.0.8",
        port=9000,
    )

    for host in ("0.0.0.0", "::", "[::]"):
        with pytest.raises(ValueError, match="explicit tailnet address"):
            load_web_settings({"web": {"bind_host": host}})


def test_read_routes_render_redacted_timeline_and_evidence_card(
    web_server: ArchiveHttpServer,
) -> None:
    status, _, timeline = _request(web_server, "GET", "/")
    assert status == 200
    assert "KOL 原始时间线" in timeline
    assert "原始正文 A" in timeline
    assert "feed：在场；来源：未复查；监控：近期窗口" in timeline

    status, _, card = _request(web_server, "GET", "/posts/1")
    assert status == 200
    assert "证据卡片：帖子 1" in card
    assert "cookie=[REDACTED]" in card
    assert f'name="csrf_token" value="{CSRF_TOKEN}"' in card
    for secret in ("feed-secret", "meta-secret", "payload-secret", "raw_meta", "raw_payload"):
        assert secret not in card

    status, _, _ = _request(web_server, "GET", "/posts/999")
    assert status == 404


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

    status, _, card = _request(web_server, "GET", "/posts/1")
    assert status == 200
    assert "继续跟踪 &lt;script&gt;alert(1)&lt;/script&gt;" in card
    assert "<script>alert(1)</script>" not in card

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
