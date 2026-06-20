"""Server construction and the serve loop wiring automation onto the handler."""

from __future__ import annotations

import logging
import secrets
import threading
from pathlib import Path
from typing import cast

from kol_archive.config import load_config
from kol_archive.database import connect_database, initialize_database
from kol_archive.obs import (
    DEFAULT_BODY_LIMIT,
    DEFAULT_LOG_RETENTION_DAYS,
    add_rotating_file_log,
    set_body_limit,
)

from .automation import (
    _automation_loop,
    _enrichment_worker_loop,
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
    server.enrich_wake = threading.Event()
    return server


def _logging_section(config_dir: Path) -> dict[str, object]:
    section = load_config(config_dir).get("logging") or {}
    return section if isinstance(section, dict) else {}


def _config_int(section: dict[str, object], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return max(0, int(value))
    except TypeError, ValueError:
        return default


def serve_archive(db_path: Path, config_dir: Path, settings: WebSettings) -> None:
    # Persist the full trace to a daily-rotating file so a long-running server keeps
    # a durable, grep-able trail. The console stays an INFO summary.
    section = _logging_section(config_dir)
    set_body_limit(_config_int(section, "body_limit", DEFAULT_BODY_LIMIT))
    add_rotating_file_log(
        db_path.parent / "logs" / "kol.log",
        _config_int(section, "retention_days", DEFAULT_LOG_RETENTION_DAYS),
    )
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
    enrichment_thread = threading.Thread(
        target=_enrichment_worker_loop,
        args=(server,),
        name="web-enrichment-worker",
        daemon=True,
    )
    enrichment_thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    LOGGER.info("web archive listening http://%s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.automation_stop.set()
        # Wake the worker so it sees the stop flag immediately instead of waiting out
        # its poll timeout.
        server.enrich_wake.set()
        automation_thread.join(timeout=2)
        enrichment_thread.join(timeout=2)
        server.server_close()
