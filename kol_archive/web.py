"""Small server-rendered web interface for the local single-user archive."""

from __future__ import annotations

import logging
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlparse

from kol_archive.config import load_config
from kol_archive.database import connect_database
from kol_archive.maintenance import redact_text
from kol_archive.presentation import build_evidence_card, list_timeline
from kol_archive.rewrite import load_rewrite_settings, request_rewrite
from kol_archive.service import Archive

LOGGER = logging.getLogger(__name__)
MAX_FORM_BYTES = 64 * 1024
REWRITE_VERDICTS = ("valid", "too_vague", "wrong")


@dataclass(frozen=True)
class WebSettings:
    bind_host: str = "127.0.0.1"
    port: int = 8765
    timeline_limit: int = 50
    window_days: int = 30


class ArchiveHttpServer(ThreadingHTTPServer):
    db_path: Path
    config_dir: Path
    csrf_token: str
    timeline_limit: int
    window_days: int


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
    settings = WebSettings(
        bind_host=str(bind_host or web.get("bind_host") or "127.0.0.1").strip(),
        port=int(port if port is not None else web.get("port") or 8765),
        timeline_limit=int(web.get("timeline_limit") or 50),
        window_days=int(monitoring.get("window_days") or 30),
    )
    if not settings.bind_host or settings.bind_host in {"0.0.0.0", "::", "[::]"}:
        raise ValueError("web.bind_host must be a loopback or explicit tailnet address")
    if not 1 <= settings.port <= 65535:
        raise ValueError("web.port must be between 1 and 65535")
    if settings.timeline_limit < 1:
        raise ValueError("web.timeline_limit must be positive")
    if settings.window_days < 1:
        raise ValueError("monitoring.window_days must be positive")
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
    server = ArchiveHttpServer((settings.bind_host, settings.port), ArchiveRequestHandler)
    server.db_path = db_path
    server.config_dir = config_dir
    server.csrf_token = csrf_token or secrets.token_urlsafe(32)
    server.timeline_limit = settings.timeline_limit
    server.window_days = settings.window_days
    return server


def serve_archive(
    db_path: Path,
    config_dir: Path,
    settings: WebSettings,
) -> None:
    server = create_server(db_path, config_dir, settings)
    host, port = cast(tuple[str, int], server.server_address)
    LOGGER.info("web archive listening http://%s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _cell(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("human_label", "")
    return escape(_text(value))


def _csrf_input(token: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{escape(token)}">'


def _table(rows: list[dict[str, object]], *, empty: str = "暂无记录") -> str:
    if not rows:
        return f"<p>{escape(empty)}</p>"
    columns = list(rows[0])
    heading = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(row.get(column))}</td>" for column in columns) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="table-wrap"><table><thead><tr>{heading}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; background: #f5f6f8; color: #18202b; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 16px; }}
    a {{ color: #075985; }}
    article, section {{ background: #fff; border: 1px solid #d8dee8; border-radius: 10px;
      margin: 12px 0; padding: 14px; }}
    h1, h2, h3 {{ margin: 0 0 10px; line-height: 1.25; }}
    h1 {{ font-size: 1.5rem; }} h2 {{ font-size: 1.2rem; }} h3 {{ font-size: 1rem; }}
    p {{ margin: 8px 0; overflow-wrap: anywhere; }}
    .muted {{ color: #5d6878; word-break: break-all; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; font-size: .88rem; }}
    th, td {{ border-bottom: 1px solid #e4e8ef; padding: 8px; text-align: left;
      vertical-align: top; white-space: pre-wrap; }}
    th {{ color: #42526a; }}
    pre {{ overflow-x: auto; background: #f7f8fa; border-radius: 6px; padding: 10px;
      white-space: pre-wrap; word-break: break-word; }}
    form {{ display: grid; gap: 8px; margin: 10px 0; }}
    label {{ display: grid; gap: 5px; }}
    input, textarea, select, button {{ box-sizing: border-box; font: inherit; max-width: 100%;
      min-height: 40px; padding: 8px; }}
    textarea {{ min-height: 84px; }}
    button {{ cursor: pointer; width: fit-content; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    .actions form {{ display: inline; margin: 0; }}
    @media (max-width: 640px) {{
      main {{ padding: 10px; }} article, section {{ padding: 11px; }}
      th, td {{ min-width: 128px; }} button {{ width: 100%; }}
      .actions, .actions form {{ display: grid; width: 100%; }}
    }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""


def _timeline_html(connection: sqlite3.Connection, limit: int) -> str:
    items = list_timeline(connection, limit=limit)
    articles = []
    for item in items:
        status = cast(dict[str, str], item["status"])
        current_text = escape(_text(item.get("current_text")))
        articles.append(
            f"""<article>
  <h2><a href="/posts/{item["post_id"]}">帖子 {item["post_id"]}</a></h2>
  <p>{escape(status["human_label"])}</p>
  <p>{escape(status["deletion_signal_label"])}</p>
  <p class="muted">首次观察：{_cell(item.get("first_seen_at"))}<br>
  最后在场观察：{_cell(item.get("last_present_at"))}<br>
  当前版本首次观察：{_cell(item.get("current_version_first_observed_at"))}<br>
  当前版本最后观察：{_cell(item.get("current_version_last_observed_at"))}<br>
  检测到缺失：{_cell(item.get("last_feed_absence_detected_at"))}</p>
  <pre>{current_text}</pre>
</article>"""
        )
    content = "".join(articles) or "<p>归档中暂无帖子。</p>"
    return _layout("KOL 原始时间线", f"<h1>KOL 原始时间线</h1>{content}")


def _version_sections(versions: list[dict[str, object]]) -> str:
    blocks = []
    for version in versions:
        diff = version.get("diff_from_prior_observed_version")
        blocks.append(
            f"""<article>
  <h3>观察版本 {version["version_id"]}</h3>
  <p class="muted">首次观察：{_cell(version.get("first_observed_at"))}<br>
  最后观察：{_cell(version.get("last_observed_at"))}</p>
  <pre>{_cell(version.get("content_text"))}</pre>
  <h3>相对上一观察版本 diff</h3>
  <pre>{_cell(diff) if diff else "首个观察版本"}</pre>
</article>"""
        )
    return "".join(blocks) or "<p>暂无完整正文版本。</p>"


def _post_html(card: dict[str, Any], csrf_token: str) -> str:
    post = cast(dict[str, object], card["post"])
    status = cast(dict[str, str], post["status"])
    post_id = int(str(post["id"]))
    version_id = post.get("current_version_id")
    version_input = (
        f'<input type="hidden" name="version_id" value="{escape(str(version_id))}">'
        if version_id is not None
        else ""
    )
    rewrite_form = (
        f"""<form method="post" action="/posts/{post_id}/rewrite">
  {_csrf_input(csrf_token)}{version_input}
  <button type="submit">生成单条改写训练</button>
</form>"""
        if version_id is not None
        else "<p>当前帖子没有完整正文版本，无法创建关注理由或改写训练。</p>"
    )
    attention_form = (
        f"""<form method="post" action="/posts/{post_id}/attention">
  {_csrf_input(csrf_token)}{version_input}
  <label>关注理由<textarea name="reason" required></textarea></label>
  <label>我的预期<textarea name="expectation"></textarea></label>
  <button type="submit">记录关注理由并钉住</button>
</form>"""
        if version_id is not None
        else ""
    )
    verdict_options = "".join(
        f'<option value="{value}">{value}</option>' for value in REWRITE_VERDICTS
    )
    verdict_forms = "".join(
        f"""<form method="post" action="/rewrite-exercises/{item["rewrite_exercise_id"]}/verdict">
  {_csrf_input(csrf_token)}
  <input type="hidden" name="post_id" value="{post_id}">
  <select name="verdict">{verdict_options}</select>
  <button type="submit">记录 verdict</button>
</form>"""
        for item in cast(list[dict[str, object]], card["rewrite_exercises"])
    )
    versions = cast(list[dict[str, object]], card["versions"])
    feed_observations = cast(list[dict[str, object]], card["feed_observations"])
    direct_probes = cast(list[dict[str, object]], card["direct_probes"])
    events = cast(list[dict[str, object]], card["events"])
    attention_log = cast(list[dict[str, object]], card["attention_log"])
    rewrite_exercises = cast(list[dict[str, object]], card["rewrite_exercises"])
    body = f"""<p><a href="/">返回原始时间线</a></p>
<h1>证据卡片：帖子 {post_id}</h1>
<section>
  <p>{escape(status["human_label"])}</p>
  <p>{escape(status["deletion_signal_label"])}</p>
  <p class="muted">首次观察：{_cell(post.get("first_seen_at"))}<br>
  最后在场观察：{_cell(post.get("last_present_at"))}<br>
  当前版本首次观察：{_cell(post.get("current_version_first_observed_at"))}<br>
  当前版本最后观察：{_cell(post.get("current_version_last_observed_at"))}<br>
  检测到缺失：{_cell(post.get("last_feed_absence_detected_at"))}</p>
</section>
<section>
  <h2>操作</h2>
  <div class="actions">
    <form method="post" action="/posts/{post_id}/pin">
      {_csrf_input(csrf_token)}<button type="submit">钉住</button>
    </form>
    <form method="post" action="/posts/{post_id}/unpin">
      {_csrf_input(csrf_token)}<button type="submit">取消钉住</button>
    </form>
  </div>
  {attention_form}
  {rewrite_form}
</section>
<section><h2>观察版本</h2>{_version_sections(versions)}</section>
<section><h2>feed 观察</h2>{_table(feed_observations)}</section>
<section><h2>直链复查</h2>{_table(direct_probes)}</section>
<section><h2>状态变迁</h2>{_table(events)}</section>
<section><h2>关注理由</h2>{_table(attention_log)}</section>
<section><h2>改写训练</h2>{_table(rewrite_exercises)}{verdict_forms}</section>"""
    return _layout(f"证据卡片 {post_id}", body)


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    server: ArchiveHttpServer

    def log_message(self, format_string: str, *args: object) -> None:
        LOGGER.info("web request " + format_string, *args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._with_connection(
                    lambda connection: self._send_html(
                        HTTPStatus.OK,
                        _timeline_html(connection, self.server.timeline_limit),
                    )
                )
                return
            post_id = self._post_id(path, suffix="")
            if post_id is not None:
                self._with_connection(
                    lambda connection: self._send_html(
                        HTTPStatus.OK,
                        _post_html(
                            build_evidence_card(connection, post_id),
                            self.server.csrf_token,
                        ),
                    )
                )
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
        self._redirect_post(post_id)

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
        self._redirect_post(post_id)

    def _attention(self, post_id: int, form: dict[str, list[str]]) -> None:
        reason = self._required_form_value(form, "reason")
        expectation = self._form_value(form, "expectation")
        version_id = self._version_id(form)
        self._with_archive(
            lambda archive: archive.add_attention(
                post_id,
                version_id,
                datetime.now(tz=UTC).isoformat(),
                reason,
                expectation,
            )
        )
        self._redirect_post(post_id)

    def _rewrite(self, post_id: int, form: dict[str, list[str]]) -> None:
        version_id = self._version_id(form)
        # Allow ignored local LLM settings to take effect without restarting the web server.
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
        self._redirect_post(post_id)

    def _verdict(self, exercise_id: int, form: dict[str, list[str]]) -> None:
        verdict = self._required_form_value(form, "verdict")
        post_id = int(self._required_form_value(form, "post_id"))
        self._with_archive(lambda archive: archive.review_rewrite_exercise(exercise_id, verdict))
        self._redirect_post(post_id)

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

    @staticmethod
    def _post_id(path: str, *, suffix: str) -> int | None:
        parts = path.strip("/").split("/")
        expected = ["posts", "", *([suffix.strip("/")] if suffix else [])]
        if len(parts) != len(expected) or parts[0] != "posts":
            return None
        if suffix and parts[2] != suffix.strip("/"):
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    @staticmethod
    def _exercise_id(path: str) -> int | None:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "rewrite-exercises" or parts[2] != "verdict":
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    @staticmethod
    def _is_mutation_path(path: str) -> bool:
        return any(
            path.endswith(suffix)
            for suffix in ("/pin", "/unpin", "/attention", "/rewrite", "/verdict")
        )

    def _redirect_post(self, post_id: int) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/posts/{quote(str(post_id))}")
        self.end_headers()

    def _send_html(self, status: HTTPStatus, html: str) -> None:
        self._send_bytes(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _send_text(self, status: HTTPStatus, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
