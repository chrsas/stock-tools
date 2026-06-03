"""Phase 3: batch LLM enrichment + label-gate filter stream."""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from kol_archive import __main__ as kol_main
from kol_archive.database import connect_database, initialize_database
from kol_archive.enrich import EnrichSettings, load_enrich_settings, request_enrichment
from kol_archive.models import (
    ContentFidelity,
    EnrichmentResult,
    FeedRun,
    IngestMode,
    LoginState,
    NormalizedPost,
    RunStatus,
)
from kol_archive.presentation import (
    build_evidence_card,
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
) -> EnrichmentResult:
    return EnrichmentResult(
        post_type=post_type,
        label_first_hand_info=first_hand,
        label_transferable_framework=framework,
        label_reasoned_non_consensus=non_consensus,
        rationale="理由",
        evidence_snippet=snippet,
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
        with pytest.raises(ValueError, match="verbatim excerpt"):
            request_enrichment(_settings(), "今天天气不错。", client=client)


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


def test_enrichment_targets_post_id_filter_and_limit(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(), [make_post("post-1", text="A"), make_post("post-2", text="B")]
    )
    only_one = archive.enrichment_targets("enrich-v1", post_id=post_id(archive, "post-1"))
    assert [t.post_id for t in only_one] == [post_id(archive, "post-1")]

    assert len(archive.enrichment_targets("enrich-v1", limit=1)) == 1


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

    def fake_request(settings: EnrichSettings, text: str) -> EnrichmentResult:
        calls.append(text)
        if text == "B":
            raise httpx.ConnectError("boom")  # one version fails this run
        return make_result(first_hand=True, post_type="研究")

    monkeypatch.setattr(kol_main, "request_enrichment", fake_request)
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
