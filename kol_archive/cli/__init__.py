"""Command-line entry points, grouped by domain.

* :mod:`.accounts` — register tracked bloggers by profile URL or uid
* :mod:`.collect` — login, run-once, backfill, run-health alerting
* :mod:`.storage` — backup/verify/restore/export
* :mod:`.reporting` — digest, timeline, queue, scorecards, evidence cards, serve
* :mod:`.curation` — pin/unpin, attention log, rewrite exercises
* :mod:`.claims` — LLM enrichment and the claim pipeline
* :mod:`.decisions` — personal decision log and settlement
* :mod:`.market` — price/K-line imports and the ticker watchlist
* :mod:`.images` — image download, OCR, vision descriptions
* :mod:`.recall` — retrospective topic recall (deterministic evidence retrieval)
"""

from __future__ import annotations

import argparse
import logging

from . import (
    accounts,
    claims,
    collect,
    curation,
    decisions,
    images,
    market,
    recall,
    reporting,
    storage,
)
from .common import configure_stdout_utf8


def main() -> None:
    configure_stdout_utf8()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="KOL evidence archive")
    subparsers = parser.add_subparsers(required=True)
    for module in (
        accounts,
        collect,
        storage,
        reporting,
        curation,
        claims,
        decisions,
        market,
        images,
        recall,
    ):
        module.register(subparsers)
    args = parser.parse_args()

    args.handler(args)


__all__ = ["main"]
