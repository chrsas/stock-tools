"""Observability: per-request correlation id, log format, DB and outbound HTTP tracing.

This is the one place that wires request-scoped tracing into an otherwise plain
stdlib stack (``http.server`` + ``sqlite3`` + ``httpx``). The goal is an
EF-Core / ASP.NET-style trace: every line carries a request id, and with verbose
logging on you can read a request's params, the SQL it ran, the third-party
calls it made (request and response), and how long the whole thing took.

Two tiers, both on by default so there is always a trail:

* INFO  — request start/end, action labels, job outcomes, third-party summaries.
  Goes to the console; stays readable.
* DEBUG — full SQL statements (with bound parameters) and HTTP request/response
  bodies. Captured to a daily-rotating file (:func:`add_rotating_file_log`),
  retained 15 days by default, so the full trace is durable without flooding the
  console. ``serve --verbose`` / ``KOL_LOG_LEVEL=DEBUG`` also mirror it inline.

Nothing here may raise into the request path: a logging failure must never break
a query or an HTTP call, so the tracing callbacks swallow their own errors.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import httpx

LOGGER = logging.getLogger("kol_archive.web")
SQL_LOGGER = logging.getLogger("kol_archive.sql")
HTTP_LOGGER = logging.getLogger("kol_archive.http")

# Per-request correlation id. ThreadingHTTPServer handles each connection in its
# own thread, so this ContextVar is naturally isolated per in-flight request; the
# "-" default covers anything logged outside a request (CLI, scheduler, startup).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(request_id)s] %(message)s"
DEFAULT_BODY_LIMIT = 2000
DEFAULT_LOG_RETENTION_DAYS = 15
_CONSOLE_MARK = "_kol_console"
_FILE_MARK = "_kol_file"

# Max characters of any logged body (request, response, third-party) before it is
# truncated. Configurable via logging.body_limit; set once at serve startup.
_body_limit = DEFAULT_BODY_LIMIT


def set_body_limit(limit: int) -> None:
    """Set the max characters kept when logging a body. <= 0 disables truncation."""
    global _body_limit
    _body_limit = limit


class _RequestIdFilter(logging.Filter):
    """Stamp every record with the current request id so the format can show it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()  # type: ignore[attr-defined]
        return True


def configure_logging(verbose: bool | None = None) -> None:
    """Install the request-id-aware console log.

    Two tiers, both always on, so the trail exists without any flag:

    * The console shows an INFO summary — request lifecycle, actions, job results,
      and third-party request/response *summaries*. It stays readable.
    * The full trace (every SQL statement, every request/response *body*, polling)
      is DEBUG. It does not clutter the console; :func:`add_rotating_file_log`
      routes it to a rotating file. ``verbose`` (or ``KOL_LOG_LEVEL=DEBUG``) also
      mirrors that full trace onto the console when you want it inline.

    The root logger is kept at DEBUG so a file handler can capture everything; each
    handler decides what level it actually emits.
    """
    env_level = os.environ.get("KOL_LOG_LEVEL", "").strip().upper()
    if verbose is None:
        verbose = env_level == "DEBUG"
    console_level = logging.DEBUG if verbose else logging.INFO
    # The default stderr stream is GBK on a Windows console; Chinese log values
    # (e.g. a recall question) would render as mojibake without this.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = next(
        (h for h in root.handlers if getattr(h, _CONSOLE_MARK, False)),
        None,
    )
    if console is None:
        console = logging.StreamHandler()
        setattr(console, _CONSOLE_MARK, True)
        console.setFormatter(logging.Formatter(LOG_FORMAT))
        console.addFilter(_RequestIdFilter())
        root.addHandler(console)
    console.setLevel(console_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def add_rotating_file_log(log_path: Path, retention_days: int = DEFAULT_LOG_RETENTION_DAYS) -> None:
    """Persist the full DEBUG trace to a daily-rotating file, kept ``retention_days``.

    This is the durable, grep-able trail: it captures everything (SQL, bodies,
    polling) regardless of the console level, rotating at local midnight and keeping
    ``retention_days`` old files. Idempotent — a second call is a no-op.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if any(getattr(h, _FILE_MARK, False) for h in root.handlers):
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=max(0, retention_days),
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(_RequestIdFilter())
    setattr(handler, _FILE_MARK, True)
    root.addHandler(handler)


def new_request_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def trace_scope() -> Iterator[str]:
    """Bind a fresh correlation id for the duration of a background task.

    HTTP requests get their id in the handler; a background thread (scheduled
    collection, the resident enrichment worker) has none, so its log lines would
    carry the bare ``-``. Wrapping each task run in this scope gives every line of
    one run a shared, independent id — the same way a request's lines share one.
    """
    token = request_id_var.set(new_request_id())
    try:
        yield request_id_var.get()
    finally:
        request_id_var.reset(token)


def truncate_for_log(text: str) -> str:
    text = " ".join(text.split())
    if _body_limit > 0 and len(text) > _body_limit:
        return text[:_body_limit] + f"…(+{len(text) - _body_limit} chars)"
    return text


def install_db_tracing(connection: sqlite3.Connection) -> None:
    """Log every executed SQL statement (parameters expanded) at DEBUG.

    sqlite3's trace callback receives the statement with bound parameters already
    substituted, so this is the equivalent of EF Core's "Executed DbCommand". It
    stays at DEBUG because a single page load runs many statements.
    """

    def trace(statement: str) -> None:
        try:
            SQL_LOGGER.debug("db exec %s", truncate_for_log(statement))
        except Exception:  # pragma: no cover - tracing must never break a query
            pass

    connection.set_trace_callback(trace)


def _safe_url(url: httpx.URL) -> str:
    """Scheme + host only, dropping path/query/userinfo.

    A notification webhook URL is itself a credential, and push services commonly
    embed the secret in the *path* (e.g. ``sctapi.ftqq.com/<KEY>.send``) or query,
    not just userinfo. The hooks can't tell a sensitive URL from a safe one, so the
    logged form keeps only enough to identify the third party — never the secret.
    """
    netloc = url.host or ""
    if url.port:
        netloc = f"{netloc}:{url.port}"
    return f"{url.scheme}://{netloc}" if netloc else url.scheme


def _log_outbound_request(request: httpx.Request) -> None:
    try:
        # Stamp the start so the response hook can report a real round-trip time:
        # httpx's response.elapsed is only populated once the body is read, which we
        # skip at INFO, so without this the <- line would always say 0.0ms.
        setattr(request, "_trace_start", time.monotonic())
        body = ""
        content_type = request.headers.get("content-type", "")
        if "json" in content_type or content_type.startswith("text/"):
            raw = request.content
            if raw:
                body = truncate_for_log(raw.decode("utf-8", "replace"))
        HTTP_LOGGER.info("http -> %s %s", request.method, _safe_url(request.url))
        if body:
            HTTP_LOGGER.debug("http -> body %s", body)
    except Exception:  # pragma: no cover - tracing must never break a call
        pass


def _log_outbound_response(response: httpx.Response) -> None:
    try:
        request = response.request
        content_type = response.headers.get("content-type", "")
        want_body = HTTP_LOGGER.isEnabledFor(logging.DEBUG) and (
            "json" in content_type or content_type.startswith("text/")
        )
        if want_body:
            response.read()
        start = getattr(request, "_trace_start", None)
        elapsed_ms = (time.monotonic() - start) * 1000 if start is not None else 0.0
        HTTP_LOGGER.info(
            "http <- %s %s status=%d in %.1fms",
            request.method,
            _safe_url(request.url),
            response.status_code,
            elapsed_ms,
        )
        if want_body:
            HTTP_LOGGER.debug("http <- body %s", truncate_for_log(response.text))
    except Exception:  # pragma: no cover - tracing must never break a call
        pass


def http_client(**kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` that logs each request/response with the request id.

    Drop-in for ``httpx.Client(...)``. Outbound summaries land at INFO; bodies at
    DEBUG. Any ``event_hooks`` a caller passes are preserved and run after ours.
    """
    caller_hooks: dict[str, list[Any]] = kwargs.pop("event_hooks", {}) or {}
    event_hooks = {
        "request": [_log_outbound_request, *caller_hooks.get("request", [])],
        "response": [_log_outbound_response, *caller_hooks.get("response", [])],
    }
    return httpx.Client(event_hooks=event_hooks, **kwargs)
