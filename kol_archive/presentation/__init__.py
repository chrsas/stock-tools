"""Read-only timeline and evidence-card projections for the local CLI.

Split by domain over a shared :mod:`.common` base; this package re-exports the
public projections so ``from kol_archive.presentation import X`` keeps working.
"""

from __future__ import annotations

from .authors import (
    author_profile,
    author_recent_viewpoint_clusters,
    author_recent_viewpoints,
    author_scorecards,
    author_viewpoint_overview,
)
from .evidence import build_evidence_card
from .frameworks import framework_library
from .market import version_descriptive_market_snapshots
from .timeline import (
    list_attention_queue,
    list_filtered_timeline,
    list_pinned_versions,
    list_timeline,
)

__all__ = [
    "author_profile",
    "author_recent_viewpoint_clusters",
    "author_recent_viewpoints",
    "author_scorecards",
    "author_viewpoint_overview",
    "build_evidence_card",
    "framework_library",
    "list_attention_queue",
    "list_filtered_timeline",
    "list_pinned_versions",
    "list_timeline",
    "version_descriptive_market_snapshots",
]
