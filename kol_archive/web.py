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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

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
from kol_archive.maintenance import redact_text
from kol_archive.models import FeedState, WatchMode
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
    server.collect_lock = threading.Lock()
    return server


def serve_archive(db_path: Path, config_dir: Path, settings: WebSettings) -> None:
    server = create_server(db_path, config_dir, settings)
    host, port = cast(tuple[str, int], server.server_address)
    LOGGER.info("web archive listening http://%s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


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
    if view == "analysis":
        return {
            "view": "analysis",
            "selective_deletion": selective_deletion_analysis(
                connection, analysis_min_group_samples
            ),
            "crowding_events": list_crowding_events(connection, limit=limit),
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
        LOGGER.info("web request " + format_string, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
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
        try:
            # Deferred import: kol_archive.cli.collect pulls in the CLI package, which
            # imports this module back — importing it lazily avoids the load-time cycle.
            # The import sits inside the try so a failure still releases the lock below.
            from kol_archive.browser import BrowserError
            from kol_archive.cli.collect import RunLockError, execute_run_once

            try:
                result = execute_run_once(self.server.config_dir)
            except RunLockError:
                self._send_text(
                    HTTPStatus.CONFLICT,
                    "采集正在进行中（已被其他进程占用），请稍候。",
                )
                return
            except BrowserError:
                self._send_text(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "采集失败：专用雪球浏览器未就绪。"
                    "请先运行 login 并完成登录/滑块，再保持窗口开着重试。",
                )
                return
        finally:
            self.server.collect_lock.release()
        message = "采集完成。" if result.healthy else f"采集完成，但有告警：{result.reason}"
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "healthy": result.healthy,
                "reason": result.reason,
                "message": message,
            },
        )

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
