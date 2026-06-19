"""JSON API and static Vue frontend for the local single-user archive."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from kol_archive.accounts import add_account
from kol_archive.analysis import (
    list_crowding_events,
    load_analysis_settings,
    post_ticker_history,
    selective_deletion_analysis,
)
from kol_archive.claims import list_claim_proposals
from kol_archive.config import load_config
from kol_archive.database import connect_database, initialize_database
from kol_archive.decisions import list_decisions
from kol_archive.enrich import enrich_targets, load_enrich_settings
from kol_archive.maintenance import redact_text
from kol_archive.models import EnrichmentTarget, FeedState, WatchMode
from kol_archive.presentation import (
    author_profile,
    author_recent_viewpoint_clusters,
    author_viewpoint_overview,
    build_evidence_card,
    framework_library,
    list_attention_queue,
    list_filtered_timeline,
    list_pinned_versions,
    list_timeline,
)
from kol_archive.recall import (
    append_topic_brief,
    build_recall_page,
    recall_query_from_values,
    retrieve,
)
from kol_archive.recall_brief import load_brief_settings, synthesize_brief
from kol_archive.recall_expand import expand_query, load_expand_settings
from kol_archive.rewrite import load_rewrite_settings, request_rewrite
from kol_archive.service import Archive
from kol_archive.watchlist import add_watchlist_ticker, list_watchlist, remove_watchlist_ticker

LOGGER = logging.getLogger(__name__)
MAX_FORM_BYTES = 64 * 1024
WEB_DIST = Path(__file__).with_name("web_dist")


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


def _automation_path(db_path: Path) -> Path:
    return db_path.parent / "web-automation.json"


def _load_automation_settings(db_path: Path) -> AutomationSettings:
    path = _automation_path(db_path)
    if not path.is_file():
        return AutomationSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("automation settings must be an object")
        interval = int(raw.get("collection_interval_minutes", 180))
        if not 5 <= interval <= 10080:
            raise ValueError("collection interval out of range")
        return AutomationSettings(
            collection_enabled=bool(raw.get("collection_enabled", False)),
            collection_interval_minutes=interval,
            auto_enrich=bool(raw.get("auto_enrich", True)),
        )
    except OSError, ValueError, TypeError, json.JSONDecodeError:
        LOGGER.warning("invalid web automation settings ignored path=%s", path)
        return AutomationSettings()


def _save_automation_settings(server: ArchiveHttpServer) -> None:
    path = _automation_path(server.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with server.automation_settings_lock:
        settings = server.automation_settings
        payload = {
            "collection_enabled": settings.collection_enabled,
            "collection_interval_minutes": settings.collection_interval_minutes,
            "auto_enrich": settings.auto_enrich,
        }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _schedule_next_collection(settings: AutomationSettings, *, immediate: bool = False) -> None:
    delay = 0 if immediate else settings.collection_interval_minutes
    settings.next_collection_at = (datetime.now(tz=UTC) + timedelta(minutes=delay)).isoformat()


def _local_post(server: ArchiveHttpServer, path: str, values: dict[str, str] | None = None) -> None:
    host, port = cast(tuple[str, int], server.server_address)
    response = httpx.post(
        f"http://{host}:{port}{path}",
        data={"csrf_token": server.csrf_token, **(values or {})},
        timeout=3600,
    )
    response.raise_for_status()


def _automation_loop(server: ArchiveHttpServer) -> None:
    while not server.automation_stop.wait(1):
        with server.automation_settings_lock:
            settings = server.automation_settings
            if not settings.collection_enabled:
                settings.next_collection_at = None
                continue
            if settings.next_collection_at is None:
                _schedule_next_collection(settings)
                continue
            due = datetime.now(tz=UTC) >= datetime.fromisoformat(settings.next_collection_at)
            if due:
                _schedule_next_collection(settings)
        if not due:
            continue
        try:
            _local_post(server, "/collect/run-once")
        except Exception:
            LOGGER.warning("automatic web collection failed")


def _start_auto_enrichment(server: ArchiveHttpServer, observed_since: str) -> bool:
    with server.automation_settings_lock:
        if not server.automation_active or not server.automation_settings.auto_enrich:
            return False

    def run() -> None:
        connection = connect_database(server.db_path)
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT a.platform_uid
                FROM authors a
                JOIN posts p ON p.author_id = a.id
                JOIN post_versions v ON v.id = p.current_version_id
                LEFT JOIN enrichments e
                  ON e.version_id = v.id AND e.prompt_version = ?
                WHERE e.id IS NULL AND v.first_observed_at >= ?
                ORDER BY a.id
                """,
                (server.enrich_prompt_version, observed_since),
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            try:
                uid = quote(str(row["platform_uid"]), safe="")
                _local_post(
                    server,
                    f"/authors/{uid}/enrich",
                    {"observed_since": observed_since},
                )
            except Exception:
                LOGGER.warning("automatic web enrichment failed author_uid=%s", row["platform_uid"])

    threading.Thread(target=run, name="web-auto-enrichment", daemon=True).start()
    return True


def _queue_counts(connection: sqlite3.Connection, prompt_version: str) -> dict[str, int]:
    def scalar(query: str, *params: object) -> int:
        return int(connection.execute(query, params).fetchone()[0])

    pending = scalar(
        """
        SELECT COUNT(*) FROM posts p
        JOIN enrichments e ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE p.watch_mode != ?
          AND (e.label_first_hand_info = 1 OR e.label_transferable_framework = 1
               OR e.label_reasoned_non_consensus = 1)
          AND NOT EXISTS (SELECT 1 FROM attention_log al WHERE al.version_id = p.current_version_id)
        """,
        prompt_version,
        WatchMode.PINNED.value,
    )
    three = scalar(
        """
        SELECT COUNT(*) FROM posts p
        JOIN enrichments e ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE p.watch_mode != ?
          AND e.label_first_hand_info = 1 AND e.label_transferable_framework = 1
          AND e.label_reasoned_non_consensus = 1
          AND NOT EXISTS (SELECT 1 FROM attention_log al WHERE al.version_id = p.current_version_id)
        """,
        prompt_version,
        WatchMode.PINNED.value,
    )
    pinned = scalar("SELECT COUNT(*) FROM posts WHERE watch_mode = ?", WatchMode.PINNED.value)
    absent = scalar(
        "SELECT COUNT(*) FROM posts WHERE feed_state = ?", FeedState.ABSENT_CONFIRMED.value
    )
    return {"pending": pending, "three": three, "pinned": pinned, "absent": absent}


def _home_payload(
    connection: sqlite3.Connection,
    prompt_version: str,
    limit: int,
    query: str,
    benchmark_ticker: str = "SH000300",
    cluster_window_days: int = 7,
    analysis_min_group_samples: int = 10,
    framework_prompt_version: str = "framework-v1",
) -> dict[str, object]:
    values = parse_qs(query)
    view = (values.get("view") or ["authors"])[0]
    tier3_only = (values.get("tier") or [""])[0] == "3"
    if view == "raw":
        return {"view": "raw", "items": list_timeline(connection, limit=limit)}
    if view == "filtered":
        return {
            "view": "filtered",
            "items": list_filtered_timeline(connection, prompt_version, limit=limit),
            "prompt_version": prompt_version,
        }
    if view == "pinned":
        return {
            "view": "pinned",
            "items": list_pinned_versions(connection, prompt_version, limit=limit),
            "counts": _queue_counts(connection, prompt_version),
        }
    if view == "queue" or tier3_only:
        items = list_attention_queue(connection, prompt_version, limit=limit)
        if tier3_only:
            items = [item for item in items if int(cast(int, item.get("tier") or 0)) >= 3]
        return {
            "view": "queue",
            "items": items,
            "counts": _queue_counts(connection, prompt_version),
            "tier3_only": tier3_only,
        }
    if view == "decisions":
        status_values = values.get("status")
        ticker_values = values.get("ticker")
        from_values = values.get("from")
        to_values = values.get("to")
        return {
            "view": "decisions",
            **list_decisions(
                connection,
                datetime.now(tz=UTC).isoformat(),
                status=status_values[0] if status_values else None,
                ticker=ticker_values[0] if ticker_values else None,
                decided_from=from_values[0] if from_values else None,
                decided_to=to_values[0] if to_values else None,
                limit=limit,
            ),
        }
    if view == "claims":
        state_values = values.get("state")
        return {
            "view": "claims",
            **list_claim_proposals(
                connection,
                review_state=state_values[0] if state_values else None,
                limit=limit,
            ),
        }
    if view == "watchlist":
        return {"view": "watchlist", "items": list_watchlist(connection)}
    if view == "frameworks":
        topic_values = values.get("topic")
        variable_values = values.get("variable")
        return {
            "view": "frameworks",
            **framework_library(
                connection,
                framework_prompt_version,
                topic=topic_values[0] if topic_values else None,
                variable=variable_values[0] if variable_values else None,
                limit=limit,
            ),
        }
    if view == "recall":
        return build_recall_page(
            connection,
            values,
            prompt_version=prompt_version,
            benchmark_ticker=benchmark_ticker,
        )
    if view == "analysis":
        return {
            "view": "analysis",
            "selective_deletion": selective_deletion_analysis(
                connection, analysis_min_group_samples
            ),
            "crowding_events": list_crowding_events(connection, limit=limit),
        }
    if view == "operations":
        return {
            "view": "operations",
            "authors": author_viewpoint_overview(connection, prompt_version),
        }
    authors = author_viewpoint_overview(connection, prompt_version)
    selected_uid = (values.get("author") or [""])[0] or None
    selected = next(
        (
            author
            for author in authors
            if str(author.get("author_platform_uid") or "") == str(selected_uid or "")
        ),
        authors[0] if authors else None,
    )
    clusters = (
        author_recent_viewpoint_clusters(
            connection,
            str(selected["author_platform_uid"]),
            prompt_version,
            limit=10,
            benchmark_ticker=benchmark_ticker,
            cluster_window_days=cluster_window_days,
        )
        if selected
        else []
    )
    return {"view": "authors", "authors": authors, "selected": selected, "clusters": clusters}


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    server: ArchiveHttpServer

    def log_message(self, format_string: str, *args: object) -> None:
        if self.path == "/api/operations/status":
            LOGGER.debug("web request " + format_string, *args)
            return
        LOGGER.info("web request " + format_string, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/collect/status":
                self._send_json(HTTPStatus.OK, self._collection_status_payload())
                return
            if path == "/api/enrich/status":
                self._send_json(HTTPStatus.OK, self._enrichment_status_payload())
                return
            if path == "/api/automation/settings":
                self._send_json(HTTPStatus.OK, self._automation_settings_payload())
                return
            if path == "/api/operations/status":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "collection": self._collection_status_payload(),
                        "enrichment": self._enrichment_status_payload(),
                        "automation": self._automation_settings_payload(),
                    },
                )
                return
            if path == "/api/home":
                self._with_connection(
                    lambda connection: self._send_json(
                        HTTPStatus.OK,
                        {
                            **_home_payload(
                                connection,
                                self.server.enrich_prompt_version,
                                self.server.timeline_limit,
                                parsed.query,
                                self.server.market_benchmark_ticker,
                                self.server.viewpoint_cluster_window_days,
                                self.server.analysis_min_group_samples,
                                self.server.framework_prompt_version,
                            ),
                            "csrf_token": self.server.csrf_token,
                        },
                    )
                )
                return
            author_uid = self._author_uid(path, prefix="/api/authors/")
            if author_uid is not None:
                self._with_connection(
                    lambda connection: self._send_json(
                        HTTPStatus.OK,
                        {
                            "view": "author",
                            "profile": author_profile(
                                connection,
                                author_uid,
                                prompt_version=self.server.enrich_prompt_version,
                                benchmark_ticker=self.server.market_benchmark_ticker,
                                cluster_window_days=self.server.viewpoint_cluster_window_days,
                            ),
                            "csrf_token": self.server.csrf_token,
                        },
                    )
                )
                return
            post_id = self._post_id(path, prefix="/api/posts/")
            if post_id is not None:
                self._with_connection(
                    lambda connection: self._send_json(
                        HTTPStatus.OK,
                        {
                            "view": "post",
                            "card": {
                                **build_evidence_card(connection, post_id),
                                "ticker_history": post_ticker_history(
                                    connection,
                                    post_id,
                                    self.server.enrich_prompt_version,
                                    self.server.market_benchmark_ticker,
                                ),
                            },
                            "csrf_token": self.server.csrf_token,
                        },
                    )
                )
                return
            if path.startswith("/assets/") or path in {"/favicon.png", "/app-icon.png"}:
                self._send_asset(path)
                return
            if path == "/" or self._author_uid(path) is not None or self._post_id(path) is not None:
                self._send_asset("/index.html")
                return
        except ValueError as error:
            self._send_text(HTTPStatus.NOT_FOUND, redact_text(str(error)))
            return
        except Exception as error:
            LOGGER.error("web read failed type=%s", type(error).__name__)
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, "页面读取失败。")
            return
        if self._is_mutation_path(path):
            self._send_text(HTTPStatus.METHOD_NOT_ALLOWED, "状态修改只接受 POST 请求。")
            return
        self._send_text(HTTPStatus.NOT_FOUND, "页面不存在。")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        form = self._read_form()
        if form is None:
            return
        if not self._valid_csrf(form):
            self._send_text(HTTPStatus.FORBIDDEN, "CSRF token 校验失败。")
            return
        try:
            routes: list[tuple[str, Callable[[int, dict[str, list[str]]], None]]] = [
                ("/pin", self._pin),
                ("/unpin", self._unpin),
                ("/attention", self._attention),
                ("/rewrite", self._rewrite),
            ]
            for suffix, action in routes:
                post_id = self._post_id(path, suffix=suffix)
                if post_id is not None:
                    action(post_id, form)
                    return
            exercise_id = self._exercise_id(path)
            if exercise_id is not None:
                self._verdict(exercise_id, form)
                return
            if path == "/decisions/add":
                self._add_decision(form)
                return
            if path == "/accounts/add":
                self._add_account(form)
                return
            if path == "/collect/run-once":
                self._run_collection(form)
                return
            if path == "/automation/settings":
                self._update_automation_settings(form)
                return
            if path == "/recall/expand":
                self._expand_recall_query(form)
                return
            if path == "/recall/brief":
                self._synthesize_recall_brief(form)
                return
            author_uid = self._author_action_uid(path, suffix="/enrich")
            if author_uid is not None:
                self._enrich_author(author_uid, form)
                return
            if path == "/watchlist/add":
                self._add_watchlist_ticker(form)
                return
            if path == "/watchlist/remove":
                self._remove_watchlist_ticker(form)
                return
            proposal_id = self._post_id(path, prefix="/claim-proposals/", suffix="/review")
            if proposal_id is not None:
                self._review_claim_proposal(proposal_id, form)
                return
            decision_id = self._post_id(path, prefix="/decisions/", suffix="/close")
            if decision_id is not None:
                self._close_decision(decision_id, form)
                return
            decision_id = self._post_id(path, prefix="/decisions/", suffix="/review")
            if decision_id is not None:
                self._review_decision(decision_id, form)
                return
        except ValueError as error:
            self._send_text(HTTPStatus.BAD_REQUEST, redact_text(str(error)))
            return
        except Exception as error:
            LOGGER.error("web action failed type=%s", type(error).__name__)
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, "操作失败。")
            return
        self._send_text(HTTPStatus.NOT_FOUND, "页面不存在。")

    def _with_connection(self, callback: Callable[[sqlite3.Connection], object]) -> None:
        connection = connect_database(self.server.db_path)
        try:
            callback(connection)
        finally:
            connection.close()

    def _with_archive(self, callback: Callable[[Archive], object]) -> None:
        self._with_connection(lambda connection: callback(Archive(connection)))

    def _pin(self, post_id: int, form: dict[str, list[str]]) -> None:
        del form
        self._with_archive(
            lambda archive: archive.pin_post(post_id, datetime.now(tz=UTC).isoformat())
        )
        self._mutation_done(post_id)

    def _unpin(self, post_id: int, form: dict[str, list[str]]) -> None:
        del form
        now = datetime.now(tz=UTC)
        self._with_archive(
            lambda archive: archive.unpin_post_for_window(
                post_id,
                now.isoformat(),
                (now - timedelta(days=self.server.window_days)).isoformat(),
            )
        )
        self._mutation_done(post_id)

    def _attention(self, post_id: int, form: dict[str, list[str]]) -> None:
        self._with_archive(
            lambda archive: archive.add_attention(
                post_id,
                self._version_id(form),
                datetime.now(tz=UTC).isoformat(),
                self._required_form_value(form, "reason"),
                self._form_value(form, "expectation"),
            )
        )
        self._mutation_done(post_id)

    def _rewrite(self, post_id: int, form: dict[str, list[str]]) -> None:
        version_id = self._version_id(form)
        config = load_config(self.server.config_dir)

        def rewrite(archive: Archive) -> None:
            settings = load_rewrite_settings(config)
            source = archive.rewrite_source(post_id, version_id)
            suggestion = request_rewrite(settings, source.original_text)
            archive.add_rewrite_exercise(
                source,
                suggestion.rewritten_claim,
                suggestion.rationale,
                settings.model,
                settings.prompt_version,
                datetime.now(tz=UTC).isoformat(),
            )

        self._with_archive(rewrite)
        self._mutation_done(post_id)

    def _verdict(self, exercise_id: int, form: dict[str, list[str]]) -> None:
        verdict = self._required_form_value(form, "verdict")
        post_id = int(self._required_form_value(form, "post_id"))
        self._with_archive(lambda archive: archive.review_rewrite_exercise(exercise_id, verdict))
        self._mutation_done(post_id)

    def _add_decision(self, form: dict[str, list[str]]) -> None:
        now = datetime.now(tz=UTC).isoformat()
        decision_id: int | None = None

        def add(archive: Archive) -> None:
            nonlocal decision_id
            decision_id = archive.add_decision(
                self._required_form_value(form, "ticker"),
                self._required_form_value(form, "direction"),
                self._required_form_value(form, "thesis"),
                self._required_form_value(form, "invalidation"),
                self._form_value(form, "decided_at") or now,
                horizon_days=self._optional_form_int(form, "horizon_days"),
                position_note=self._form_value(form, "position_note"),
                source_post_id=self._optional_form_int(form, "source_post_id"),
                source_version_id=self._optional_form_int(form, "source_version_id"),
                notes=self._form_value(form, "notes"),
            )

        self._with_archive(add)
        assert decision_id is not None
        self._mutation_done(decision_id, key="decision_id", location="/?view=decisions")

    def _add_account(self, form: dict[str, list[str]]) -> None:
        result = add_account(
            self.server.config_dir,
            self._required_form_value(form, "account"),
            note=self._form_value(form, "note"),
        )
        if "application/json" in self.headers.get("Accept", ""):
            self._send_json(HTTPStatus.OK, {"ok": True, "uid": result.uid, "status": result.status})
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def _run_collection(self, form: dict[str, list[str]]) -> None:
        del form
        # The collection shares one dedicated browser session and writes the archive;
        # never let two run-once passes overlap. Reject (not queue) a concurrent click.
        if not self.server.collect_lock.acquire(blocking=False):
            self._send_text(HTTPStatus.CONFLICT, "采集正在进行中，请稍候。")
            return
        collection_started_at = datetime.now(tz=UTC).isoformat()
        self._set_collection_status(running=True, phase="正在启动采集", healthy=None)
        failure_response: tuple[HTTPStatus, str] | None = None
        try:
            # Deferred import: kol_archive.cli.collect pulls in the CLI package, which
            # imports this module back — importing it lazily avoids the load-time cycle.
            # The import sits inside the try so a failure still releases the lock below.
            from kol_archive.browser import BrowserError
            from kol_archive.cli.collect import RunLockError, execute_run_once

            try:
                result = execute_run_once(
                    self.server.config_dir,
                    progress=lambda phase: self._set_collection_status(
                        running=True,
                        phase=phase,
                        healthy=None,
                    ),
                )
            except RunLockError:
                self._set_collection_status(
                    running=False,
                    phase="采集未启动，另一处采集正在运行",
                    healthy=False,
                )
                failure_response = (
                    HTTPStatus.CONFLICT,
                    "采集正在进行中（已被其他进程占用），请稍候。",
                )
            except BrowserError:
                self._set_collection_status(
                    running=False,
                    phase="采集失败，专用雪球浏览器未就绪",
                    healthy=False,
                )
                failure_response = (
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "采集失败：已自动尝试启动专用雪球浏览器但未能就绪。"
                    "请看刚弹出的 Edge 窗口是否卡在滑块/登录，处理完后再点一次采集。",
                )
        except Exception:
            self._set_collection_status(
                running=False,
                phase="采集失败，请查看服务日志",
                healthy=False,
            )
            raise
        finally:
            self.server.collect_lock.release()
        if failure_response is not None:
            self._send_text(*failure_response)
            return
        message = "采集完成。" if result.healthy else f"采集完成，但有告警：{result.reason}"
        self._set_collection_status(running=False, phase=message, healthy=result.healthy)
        with self.server.automation_settings_lock:
            if self.server.automation_settings.collection_enabled:
                _schedule_next_collection(self.server.automation_settings)
        auto_enrich_started = _start_auto_enrichment(self.server, collection_started_at)
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "healthy": result.healthy,
                "reason": result.reason,
                "message": message,
                "auto_enrich_started": auto_enrich_started,
            },
        )

    def _set_collection_status(self, *, running: bool, phase: str, healthy: bool | None) -> None:
        now = datetime.now(tz=UTC).isoformat()
        with self.server.collection_status_lock:
            status = self.server.collection_status
            if running and not status.running:
                status.started_at = now
                status.finished_at = None
                status.logs = []
            status.running = running
            status.phase = phase
            status.updated_at = now
            status.healthy = healthy
            if not status.logs or status.logs[-1]["message"] != phase:
                status.logs.append({"at": now, "message": phase})
            if not running:
                status.finished_at = now

    def _collection_status_payload(self) -> dict[str, object]:
        with self.server.collection_status_lock:
            status = self.server.collection_status
            elapsed_seconds = 0
            if status.started_at is not None:
                started_at = datetime.fromisoformat(status.started_at)
                ended_at = (
                    datetime.now(tz=UTC)
                    if status.running or status.finished_at is None
                    else datetime.fromisoformat(status.finished_at)
                )
                elapsed_seconds = max(0, int((ended_at - started_at).total_seconds()))
            return {
                "running": status.running,
                "phase": status.phase,
                "started_at": status.started_at,
                "updated_at": status.updated_at,
                "finished_at": status.finished_at,
                "healthy": status.healthy,
                "elapsed_seconds": elapsed_seconds,
                "logs": list(status.logs),
            }

    def _automation_settings_payload(self) -> dict[str, object]:
        with self.server.automation_settings_lock:
            settings = self.server.automation_settings
            return {
                "collection_enabled": settings.collection_enabled,
                "collection_interval_minutes": settings.collection_interval_minutes,
                "auto_enrich": settings.auto_enrich,
                "next_collection_at": settings.next_collection_at,
            }

    def _update_automation_settings(self, form: dict[str, list[str]]) -> None:
        interval = int(self._required_form_value(form, "collection_interval_minutes"))
        if not 5 <= interval <= 10080:
            raise ValueError("自动采集周期必须在 5 至 10080 分钟之间")
        enabled = self._form_value(form, "collection_enabled") == "true"
        auto_enrich = self._form_value(form, "auto_enrich") == "true"
        with self.server.automation_settings_lock:
            settings = self.server.automation_settings
            was_enabled = settings.collection_enabled
            interval_changed = settings.collection_interval_minutes != interval
            settings.collection_enabled = enabled
            settings.collection_interval_minutes = interval
            settings.auto_enrich = auto_enrich
            if not enabled:
                settings.next_collection_at = None
            elif not was_enabled:
                _schedule_next_collection(settings, immediate=True)
            elif interval_changed or settings.next_collection_at is None:
                _schedule_next_collection(settings)
        _save_automation_settings(self.server)
        self._send_json(HTTPStatus.OK, {"ok": True, **self._automation_settings_payload()})

    def _expand_recall_query(self, form: dict[str, list[str]]) -> None:
        # 扩词是主题回溯里唯一花费 token 的步骤，且只产出可改的检索词/建议窗，
        # 不生成结论、不落库。把它放在 POST + CSRF 之后；确定性检索仍走只读 GET。
        question = self._required_form_value(form, "question")
        config = load_config(self.server.config_dir)
        settings = load_expand_settings(config)
        expansion = expand_query(settings, question)
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "prompt_version": settings.prompt_version, **expansion.to_payload()},
        )

    def _synthesize_recall_brief(self, form: dict[str, list[str]]) -> None:
        # 简报合成是主题回溯里唯一生成文字、花费 token 的步骤：在已确认的确定性
        # 检索结果上合成，固定四块、每条带 version_id 引用，并把结果连同当时的
        # coverage/selection 一起 append 到 append-only 的 topic_briefs。POST + CSRF。
        query, _, error = recall_query_from_values(form)
        if query is None:
            raise ValueError(error or "请先确认检索词分组与回溯时间窗，再生成简报。")
        if not query.question.strip():
            raise ValueError("请先填写主题问题：简报需要一个可追溯的问题作为标题。")
        config = load_config(self.server.config_dir)
        settings = load_brief_settings(config)
        captured: dict[str, object] = {}

        def run(connection: sqlite3.Connection) -> None:
            result = retrieve(
                connection,
                query,
                prompt_version=self.server.enrich_prompt_version,
                benchmark_ticker=self.server.market_benchmark_ticker,
            )
            coverage = cast(dict[str, object], result["coverage"])
            if int(cast(int, coverage["version_count"])) == 0:
                raise ValueError("该检索条件下窗内没有命中发言，无法生成简报。")
            brief = synthesize_brief(settings, result)
            brief_id = append_topic_brief(
                connection,
                query=query,
                coverage=result["coverage"],
                selection=result["selection"],
                cited_version_ids=brief.cited_version_ids,
                brief_text=brief.brief_text,
                model=settings.model,
                prompt_version=settings.prompt_version,
                created_at=datetime.now(tz=UTC).isoformat(),
            )
            captured["payload"] = {
                "ok": True,
                "brief_id": brief_id,
                "prompt_version": settings.prompt_version,
                "coverage": result["coverage"],
                "selection": result["selection"],
                **brief.to_payload(),
            }

        self._with_connection(run)
        self._send_json(HTTPStatus.OK, captured["payload"])

    def _add_watchlist_ticker(self, form: dict[str, list[str]]) -> None:
        ticker = self._required_form_value(form, "ticker")
        self._with_connection(
            lambda connection: add_watchlist_ticker(
                connection,
                ticker,
                datetime.now(tz=UTC).isoformat(),
                name=self._form_value(form, "name"),
                note=self._form_value(form, "note"),
            )
        )
        self._mutation_done(0, key="watchlist_id", location="/?view=watchlist")

    def _enrich_author(self, author_uid: str, form: dict[str, list[str]]) -> None:
        observed_since = self._form_value(form, "observed_since")
        if not self.server.enrichment_lock.acquire(blocking=False):
            self._send_text(HTTPStatus.CONFLICT, "富化正在进行中，请稍候。")
            return
        self._set_enrichment_status(
            running=True,
            author_uid=author_uid,
            phase="正在准备富化",
            processed=0,
            total=0,
            enriched=0,
            failed=0,
            details=[],
        )
        try:
            config = load_config(self.server.config_dir)
            settings = replace(
                load_enrich_settings(config),
                prompt_version=self.server.enrich_prompt_version,
            )
            connection = connect_database(self.server.db_path)
            archive = Archive(connection)
            try:
                author_row = connection.execute(
                    "SELECT id FROM authors WHERE platform = 'xueqiu' AND platform_uid = ?",
                    (author_uid,),
                ).fetchone()
                if author_row is None:
                    raise ValueError("author not found")
                targets = archive.enrichment_targets(
                    settings.prompt_version,
                    author_id=int(author_row["id"]),
                    current_only=True,
                    observed_since=observed_since,
                )
                self._set_enrichment_status(
                    running=True,
                    author_uid=author_uid,
                    phase=f"准备富化 {len(targets)} 条发言",
                    processed=0,
                    total=len(targets),
                    enriched=0,
                    failed=0,
                    details=[],
                )
                enriched = failed = 0
                details: list[dict[str, object]] = []
                # The LLM calls run concurrently (settings.concurrency); this loop
                # consumes them in completion order and persists each result here,
                # in the single request thread, so the lone SQLite writer is never
                # contended. add_enrichment failures (sqlite3.Error) surface here;
                # network/parse failures arrive as the third tuple element.
                with httpx.Client(timeout=30.0) as client:
                    for index, (target, result, error) in enumerate(
                        enrich_targets(settings, targets, client=client), start=1
                    ):
                        try:
                            if error is not None:
                                raise error
                            assert result is not None  # error is None ⟹ result present
                            if (
                                archive.add_enrichment(
                                    target,
                                    result,
                                    settings.model,
                                    settings.prompt_version,
                                    datetime.now(tz=UTC).isoformat(),
                                )
                                is not None
                            ):
                                enriched += 1
                                details.append(self._enrichment_detail(target, status="success"))
                        except (httpx.HTTPError, sqlite3.Error, ValueError) as failure:
                            failed += 1
                            details.append(
                                self._enrichment_detail(
                                    target,
                                    status="failed",
                                    error=failure,
                                )
                            )
                            LOGGER.warning(
                                "web enrichment failed version_id=%s type=%s",
                                target.version_id,
                                type(failure).__name__,
                            )
                        self._set_enrichment_status(
                            running=True,
                            author_uid=author_uid,
                            phase=f"正在富化 {index}/{len(targets)}",
                            processed=index,
                            total=len(targets),
                            enriched=enriched,
                            failed=failed,
                            details=details,
                        )
                self._set_enrichment_status(
                    running=False,
                    author_uid=author_uid,
                    phase=f"富化完成，成功 {enriched} 条，失败 {failed} 条",
                    processed=len(targets),
                    total=len(targets),
                    enriched=enriched,
                    failed=failed,
                    details=details,
                )
            finally:
                connection.close()
        except Exception:
            with self.server.enrichment_status_lock:
                status = self.server.enrichment_status
                processed = status.processed
                total = status.total
                enriched = status.enriched
                failed = status.failed
                details = status.details
            self._set_enrichment_status(
                running=False,
                author_uid=author_uid,
                phase=(
                    f"富化中止，已处理 {processed}/{total}，成功 {enriched} 条，失败 {failed} 条"
                ),
                processed=processed,
                total=total,
                enriched=enriched,
                failed=failed,
                details=details,
            )
            raise
        finally:
            self.server.enrichment_lock.release()
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "prompt_version": settings.prompt_version,
                "candidates": len(targets),
                "enriched": enriched,
                "failed": failed,
                "details": details,
                "message": f"富化完成，成功 {enriched} 条，失败 {failed} 条。",
            },
        )

    @staticmethod
    def _enrichment_detail(
        target: EnrichmentTarget,
        *,
        status: str,
        error: Exception | None = None,
    ) -> dict[str, object]:
        original_text = re.sub(r"\s+", " ", target.original_text).strip()
        detail: dict[str, object] = {
            "post_id": target.post_id,
            "version_id": target.version_id,
            "status": status,
            "excerpt": original_text[:120],
        }
        if error is not None:
            detail["error_type"] = type(error).__name__
            detail["error"] = redact_text(str(error)).strip()[:500] or "未提供错误详情"
        return detail

    def _set_enrichment_status(
        self,
        *,
        running: bool,
        author_uid: str,
        phase: str,
        processed: int,
        total: int,
        enriched: int,
        failed: int,
        details: list[dict[str, object]],
    ) -> None:
        now = datetime.now(tz=UTC).isoformat()
        with self.server.enrichment_status_lock:
            previous = self.server.enrichment_status
            logs = [] if running and not previous.running else list(previous.logs)
            if not logs or logs[-1]["message"] != phase:
                logs.append({"at": now, "message": phase})
            self.server.enrichment_status = EnrichmentStatus(
                running=running,
                author_uid=author_uid,
                phase=phase,
                processed=processed,
                total=total,
                enriched=enriched,
                failed=failed,
                details=list(details),
                logs=logs,
            )

    def _enrichment_status_payload(self) -> dict[str, object]:
        with self.server.enrichment_status_lock:
            status = self.server.enrichment_status
            return {
                "running": status.running,
                "author_uid": status.author_uid,
                "phase": status.phase,
                "processed": status.processed,
                "total": status.total,
                "enriched": status.enriched,
                "failed": status.failed,
                "details": status.details,
                "logs": list(status.logs),
            }

    def _remove_watchlist_ticker(self, form: dict[str, list[str]]) -> None:
        ticker = self._required_form_value(form, "ticker")
        self._with_connection(lambda connection: remove_watchlist_ticker(connection, ticker))
        self._mutation_done(0, key="watchlist_id", location="/?view=watchlist")

    def _review_claim_proposal(self, proposal_id: int, form: dict[str, list[str]]) -> None:
        self._with_archive(
            lambda archive: archive.review_claim_proposal(
                proposal_id,
                self._required_form_value(form, "review_state"),
                datetime.now(tz=UTC).isoformat(),
            )
        )
        self._mutation_done(proposal_id, key="proposal_id", location="/?view=claims")

    def _close_decision(self, decision_id: int, form: dict[str, list[str]]) -> None:
        self._with_archive(
            lambda archive: archive.close_decision(
                decision_id,
                self._required_form_value(form, "status"),
                self._form_value(form, "closed_at") or datetime.now(tz=UTC).isoformat(),
                self._form_value(form, "notes"),
            )
        )
        self._mutation_done(decision_id, key="decision_id", location="/?view=decisions")

    def _review_decision(self, decision_id: int, form: dict[str, list[str]]) -> None:
        self._with_archive(
            lambda archive: archive.review_decision(
                decision_id,
                self._form_value(form, "reviewed_at") or datetime.now(tz=UTC).isoformat(),
                self._required_form_value(form, "retro"),
                self._form_value(form, "lesson"),
            )
        )
        self._mutation_done(decision_id, key="decision_id", location="/?view=decisions")

    def _read_form(self) -> dict[str, list[str]] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_text(HTTPStatus.BAD_REQUEST, "请求体长度无效。")
            return None
        if not 0 <= length <= MAX_FORM_BYTES:
            self._send_text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体过大。")
            return None
        try:
            return parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            self._send_text(HTTPStatus.BAD_REQUEST, "表单编码无效。")
            return None

    def _valid_csrf(self, form: dict[str, list[str]]) -> bool:
        token = self._form_value(form, "csrf_token")
        return (
            token is not None
            and token.isascii()
            and self.server.csrf_token.isascii()
            and secrets.compare_digest(token, self.server.csrf_token)
        )

    def _mutation_done(
        self, item_id: int, *, key: str = "post_id", location: str | None = None
    ) -> None:
        if "application/json" in self.headers.get("Accept", ""):
            self._send_json(HTTPStatus.OK, {"ok": True, key: item_id})
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location or f"/posts/{item_id}")
        self.end_headers()

    def _send_asset(self, path: str) -> None:
        relative = path.lstrip("/")
        target = WEB_DIST.joinpath(relative).resolve()
        if WEB_DIST.resolve() not in target.parents and target != WEB_DIST.resolve():
            self._send_text(HTTPStatus.NOT_FOUND, "页面不存在。")
            return
        if not target.is_file():
            self._send_text(HTTPStatus.NOT_FOUND, "页面不存在。")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        cache_control = (
            "public, max-age=31536000, immutable" if relative.startswith("assets/") else None
        )
        self._send_bytes(
            HTTPStatus.OK,
            target.read_bytes(),
            content_type,
            cache_control=cache_control,
        )

    @staticmethod
    def _form_value(form: dict[str, list[str]], key: str) -> str | None:
        values = form.get(key)
        return None if not values else values[0].strip() or None

    def _required_form_value(self, form: dict[str, list[str]], key: str) -> str:
        value = self._form_value(form, key)
        if value is None:
            raise ValueError(f"missing form field: {key}")
        return value

    def _version_id(self, form: dict[str, list[str]]) -> int:
        return int(self._required_form_value(form, "version_id"))

    def _optional_form_int(self, form: dict[str, list[str]], key: str) -> int | None:
        value = self._form_value(form, key)
        return None if value is None else int(value)

    @staticmethod
    def _author_uid(path: str, *, prefix: str = "/authors/") -> str | None:
        if not path.startswith(prefix) or path == prefix or "/" in path[len(prefix) :]:
            return None
        return unquote(path[len(prefix) :])

    @staticmethod
    def _author_action_uid(path: str, *, suffix: str) -> str | None:
        if not path.endswith(suffix):
            return None
        return ArchiveRequestHandler._author_uid(path[: -len(suffix)])

    @staticmethod
    def _post_id(path: str, *, prefix: str = "/posts/", suffix: str = "") -> int | None:
        if not path.startswith(prefix):
            return None
        tail = path[len(prefix) :]
        expected_suffix = suffix.lstrip("/")
        if expected_suffix:
            marker = f"/{expected_suffix}"
            if not tail.endswith(marker):
                return None
            tail = tail[: -len(marker)]
        elif "/" in tail:
            return None
        try:
            return int(tail)
        except ValueError:
            return None

    @staticmethod
    def _exercise_id(path: str) -> int | None:
        prefix = "/rewrite-exercises/"
        suffix = "/verdict"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        try:
            return int(path[len(prefix) : -len(suffix)])
        except ValueError:
            return None

    @staticmethod
    def _is_mutation_path(path: str) -> bool:
        if (
            path.startswith("/decisions/")
            or path.startswith("/claim-proposals/")
            or path.startswith("/watchlist/")
            or path.startswith("/accounts/")
            or path.startswith("/collect/")
            or path.startswith("/automation/")
            or path.startswith("/recall/")
            or path.startswith("/authors/")
        ):
            return True
        return any(
            path.endswith(suffix)
            for suffix in ("/pin", "/unpin", "/attention", "/rewrite", "/verdict")
        )

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send_text(self, status: HTTPStatus, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        cache_control: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control or "no-store")
        self.end_headers()
        self.wfile.write(body)
