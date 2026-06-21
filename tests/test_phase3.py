"""Phase 3: batch LLM enrichment + label-gate filter stream."""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest

from kol_archive.cli import claims as kol_main
from kol_archive.database import connect_database, initialize_database
from kol_archive.enrich import (
    EnrichSettings,
    enrich_targets,
    load_enrich_settings,
    request_enrichment,
)
from kol_archive.models import (
    ContentFidelity,
    EnrichmentResult,
    EnrichmentTarget,
    FeedRun,
    IngestMode,
    LoginState,
    NormalizedPost,
    ProbeResult,
    ProbeRun,
    RunStatus,
)
from kol_archive.presentation import (
    author_recent_viewpoint_clusters,
    author_recent_viewpoints,
    author_scorecards,
    author_viewpoint_overview,
    build_evidence_card,
    list_attention_queue,
    list_filtered_timeline,
    list_timeline,
)
from kol_archive.service import Archive

BASE_TIME = "2026-06-01T00:00:00+00:00"


@pytest.fixture
def archive() -> Iterator[Archive]:
    connection = connect_database(":memory:")
    initialize_database(connection)
    service = Archive(connection)
    service.add_author("xueqiu", "100", BASE_TIME)
    try:
        yield service
    finally:
        connection.close()


def make_feed_run(finished_at: str = BASE_TIME) -> FeedRun:
    return FeedRun(
        author_id=1,
        platform="xueqiu",
        started_at=finished_at,
        finished_at=finished_at,
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
        notes=None,
    )


def make_post(
    platform_post_id: str = "post-1", *, observed_at: str = BASE_TIME, text: str = "A"
) -> NormalizedPost:
    return NormalizedPost(
        platform_post_id=platform_post_id,
        author_id=1,
        observed_at=observed_at,
        content_fidelity=ContentFidelity.FULL,
        content_text=text,
        content_hash=f"hash-{text}",
        posted_at_claimed="2026-05-20T00:00:00+00:00",
        url=f"https://xueqiu.com/100/{platform_post_id}",
        raw_payload={"stockCorrelation": ["SH000001"]},
    )


def post_id(archive: Archive, platform_post_id: str = "post-1") -> int:
    row = archive.connection.execute(
        "SELECT id FROM posts WHERE platform_post_id = ?", (platform_post_id,)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def make_result(
    *,
    first_hand: bool = False,
    framework: bool = False,
    non_consensus: bool = False,
    post_type: str = "观点",
    snippet: str = "片段",
    stance_summary: str = "",
) -> EnrichmentResult:
    return EnrichmentResult(
        post_type=post_type,
        label_first_hand_info=first_hand,
        label_transferable_framework=framework,
        label_reasoned_non_consensus=non_consensus,
        rationale="理由",
        evidence_snippet=snippet,
        stance_summary=stance_summary,
    )


def _llm_response(payload: dict[str, object]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
        )

    return httpx.MockTransport(handle)


def _settings() -> EnrichSettings:
    return EnrichSettings(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="secret-key",
        prompt_version="enrich-v1",
    )


# ── LLM transport / parsing ──────────────────────────────────────────────


def test_load_enrich_settings_reads_key_and_prompt_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret-key")
    settings = load_enrich_settings(
        {
            "llm": {
                "provider": "openai_compatible",
                "base_url": "https://llm.example/v1/",
                "model": "test-model",
                "api_key_env": "TEST_LLM_KEY",
                "prompt_version": "rewrite-v1",
                "enrich_prompt_version": "enrich-v2",
            }
        }
    )
    assert settings == EnrichSettings(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="secret-key",
        prompt_version="enrich-v2",
    )


def test_request_enrichment_parses_structured_labels() -> None:
    original = "我实地走访了三家门店，结论与市场共识相反。"
    payload = {
        "post_type": "研究",
        "label_first_hand_info": True,
        "label_transferable_framework": False,
        "label_reasoned_non_consensus": True,
        "rationale": "给出原始调研数据并提出非共识判断",
        "stance_summary": "作者认为门店结论与市场共识相反",
        "evidence_snippet": "我实地走访了三家门店",
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        result = request_enrichment(_settings(), original, client=client)
    assert result == EnrichmentResult(
        post_type="研究",
        label_first_hand_info=True,
        label_transferable_framework=False,
        label_reasoned_non_consensus=True,
        rationale="给出原始调研数据并提出非共识判断",
        evidence_snippet="我实地走访了三家门店",
        stance_summary="作者认为门店结论与市场共识相反",
    )


def test_request_enrichment_allows_empty_snippet() -> None:
    payload = {
        "post_type": "情绪",
        "label_first_hand_info": False,
        "label_transferable_framework": False,
        "label_reasoned_non_consensus": False,
        "rationale": "原文无可摘录的可证伪内容",
        "evidence_snippet": "",
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        result = request_enrichment(_settings(), "随便说说", client=client)
    assert result.evidence_snippet == ""
    assert result.post_type == "情绪"


@pytest.mark.parametrize(
    "payload",
    [
        {  # boolean field missing
            "post_type": "观点",
            "label_transferable_framework": False,
            "label_reasoned_non_consensus": False,
            "rationale": "理由",
            "evidence_snippet": "",
        },
        {  # boolean field is not a JSON bool
            "post_type": "观点",
            "label_first_hand_info": "yes",
            "label_transferable_framework": False,
            "label_reasoned_non_consensus": False,
            "rationale": "理由",
            "evidence_snippet": "",
        },
        {  # empty post_type
            "post_type": "",
            "label_first_hand_info": False,
            "label_transferable_framework": False,
            "label_reasoned_non_consensus": False,
            "rationale": "理由",
            "evidence_snippet": "",
        },
    ],
)
def test_request_enrichment_rejects_malformed_response(payload: dict[str, object]) -> None:
    with httpx.Client(transport=_llm_response(payload)) as client:
        with pytest.raises(ValueError):
            request_enrichment(_settings(), "原文", client=client)


def test_request_enrichment_rejects_hallucinated_snippet() -> None:
    # A snippet that does not appear in the original would become durable false
    # evidence in the filter stream / card, so it must be rejected.
    payload = {
        "post_type": "观点",
        "label_first_hand_info": True,
        "label_transferable_framework": False,
        "label_reasoned_non_consensus": False,
        "rationale": "理由",
        "evidence_snippet": "我实地走访了三家门店",
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        with pytest.raises(ValueError, match="returned snippet: 我实地走访了三家门店"):
            request_enrichment(_settings(), "今天天气不错。", client=client)


def test_request_enrichment_reports_invalid_json_excerpt() -> None:
    response = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"post_type": "观点" trailing'}}]},
        )
    )
    with httpx.Client(transport=response) as client:
        with pytest.raises(ValueError, match=r'response excerpt: \{"post_type": "观点" trailing'):
            request_enrichment(_settings(), "原文", client=client)


def test_request_enrichment_accepts_json_markdown_fence() -> None:
    payload = {
        "post_type": "观点",
        "label_first_hand_info": False,
        "label_transferable_framework": False,
        "label_reasoned_non_consensus": False,
        "rationale": "理由",
        "stance_summary": "",
        "evidence_snippet": "",
    }
    content = f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"
    response = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    )
    with httpx.Client(transport=response) as client:
        result = request_enrichment(_settings(), "原文", client=client)
    assert result.post_type == "观点"


def test_request_enrichment_retries_invalid_json_once() -> None:
    calls: list[dict[str, object]] = []
    valid = {
        "post_type": "情绪",
        "label_first_hand_info": False,
        "label_transferable_framework": False,
        "label_reasoned_non_consensus": False,
        "rationale": "口号式陈述",
        "stance_summary": "认为运力时代到来",
        "evidence_snippet": "时代到来",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        content = (
            '{"post_type":"情绪","evidence_snippet":"“运力""时代到来！"}'
            if len(calls) == 1
            else json.dumps(valid, ensure_ascii=False)
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = request_enrichment(_settings(), '“运力""时代到来！', client=client)

    assert result.evidence_snippet == "时代到来"
    assert len(calls) == 2
    messages = cast(list[dict[str, str]], calls[1]["messages"])
    assert "上次输出未通过严格校验" in messages[-1]["content"]


def test_request_enrichment_requires_snippet_when_a_label_fires() -> None:
    # A label that gates a post into the filter stream must carry its evidence.
    payload = {
        "post_type": "研究",
        "label_first_hand_info": True,
        "label_transferable_framework": False,
        "label_reasoned_non_consensus": False,
        "rationale": "理由",
        "evidence_snippet": "",
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        with pytest.raises(ValueError, match="non-empty evidence_snippet"):
            request_enrichment(_settings(), "我实地走访了三家门店。", client=client)


def test_request_enrichment_tolerates_whitespace_in_snippet() -> None:
    # The model may reflow whitespace; comparison strips it before matching.
    payload = {
        "post_type": "研究",
        "label_first_hand_info": True,
        "label_transferable_framework": False,
        "label_reasoned_non_consensus": False,
        "rationale": "理由",
        "evidence_snippet": "我 实地  走访了\n三家门店",
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        result = request_enrichment(_settings(), "我实地走访了三家门店，结论如下。", client=client)
    assert result.label_first_hand_info is True
    assert result.evidence_snippet == "我实地走访了三家门店"


def test_request_enrichment_restores_original_quote_style_in_snippet() -> None:
    original = '$澜起科技(SH688008)$“运力""时代到来！'
    payload = {
        "post_type": "情绪",
        "label_first_hand_info": False,
        "label_transferable_framework": False,
        "label_reasoned_non_consensus": False,
        "rationale": "口号式陈述",
        "stance_summary": "认为运力时代到来",
        "evidence_snippet": '$澜起科技(SH688008)$"运力""时代到来！',
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        result = request_enrichment(_settings(), original, client=client)
    assert result.evidence_snippet == original


# ── concurrent batch runner ──────────────────────────────────────────────


def _valid_payload() -> dict[str, object]:
    # All labels false + empty snippet is the minimal contract-valid response.
    return {
        "post_type": "观点",
        "label_first_hand_info": False,
        "label_transferable_framework": False,
        "label_reasoned_non_consensus": False,
        "rationale": "示例依据",
        "stance_summary": "",
        "evidence_snippet": "",
    }


def _make_target(n: int) -> EnrichmentTarget:
    return EnrichmentTarget(post_id=n, version_id=n, original_text=f"原文{n}", raw_payload=None)


def test_load_enrich_settings_reads_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret-key")
    base = {
        "provider": "openai_compatible",
        "base_url": "https://llm.example/v1",
        "model": "test-model",
        "api_key_env": "TEST_LLM_KEY",
    }
    assert load_enrich_settings({"llm": base}).concurrency == 1
    assert load_enrich_settings({"llm": {**base, "enrich_concurrency": 6}}).concurrency == 6
    with pytest.raises(ValueError, match="enrich_concurrency"):
        load_enrich_settings({"llm": {**base, "enrich_concurrency": 0}})


def test_enrich_targets_runs_llm_calls_in_parallel() -> None:
    # A barrier of N only releases once all N requests are simultaneously in
    # flight, so it would deadlock (BrokenBarrierError) under a sequential runner.
    import threading

    targets = [_make_target(n) for n in range(4)]
    barrier = threading.Barrier(len(targets), timeout=5)

    def handle(request: httpx.Request) -> httpx.Response:
        barrier.wait()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_valid_payload())}}]},
        )

    settings = dataclasses.replace(_settings(), concurrency=len(targets))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        results = list(enrich_targets(settings, targets, client=client))

    assert len(results) == len(targets)
    assert all(result is not None and error is None for _, result, error in results)
    assert {target.version_id for target, _, _ in results} == {t.version_id for t in targets}


def test_enrich_targets_surfaces_per_target_errors_without_aborting() -> None:
    targets = [_make_target(n) for n in range(3)]

    def handle(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content.decode())["messages"][1]["content"]
        if text == "原文1":
            return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_valid_payload())}}]},
        )

    settings = dataclasses.replace(_settings(), concurrency=3)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        by_version = {
            target.version_id: (result, error)
            for target, result, error in enrich_targets(settings, targets, client=client)
        }

    assert len(by_version) == 3
    result_1, error_1 = by_version[1]
    assert result_1 is None and isinstance(error_1, ValueError)
    for version_id in (0, 2):
        result, error = by_version[version_id]
        assert error is None and result is not None


def test_enrich_targets_sequential_preserves_input_order() -> None:
    targets = [_make_target(n) for n in range(5)]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_valid_payload())}}]},
        )

    settings = dataclasses.replace(_settings(), concurrency=1)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        order = [
            target.version_id for target, _, _ in enrich_targets(settings, targets, client=client)
        ]

    assert order == [t.version_id for t in targets]


# ── service: targets + persistence ───────────────────────────────────────


def test_enrichment_targets_skip_already_enriched_per_prompt_version(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    [target] = archive.enrichment_targets("enrich-v1")

    enrichment_id = archive.add_enrichment(
        target, make_result(first_hand=True), "test-model", "enrich-v1", BASE_TIME
    )
    assert enrichment_id is not None

    # The same prompt version no longer lists it; a different version still does.
    assert archive.enrichment_targets("enrich-v1") == []
    assert [t.version_id for t in archive.enrichment_targets("enrich-v2")] == [target.version_id]


def test_add_enrichment_is_idempotent_on_unique_key(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    [target] = archive.enrichment_targets("enrich-v1")

    first = archive.add_enrichment(target, make_result(), "test-model", "enrich-v1", BASE_TIME)
    second = archive.add_enrichment(target, make_result(), "test-model", "enrich-v1", BASE_TIME)
    assert first is not None
    assert second is None

    count = archive.connection.execute("SELECT COUNT(*) FROM enrichments").fetchone()[0]
    assert count == 1


def test_add_enrichment_persists_stance_summary(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    [target] = archive.enrichment_targets("enrich-v1")

    archive.add_enrichment(
        target,
        make_result(stance_summary="作者继续看好该标的"),
        "test-model",
        "enrich-v1",
        BASE_TIME,
    )

    assert (
        archive.connection.execute("SELECT stance_summary FROM enrichments").fetchone()[0]
        == "作者继续看好该标的"
    )


def test_add_enrichment_persists_market_relation_from_archived_payload(archive: Archive) -> None:
    unrelated = dataclasses.replace(
        make_post("unrelated", text="今天跑步状态很好"),
        raw_payload={"text": "今天跑步状态很好"},
    )
    archive.record_feed_run(make_feed_run(), [unrelated, make_post("related")])
    for target in archive.enrichment_targets("enrich-v1"):
        archive.add_enrichment(target, make_result(), "test-model", "enrich-v1", BASE_TIME)

    rows = archive.connection.execute(
        """
        SELECT p.platform_post_id, e.is_market_related
        FROM enrichments e JOIN posts p ON p.id = e.post_id
        ORDER BY p.platform_post_id
        """
    ).fetchall()

    assert [(row["platform_post_id"], row["is_market_related"]) for row in rows] == [
        ("related", 1),
        ("unrelated", 0),
    ]


def test_market_relation_lookup_indexes_exist(archive: Archive) -> None:
    enrichment_indexes = {
        row["name"] for row in archive.connection.execute("PRAGMA index_list(enrichments)")
    }
    claim_indexes = {row["name"] for row in archive.connection.execute("PRAGMA index_list(claims)")}

    assert "idx_enrichments_market_viewpoints" in enrichment_indexes
    assert "idx_claims_version_id" in claim_indexes


def test_initialize_database_backfills_legacy_market_relation_column() -> None:
    connection = connect_database(":memory:")
    connection.executescript(
        """
        CREATE TABLE post_versions (
            id INTEGER PRIMARY KEY,
            content_text TEXT NOT NULL,
            raw_payload TEXT
        );
        CREATE TABLE enrichments (
            id INTEGER PRIMARY KEY,
            version_id INTEGER NOT NULL,
            prompt_version TEXT NOT NULL,
            post_type TEXT NOT NULL
        );
        INSERT INTO post_versions(id, content_text, raw_payload)
        VALUES
            (1, '生活随笔', '{"text":"生活随笔"}'),
            (2, '继续看好', '{"stockCorrelation":["SH688777"]}');
        INSERT INTO enrichments(id, version_id, prompt_version, post_type)
        VALUES (1, 1, 'enrich-v1', '观点'), (2, 2, 'enrich-v1', '观点');
        """
    )

    initialize_database(connection)

    rows = connection.execute(
        "SELECT id, is_market_related FROM enrichments ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["is_market_related"]) for row in rows] == [(1, 0), (2, 1)]
    connection.close()


def test_enrichment_targets_post_id_filter_and_limit(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(), [make_post("post-1", text="A"), make_post("post-2", text="B")]
    )
    only_one = archive.enrichment_targets("enrich-v1", post_id=post_id(archive, "post-1"))
    assert [t.post_id for t in only_one] == [post_id(archive, "post-1")]

    assert len(archive.enrichment_targets("enrich-v1", limit=1)) == 1


def test_enrichment_targets_can_scope_current_versions_to_author(archive: Archive) -> None:
    second_author_id = archive.add_author("xueqiu", "200", BASE_TIME)
    archive.record_feed_run(
        make_feed_run(),
        [make_post("post-1", text="A"), make_post("post-2", text="B")],
    )
    archive.record_feed_run(
        dataclasses.replace(make_feed_run(), author_id=second_author_id),
        [dataclasses.replace(make_post("post-3", text="C"), author_id=second_author_id)],
    )

    targets = archive.enrichment_targets("enrich-v1", author_id=1, current_only=True)

    assert [target.original_text for target in targets] == ["A", "B"]


# ── presentation: filter stream + evidence card ──────────────────────────


def test_filtered_timeline_only_includes_label_hits(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(), [make_post("hit", text="H"), make_post("miss", text="M")]
    )
    hit_target = next(
        t for t in archive.enrichment_targets("enrich-v1") if t.post_id == post_id(archive, "hit")
    )
    miss_target = next(
        t for t in archive.enrichment_targets("enrich-v1") if t.post_id == post_id(archive, "miss")
    )
    archive.add_enrichment(
        hit_target,
        make_result(framework=True, post_type="研究", snippet="可迁移框架"),
        "test-model",
        "enrich-v1",
        BASE_TIME,
    )
    archive.add_enrichment(miss_target, make_result(), "test-model", "enrich-v1", BASE_TIME)

    filtered = list_filtered_timeline(archive.connection, "enrich-v1")
    assert [item["post_id"] for item in filtered] == [post_id(archive, "hit")]
    assert filtered[0]["post_type"] == "研究"
    assert filtered[0]["label_transferable_framework"] == 1
    assert filtered[0]["enrichment_evidence_snippet"] == "可迁移框架"

    # The raw stream still shows every post — the filter never hides evidence.
    assert {item["post_id"] for item in list_timeline(archive.connection)} == {
        post_id(archive, "hit"),
        post_id(archive, "miss"),
    }


def test_filtered_timeline_isolated_by_prompt_version(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    [target] = archive.enrichment_targets("enrich-v1")
    archive.add_enrichment(target, make_result(first_hand=True), "m", "enrich-v1", BASE_TIME)

    assert len(list_filtered_timeline(archive.connection, "enrich-v1")) == 1
    assert list_filtered_timeline(archive.connection, "enrich-v2") == []


def test_timeline_offset_paginates_without_overlap(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post(f"p{i}", text=f"T{i}") for i in range(5)])
    full = [item["post_id"] for item in list_timeline(archive.connection)]
    assert len(full) == 5

    pages = [
        [item["post_id"] for item in list_timeline(archive.connection, limit=2, offset=off)]
        for off in (0, 2, 4)
    ]
    # Consecutive offset windows tile the full stream with no gaps or repeats.
    assert pages[0] + pages[1] + pages[2] == full
    assert len(pages[0]) == 2 and len(pages[2]) == 1
    assert list_timeline(archive.connection, limit=2, offset=5) == []
    with pytest.raises(ValueError):
        list_timeline(archive.connection, offset=-1)


def test_timeline_cursor_paginates_from_full_sort_tuple(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post(f"p{i}", text=f"T{i}") for i in range(5)])
    full = [item["post_id"] for item in list_timeline(archive.connection)]

    first = list_timeline(archive.connection, limit=2)
    second = list_timeline(archive.connection, limit=2, cursor=str(first[-1]["_cursor"]))
    third = list_timeline(archive.connection, limit=2, cursor=str(second[-1]["_cursor"]))

    assert [item["post_id"] for item in first + second + third] == full
    assert len({item["_cursor"] for item in first + second + third}) == len(full)
    with pytest.raises(ValueError, match="invalid timeline cursor"):
        list_timeline(archive.connection, cursor="not-a-cursor")


def test_evidence_card_exposes_enrichments(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    [target] = archive.enrichment_targets("enrich-v1")
    archive.add_enrichment(
        target,
        make_result(first_hand=True, post_type="研究", snippet="实地调研"),
        "test-model",
        "enrich-v1",
        BASE_TIME,
    )

    card = build_evidence_card(archive.connection, post_id(archive))
    assert len(card["enrichments"]) == 1
    enrichment = card["enrichments"][0]
    assert enrichment["post_type"] == "研究"
    assert enrichment["label_first_hand_info"] == 1
    assert enrichment["prompt_version"] == "enrich-v1"


# ── presentation: attention queue (pure-derivation) ──────────────────────


def _enrich(
    archive: Archive,
    platform_post_id: str,
    result: EnrichmentResult,
    prompt_version: str = "enrich-v1",
) -> None:
    target = next(
        t
        for t in archive.enrichment_targets(prompt_version)
        if t.post_id == post_id(archive, platform_post_id)
    )
    archive.add_enrichment(target, result, "test-model", prompt_version, BASE_TIME)


def test_attention_queue_orders_by_tier_and_excludes_label_misses(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(),
        [make_post("t1", text="T1"), make_post("t3", text="T3"), make_post("miss", text="MISS")],
    )
    _enrich(archive, "t1", make_result(first_hand=True, snippet="片段"))
    _enrich(
        archive,
        "t3",
        make_result(first_hand=True, framework=True, non_consensus=True, snippet="片段"),
    )
    _enrich(archive, "miss", make_result(snippet="片段"))

    queue = list_attention_queue(archive.connection, "enrich-v1")
    # Densest signal (3 labels) floats above the single-label hit; the miss is absent.
    assert [item["post_id"] for item in queue] == [
        post_id(archive, "t3"),
        post_id(archive, "t1"),
    ]
    assert queue[0]["tier"] == 3
    assert queue[0]["version_count"] == 1


def test_attention_queue_evidence_source_covers_direct_probe_versions(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post("p", text="A")])
    pid = post_id(archive, "p")
    # A healthy reachable full probe observes changed content → it creates the new
    # current version via Track B, which has NO feed observation of its own.
    observed = make_post("p", text="B", observed_at="2026-06-02T00:00:00+00:00")
    probe_id = archive.record_probe_run(
        ProbeRun(
            post_id=pid,
            started_at=observed.observed_at,
            finished_at=observed.observed_at,
            observed_at=observed.observed_at,
            status=RunStatus.OK,
            http_status=200,
            login_state=LoginState.VALID,
            rate_limited=False,
            result=ProbeResult.REACHABLE,
            content_fidelity=ContentFidelity.FULL,
            ingest_mode=IngestMode.LIVE,
            adapter_version="xueqiu-2",
        ),
        observed,
    )
    current_vid = archive.current_version_id(pid)
    target = next(t for t in archive.enrichment_targets("enrich-v1") if t.version_id == current_vid)
    archive.add_enrichment(
        target, make_result(first_hand=True, snippet="片段"), "test-model", "enrich-v1", BASE_TIME
    )

    [item] = list_attention_queue(archive.connection, "enrich-v1")
    # Evidence source resolves to the direct-link probe, not an empty fetch_run.
    assert item["latest_evidence_channel"] == "direct"
    assert item["latest_evidence_run_id"] == probe_id
    assert item["current_content_fidelity"] == "full"
    assert item["version_count"] == 2


def test_attention_queue_dequeues_after_pin(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post("hit", text="H")])
    _enrich(archive, "hit", make_result(non_consensus=True, snippet="片段"))
    pid = post_id(archive, "hit")
    assert [item["post_id"] for item in list_attention_queue(archive.connection, "enrich-v1")] == [
        pid
    ]

    archive.pin_post(pid, BASE_TIME)
    assert list_attention_queue(archive.connection, "enrich-v1") == []


def test_attention_queue_floats_recently_reobserved_version(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run("2026-06-01T00:00:00+00:00"),
        [
            make_post("A", text="A", observed_at="2026-06-01T00:00:00+00:00"),
            make_post("B", text="B", observed_at="2026-06-01T00:00:00+00:00"),
        ],
    )
    _enrich(archive, "A", make_result(first_hand=True, snippet="片段"))
    _enrich(archive, "B", make_result(first_hand=True, snippet="片段"))
    pa, pb = post_id(archive, "A"), post_id(archive, "B")
    # Same tier, same first/last observation → tie broken by post id desc.
    before = [item["post_id"] for item in list_attention_queue(archive.connection, "enrich-v1")]
    assert before == [pb, pa]

    # Re-observe A after the weekly positive-observation throttle. B is absent
    # this round, but its streak stays below the threshold and it remains queued.
    archive.record_feed_run(
        make_feed_run("2026-06-08T00:00:00+00:00"),
        [make_post("A", text="A", observed_at="2026-06-08T00:00:00+00:00")],
    )
    after = [item["post_id"] for item in list_attention_queue(archive.connection, "enrich-v1")]
    assert after == [pa, pb]  # A's newer observation floats it above B


def test_attention_queue_stays_out_after_attention_even_when_unpinned(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post("hit", text="H")])
    _enrich(archive, "hit", make_result(framework=True, snippet="片段"))
    pid = post_id(archive, "hit")

    archive.add_attention(pid, archive.current_version_id(pid), BASE_TIME, "值得跟踪")
    assert list_attention_queue(archive.connection, "enrich-v1") == []

    # Unpinning drops watch_mode back to recent_window; the attention_log entry is
    # what must keep an already-dispositioned version out of the queue.
    archive.unpin_post(pid, BASE_TIME, within_recent_window=True)
    assert list_attention_queue(archive.connection, "enrich-v1") == []


def test_author_scorecards_are_unranked_counts_without_hit_rate(archive: Archive) -> None:
    archive.add_author("xueqiu", "200", BASE_TIME)
    # author 1 (uid 100, id 1): 2 enriched, 0 hits
    archive.record_feed_run(
        make_feed_run(), [make_post("a1p1", text="A1"), make_post("a1p2", text="A2")]
    )
    run2 = dataclasses.replace(make_feed_run(), author_id=2)
    posts2 = [
        dataclasses.replace(make_post("a2p1", text="B1"), author_id=2),
        dataclasses.replace(make_post("a2p2", text="B2"), author_id=2),
    ]
    archive.record_feed_run(run2, posts2)  # author 2 (uid 200, id 2): 2 enriched, 1 hit

    _enrich(archive, "a1p1", make_result(snippet="片段"))
    _enrich(archive, "a1p2", make_result(snippet="片段"))
    _enrich(archive, "a2p1", make_result(first_hand=True, framework=True, snippet="片段"))
    _enrich(archive, "a2p2", make_result(snippet="片段"))

    result = author_scorecards(archive.connection, "enrich-v1")
    cards = cast(list[dict[str, object]], result["scorecards"])
    # Charter §0.11: neutral author-id order, NOT ranked by hit rate; the denser
    # author (200) must NOT be floated above the sparser one (100).
    assert [card["author_platform_uid"] for card in cards] == ["100", "200"]
    assert all("density_pct" not in card for card in cards)
    assert (cards[0]["enriched"], cards[0]["hit"]) == (2, 0)
    assert (cards[1]["enriched"], cards[1]["hit"]) == (2, 1)
    assert (cards[1]["first_hand"], cards[1]["framework"]) == (1, 1)
    assert result["label_scale"] == 1


def test_author_recent_viewpoints_only_returns_latest_ten_viewpoints(archive: Archive) -> None:
    posts = [make_post(f"view-{index}", text=f"观点 {index}") for index in range(12)]
    posts.append(make_post("research", text="研究"))
    archive.record_feed_run(make_feed_run(), posts)
    for index in range(12):
        _enrich(archive, f"view-{index}", make_result())
    _enrich(archive, "research", make_result(post_type="研究"))

    viewpoints = author_recent_viewpoints(archive.connection, "100", "enrich-v1")

    assert len(viewpoints) == 10
    assert [item["platform_post_id"] for item in viewpoints] == [
        f"view-{index}" for index in range(11, 1, -1)
    ]
    assert all(item["platform_post_id"] != "research" for item in viewpoints)


def test_author_recent_viewpoints_excludes_market_unrelated_opinions(archive: Archive) -> None:
    unrelated = dataclasses.replace(
        make_post("unrelated", text="今天跑步状态很好"),
        raw_payload={"text": "今天跑步状态很好"},
    )
    related = make_post("related", text="继续看好市场")
    archive.record_feed_run(make_feed_run(), [unrelated, related])
    _enrich(archive, "unrelated", make_result())
    _enrich(archive, "related", make_result())

    viewpoints = author_recent_viewpoints(archive.connection, "100", "enrich-v1")
    overview = author_viewpoint_overview(archive.connection, "enrich-v1")

    assert [item["platform_post_id"] for item in viewpoints] == ["related"]
    assert overview[0]["viewpoint_count"] == 1
    assert overview[0]["latest_post_at"] == overview[0]["latest_viewpoint_at"]
    assert overview[0]["pending_enrichment_count"] == 0
    assert overview[0]["latest_enrichment_at"] == BASE_TIME


def test_author_viewpoint_overview_exposes_pending_enrichment(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post("pending", text="待富化发言")])

    [overview] = author_viewpoint_overview(archive.connection, "enrich-v1")

    assert overview["viewpoint_count"] == 0
    assert overview["latest_post_at"] is not None
    assert overview["latest_viewpoint_at"] is None
    assert overview["pending_enrichment_count"] == 1
    assert overview["latest_enrichment_at"] is None


def test_author_recent_viewpoint_clusters_groups_shared_nested_ticker(archive: Archive) -> None:
    original = dataclasses.replace(
        make_post("original", text="$中控技术(SH688777)$ 首次观点"),
        raw_payload={"stockCorrelation": ["SH688777"]},
    )
    reply = dataclasses.replace(
        make_post("reply", text="回复：继续强化"),
        raw_payload={"retweeted_status": {"stockCorrelation": ["SH688777"]}},
    )
    archive.record_feed_run(make_feed_run(), [original, reply])
    _enrich(archive, "original", make_result())
    _enrich(archive, "reply", make_result())

    clusters = author_recent_viewpoint_clusters(archive.connection, "100", "enrich-v1")

    assert len(clusters) == 1
    assert clusters[0]["ticker"] == "SH688777"
    assert clusters[0]["title"] == "中控技术（SH688777）"
    assert clusters[0]["statement_count"] == 2
    assert [
        item["platform_post_id"]
        for item in cast(list[dict[str, object]], clusters[0]["viewpoints"])
    ] == ["reply", "original"]


def test_author_recent_viewpoint_clusters_reads_structured_name_and_market_snapshot(
    archive: Archive,
) -> None:
    viewpoint = dataclasses.replace(
        make_post("structured", observed_at="2026-06-03T10:00:00+00:00", text="继续关注"),
        posted_at_claimed=None,
        raw_payload={
            "stockCorrelation": [{"symbol": "SH688777", "name": "中控技术"}],
        },
    )
    archive.record_feed_run(make_feed_run("2026-06-03T10:00:00+00:00"), [viewpoint])
    _enrich(archive, "structured", make_result())
    archive.connection.executemany(
        "INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)",
        [
            ("SH688777", "2026-06-02", 10.0),
            ("SH000300", "2026-06-02", 100.0),
            ("SH688777", "2026-06-04", 12.0),
            ("SH000300", "2026-06-04", 105.0),
            ("SH688777", "2026-06-05", 11.0),
            ("SH000300", "2026-06-05", 102.0),
        ],
    )

    [cluster] = author_recent_viewpoint_clusters(archive.connection, "100", "enrich-v1")

    assert cluster["title"] == "中控技术（SH688777）"
    snapshot = cast(dict[str, object], cluster["market_snapshot"])
    assert snapshot["start_date"] == "2026-06-02"
    assert snapshot["end_date"] == "2026-06-05"
    assert snapshot["raw_return"] == pytest.approx(0.1)
    assert snapshot["benchmark_return"] == pytest.approx(0.02)
    assert snapshot["excess_return"] == pytest.approx(0.08)
    assert snapshot["method_version"] == "descriptive-common-close-v1"


def test_market_snapshot_uses_beijing_date_across_utc_day_boundary(archive: Archive) -> None:
    viewpoint = dataclasses.replace(
        make_post("early-utc", observed_at="2026-06-03T22:00:00+00:00", text="继续关注"),
        posted_at_claimed=None,
        raw_payload={"stockCorrelation": ["SH688777"]},
    )
    archive.record_feed_run(make_feed_run("2026-06-03T22:00:00+00:00"), [viewpoint])
    _enrich(archive, "early-utc", make_result())
    archive.connection.executemany(
        "INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)",
        [
            ("SH688777", "2026-06-02", 10.0),
            ("SH000300", "2026-06-02", 100.0),
            ("SH688777", "2026-06-03", 12.0),
            ("SH000300", "2026-06-03", 105.0),
            ("SH688777", "2026-06-04", 13.0),
            ("SH000300", "2026-06-04", 110.0),
        ],
    )

    [cluster] = author_recent_viewpoint_clusters(archive.connection, "100", "enrich-v1")

    snapshot = cast(dict[str, object], cluster["market_snapshot"])
    assert snapshot["start_date"] == "2026-06-03"
    assert snapshot["end_date"] == "2026-06-04"


def test_author_recent_viewpoint_clusters_uses_local_name_and_configurable_window(
    archive: Archive,
) -> None:
    older = dataclasses.replace(
        make_post("older", observed_at="2026-05-01T00:00:00+00:00", text="较早观点"),
        posted_at_claimed=None,
        raw_payload={"stockCorrelation": ["BJ920982"]},
    )
    newer = dataclasses.replace(
        make_post("newer", observed_at="2026-05-20T00:00:00+00:00", text="后续观点"),
        posted_at_claimed=None,
        raw_payload={"stockCorrelation": ["BJ920982"]},
    )
    archive.record_feed_run(make_feed_run("2026-05-20T00:00:00+00:00"), [older, newer])
    _enrich(archive, "older", make_result())
    _enrich(archive, "newer", make_result())
    archive.connection.execute(
        "INSERT INTO ticker_names(ticker, name) VALUES ('BJ920982', '锦波生物')"
    )

    default_clusters = author_recent_viewpoint_clusters(archive.connection, "100", "enrich-v1")
    widened_clusters = author_recent_viewpoint_clusters(
        archive.connection, "100", "enrich-v1", cluster_window_days=30
    )

    assert len(default_clusters) == 2
    assert len(widened_clusters) == 1
    assert widened_clusters[0]["title"] == "锦波生物（BJ920982）"
    assert widened_clusters[0]["statement_count"] == 2


def test_author_recent_viewpoint_clusters_use_rolling_window_and_observation_fallback(
    archive: Archive,
) -> None:
    posts = []
    for index, observed_at in enumerate(
        (
            "2026-06-03T00:00:00+00:00",
            "2026-06-08T00:00:00+00:00",
            "2026-06-13T00:00:00+00:00",
        )
    ):
        posts.append(
            dataclasses.replace(
                make_post(f"chain-{index}", observed_at=observed_at, text=f"观点 {index}"),
                posted_at_claimed=None,
                raw_payload={"stockCorrelation": ["SH688777"]},
            )
        )
    archive.record_feed_run(make_feed_run("2026-06-13T00:00:00+00:00"), posts)
    for index in range(3):
        _enrich(archive, f"chain-{index}", make_result())

    clusters = author_recent_viewpoint_clusters(archive.connection, "100", "enrich-v1")

    assert len(clusters) == 1
    assert clusters[0]["statement_count"] == 3
    assert clusters[0]["latest_at"] == "2026-06-13T00:00:00+00:00"
    assert clusters[0]["first_at"] == "2026-06-03T00:00:00+00:00"


def test_author_viewpoint_overview_is_one_lightweight_summary_query(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post("viewpoint", text="观点")])
    _enrich(archive, "viewpoint", make_result())
    selects: list[str] = []
    archive.connection.set_trace_callback(
        lambda statement: (
            selects.append(statement) if statement.lstrip().upper().startswith("SELECT") else None
        )
    )

    overview = author_viewpoint_overview(archive.connection, "enrich-v1")

    archive.connection.set_trace_callback(None)
    assert len(selects) == 1
    assert "json_tree" not in selects[0]
    assert " GLOB " not in selects[0]
    assert overview[0]["viewpoint_count"] == 1
    assert "viewpoint_clusters" not in overview[0]


# ── CLI: batch enrich ────────────────────────────────────────────────────


def _write_config(config_dir: Path, db_path: Path) -> None:
    db = str(db_path).replace(chr(92), "/")
    (config_dir / "config.yml").write_text(
        "\n".join(
            [
                "storage:",
                f"  db_path: {db}",
                "llm:",
                "  provider: openai_compatible",
                "  base_url: https://llm.example/v1",
                "  model: test-model",
                "  api_key_env: TEST_LLM_KEY",
                "  enrich_prompt_version: enrich-v1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _seed_two_posts(db_path: Path) -> None:
    connection = connect_database(db_path)
    initialize_database(connection)
    seed = Archive(connection)
    seed.add_author("xueqiu", "100", BASE_TIME)
    seed.record_feed_run(
        make_feed_run(), [make_post("post-1", text="A"), make_post("post-2", text="B")]
    )
    connection.close()


def test_enrich_command_labels_pending_versions_and_survives_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret-key")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    db_path = tmp_path / "kol.sqlite3"
    _write_config(config_dir, db_path)
    _seed_two_posts(db_path)

    calls: list[str] = []

    def fake_request(
        settings: EnrichSettings, text: str, *, client: object = None
    ) -> EnrichmentResult:
        calls.append(text)
        if text == "B":
            raise httpx.ConnectError("boom")  # one version fails this run
        return make_result(first_hand=True, post_type="研究")

    monkeypatch.setattr("kol_archive.enrich.request_enrichment", fake_request)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    args = argparse.Namespace(
        post_id=None, limit=None, prompt_version=None, path=None, config_dir=config_dir
    )
    kol_main._enrich_command(args)

    summary = json.loads(stdout.getvalue())
    assert summary["candidates"] == 2
    assert summary["enriched"] == 1
    assert summary["failed"] == 1
    assert sorted(calls) == ["A", "B"]

    # The failed version stays pending so a later run retries it.
    connection = connect_database(db_path)
    try:
        pending = Archive(connection).enrichment_targets("enrich-v1")
    finally:
        connection.close()
    assert [t.original_text for t in pending] == ["B"]
