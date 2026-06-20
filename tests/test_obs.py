"""Observability tracing: credential-safe outbound URL logging."""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import httpx
import pytest

from kol_archive import obs


def test_trace_scope_binds_independent_nonempty_id() -> None:
    assert obs.request_id_var.get() == "-"
    with obs.trace_scope() as first:
        assert first != "-"
        assert obs.request_id_var.get() == first
        with obs.trace_scope() as second:
            assert second not in ("-", first)
            assert obs.request_id_var.get() == second
        assert obs.request_id_var.get() == first
    assert obs.request_id_var.get() == "-"


def test_rotating_file_log_writes_full_debug_trace(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "kol.log"
    root = logging.getLogger()
    original_level = root.level
    try:
        obs.add_rotating_file_log(log_path, retention_days=7)
        added = [h for h in root.handlers if getattr(h, obs._FILE_MARK, False)]
        assert added, "a rotating file handler should be attached"
        handler = added[0]
        assert isinstance(handler, TimedRotatingFileHandler)
        assert handler.backupCount == 7  # ~7-day retention
        # A DEBUG line (SQL/body level) must reach the file even though the console
        # only shows INFO.
        logging.getLogger("kol_archive.sql").debug("db exec SELECT 42 FROM t")
        handler.flush()
        assert log_path.is_file()
        assert "db exec SELECT 42 FROM t" in log_path.read_text(encoding="utf-8")
        # Idempotent: a second call adds no duplicate handler.
        obs.add_rotating_file_log(log_path, retention_days=7)
        assert len([h for h in root.handlers if getattr(h, obs._FILE_MARK, False)]) == 1
    finally:
        for h in root.handlers[:]:
            if getattr(h, obs._FILE_MARK, False):
                root.removeHandler(h)
                h.close()
        root.setLevel(original_level)


def test_set_body_limit_controls_truncation() -> None:
    original = obs._body_limit
    try:
        obs.set_body_limit(10)
        out = obs.truncate_for_log("x" * 50)
        assert out.startswith("x" * 10)
        assert "+40 chars" in out
        obs.set_body_limit(0)  # <= 0 disables truncation
        assert obs.truncate_for_log("y" * 100) == "y" * 100
    finally:
        obs.set_body_limit(original)


def test_safe_url_keeps_only_scheme_and_host() -> None:
    url = httpx.URL("https://api.deepseek.com/chat/completions?key=v")
    assert obs._safe_url(url) == "https://api.deepseek.com"


def test_safe_url_drops_path_query_and_userinfo_secrets() -> None:
    # A webhook URL is itself a credential; the secret may live in the path or
    # query, or as userinfo. None of it may reach a log line.
    url = httpx.URL("https://user:pass@sctapi.ftqq.com/SUPERSECRETKEY.send?token=abc")
    safe = obs._safe_url(url)
    assert safe == "https://sctapi.ftqq.com"
    for secret in ("SUPERSECRETKEY", "token", "abc", "pass", "user"):
        assert secret not in safe


def test_safe_url_preserves_nonstandard_port() -> None:
    url = httpx.URL("http://127.0.0.1:8765/notify/SECRET")
    assert obs._safe_url(url) == "http://127.0.0.1:8765"


def test_outbound_logs_never_contain_url_secret(caplog: pytest.LogCaptureFixture) -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"ok": True}, request=request)

    with caplog.at_level("DEBUG", logger="kol_archive.http"):
        with obs.http_client(transport=httpx.MockTransport(handler)) as client:
            client.post(
                "https://sctapi.ftqq.com/SUPERSECRETKEY.send?token=abc",
                json={"title": "x"},
            )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "http -> POST https://sctapi.ftqq.com" in text
    assert "http <- POST https://sctapi.ftqq.com" in text
    assert "SUPERSECRETKEY" not in text
    assert "token=abc" not in text
