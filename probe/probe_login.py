"""带登录补测：验证 cookie 有效、登录解锁 feed page>=2、追踪账号 uid 有效。

配置加载约定（1b 加载器雏形）：config.yml 默认 <- config.local.yml 覆盖；
cookie 优先环境变量 XUEQIU_COOKIE，其次 config.local.auth.cookie。
不入库、不导出 cookie。

用法: python -m probe.probe_login
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from kol_archive.config import load_config, resolve_cookie
from probe.probe_xueqiu import bootstrap, new_client

ROOT = Path(__file__).parent.parent
CONF_DIR = ROOT / "config"


def client_with_cookie(cookie_str: str | None) -> httpx.Client:
    client = new_client()
    bootstrap(client)
    if cookie_str:
        for part in cookie_str.split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                client.cookies.set(key.strip(), value.strip(), domain=".xueqiu.com")
    return client


def main() -> None:
    cfg = load_config(CONF_DIR)
    if not cfg["accounts"]:
        raise SystemExit(
            "未配置追踪账号：请在 config/config.local.yml 的 accounts 中至少填写一个 uid"
        )
    cookie, source = resolve_cookie(cfg)
    print(f"accounts loaded: {[(a.get('uid'), a.get('note')) for a in cfg['accounts']]}")
    print(f"cookie present: {bool(cookie)} (source: {source})")
    client = client_with_cookie(cookie)
    try:
        uid = cfg["accounts"][0]["uid"]
        for page in (1, 2):
            response = client.get(
                f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page={page}"
            )
            ok = response.status_code == 200
            extra = ""
            if ok:
                result = response.json()
                extra = (
                    f"statuses={len(result.get('statuses', []))} maxPage={result.get('maxPage')}"
                )
            else:
                try:
                    extra = f"error_code={response.json().get('error_code')}"
                except Exception:
                    extra = response.text[:80]
            print(f"  uid={uid} page={page} -> {response.status_code}  {extra}")
            time.sleep(2.0)

        print("\n--- uid 有效性 ---")
        for account in cfg["accounts"]:
            uid = account["uid"]
            response = client.get(
                f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page=1"
            )
            screen_name = ""
            if response.status_code == 200:
                statuses = response.json().get("statuses", [])
                if statuses:
                    screen_name = (statuses[0].get("user") or {}).get("screen_name", "")
            print(
                f"  uid={uid} ({account.get('note')}) -> {response.status_code}  "
                f"screen_name={screen_name!r}"
            )
            time.sleep(2.0)
    finally:
        client.close()


if __name__ == "__main__":
    main()
