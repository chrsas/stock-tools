from __future__ import annotations

from pathlib import Path
from typing import Any

from probe.probe_login import deep_merge, load_config, resolve_cookie


def write_config(conf_dir: Path, name: str, content: str) -> None:
    conf_dir.mkdir(exist_ok=True)
    (conf_dir / name).write_text(content, encoding="utf-8")


def test_deep_merge_recurses_without_mutating_base() -> None:
    base: dict[str, Any] = {
        "auth": {"cookie_env": "BASE_COOKIE", "cookie": ""},
        "polling": {"feed_interval_minutes": 180},
    }

    merged = deep_merge(base, {"auth": {"cookie_env": "LOCAL_COOKIE"}})

    assert merged == {
        "auth": {"cookie_env": "LOCAL_COOKIE", "cookie": ""},
        "polling": {"feed_interval_minutes": 180},
    }
    assert base["auth"]["cookie_env"] == "BASE_COOKIE"


def test_load_config_applies_local_override_and_deduplicates_accounts(tmp_path: Path) -> None:
    conf_dir = tmp_path / "config"
    write_config(
        conf_dir,
        "config.yml",
        """
auth:
  cookie_env: BASE_COOKIE
polling:
  feed_interval_minutes: 180
accounts:
  - uid: "100"
    note: base-first
  - uid: ""
    note: ignored-empty
  - uid: 200
    note: base-second
""",
    )
    write_config(
        conf_dir,
        "config.local.yml",
        """
auth:
  cookie_env: LOCAL_COOKIE
accounts:
  - uid: "200"
    note: ignored-duplicate
  - uid: "300"
    note: local-third
""",
    )

    cfg = load_config(conf_dir)

    assert cfg["auth"]["cookie_env"] == "LOCAL_COOKIE"
    assert cfg["polling"]["feed_interval_minutes"] == 180
    assert [(str(account["uid"]), account["note"]) for account in cfg["accounts"]] == [
        ("100", "base-first"),
        ("200", "base-second"),
        ("300", "local-third"),
    ]


def test_load_config_works_without_local_file(tmp_path: Path) -> None:
    conf_dir = tmp_path / "config"
    write_config(
        conf_dir,
        "config.yml",
        """
accounts:
  - uid: "100"
""",
    )

    cfg = load_config(conf_dir)

    assert cfg["accounts"] == [{"uid": "100"}]


def test_resolve_cookie_prefers_configured_environment_variable(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("CUSTOM_COOKIE", "from-env")
    cfg = {"auth": {"cookie_env": "CUSTOM_COOKIE", "cookie": "from-local"}}

    assert resolve_cookie(cfg) == ("from-env", "env")
