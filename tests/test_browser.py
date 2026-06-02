from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from kol_archive.browser import (
    BrowserClient,
    BrowserError,
    body_looks_like_challenge,
    find_browser_executable,
    parse_cdp_port,
)


class FakePage:
    """Stands in for a Playwright page: records the fetch arg, returns a canned body."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    @property
    def requested_urls(self) -> list[str]:
        return [call["url"] for call in self.calls]

    def evaluate(self, _script: str, arg: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(arg)
        return {"status": self.status, "body": self.body}

    def close(self) -> None:
        self.closed = True


def test_parse_cdp_port_reads_port() -> None:
    assert parse_cdp_port("http://127.0.0.1:9224") == 9224


def test_parse_cdp_port_requires_port() -> None:
    with pytest.raises(BrowserError):
        parse_cdp_port("http://127.0.0.1")


def test_find_browser_executable_prefers_explicit_path(tmp_path: Path) -> None:
    fake_exe = tmp_path / "msedge.exe"
    fake_exe.write_text("", encoding="utf-8")
    assert find_browser_executable(str(fake_exe)) == fake_exe


def test_find_browser_executable_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no auto-detect candidates and a missing explicit path, it must raise.
    monkeypatch.setattr("kol_archive.browser.DEFAULT_EDGE_PATHS", ())
    with pytest.raises(BrowserError):
        find_browser_executable(r"Z:\does\not\exist\msedge.exe")


def test_find_browser_executable_falls_back_to_autodetect() -> None:
    # A missing explicit path is ignored; auto-detect picks an installed browser if present.
    fake = "Z:\\nope\\msedge.exe"
    try:
        resolved = find_browser_executable(fake)
    except BrowserError:
        pytest.skip("no system browser installed to auto-detect")
    assert str(resolved) != fake


def test_browser_client_get_wraps_response_with_query() -> None:
    page = FakePage(200, '{"statuses": []}')
    client = BrowserClient(page)
    response = client.get(
        "https://xueqiu.com/v4/statuses/user_timeline.json", params={"user_id": "100", "page": 1}
    )

    assert isinstance(response, httpx.Response)
    assert response.status_code == 200
    assert response.json() == {"statuses": []}
    assert page.requested_urls == [
        "https://xueqiu.com/v4/statuses/user_timeline.json?user_id=100&page=1"
    ]


def test_browser_client_get_raises_httpx_error_on_failure() -> None:
    class BrokenPage(FakePage):
        def evaluate(self, _script: str, arg: dict[str, Any]) -> dict[str, Any]:
            # Mirrors the AbortController firing: the in-page fetch rejects, evaluate raises.
            raise RuntimeError("AbortError: signal is aborted")

    client = BrowserClient(BrokenPage(200, ""))
    with pytest.raises(httpx.HTTPError):
        client.get("https://xueqiu.com/v4/statuses/user_timeline.json")


def test_browser_client_threads_timeout_into_fetch() -> None:
    page = FakePage(200, "{}")
    BrowserClient(page, default_timeout_seconds=12.0).get("https://xueqiu.com/x")
    assert page.calls == [{"url": "https://xueqiu.com/x", "timeoutMs": 12000}]


def test_browser_client_per_call_timeout_overrides_default() -> None:
    page = FakePage(200, "{}")
    BrowserClient(page, default_timeout_seconds=30.0).get("https://xueqiu.com/x", timeout=2.5)
    assert page.calls[0]["timeoutMs"] == 2500


def test_browser_client_challenge_body_surfaces_as_non_json() -> None:
    # WAF challenge: 200 + HTML. The collector then records response_not_json (no false absence).
    page = FakePage(200, "<html>aliyun_waf 请按住滑块</html>")
    response = client_get(page)
    assert response.status_code == 200
    with pytest.raises(ValueError):
        response.json()
    assert body_looks_like_challenge(response.text)


def client_get(page: FakePage) -> httpx.Response:
    return BrowserClient(page).get("https://xueqiu.com/v4/statuses/user_timeline.json")


def test_browser_client_close_is_best_effort() -> None:
    page = FakePage(200, "{}")
    client = BrowserClient(page)
    client.close()
    assert page.closed is True
