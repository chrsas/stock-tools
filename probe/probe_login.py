"""带登录补测：验证 cookie 有效、登录解锁 feed page≥2、追踪账号 uid 有效。

配置加载约定（1b 加载器雏形）：config.yml 默认 ← config.local.yml 覆盖；
cookie 优先环境变量 XUEQIU_COOKIE，其次 config.local.auth.cookie。
不入库、不导出 cookie。
"""
from __future__ import annotations
import os, time, sys
from pathlib import Path
import httpx, yaml
from probe_xueqiu import new_client, bootstrap, HEADERS, dump

ROOT = Path(__file__).parent.parent
CONF_DIR = ROOT / "config"


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    base = yaml.safe_load((CONF_DIR / "config.yml").read_text(encoding="utf-8")) or {}
    local_p = CONF_DIR / "config.local.yml"
    local = yaml.safe_load(local_p.read_text(encoding="utf-8")) if local_p.exists() else {}
    cfg = deep_merge(base, local or {})
    # accounts: 合并两文件中所有非空 uid（去重，保序）
    seen, accts = set(), []
    for src in ((base.get("accounts") or []), (local.get("accounts") or [])):
        for a in src:
            uid = str((a or {}).get("uid") or "").strip()
            if uid and uid not in seen:
                seen.add(uid); accts.append(a)
    cfg["accounts"] = accts
    return cfg


def resolve_cookie(cfg: dict) -> str | None:
    env_name = (cfg.get("auth") or {}).get("cookie_env") or "XUEQIU_COOKIE"
    return os.environ.get(env_name) or (cfg.get("auth") or {}).get("cookie") or None


def client_with_cookie(cookie_str: str | None) -> httpx.Client:
    c = new_client()
    bootstrap(c)  # 先拿 guest 设备 cookie（aliyungf_tc 等）
    if cookie_str:
        for part in cookie_str.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                c.cookies.set(k.strip(), v.strip(), domain=".xueqiu.com")
    return c


def main():
    cfg = load_config()
    cookie = resolve_cookie(cfg)
    print(f"accounts loaded: {[(a.get('uid'), a.get('note')) for a in cfg['accounts']]}")
    print(f"cookie present: {bool(cookie)} (source: "
          f"{'env' if os.environ.get((cfg.get('auth') or {}).get('cookie_env','XUEQIU_COOKIE')) else 'config.local'})")
    c = client_with_cookie(cookie)
    try:
        # 1) 登录是否解锁 page>=2：用第一个账号测 page1 与 page2
        uid = cfg["accounts"][0]["uid"]
        for page in (1, 2):
            r = c.get(f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page={page}")
            ok = r.status_code == 200
            extra = ""
            if ok:
                j = r.json(); extra = f"statuses={len(j.get('statuses',[]))} maxPage={j.get('maxPage')}"
            else:
                try: extra = f"error_code={r.json().get('error_code')}"
                except Exception: extra = r.text[:80]
            print(f"  uid={uid} page={page} -> {r.status_code}  {extra}")
            time.sleep(2.0)
        # 2) 校验每个 uid 有效（page1 200 且能拿到作者）
        print("\n--- uid 有效性 ---")
        for a in cfg["accounts"]:
            u = a["uid"]
            r = c.get(f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={u}&page=1")
            sn = ""
            if r.status_code == 200:
                sts = r.json().get("statuses", [])
                if sts:
                    sn = (sts[0].get("user") or {}).get("screen_name", "")
            print(f"  uid={u} ({a.get('note')}) -> {r.status_code}  screen_name={sn!r}")
            time.sleep(2.0)
    finally:
        c.close()


if __name__ == "__main__":
    main()
