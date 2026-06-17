from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from kol_archive.browser import (
    BrowserClient,
    BrowserError,
    body_looks_like_challenge,
    ensure_dedicated_browser,
    find_browser_executable,
    is_cdp_reachable,
    parse_cdp_port,
    start_dedicated_browser,
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


def test_is_cdp_reachable_true_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_get(url: str, timeout: float) -> httpx.Response:
        seen["url"] = url
        return httpx.Response(200, text="{}")

    monkeypatch.setattr("kol_archive.browser.httpx.get", fake_get)
    assert is_cdp_reachable("http://127.0.0.1:9224") is True
    assert seen["url"] == "http://127.0.0.1:9224/json/version"


def test_is_cdp_reachable_false_when_port_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("kol_archive.browser.httpx.get", fake_get)
    assert is_cdp_reachable("http://127.0.0.1:9224") is False


def test_ensure_dedicated_browser_skips_launch_when_already_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("kol_archive.browser.is_cdp_reachable", lambda _url: True)

    def fail_launch(**_kwargs: Any) -> None:
        raise AssertionError("must not launch a browser when CDP already answers")

    monkeypatch.setattr("kol_archive.browser.start_dedicated_browser", fail_launch)
    assert (
        ensure_dedicated_browser(
            profile_dir=tmp_path, cdp_url="http://127.0.0.1:9224", landing_url="https://xueqiu.com"
        )
        is True
    )


def test_ensure_dedicated_browser_launches_then_waits_for_cdp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # First reachability check fails (not running), launch happens, then it comes up.
    reachable = iter([False, False, True])
    monkeypatch.setattr("kol_archive.browser.is_cdp_reachable", lambda _url: next(reachable))
    launched: dict[str, Any] = {}

    def fake_launch(**kwargs: Any) -> None:
        launched.update(kwargs)

    monkeypatch.setattr("kol_archive.browser.start_dedicated_browser", fake_launch)
    monkeypatch.setattr("kol_archive.browser.time.sleep", lambda _s: None)

    result = ensure_dedicated_browser(
        profile_dir=tmp_path, cdp_url="http://127.0.0.1:9224", landing_url="https://xueqiu.com"
    )
    assert result is False
    assert launched["url"] == "https://xueqiu.com"
    assert launched["profile_dir"] == tmp_path


def test_start_dedicated_browser_passes_absolute_user_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Regression: a relative --user-data-dir let Edge hand off to the user's running Edge
    # and skip the debug port. The launched arg must always be absolute.
    fake_exe = tmp_path / "msedge.exe"
    fake_exe.write_text("", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_popen(args: list[str], **_kwargs: Any) -> object:
        captured["args"] = args
        return object()

    monkeypatch.setattr("kol_archive.browser.subprocess.Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    start_dedicated_browser(
        profile_dir=Path("data/browser_profile"),
        cdp_url="http://127.0.0.1:9224",
        url="https://xueqiu.com",
        edge_path=str(fake_exe),
    )
    user_data_arg = next(a for a in captured["args"] if a.startswith("--user-data-dir="))
    profile_path = Path(user_data_arg.split("=", 1)[1])
    assert profile_path.is_absolute()


def test_ensure_dedicated_browser_raises_on_startup_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("kol_archive.browser.is_cdp_reachable", lambda _url: False)
    monkeypatch.setattr("kol_archive.browser.start_dedicated_browser", lambda **_k: None)
    monkeypatch.setattr("kol_archive.browser.time.sleep", lambda _s: None)

    with pytest.raises(BrowserError, match="调试端口在超时内仍未就绪"):
        ensure_dedicated_browser(
            profile_dir=tmp_path,
            cdp_url="http://127.0.0.1:9224",
            landing_url="https://xueqiu.com",
            startup_timeout_seconds=0.0,
        )
