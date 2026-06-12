"""The evidence archive, split by responsibility.

``Archive`` is composed from per-domain mixins over one shared base:

* :mod:`.base` — connection/transaction plumbing, authors, common row access
* :mod:`.ingest` — feed polling and direct-link probe writes (state machine)
* :mod:`.curation` — manual pinning, attention log, rewrite exercises
* :mod:`.decisions` — personal decision log and settlement
* :mod:`.enrichment` — LLM labels and falsifiable claim proposals/outcomes
* :mod:`.images` — image downloads, OCR, vision descriptions
"""

from __future__ import annotations

from .base import ArchiveBase, is_healthy_feed_run, is_healthy_probe_run
from .curation import CurationMixin
from .decisions import DecisionsMixin
from .enrichment import EnrichmentMixin
from .images import ImagesMixin
from .ingest import IngestMixin


class Archive(
    IngestMixin,
    CurationMixin,
    DecisionsMixin,
    EnrichmentMixin,
    ImagesMixin,
    ArchiveBase,
):
    """Atomic archive writes for feed polling, probes, and derived layers."""


__all__ = ["Archive", "is_healthy_feed_run", "is_healthy_probe_run"]
