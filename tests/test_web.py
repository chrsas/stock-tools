from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
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
    _avatar_url,
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


def test_queue_view_keeps_label_guide(web_server: ArchiveHttpServer) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    status, _, html = _request(web_server, "GET", "/?view=queue")
    assert status == 200
    assert "待处理注意力" in html
    assert "标签说明" in html  # label guide must stay (explicit requirement)
    assert "有据非共识" in html  # the fired label pill
    assert "可证伪片段" in html  # evidence snippet surfaced
    assert "雪球 post-1" in html  # the queued post card uses the platform post id
    assert "本地记录 1" in html  # internal id is clearly labeled as local-only
    assert f'name="csrf_token" value="{CSRF_TOKEN}"' in html  # pin form CSRF
    # Charter §0.11: the default home carries no per-author hit-rate / ranking.
    assert "账号标签构成" not in html
    assert "密度" not in html


def test_queue_dequeues_after_pin(web_server: ArchiveHttpServer) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    _, _, before = _request(web_server, "GET", "/?view=queue")
    assert "雪球 post-1" in before

    status, _, _ = _request(web_server, "POST", "/posts/1/pin", {"csrf_token": CSRF_TOKEN})
    assert status == 303
    _, _, after = _request(web_server, "GET", "/?view=queue")
    assert "雪球 post-1" not in after  # pinned -> dispositioned -> out of the queue


def test_pinned_view_lists_pinned_versions_and_offers_unpin(
    web_server: ArchiveHttpServer,
) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    # Before pinning: nothing pinned, toolbar count is zero, list is empty.
    _, _, before = _request(web_server, "GET", "/?view=pinned")
    assert "已钉住 0" in before
    assert "还没有钉住任何版本" in before
    assert "雪球 post-1" not in before

    status, _, _ = _request(web_server, "POST", "/posts/1/pin", {"csrf_token": CSRF_TOKEN})
    assert status == 303

    # The home toolbar now exposes a clickable 已钉住 filter, and the count rose.
    _, _, home = _request(web_server, "GET", "/?view=queue")
    assert 'href="/?view=pinned"' in home
    assert "已钉住 1" in home
    assert "操作说明" in home  # the persistent action guide stays in the aside

    # The pinned view surfaces the dispositioned post with an unpin action.
    status, _, pinned = _request(web_server, "GET", "/?view=pinned")
    assert status == 200
    assert "雪球 post-1" in pinned
    assert "取消钉住" in pinned
    assert "钉住当前版本" not in pinned  # already pinned: no re-pin button
    assert f'name="csrf_token" value="{CSRF_TOKEN}"' in pinned  # unpin form CSRF


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

    _, _, pinned = _request(web_server, "GET", "/?view=pinned")
    assert "已钉住 1" in pinned  # toolbar count
    assert "雪球 post-preview" in pinned  # and the list agrees, one-for-one
    assert f"本地记录 {preview_id}" in pinned
    assert "暂无完整正文版本" in pinned  # placeholder instead of an empty body


def test_authors_view_renders_recent_viewpoints_without_ranking(
    web_server: ArchiveHttpServer,
) -> None:
    _enrich_post_one(web_server, non_consensus=True)
    status, _, html = _request(web_server, "GET", "/")
    assert status == 200
    assert "博主最近观点" in html
    assert "观点发言 1 · 已评估观点 0" in html
    assert "最近 1 个观点簇" in html
    assert "观点依据「可证伪片段」" in html
    assert "尚未提取可证伪命题" in html
    assert 'aria-label="博主列表"' in html
    assert "选择博主" in html
    assert 'href="/?author=100"' in html
    assert 'class="author-option active"' in html
    assert "密度" not in html  # no hit-rate metric / ranking label


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
            raw_payload={"user": {"screen_name": "第二位博主"}},
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

    _, _, first = _request(web_server, "GET", "/")
    assert "第二位博主" in first  # visible in the author list
    assert "第二位博主的观点" not in first  # first author remains selected

    _, _, second = _request(web_server, "GET", "/?author=200")
    assert 'href="/?author=200"' in second
    assert "第二位博主的观点" in second
    assert "原始正文 A" not in second


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
            ) VALUES (?, ?, 0.10, 0.03, 0.07, 'test-2')
            """,
            (second_claim_id, BASE_TIME),
        )
        connection.commit()
    finally:
        connection.close()

    status, _, author = _request(web_server, "GET", "/authors/100")
    assert status == 200
    assert "最近 10 个观点簇与市场变化" in author
    assert "SH000300 · long · 10 天" in author
    assert "标的变化 +12.00%" in author
    assert "基准变化 +3.00%" in author
    assert "超额变化 +9.00%" in author

    status, _, overview = _request(web_server, "GET", "/")
    assert status == 200
    assert "观点发言 1 · 已评估观点 1" in overview


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
    status, _, timeline = _request(web_server, "GET", "/?view=raw")
    assert status == 200
    assert "KOL 原始时间线" in timeline
    assert "测试作者" in timeline
    assert 'src="https://xqimg.imedao.com/community/avatar.jpg!50x50.png"' in timeline
    assert "原始正文 A" in timeline
    assert "feed：在场；来源：未复查；监控：近期窗口" in timeline
    assert 'href="https://xueqiu.com/100/post-1"' in timeline
    assert 'href="https://xueqiu.com/u/100"' in timeline
    assert 'href="/authors/100"' in timeline
    assert 'target="_blank"' in timeline

    status, _, card = _request(web_server, "GET", "/posts/1")
    assert status == 200
    assert "<title>证据卡片 雪球 post-1 &amp; qa</title>" in card
    assert "&amp;amp; qa</title>" not in card
    assert "证据卡片：雪球 post-1" in card
    assert "测试作者" in card
    assert "本地记录 1" in card
    assert 'href="https://xueqiu.com/100/post-1"' in card
    assert "打开雪球原帖" in card
    assert "cookie=[REDACTED]" in card
    assert f'name="csrf_token" value="{CSRF_TOKEN}"' in card
    for secret in ("feed-secret", "meta-secret", "payload-secret", "raw_meta", "raw_payload"):
        assert secret not in card

    status, _, _ = _request(web_server, "GET", "/posts/999")
    assert status == 404

    status, _, author = _request(web_server, "GET", "/authors/100")
    assert status == 200
    assert "<title>作者 测试作者 &amp; QA</title>" in author
    assert "&amp;amp; QA</title>" not in author
    assert "作者 测试作者" in author
    assert "作者简介" in author
    assert "最近 10 个观点簇与市场变化" in author
    assert "最近还没有被富化为“观点”的发言" in author
    assert "雪球 post-1" in author
    assert 'href="https://xueqiu.com/u/100"' in author


def test_avatar_url_only_mints_known_xqimg_relative_keys() -> None:
    assert _avatar_url("community/avatar.jpg!50x50.png") == (
        "https://xqimg.imedao.com/community/avatar.jpg!50x50.png"
    )
    assert _avatar_url("javascript:alert(1)") == ""
    assert _avatar_url("other-cdn/avatar.jpg") == ""


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
