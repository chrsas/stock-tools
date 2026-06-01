"""带登录补测：验证 cookie 有效、登录解锁 feed page>=2、追踪账号 uid 有效。

配置加载约定（1b 加载器雏形）：config.yml 默认 <- config.local.yml 覆盖；
cookie 优先环境变量 XUEQIU_COOKIE，其次 config.local.auth.cookie。
不入库、不导出 cookie。

用法: python -m probe.probe_login
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from probe.probe_xueqiu import bootstrap, new_client

ROOT = Path(__file__).parent.parent
CONF_DIR = ROOT / "config"


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(conf_dir: Path = CONF_DIR) -> dict[str, Any]:
    base = yaml.safe_load((conf_dir / "config.yml").read_text(encoding="utf-8")) or {}
    local_path = conf_dir / "config.local.yml"
    local = yaml.safe_load(local_path.read_text(encoding="utf-8")) if local_path.exists() else {}
    cfg = deep_merge(base, local or {})

    seen: set[str] = set()
    accounts: list[dict[str, Any]] = []
    for source in ((base.get("accounts") or []), (local.get("accounts") or [])):
        for account in source:
            uid = str((account or {}).get("uid") or "").strip()
            if uid and uid not in seen:
                seen.add(uid)
                accounts.append(account)
    cfg["accounts"] = accounts
    return cfg


def resolve_cookie(cfg: dict[str, Any]) -> tuple[str | None, str]:
    env_name = (cfg.get("auth") or {}).get("cookie_env") or "XUEQIU_COOKIE"
    env_cookie = os.environ.get(env_name)
    if env_cookie:
        return env_cookie, "env"
    local_cookie = (cfg.get("auth") or {}).get("cookie")
    if local_cookie:
        return local_cookie, "config.local"
    return None, "none"


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
    cfg = load_config()
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
