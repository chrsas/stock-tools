"""Dedicated-browser session for Xueqiu, fronting the WAF/slider via real Edge over CDP.

Background: Xueqiu's data path sits behind an Aliyun WAF JS challenge plus a slider
captcha that fingerprint datacenter/headless sessions (see ``probe/probe_findings.md``
section 14). Pure ``httpx`` cookie replay and Playwright's bundled Chromium both get
blocked. The proven workaround (ported from ``E:\\Bmw\\tools\\xueqiu_monitoring``):

1. Launch the *real, installed* browser (Edge/Chrome) as its own process with a
   persistent ``--user-data-dir`` and ``--remote-debugging-port``.
2. The user passes the slider / logs in once in that window; trust cookies persist
   in the profile.
3. Connect Playwright to that already-running browser over CDP and fetch through it,
   so every request carries a genuine browser fingerprint.

``BrowserClient`` mimics the slice of ``httpx.Client`` the collector already uses
(``get(url, params=...)`` returning an ``httpx.Response``, raising ``httpx.HTTPError``
on transport failure), so the collector and its offline tests stay untouched.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on local runtime
    sync_playwright = None  # type: ignore[assignment]

DEFAULT_CDP_URL = "http://127.0.0.1:9224"
DEFAULT_PROFILE_DIR = "data/browser_profile"
DEFAULT_LANDING_URL = "https://xueqiu.com"
DEFAULT_EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
NAV_TIMEOUT_MS = 60000
# Default per-request budget so a stuck Xueqiu/WAF/CDP call can never hang the run forever.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
# Bound the in-page fetch with AbortController, plus a small margin for the CDP round-trip
# so the Python-side evaluate returns (with an AbortError) before any outer wait.
_FETCH_JS = """
async ({ url, timeoutMs }) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, { credentials: 'include', signal: controller.signal });
        const body = await response.text();
        return { status: response.status, body: body };
    } finally {
        clearTimeout(timer);
    }
}
"""
# Substrings that mark the Aliyun WAF challenge / slider page rather than real data.
CHALLENGE_HINTS = (
    "aliyun_waf",
    "_waf_",
    "访问验证",
    "请按住滑块",
    "滑块验证",
    "滑动验证",
)


class BrowserError(RuntimeError):
    """Raised when the dedicated browser cannot be launched or reached."""


def require_playwright() -> None:
    if sync_playwright is not None:
        return
    raise BrowserError("Playwright 未安装。请在项目虚拟环境中安装 playwright 后重试。")


def parse_cdp_port(cdp_url: str) -> int:
    parsed = urlparse(cdp_url)
    if parsed.port:
        return parsed.port
    raise BrowserError(f"CDP URL 必须带端口：{cdp_url}")


def find_browser_executable(edge_path: str | None = None) -> Path:
    candidates = [edge_path] if edge_path else []
    candidates.extend(DEFAULT_EDGE_PATHS)
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return path
    raise BrowserError(
        "未找到浏览器可执行文件。请在 config 的 browser.edge_path 指向 msedge.exe 或 chrome.exe。"
    )


def start_dedicated_browser(
    *,
    profile_dir: Path,
    cdp_url: str,
    url: str,
    edge_path: str | None = None,
    minimized: bool = False,
) -> subprocess.Popen[Any]:
    """Launch the real browser as its own process with a persistent profile + CDP port."""
    executable = find_browser_executable(edge_path)
    # Edge resolves a relative --user-data-dir against an unpredictable working directory
    # (the serve process's cwd, not the repo root). When it lands on a path that isn't a
    # valid distinct profile, Edge silently hands the URL off to the user's already-running
    # Edge and exits — no dedicated process, no debug port. An absolute path is the fix.
    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(executable),
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--new-window",
        f"--remote-debugging-port={parse_cdp_port(cdp_url)}",
    ]
    if minimized:
        args.append("--start-minimized")
    args.append(url)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def is_cdp_reachable(cdp_url: str, *, timeout_seconds: float = 1.0) -> bool:
    """Return True if the dedicated browser's CDP debug endpoint answers.

    A lightweight ``/json/version`` GET is enough to tell "browser process is up" from
    "nothing is listening", without paying for a full Playwright CDP attach.
    """
    version_url = cdp_url.rstrip("/") + "/json/version"
    try:
        response = httpx.get(version_url, timeout=timeout_seconds)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def ensure_dedicated_browser(
    *,
    profile_dir: Path,
    cdp_url: str,
    landing_url: str,
    edge_path: str | None = None,
    minimized: bool = False,
    startup_timeout_seconds: float = 20.0,
    poll_interval_seconds: float = 0.5,
) -> bool:
    """Make sure the dedicated browser is running; launch it from the persistent profile if not.

    Returns ``True`` if the browser was already up, ``False`` if it had to be launched.
    Because trust cookies live in ``profile_dir``, a relaunch usually comes back already
    authenticated, so callers can proceed straight to collecting. Raises ``BrowserError``
    if the CDP port never becomes reachable within ``startup_timeout_seconds`` (e.g. the
    window is stuck on a slider / security prompt the user must clear by hand).
    """
    if is_cdp_reachable(cdp_url):
        return True
    start_dedicated_browser(
        profile_dir=profile_dir,
        cdp_url=cdp_url,
        url=landing_url,
        edge_path=edge_path,
        minimized=minimized,
    )
    deadline = time.monotonic() + startup_timeout_seconds
    while time.monotonic() < deadline:
        if is_cdp_reachable(cdp_url):
            return False
        time.sleep(poll_interval_seconds)
    raise BrowserError(
        "已尝试自动启动专用雪球浏览器，但调试端口在超时内仍未就绪。"
        "请查看弹出的窗口是否卡在滑块或安全提示上，手动处理后重试。"
    )


def body_looks_like_challenge(body: str) -> bool:
    haystack = body.lower()
    return any(hint.lower() in haystack for hint in CHALLENGE_HINTS)


class BrowserClient:
    """Drop-in replacement for the ``httpx.Client`` slice the collector relies on.

    Fetches run as same-origin ``fetch`` calls inside a real browser tab connected over
    CDP, so cookies and the genuine fingerprint that cleared the WAF are reused.
    """

    def __init__(
        self,
        page: Any,
        *,
        default_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        _playwright: Any = None,
        _browser: Any = None,
    ) -> None:
        self._page = page
        self._default_timeout_seconds = default_timeout_seconds
        self._playwright = _playwright
        self._browser = _browser

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        full_url = f"{url}?{urlencode(params)}" if params else url
        budget = self._default_timeout_seconds if timeout is None else timeout
        timeout_ms = max(1, int(budget * 1000))
        # The AbortController forces the in-page fetch to reject after timeout_ms, so a stalled
        # Xueqiu/WAF response (the realistic hang) makes evaluate return instead of blocking.
        try:
            result = self._page.evaluate(_FETCH_JS, {"url": full_url, "timeoutMs": timeout_ms})
        except Exception as exc:
            raise httpx.HTTPError(f"专用浏览器请求失败：{type(exc).__name__}") from exc
        return httpx.Response(int(result["status"]), text=str(result["body"]))

    def close(self) -> None:
        for closer in (
            lambda: self._page.close(),
            lambda: self._browser.close() if self._browser is not None else None,
            lambda: self._playwright.stop() if self._playwright is not None else None,
        ):
            try:
                closer()
            except Exception:  # pragma: no cover - best-effort teardown
                pass


def first_context(browser: Any) -> Any:
    if browser.contexts:
        return browser.contexts[0]
    raise BrowserError("已连接到专用浏览器，但没有可用的浏览器上下文。请先在窗口里打开雪球。")


def create_xueqiu_browser_client(
    cdp_url: str = DEFAULT_CDP_URL,
    *,
    landing_url: str = DEFAULT_LANDING_URL,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> BrowserClient:
    """Connect to the running dedicated browser over CDP and return a fetch client.

    The caller owns the returned client and must ``close()`` it. Requires that the
    dedicated browser has already been launched (see ``start_dedicated_browser`` / the
    ``login`` command) and that the user has cleared any slider once.
    """
    require_playwright()
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
    except Exception as exc:
        playwright.stop()
        raise BrowserError(
            f"连不上专用雪球浏览器的调试端口：{cdp_url}。"
            "请先用 `login` 命令或 start_xueqiu_browser.ps1 启动专用浏览器。"
        ) from exc

    try:
        context = first_context(browser)
        page = context.new_page()
        page.goto(landing_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception:
        browser.close()
        playwright.stop()
        raise
    return BrowserClient(
        page,
        default_timeout_seconds=request_timeout_seconds,
        _playwright=playwright,
        _browser=browser,
    )
