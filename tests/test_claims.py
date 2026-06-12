from __future__ import annotations

from typing import cast

import httpx
import pytest

from kol_archive.claims import (
    ClaimSettings,
    _parse_result,
    common_close_claim_outcome,
    list_claim_proposals,
    request_claim_proposals,
)
from kol_archive.database import connect_database, initialize_database
from kol_archive.models import (
    ClaimProposalResult,
    ContentFidelity,
    EnrichmentResult,
    FeedRun,
    IngestMode,
    LoginState,
    NormalizedPost,
    RunStatus,
)
from kol_archive.service import Archive

NOW = "2026-06-01T00:00:00+00:00"


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
                content_text="$测试股份(SH600000)$ 未来 7 天看多，目标价 12 元",
                content_hash="hash-1",
                ingest_mode=IngestMode.LIVE,
            )
        ],
    )
    [target] = archive.enrichment_targets("enrich-v2")
    archive.add_enrichment(
        target,
        EnrichmentResult(
            post_type="观点",
            label_first_hand_info=False,
            label_transferable_framework=False,
            label_reasoned_non_consensus=False,
            rationale="明确市场观点",
            evidence_snippet="未来 7 天看多",
        ),
        "test-model",
        "enrich-v2",
        NOW,
    )
    return archive


def _result() -> ClaimProposalResult:
    return ClaimProposalResult(
        ticker="SH600000",
        direction="long",
        horizon_days=7,
        target_price=12.0,
        confidence_phrasing=None,
        evidence_snippet="未来 7 天看多，目标价 12 元",
    )


def test_claim_response_requires_verbatim_evidence_and_explicit_fields() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"claims":[{"ticker":"SH600000","direction":"long",'
                        '"horizon_days":7,"target_price":12,'
                        '"confidence_phrasing":null,'
                        '"evidence_snippet":"未来 7 天看多，目标价 12 元"}]}'
                    )
                }
            }
        ]
    }
    [result] = _parse_result(payload, "$测试股份(SH600000)$ 未来 7 天看多，目标价 12 元")
    assert result == _result()
    payload["choices"][0]["message"]["content"] = (
        '{"claims":[{"ticker":"SH600000","direction":"long","horizon_days":7,'
        '"target_price":12,"confidence_phrasing":null,"evidence_snippet":"保证上涨"}]}'
    )
    with pytest.raises(ValueError, match="verbatim"):
        _parse_result(payload, "$测试股份(SH600000)$ 未来 7 天看多，目标价 12 元")

    payload["choices"][0]["message"]["content"] = (
        '{"claims":[{"ticker":"SH600000","direction":"long","horizon_days":30,'
        '"target_price":20,"confidence_phrasing":null,"evidence_snippet":"明确看多"}]}'
    )
    with pytest.raises(ValueError, match="horizon_days"):
        _parse_result(payload, "$测试股份(SH600000)$ 明确看多")

    payload["choices"][0]["message"]["content"] = (
        '{"claims":[{"ticker":"SZ000007","direction":"long","horizon_days":7,'
        '"target_price":null,"confidence_phrasing":null,"evidence_snippet":"我看好"}]}'
    )
    with pytest.raises(ValueError, match="horizon_days"):
        _parse_result(payload, "$平安银行(SZ000007)$ 我看好")


def test_request_claim_proposals_uses_json_object_mode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert b'"response_format":{"type":"json_object"}' in request.content
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"claims":[]}'}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert (
            request_claim_proposals(
                ClaimSettings("https://llm.example/v1", "test-model", "secret"),
                "原文",
                client=client,
            )
            == []
        )
    finally:
        client.close()


def test_empty_claim_scan_is_recorded_and_not_reselected() -> None:
    archive = _archive()
    [target] = archive.claim_proposal_targets("claim-v1")

    assert archive.add_claim_proposals(target, [], "test-model", "claim-v1", NOW) == []
    assert archive.claim_proposal_targets("claim-v1") == []
    scan = archive.connection.execute(
        "SELECT proposal_count FROM claim_proposal_scans WHERE version_id = ?",
        (target.version_id,),
    ).fetchone()
    assert scan["proposal_count"] == 0


def test_service_reuses_full_claim_proposal_validation() -> None:
    archive = _archive()
    [target] = archive.claim_proposal_targets("claim-v1")
    fabricated = ClaimProposalResult(
        ticker="SH600000",
        direction="long",
        horizon_days=600000,
        target_price=None,
        confidence_phrasing=None,
        evidence_snippet="未来 7 天看多",
    )

    with pytest.raises(ValueError, match="horizon_days"):
        archive.add_claim_proposals(target, [fabricated], "test-model", "claim-v1", NOW)


def test_claim_proposal_acceptance_is_atomic_and_idempotent_by_version_ticker() -> None:
    archive = _archive()
    [target] = archive.claim_proposal_targets("claim-v1")
    [proposal_id] = archive.add_claim_proposals(target, [_result()], "test-model", "claim-v1", NOW)

    claim_id = archive.review_claim_proposal(proposal_id, "accepted", NOW)

    assert claim_id is not None
    proposal = archive.connection.execute(
        "SELECT review_state, claim_id FROM claim_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    claim = archive.connection.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    assert tuple(proposal) == ("accepted", claim_id)
    assert claim["claim_made_at"] == NOW
    assert claim["ingest_mode"] == "live"
    assert claim["status"] == "open"
    with pytest.raises(ValueError, match="already reviewed"):
        archive.review_claim_proposal(proposal_id, "accepted", NOW)


def test_claim_targets_require_observation_after_live_monitoring_start() -> None:
    archive = _archive()
    assert len(archive.claim_proposal_targets("claim-v1")) == 1

    archive.connection.execute(
        "UPDATE authors SET live_monitoring_started_at = '2026-06-02T00:00:00+00:00'"
    )

    assert archive.claim_proposal_targets("claim-v1") == []


def test_claim_targets_exclude_backfill_versions() -> None:
    archive = _archive()
    [live_target] = archive.claim_proposal_targets("claim-v1")
    archive.add_claim_proposals(live_target, [_result()], "test-model", "claim-v1", NOW)
    cursor = archive.connection.execute(
        """
        INSERT INTO post_versions(
            post_id, content_text, content_hash, first_observed_at, ingest_mode
        ) VALUES (1, '$测试股份(SH600000)$ 回填观点', 'backfill-hash', ?, 'backfill')
        """,
        (NOW,),
    )
    version_id = cursor.lastrowid
    assert version_id is not None
    archive.connection.execute(
        """
        INSERT INTO enrichments(
            post_id, version_id, post_type, label_first_hand_info,
            label_transferable_framework, label_reasoned_non_consensus,
            is_market_related, rationale, evidence_snippet, model, prompt_version, created_at
        ) VALUES (1, ?, '观点', 0, 0, 0, 1, '回填', '回填观点', 'test-model', 'enrich-v2', ?)
        """,
        (version_id, NOW),
    )

    assert archive.claim_proposal_targets("claim-v1") == []


def test_rejected_proposal_is_retained_without_claim() -> None:
    archive = _archive()
    [target] = archive.claim_proposal_targets("claim-v1")
    [proposal_id] = archive.add_claim_proposals(target, [_result()], "test-model", "claim-v1", NOW)

    assert archive.review_claim_proposal(proposal_id, "rejected", NOW) is None
    payload = list_claim_proposals(archive.connection, review_state="rejected")
    assert payload["counts"] == {"pending": 0, "accepted": 0, "rejected": 1}
    items = cast(list[dict[str, object]], payload["items"])
    assert items[0]["review_state"] == "rejected"
    assert archive.connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_claim_outcome_is_immutable_and_marks_claim_resolved() -> None:
    archive = _archive()
    [target] = archive.claim_proposal_targets("claim-v1")
    [proposal_id] = archive.add_claim_proposals(target, [_result()], "test-model", "claim-v1", NOW)
    claim_id = archive.review_claim_proposal(proposal_id, "accepted", NOW)
    assert claim_id is not None

    assert archive.add_claim_outcome(
        claim_id,
        "2026-06-08",
        0.1,
        0.03,
        0.07,
        "SH000300",
        "descriptive-common-close-v1",
        "test",
    )
    assert not archive.add_claim_outcome(
        claim_id,
        "2026-06-08",
        0.1,
        0.03,
        0.07,
        "SH000300",
        "descriptive-common-close-v1",
        "test",
    )
    status = archive.connection.execute(
        "SELECT status FROM claims WHERE id = ?", (claim_id,)
    ).fetchone()[0]
    assert status == "resolved"
    with pytest.raises(ValueError, match="conflicts"):
        archive.add_claim_outcome(
            claim_id,
            "2026-06-08",
            0.2,
            0.03,
            0.17,
            "SH000300",
            "descriptive-common-close-v1",
            "test",
        )


def test_common_close_claim_outcome_waits_for_shared_market_dates() -> None:
    archive = _archive()
    archive.connection.executemany(
        "INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)",
        [
            ("SH600000", "2026-05-29", 10.0),
            ("SH000300", "2026-05-29", 100.0),
            ("SH600000", "2026-06-08", 11.0),
            ("SH000300", "2026-06-08", 105.0),
        ],
    )

    outcome = common_close_claim_outcome(archive.connection, "SH600000", "SH000300", NOW, 7)

    assert outcome is not None
    assert outcome["resolved_at"] == "2026-06-08"
    assert outcome["raw_return"] == pytest.approx(0.1)
    assert outcome["benchmark_return"] == pytest.approx(0.05)
    assert outcome["excess_return"] == pytest.approx(0.05)


def test_common_close_claim_outcome_rejects_arbitrarily_distant_close() -> None:
    archive = _archive()
    archive.connection.executemany(
        "INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)",
        [
            ("SH600000", "2026-05-29", 10.0),
            ("SH000300", "2026-05-29", 100.0),
            ("SH600000", "2026-09-01", 11.0),
            ("SH000300", "2026-09-01", 105.0),
        ],
    )

    assert common_close_claim_outcome(archive.connection, "SH600000", "SH000300", NOW, 7) is None
