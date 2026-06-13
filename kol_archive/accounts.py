"""Register tracked bloggers from a profile URL or numeric uid.

Shared by the ``add-account`` CLI command and the web form. New accounts are written
to a tool-managed ``accounts.local.yml`` (see :data:`config.MANAGED_ACCOUNTS_FILENAME`)
rather than the hand-written ``config.local.yml``, so rewrites never disturb the user's
comments or credentials. Collection itself stays with ``run-once`` — registering only
adds the account; the first run auto-backfills a baseline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from kol_archive.config import MANAGED_ACCOUNTS_FILENAME, load_config

# Xueqiu profile pages live at ``https://xueqiu.com/u/<uid>``. We accept a bare numeric
# uid or a full profile URL, but a URL must point at a Xueqiu host and its path must be
# exactly ``/u/<uid>`` — anchored, not a substring search. Otherwise ``example.com/u/123``
# or ``prefix/u/987suffix`` would be misread as a Xueqiu account.
_BARE_UID = re.compile(r"\d+")
_PROFILE_PATH = re.compile(r"^/u/(\d+)/?$")


def _is_xueqiu_host(host: str) -> bool:
    return host == "xueqiu.com" or host.endswith(".xueqiu.com")


_MANAGED_HEADER = (
    "# 由 add-account 命令与网页表单托管：可手工编辑，但下次自动写入会规整格式。\n"
    "# 本文件仅登记追踪账号；登录凭据与其它配置仍放 config.local.yml。\n"
)


@dataclass(frozen=True)
class AddAccountResult:
    uid: str
    status: str  # "added" when newly registered, "exists" when already tracked
    note: str | None


def parse_account_input(raw: str) -> str:
    """Extract a numeric Xueqiu uid from a bare uid or a Xueqiu profile URL.

    A URL must resolve to a Xueqiu host (``xueqiu.com`` or a subdomain) and its path must
    be exactly ``/u/<uid>``; anything else is rejected rather than guessed.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("请输入雪球主页 URL 或数字 uid")
    if _BARE_UID.fullmatch(text):
        return text
    # Accept scheme-less inputs like ``xueqiu.com/u/123`` by giving urlparse a host to find.
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower()
    if not _is_xueqiu_host(host):
        raise ValueError(f"无法识别雪球主页 URL（主机需为 xueqiu.com）：{text}")
    match = _PROFILE_PATH.match(parsed.path)
    if not match:
        raise ValueError(f"雪球主页 URL 路径需形如 /u/<uid>：{text}")
    return match.group(1)


def _load_managed(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 顶层必须是映射")
    return data


def _write_managed(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    path.write_text(_MANAGED_HEADER + body, encoding="utf-8")


def add_account(config_dir: Path, raw_input: str, *, note: str | None = None) -> AddAccountResult:
    """Register one tracked blogger; idempotent across all configured account sources."""
    uid = parse_account_input(raw_input)
    note = (note or "").strip() or None

    existing = {
        str((account or {}).get("uid") or "").strip()
        for account in (load_config(config_dir).get("accounts") or [])
    }
    if uid in existing:
        return AddAccountResult(uid, "exists", note)

    path = config_dir / MANAGED_ACCOUNTS_FILENAME
    managed = _load_managed(path)
    accounts = managed.get("accounts")
    if accounts is None:
        accounts = []
    if not isinstance(accounts, list):
        raise ValueError(f"{path.name} 的 accounts 必须是列表")

    entry: dict[str, Any] = {"uid": uid}
    if note:
        entry["note"] = note
    entry["watch_mode"] = "recent_window"
    accounts.append(entry)
    managed["accounts"] = accounts
    _write_managed(path, managed)
    return AddAccountResult(uid, "added", note)
