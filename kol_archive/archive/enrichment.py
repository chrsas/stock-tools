"""LLM enrichment labels and falsifiable claim proposals/outcomes."""

from __future__ import annotations

from datetime import date

from kol_archive.claims import validate_claim_proposal_result
from kol_archive.market import has_explicit_market_relation
from kol_archive.models import (
    ClaimProposalResult,
    ClaimProposalTarget,
    EnrichmentResult,
    EnrichmentTarget,
)
from kol_archive.time import parse_utc_timestamp

from .base import ArchiveBase, _required_lastrowid


class EnrichmentMixin(ArchiveBase):
    def enrichment_targets(
        self, prompt_version: str, *, post_id: int | None = None, limit: int | None = None
    ) -> list[EnrichmentTarget]:
        """Observed versions still missing an enrichment for ``prompt_version``.

        Excluding already-enriched versions makes the batch idempotent and
        resumable: a run that dies (or whose LLM call fails on some versions)
        leaves the rest pending so a later run picks them up. Ordered oldest
        first so reruns make steady forward progress. Pass ``post_id`` to scope
        to one post's versions.
        """
        if not prompt_version.strip():
            raise ValueError("prompt_version must not be empty")
        query = """
            SELECT v.post_id, v.id AS version_id, v.content_text, v.raw_payload
            FROM post_versions v
            LEFT JOIN enrichments e
                ON e.version_id = v.id AND e.prompt_version = ?
            WHERE e.id IS NULL
        """
        params: list[object] = [prompt_version.strip()]
        if post_id is not None:
            query += " AND v.post_id = ?"
            params.append(post_id)
        query += " ORDER BY v.first_observed_at, v.id"
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [
            EnrichmentTarget(
                post_id=int(row["post_id"]),
                version_id=int(row["version_id"]),
                original_text=str(row["content_text"]),
                raw_payload=str(row["raw_payload"]) if row["raw_payload"] is not None else None,
            )
            for row in rows
        ]

    def add_enrichment(
        self,
        target: EnrichmentTarget,
        result: EnrichmentResult,
        model: str,
        prompt_version: str,
        created_at: str,
    ) -> int | None:
        """Persist one enrichment; returns its id, or ``None`` if one already
        existed for ``UNIQUE(version_id, prompt_version)`` (idempotent rerun)."""
        for label, value in {"post_type": result.post_type, "rationale": result.rationale}.items():
            if not value.strip():
                raise ValueError(f"{label} must not be empty")
        if not model.strip():
            raise ValueError("model must not be empty")
        if not prompt_version.strip():
            raise ValueError("prompt_version must not be empty")
        with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO enrichments(
                    post_id, version_id, post_type,
                    label_first_hand_info, label_transferable_framework,
                    label_reasoned_non_consensus, is_market_related,
                    rationale, evidence_snippet, stance_summary, model, prompt_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target.post_id,
                    target.version_id,
                    result.post_type.strip(),
                    int(result.label_first_hand_info),
                    int(result.label_transferable_framework),
                    int(result.label_reasoned_non_consensus),
                    int(has_explicit_market_relation(target.original_text, target.raw_payload)),
                    result.rationale.strip(),
                    result.evidence_snippet.strip(),
                    result.stance_summary.strip(),
                    model.strip(),
                    prompt_version.strip(),
                    created_at,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return _required_lastrowid(cursor)

    def claim_proposal_targets(
        self, prompt_version: str, *, limit: int | None = None
    ) -> list[ClaimProposalTarget]:
        """Eligible live market-related versions lacking a proposal for this prompt."""
        if not prompt_version.strip():
            raise ValueError("prompt_version must not be empty")
        query = """
            SELECT v.post_id, v.id AS version_id, v.content_text
            FROM post_versions v
            JOIN posts p ON p.id = v.post_id
            JOIN authors a ON a.id = p.author_id
            WHERE v.ingest_mode = 'live'
              AND v.first_observed_at >= a.live_monitoring_started_at
              AND EXISTS (
                  SELECT 1 FROM enrichments e
                  WHERE e.version_id = v.id AND e.is_market_related = 1
              )
              AND NOT EXISTS (
                  SELECT 1 FROM claim_proposal_scans scan
                  WHERE scan.version_id = v.id AND scan.prompt_version = ?
              )
            ORDER BY v.first_observed_at, v.id
        """
        params: list[object] = [prompt_version.strip()]
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            query += " LIMIT ?"
            params.append(limit)
        return [
            ClaimProposalTarget(
                post_id=int(row["post_id"]),
                version_id=int(row["version_id"]),
                original_text=str(row["content_text"]),
            )
            for row in self.connection.execute(query, params).fetchall()
        ]

    def add_claim_proposals(
        self,
        target: ClaimProposalTarget,
        results: list[ClaimProposalResult],
        model: str,
        prompt_version: str,
        created_at: str,
    ) -> list[int]:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not prompt_version.strip():
            raise ValueError("prompt_version must not be empty")
        row_ids: list[int] = []
        with self._transaction():
            self._get_post_version(target.post_id, target.version_id)
            if (
                self.connection.execute(
                    """
                    SELECT 1 FROM claim_proposal_scans
                    WHERE version_id = ? AND prompt_version = ?
                    """,
                    (target.version_id, prompt_version.strip()),
                ).fetchone()
                is not None
            ):
                return []
            for result in results:
                evidence = result.evidence_snippet.strip()
                validate_claim_proposal_result(result, target.original_text)
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO claim_proposals(
                        version_id, ticker, direction, horizon_days, target_price,
                        confidence_phrasing, evidence_snippet, model, prompt_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target.version_id,
                        result.ticker.strip().upper(),
                        result.direction,
                        result.horizon_days,
                        result.target_price,
                        result.confidence_phrasing,
                        evidence,
                        model.strip(),
                        prompt_version.strip(),
                        created_at,
                    ),
                )
                if cursor.rowcount:
                    row_ids.append(_required_lastrowid(cursor))
            self.connection.execute(
                """
                INSERT INTO claim_proposal_scans(
                    version_id, model, prompt_version, proposal_count, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    target.version_id,
                    model.strip(),
                    prompt_version.strip(),
                    len(row_ids),
                    created_at,
                ),
            )
        return row_ids

    def review_claim_proposal(
        self, proposal_id: int, review_state: str, reviewed_at: str
    ) -> int | None:
        if review_state not in {"accepted", "rejected"}:
            raise ValueError("claim proposal review must be accepted or rejected")
        reviewed_at = parse_utc_timestamp(reviewed_at).isoformat()
        claim_id: int | None = None
        with self._transaction():
            proposal = self.connection.execute(
                """
                SELECT
                    cp.*, v.post_id, v.first_observed_at, v.ingest_mode, p.author_id
                FROM claim_proposals cp
                JOIN post_versions v ON v.id = cp.version_id
                JOIN posts p ON p.id = v.post_id
                WHERE cp.id = ? AND cp.review_state = 'pending'
                """,
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise ValueError(f"unknown or already reviewed claim proposal id: {proposal_id}")
            if review_state == "accepted":
                existing = self.connection.execute(
                    """
                    SELECT id, direction, horizon_days, target_price, confidence_phrasing
                    FROM claims
                    WHERE version_id = ? AND ticker = ?
                    ORDER BY id LIMIT 1
                    """,
                    (proposal["version_id"], proposal["ticker"]),
                ).fetchone()
                if existing is not None:
                    existing_shape = tuple(
                        existing[key]
                        for key in (
                            "direction",
                            "horizon_days",
                            "target_price",
                            "confidence_phrasing",
                        )
                    )
                    proposal_shape = tuple(
                        proposal[key]
                        for key in (
                            "direction",
                            "horizon_days",
                            "target_price",
                            "confidence_phrasing",
                        )
                    )
                    if existing_shape != proposal_shape:
                        raise ValueError(
                            "accepted claim conflicts with an existing version/ticker claim"
                        )
                    claim_id = int(existing["id"])
                else:
                    cursor = self.connection.execute(
                        """
                        INSERT INTO claims(
                            post_id, version_id, author_id, ticker, direction, horizon_days,
                            target_price, confidence_phrasing, claim_made_at, ingest_mode,
                            status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                        """,
                        (
                            proposal["post_id"],
                            proposal["version_id"],
                            proposal["author_id"],
                            proposal["ticker"],
                            proposal["direction"],
                            proposal["horizon_days"],
                            proposal["target_price"],
                            proposal["confidence_phrasing"],
                            proposal["first_observed_at"],
                            proposal["ingest_mode"],
                            reviewed_at,
                        ),
                    )
                    claim_id = _required_lastrowid(cursor)
            cursor = self.connection.execute(
                """
                UPDATE claim_proposals
                SET review_state = ?, reviewed_at = ?, claim_id = ?
                WHERE id = ? AND review_state = 'pending'
                """,
                (review_state, reviewed_at, claim_id, proposal_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"claim proposal review race: {proposal_id}")
        return claim_id

    def add_claim_outcome(
        self,
        claim_id: int,
        resolved_at: str,
        raw_return: float,
        benchmark_return: float,
        excess_return: float,
        benchmark_ticker: str,
        outcome_method_version: str,
        notes: str | None = None,
    ) -> bool:
        date.fromisoformat(resolved_at)
        benchmark_ticker = benchmark_ticker.strip().upper()
        if not benchmark_ticker or not outcome_method_version.strip():
            raise ValueError("benchmark ticker and outcome method version are required")
        with self._transaction():
            existing = self.connection.execute(
                "SELECT * FROM claim_outcomes WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            if existing is not None:
                expected = (
                    resolved_at,
                    raw_return,
                    benchmark_return,
                    excess_return,
                    benchmark_ticker,
                    outcome_method_version.strip(),
                    notes,
                )
                actual = tuple(
                    existing[key]
                    for key in (
                        "resolved_at",
                        "raw_return",
                        "benchmark_return",
                        "excess_return",
                        "benchmark_ticker",
                        "outcome_method_version",
                        "notes",
                    )
                )
                if actual != expected:
                    raise ValueError(f"claim outcome conflicts with immutable result: {claim_id}")
                return False
            cursor = self.connection.execute(
                """
                INSERT INTO claim_outcomes(
                    claim_id, resolved_at, raw_return, benchmark_return, excess_return,
                    benchmark_ticker, outcome_method_version, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    resolved_at,
                    raw_return,
                    benchmark_return,
                    excess_return,
                    benchmark_ticker,
                    outcome_method_version.strip(),
                    notes,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"claim outcome insert failed: {claim_id}")
            self.connection.execute(
                "UPDATE claims SET status = 'resolved' WHERE id = ?", (claim_id,)
            )
        return True
