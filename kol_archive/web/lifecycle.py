"""Server construction and the serve loop wiring automation onto the handler."""

from __future__ import annotations

import logging
import secrets
import threading
from pathlib import Path
from typing import cast

from kol_archive.database import connect_database, initialize_database

from .automation import (
    _automation_loop,
    _load_automation_settings,
    _prime_startup_collection_schedule,
)
from .handler import ArchiveRequestHandler
from .settings import (
    WEB_DIST,
    ArchiveHttpServer,
    CollectionStatus,
    EnrichmentStatus,
    WebSettings,
)

LOGGER = logging.getLogger("kol_archive.web")


def create_server(
    db_path: Path,
    config_dir: Path,
    settings: WebSettings,
    *,
    csrf_token: str | None = None,
) -> ArchiveHttpServer:
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite archive does not exist: {db_path}")
    if not WEB_DIST.joinpath("index.html").is_file():
        raise FileNotFoundError("Vue frontend is missing. Run npm run build in frontend.")
    connection = connect_database(db_path)
    try:
        initialize_database(connection)
    finally:
        connection.close()
    server = ArchiveHttpServer((settings.bind_host, settings.port), ArchiveRequestHandler)
    server.db_path = db_path
    server.config_dir = config_dir
    server.csrf_token = csrf_token or secrets.token_urlsafe(32)
    server.timeline_limit = settings.timeline_limit
    server.window_days = settings.window_days
    server.enrich_prompt_version = settings.enrich_prompt_version
    server.market_benchmark_ticker = settings.market_benchmark_ticker
    server.viewpoint_cluster_window_days = settings.viewpoint_cluster_window_days
    server.analysis_min_group_samples = settings.analysis_min_group_samples
    server.framework_prompt_version = settings.framework_prompt_version
    long_task_lock = threading.Lock()
    server.collect_lock = long_task_lock
    server.collection_status_lock = threading.Lock()
    server.collection_status = CollectionStatus()
    server.enrichment_lock = long_task_lock
    server.enrichment_status_lock = threading.Lock()
    server.enrichment_status = EnrichmentStatus()
    server.automation_settings_lock = threading.Lock()
    server.automation_settings = _load_automation_settings(db_path)
    server.automation_stop = threading.Event()
    server.automation_active = False
    return server


def serve_archive(db_path: Path, config_dir: Path, settings: WebSettings) -> None:
    server = create_server(db_path, config_dir, settings)
    server.automation_active = True
    _prime_startup_collection_schedule(server)
    automation_thread = threading.Thread(
        target=_automation_loop,
        args=(server,),
        name="web-automation",
        daemon=True,
    )
    automation_thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    LOGGER.info("web archive listening http://%s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.automation_stop.set()
        automation_thread.join(timeout=2)
        server.server_close()
