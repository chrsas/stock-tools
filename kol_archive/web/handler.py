"""The HTTP request handler: JSON API routes and static asset serving."""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import cast
from urllib.parse import parse_qs, unquote, urlparse

from kol_archive.accounts import add_account
from kol_archive.analysis import post_ticker_history
from kol_archive.config import load_config
from kol_archive.database import connect_database
from kol_archive.maintenance import redact_text
from kol_archive.presentation import author_profile, build_evidence_card
from kol_archive.recall import append_topic_brief, recall_query_from_values, retrieve
from kol_archive.recall_brief import load_brief_settings, synthesize_brief
from kol_archive.recall_expand import expand_query, load_expand_settings
from kol_archive.rewrite import load_rewrite_settings, request_rewrite
from kol_archive.service import Archive
from kol_archive.watchlist import add_watchlist_ticker, remove_watchlist_ticker

from . import jobs
from .automation import _save_automation_settings, _schedule_next_collection
from .payload import _home_payload
from .settings import WEB_DIST, ArchiveHttpServer

LOGGER = logging.getLogger("kol_archive.web")
MAX_FORM_BYTES = 64 * 1024


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
        payload, failure_response = jobs._execute_collection(self.server)
        if failure_response is not None:
            self._send_text(*failure_response)
            return
        assert payload is not None
        with self.server.automation_settings_lock:
            if self.server.automation_settings.collection_enabled:
                _schedule_next_collection(self.server.automation_settings)
        self._send_json(HTTPStatus.OK, payload)

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
        payload, failure_response = jobs._execute_author_enrichment(
            self.server, author_uid, observed_since
        )
        if failure_response is not None:
            self._send_text(*failure_response)
            return
        assert payload is not None
        self._send_json(HTTPStatus.OK, payload)

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
