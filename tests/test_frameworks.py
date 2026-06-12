from __future__ import annotations

from typing import cast

import pytest

from kol_archive.database import connect_database, initialize_database
from kol_archive.framework import parse_framework_result
from kol_archive.models import (
    ContentFidelity,
    EnrichmentResult,
    FeedRun,
    FrameworkExtractionResult,
    IngestMode,
    LoginState,
    NormalizedPost,
    RunStatus,
)
from kol_archive.presentation import framework_library
from kol_archive.service import Archive
from kol_archive.web import _home_payload

NOW = "2026-06-01T00:00:00+00:00"
FRAMEWORK_TEXT = "看存货周转天数和毛利率变化，两者同时恶化两个季度就排除，仅适用于制造业。"
PLAIN_TEXT = "今天大盘不错。"


def _enrichment(*, transferable: bool, snippet: str) -> EnrichmentResult:
    return EnrichmentResult(
        post_type="观点",
        label_first_hand_info=False,
        label_transferable_framework=transferable,
        label_reasoned_non_consensus=False,
        rationale="测试标签",
        evidence_snippet=snippet if transferable else "",
    )


def _archive() -> Archive:
    connection = connect_database(":memory:")
    initialize_database(connection)
    archive = Archive(connection)
    author_id = archive.add_author("xueqiu", "100", NOW)
    archive.record_feed_run(
        FeedRun(
            author_id=author_id,
            platform="xueqiu",
            started_at=NOW,
            finished_at=NOW,
            status=RunStatus.OK,
            login_state=LoginState.VALID,
            pages_fetched=1,
            pagination_complete=True,
            covered_from=NOW,
            covered_to=NOW,
            rate_limited=False,
            http_error_count=0,
            ingest_mode=IngestMode.LIVE,
            adapter_version="test",
        ),
        [
            NormalizedPost(
                platform_post_id="post-1",
                author_id=author_id,
                observed_at=NOW,
                content_fidelity=ContentFidelity.FULL,
                content_text=FRAMEWORK_TEXT,
                content_hash="hash-1",
                ingest_mode=IngestMode.LIVE,
            ),
            NormalizedPost(
                platform_post_id="post-2",
                author_id=author_id,
                observed_at=NOW,
                content_fidelity=ContentFidelity.FULL,
                content_text=PLAIN_TEXT,
                content_hash="hash-2",
                ingest_mode=IngestMode.LIVE,
            ),
        ],
    )
    for target in archive.enrichment_targets("enrich-v2"):
        archive.add_enrichment(
            target,
            _enrichment(
                transferable=target.original_text == FRAMEWORK_TEXT,
                snippet="看存货周转天数和毛利率变化",
            ),
            "test-model",
            "enrich-v2",
            NOW,
        )
    return archive


def _result() -> FrameworkExtractionResult:
    return FrameworkExtractionResult(
        topic="财报分析",
        summary="用存货与毛利率联动变化做排除筛选。",
        input_variables=("存货周转天数", "毛利率变化"),
        logic_chain="存货周转天数与毛利率同时恶化两个季度，判定基本面变差，排除该标的。",
        conclusion_shape="排除筛选条件",
        applicability_conditions="仅适用于制造业",
        invalidation_conditions="",
        evidence_snippet="看存货周转天数和毛利率变化",
    )


def test_parse_framework_result_validates_shape_and_verbatim_snippet() -> None:
    parsed = {
        "framework_found": True,
        "topic": "财报分析",
        "summary": "用存货与毛利率联动变化做排除筛选。",
        "input_variables": ["存货周转天数", "毛利率变化", "存货周转天数"],
        "logic_chain": "两者同时恶化两个季度即排除。",
        "conclusion_shape": "排除筛选条件",
        "applicability_conditions": "仅适用于制造业",
        "invalidation_conditions": "",
        "evidence_snippet": "看存货周转天数和毛利率变化",
    }
    result = parse_framework_result(parsed, FRAMEWORK_TEXT)
    assert result is not None
    # duplicates are collapsed, order preserved
    assert result.input_variables == ("存货周转天数", "毛利率变化")

    assert parse_framework_result({"framework_found": False}, FRAMEWORK_TEXT) is None
    with pytest.raises(ValueError, match="framework_found"):
        parse_framework_result({"framework_found": "yes"}, FRAMEWORK_TEXT)
    with pytest.raises(ValueError, match="verbatim"):
        parse_framework_result({**parsed, "evidence_snippet": "稳赚方法论"}, FRAMEWORK_TEXT)
    with pytest.raises(ValueError, match="input_variables"):
        parse_framework_result({**parsed, "input_variables": []}, FRAMEWORK_TEXT)
    with pytest.raises(ValueError, match="logic_chain"):
        parse_framework_result({**parsed, "logic_chain": "  "}, FRAMEWORK_TEXT)


def test_framework_targets_gate_on_transferable_label() -> None:
    archive = _archive()
    targets = archive.framework_targets("framework-v1")
    assert [target.original_text for target in targets] == [FRAMEWORK_TEXT]


def test_extraction_is_idempotent_and_resumable() -> None:
    archive = _archive()
    [target] = archive.framework_targets("framework-v1")
    extraction_id = archive.add_framework_extraction(
        target, _result(), "test-model", "framework-v1", NOW
    )
    assert extraction_id is not None
    # scanned versions drop out of the batch, so a rerun continues past them
    assert archive.framework_targets("framework-v1") == []
    # a duplicate add is ignored without erroring (idempotent rerun)
    assert archive.add_framework_extraction(target, _result(), "m", "framework-v1", NOW) is None
    count = archive.connection.execute("SELECT COUNT(*) FROM framework_extractions").fetchone()[0]
    assert count == 1


def test_none_found_scan_is_recorded_and_not_retried() -> None:
    archive = _archive()
    [target] = archive.framework_targets("framework-v1")
    assert archive.add_framework_extraction(target, None, "test-model", "framework-v1", NOW) is None
    assert archive.framework_targets("framework-v1") == []
    count = archive.connection.execute("SELECT COUNT(*) FROM framework_extractions").fetchone()[0]
    assert count == 0


def test_prompt_upgrade_rescans_without_touching_prior_rows() -> None:
    archive = _archive()
    [target] = archive.framework_targets("framework-v1")
    archive.add_framework_extraction(target, _result(), "test-model", "framework-v1", NOW)
    # the existing migration mechanism: a new prompt version re-queues the version
    [retarget] = archive.framework_targets("framework-v2")
    assert retarget.version_id == target.version_id
    archive.add_framework_extraction(retarget, _result(), "test-model", "framework-v2", NOW)
    rows = archive.connection.execute(
        "SELECT prompt_version FROM framework_extractions ORDER BY id"
    ).fetchall()
    assert [row["prompt_version"] for row in rows] == ["framework-v1", "framework-v2"]


def test_framework_library_links_back_and_aggregates() -> None:
    archive = _archive()
    [target] = archive.framework_targets("framework-v1")
    archive.add_framework_extraction(target, _result(), "test-model", "framework-v1", NOW)
    library = framework_library(archive.connection, "framework-v1")
    [item] = cast(list[dict[str, object]], library["items"])
    assert item["version_id"] == target.version_id
    assert item["post_id"] == target.post_id
    assert item["input_variables"] == ["存货周转天数", "毛利率变化"]
    assert item["source_readable"] is True
    assert item["is_current_version"] is True
    assert library["topics"] == [{"topic": "财报分析", "count": 1}]
    assert {"variable": "存货周转天数", "count": 1} in cast(
        list[dict[str, object]], library["variables"]
    )
    # topic/variable filters narrow items but keep the aggregations
    assert framework_library(archive.connection, "framework-v1", topic="别的")["items"] == []
    assert framework_library(archive.connection, "framework-v1", variable="毛利率变化")["items"]


def test_framework_survives_source_removal_with_state_annotation() -> None:
    archive = _archive()
    [target] = archive.framework_targets("framework-v1")
    archive.add_framework_extraction(target, _result(), "test-model", "framework-v1", NOW)
    archive.connection.execute(
        "UPDATE posts SET source_state = 'gone_confirmed', feed_state = 'absent_confirmed' "
        "WHERE id = ?",
        (target.post_id,),
    )
    [item] = cast(
        list[dict[str, object]], framework_library(archive.connection, "framework-v1")["items"]
    )
    assert item["source_readable"] is False
    assert "来源页明确显示已移除" in str(item["source_status_label"])
    # the framework itself stays fully usable
    assert item["logic_chain"]
    assert item["content_text"] == FRAMEWORK_TEXT


def test_home_payload_serves_frameworks_view() -> None:
    archive = _archive()
    [target] = archive.framework_targets("framework-v1")
    archive.add_framework_extraction(target, _result(), "test-model", "framework-v1", NOW)
    payload = _home_payload(
        archive.connection,
        "enrich-v2",
        50,
        "view=frameworks&topic=%E8%B4%A2%E6%8A%A5%E5%88%86%E6%9E%90",
    )
    assert payload["view"] == "frameworks"
    assert payload["topic"] == "财报分析"
    assert len(cast(list[object], payload["items"])) == 1
