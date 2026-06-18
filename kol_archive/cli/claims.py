"""LLM enrichment and claim-pipeline commands: enrich, propose, review, resolve."""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx

from kol_archive.claims import (
    common_close_claim_outcome,
    list_claim_proposals,
    load_claim_settings,
    request_claim_proposals,
)
from kol_archive.config import load_config
from kol_archive.enrich import enrich_targets, load_enrich_settings
from kol_archive.framework import load_framework_settings, request_framework_extraction
from kol_archive.market import OUTCOME_METHOD_VERSION

from .common import configured_db_path, connect_existing_archive, print_json, resolve_db_path

LOGGER = logging.getLogger(__name__)


def _enrich_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    settings = load_enrich_settings(config)
    if args.prompt_version:
        settings = replace(settings, prompt_version=args.prompt_version)
    connection, archive = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        targets = archive.enrichment_targets(
            settings.prompt_version, post_id=args.post_id, limit=args.limit
        )
        enriched = skipped = failed = 0
        # LLM calls run concurrently (settings.concurrency); results stream back
        # in completion order and are persisted here, serially, by this single
        # thread — the lone SQLite writer stays uncontended.
        for target, result, error in enrich_targets(settings, targets):
            if error is not None:
                # One bad version (LLM/network/parse failure) must not abort the
                # batch; it stays pending so a later run retries it.
                failed += 1
                LOGGER.warning("enrichment failed for version %s: %s", target.version_id, error)
                continue
            assert result is not None  # error is None ⟹ result present
            enrichment_id = archive.add_enrichment(
                target,
                result,
                settings.model,
                settings.prompt_version,
                datetime.now(tz=UTC).isoformat(),
            )
            if enrichment_id is None:
                skipped += 1
            else:
                enriched += 1
        print_json(
            {
                "prompt_version": settings.prompt_version,
                "model": settings.model,
                "candidates": len(targets),
                "enriched": enriched,
                "skipped": skipped,
                "failed": failed,
            }
        )
    finally:
        connection.close()


def _extract_frameworks_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    settings = load_framework_settings(config)
    if args.prompt_version:
        settings = replace(settings, prompt_version=args.prompt_version)
    connection, archive = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        targets = archive.framework_targets(settings.prompt_version, limit=args.limit)
        extracted = none_found = skipped = failed = 0
        for target in targets:
            try:
                result = request_framework_extraction(settings, target.original_text)
            except (httpx.HTTPError, ValueError) as error:
                # A failed version stays unscanned, so a later run retries it.
                failed += 1
                LOGGER.warning(
                    "framework extraction failed for version %s: %s", target.version_id, error
                )
                continue
            extraction_id = archive.add_framework_extraction(
                target,
                result,
                settings.model,
                settings.prompt_version,
                datetime.now(tz=UTC).isoformat(),
            )
            if result is None:
                none_found += 1
            elif extraction_id is None:
                skipped += 1
            else:
                extracted += 1
        print_json(
            {
                "prompt_version": settings.prompt_version,
                "model": settings.model,
                "candidates": len(targets),
                "extracted": extracted,
                "none_found": none_found,
                "skipped": skipped,
                "failed": failed,
            }
        )
    finally:
        connection.close()


def _propose_claims_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    settings = load_claim_settings(config)
    if args.prompt_version:
        settings = replace(settings, prompt_version=args.prompt_version)
    connection, archive = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        targets = archive.claim_proposal_targets(settings.prompt_version, limit=args.limit)
        proposed = empty = failed = 0
        for target in targets:
            try:
                results = request_claim_proposals(settings, target.original_text)
            except (httpx.HTTPError, ValueError) as error:
                failed += 1
                LOGGER.warning("claim proposal failed for version %s: %s", target.version_id, error)
                continue
            row_ids = archive.add_claim_proposals(
                target,
                results,
                settings.model,
                settings.prompt_version,
                datetime.now(tz=UTC).isoformat(),
            )
            proposed += len(row_ids)
            empty += int(not results)
        print_json(
            {
                "prompt_version": settings.prompt_version,
                "model": settings.model,
                "candidates": len(targets),
                "proposed": proposed,
                "empty": empty,
                "failed": failed,
            }
        )
    finally:
        connection.close()


def _claim_proposals_command(args: argparse.Namespace) -> None:
    connection, _ = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        print_json(
            list_claim_proposals(connection, review_state=args.review_state, limit=args.limit)
        )
    finally:
        connection.close()


def _review_claim_proposal_command(args: argparse.Namespace) -> None:
    connection, archive = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        claim_id = archive.review_claim_proposal(
            args.proposal_id, args.review_state, datetime.now(tz=UTC).isoformat()
        )
        print_json(
            {
                "proposal_id": args.proposal_id,
                "review_state": args.review_state,
                "claim_id": claim_id,
            }
        )
    finally:
        connection.close()


def _resolve_claims_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    benchmark = str((config.get("prices") or {}).get("benchmark_ticker") or "SH000300").upper()
    connection, archive = connect_existing_archive(resolve_db_path(args.path, config))
    resolved = pending = 0
    try:
        rows = connection.execute(
            """
            SELECT id, ticker, claim_made_at, horizon_days
            FROM claims
            WHERE status = 'open' AND horizon_days IS NOT NULL
              AND date(claim_made_at, '+8 hours', '+' || horizon_days || ' days')
                  <= date('now', '+8 hours')
              AND NOT EXISTS (SELECT 1 FROM claim_outcomes o WHERE o.claim_id = claims.id)
            ORDER BY claim_made_at, id
            """
        ).fetchall()
        for row in rows:
            outcome = common_close_claim_outcome(
                connection,
                str(row["ticker"]),
                benchmark,
                str(row["claim_made_at"]),
                int(row["horizon_days"]),
            )
            if outcome is None:
                pending += 1
                continue
            resolved += int(
                archive.add_claim_outcome(
                    int(row["id"]),
                    str(outcome["resolved_at"]),
                    cast(float, outcome["raw_return"]),
                    cast(float, outcome["benchmark_return"]),
                    cast(float, outcome["excess_return"]),
                    benchmark,
                    OUTCOME_METHOD_VERSION,
                    str(outcome["notes"]),
                )
            )
        print_json(
            {
                "resolved": resolved,
                "pending_prices": pending,
                "method_version": OUTCOME_METHOD_VERSION,
                "benchmark_ticker": benchmark,
            }
        )
    finally:
        connection.close()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    enrich_parser = subparsers.add_parser(
        "enrich", help="batch-label observed versions with the LLM (post_type + labels)"
    )
    enrich_parser.add_argument("--post-id", type=int, help="restrict to one post's versions")
    enrich_parser.add_argument("--limit", type=int, help="cap versions labelled this run")
    enrich_parser.add_argument(
        "--prompt-version", help="override llm.enrich_prompt_version for this run"
    )
    enrich_parser.add_argument("--path", type=Path)
    enrich_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    enrich_parser.set_defaults(handler=_enrich_command)
    extract_frameworks_parser = subparsers.add_parser(
        "extract-frameworks",
        help="structurally extract stated analysis frameworks from labelled versions",
    )
    extract_frameworks_parser.add_argument(
        "--limit", type=int, help="cap versions scanned this run"
    )
    extract_frameworks_parser.add_argument(
        "--prompt-version", help="override llm.framework_prompt_version for this run"
    )
    extract_frameworks_parser.add_argument("--path", type=Path)
    extract_frameworks_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    extract_frameworks_parser.set_defaults(handler=_extract_frameworks_command)
    propose_claims_parser = subparsers.add_parser(
        "propose-claims", help="extract falsifiable claim proposals from eligible live versions"
    )
    propose_claims_parser.add_argument("--limit", type=int, help="cap versions proposed this run")
    propose_claims_parser.add_argument("--prompt-version", help="override llm.claim_prompt_version")
    propose_claims_parser.add_argument("--path", type=Path)
    propose_claims_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    propose_claims_parser.set_defaults(handler=_propose_claims_command)
    claim_proposals_parser = subparsers.add_parser(
        "claim-proposals", help="list extracted claim proposals and review state"
    )
    claim_proposals_parser.add_argument(
        "--review-state", choices=["pending", "accepted", "rejected"]
    )
    claim_proposals_parser.add_argument("--limit", type=int, default=100)
    claim_proposals_parser.add_argument("--path", type=Path)
    claim_proposals_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    claim_proposals_parser.set_defaults(handler=_claim_proposals_command)
    review_claim_parser = subparsers.add_parser(
        "review-claim-proposal", help="accept or reject one claim proposal"
    )
    review_claim_parser.add_argument("proposal_id", type=int)
    review_claim_parser.add_argument(
        "--review-state", choices=["accepted", "rejected"], required=True
    )
    review_claim_parser.add_argument("--path", type=Path)
    review_claim_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    review_claim_parser.set_defaults(handler=_review_claim_proposal_command)
    resolve_claims_parser = subparsers.add_parser(
        "resolve-claims", help="settle due accepted claims from imported prices"
    )
    resolve_claims_parser.add_argument("--path", type=Path)
    resolve_claims_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    resolve_claims_parser.set_defaults(handler=_resolve_claims_command)
