"""JSON API and static Vue frontend for the local single-user archive.

Split by responsibility over a shared :mod:`.settings` base:

* :mod:`.settings` — ``WebSettings``/status dataclasses, ``ArchiveHttpServer``, config
* :mod:`.jobs` — collection and enrichment execution (the long-running jobs)
* :mod:`.automation` — background scheduling and the polling loop
* :mod:`.payload` — ``/api/home`` view assembly from the read-only projections
* :mod:`.handler` — the HTTP request handler (routes + static assets)
* :mod:`.lifecycle` — ``create_server`` / ``serve_archive`` wiring

The public names are re-exported here so ``from kol_archive.web import X`` keeps
working; the patched-in-tests helpers live in their own submodules.
"""

from __future__ import annotations

from .automation import (
    _automation_loop,
    _load_automation_settings,
    _prime_startup_collection_schedule,
    _startup_collection_due_at,
)
from .handler import ArchiveRequestHandler
from .jobs import _execute_author_enrichment, _execute_collection, _start_auto_enrichment
from .lifecycle import create_server, serve_archive
from .payload import _home_payload
from .settings import (
    ArchiveHttpServer,
    AutomationSettings,
    CollectionStatus,
    EnrichmentStatus,
    WebSettings,
    load_web_settings,
)

__all__ = [
    "ArchiveHttpServer",
    "ArchiveRequestHandler",
    "AutomationSettings",
    "CollectionStatus",
    "EnrichmentStatus",
    "WebSettings",
    "create_server",
    "load_web_settings",
    "serve_archive",
    "_automation_loop",
    "_execute_author_enrichment",
    "_execute_collection",
    "_home_payload",
    "_load_automation_settings",
    "_prime_startup_collection_schedule",
    "_start_auto_enrichment",
    "_startup_collection_due_at",
]
