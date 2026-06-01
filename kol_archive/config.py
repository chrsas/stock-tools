"""Configuration loading with ignored local overrides and environment credentials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(conf_dir: Path) -> dict[str, Any]:
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
        return str(local_cookie), "config.local"
    return None, "none"
