"""Web server settings, runtime status dataclasses, and the HTTP server type."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from kol_archive.analysis import load_analysis_settings

WEB_DIST = Path(__file__).resolve().parent.parent / "web_dist"


@dataclass(frozen=True)
class WebSettings:
    bind_host: str = "127.0.0.1"
    port: int = 8765
    timeline_limit: int = 50
    window_days: int = 30
    enrich_prompt_version: str = "enrich-v2"
    market_benchmark_ticker: str = "SH000300"
    viewpoint_cluster_window_days: int = 7
    analysis_min_group_samples: int = 10
    framework_prompt_version: str = "framework-v1"


@dataclass
class CollectionStatus:
    running: bool = False
    phase: str = "尚未开始采集"
    started_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    healthy: bool | None = None
    logs: list[dict[str, str]] = field(default_factory=list)


@dataclass
class EnrichmentStatus:
    running: bool = False
    author_uid: str | None = None
    phase: str = "尚未开始富化"
    processed: int = 0
    total: int = 0
    enriched: int = 0
    failed: int = 0
    details: list[dict[str, object]] = field(default_factory=list)
    logs: list[dict[str, str]] = field(default_factory=list)


@dataclass
class AutomationSettings:
    collection_enabled: bool = False
    collection_interval_minutes: int = 180
    auto_enrich: bool = True
    next_collection_at: str | None = None


class ArchiveHttpServer(ThreadingHTTPServer):
    db_path: Path
    config_dir: Path
    csrf_token: str
    timeline_limit: int
    window_days: int
    enrich_prompt_version: str
    market_benchmark_ticker: str
    viewpoint_cluster_window_days: int
    analysis_min_group_samples: int
    framework_prompt_version: str
    collect_lock: threading.Lock
    collection_status_lock: threading.Lock
    collection_status: CollectionStatus
    enrichment_lock: threading.Lock
    enrichment_status_lock: threading.Lock
    enrichment_status: EnrichmentStatus
    automation_settings_lock: threading.Lock
    automation_settings: AutomationSettings
    automation_stop: threading.Event
    automation_active: bool
    enrich_wake: threading.Event


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def load_web_settings(
    config: dict[str, Any],
    *,
    bind_host: str | None = None,
    port: int | None = None,
) -> WebSettings:
    web = _section(config, "web")
    monitoring = _section(config, "monitoring")
    llm = _section(config, "llm")
    prices = _section(config, "prices")
    settings = WebSettings(
        bind_host=str(bind_host or web.get("bind_host") or "127.0.0.1").strip(),
        port=int(port if port is not None else web.get("port") or 8765),
        timeline_limit=int(web.get("timeline_limit") or 50),
        window_days=int(monitoring.get("window_days") or 30),
        enrich_prompt_version=str(
            web.get("enrich_prompt_version") or llm.get("enrich_prompt_version") or "enrich-v2"
        ).strip()
        or "enrich-v2",
        market_benchmark_ticker=str(prices.get("benchmark_ticker") or "SH000300").strip(),
        viewpoint_cluster_window_days=int(
            7
            if web.get("viewpoint_cluster_window_days") is None
            else web["viewpoint_cluster_window_days"]
        ),
        analysis_min_group_samples=load_analysis_settings(config).min_group_samples,
        framework_prompt_version=str(llm.get("framework_prompt_version") or "framework-v1").strip()
        or "framework-v1",
    )
    if not settings.bind_host or settings.bind_host in {"0.0.0.0", "::", "[::]"}:
        raise ValueError("web.bind_host must be a loopback or explicit tailnet address")
    if not 1 <= settings.port <= 65535:
        raise ValueError("web.port must be between 1 and 65535")
    if settings.timeline_limit < 1:
        raise ValueError("web.timeline_limit must be positive")
    if settings.window_days < 1:
        raise ValueError("monitoring.window_days must be positive")
    if not re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", settings.market_benchmark_ticker):
        raise ValueError("prices.benchmark_ticker must be an A-share ticker")
    if settings.viewpoint_cluster_window_days < 1:
        raise ValueError("web.viewpoint_cluster_window_days must be positive")
    return settings
