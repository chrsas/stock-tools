from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kol_archive.accounts import add_account, parse_account_input
from kol_archive.config import MANAGED_ACCOUNTS_FILENAME, load_config


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234567890", "1234567890"),
        ("  1234567890  ", "1234567890"),
        ("https://xueqiu.com/u/1234567890", "1234567890"),
        ("https://xueqiu.com/u/1234567890/", "1234567890"),
        ("https://xueqiu.com/u/1234567890#/", "1234567890"),
        ("https://xueqiu.com/u/1234567890?ref=share", "1234567890"),
        ("https://www.xueqiu.com/u/1234567890", "1234567890"),
        ("xueqiu.com/u/1234567890", "1234567890"),
    ],
)
def test_parse_account_input_extracts_uid(raw: str, expected: str) -> None:
    assert parse_account_input(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://example.com/profile",
        "@nickname",
        # Foreign host carrying a /u/ path must not be read as a Xueqiu account.
        "https://example.com/u/1234567890",
        # Substring /u/<digits> inside a longer path is not a profile URL.
        "prefix/u/987suffix",
        "https://xueqiu.com/u/123/extra",
        # Look-alike hosts must not pass the suffix check.
        "https://xueqiu.com.evil.com/u/123",
        "https://evil-xueqiu.com/u/123",
        # Bare profile path with no host can't be confirmed as Xueqiu.
        "/u/1234567890",
    ],
)
def test_parse_account_input_rejects_unparseable(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_account_input(raw)


def _config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_dir.joinpath("config.yml").write_text("platform: xueqiu\n", encoding="utf-8")
    return config_dir


def test_add_account_writes_managed_file_and_merges(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)

    result = add_account(config_dir, "https://xueqiu.com/u/1234567890", note="测试博主")
    assert result.status == "added"
    assert result.uid == "1234567890"

    managed = yaml.safe_load(
        config_dir.joinpath(MANAGED_ACCOUNTS_FILENAME).read_text(encoding="utf-8")
    )
    assert managed["accounts"] == [
        {"uid": "1234567890", "note": "测试博主", "watch_mode": "recent_window"}
    ]

    merged = {str(a["uid"]) for a in load_config(config_dir)["accounts"]}
    assert merged == {"1234567890"}


def test_add_account_is_idempotent(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    add_account(config_dir, "1234567890")
    again = add_account(config_dir, "https://xueqiu.com/u/1234567890")
    assert again.status == "exists"

    managed = yaml.safe_load(
        config_dir.joinpath(MANAGED_ACCOUNTS_FILENAME).read_text(encoding="utf-8")
    )
    assert len(managed["accounts"]) == 1


def test_add_account_skips_uid_already_in_committed_config(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    config_dir.joinpath("config.yml").write_text(
        "platform: xueqiu\naccounts:\n  - uid: '999'\n    note: 手填\n",
        encoding="utf-8",
    )
    result = add_account(config_dir, "999")
    assert result.status == "exists"
    assert not config_dir.joinpath(MANAGED_ACCOUNTS_FILENAME).exists()


def test_managed_accounts_do_not_shadow_handwritten(tmp_path: Path) -> None:
    config_dir = _config_dir(tmp_path)
    config_dir.joinpath("config.local.yml").write_text(
        "accounts:\n  - uid: '555'\n    note: 手填\n    watch_mode: pinned\n",
        encoding="utf-8",
    )
    # A managed entry for the same uid must not override the hand-written watch_mode.
    config_dir.joinpath(MANAGED_ACCOUNTS_FILENAME).write_text(
        "accounts:\n  - uid: '555'\n    watch_mode: recent_window\n",
        encoding="utf-8",
    )
    accounts = load_config(config_dir)["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["watch_mode"] == "pinned"
