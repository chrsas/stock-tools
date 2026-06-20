"""Shared projection helpers for the read-only presentation layer."""

from __future__ import annotations

import re
import sqlite3

from kol_archive.maintenance import redact_text
from kol_archive.models import FeedState, SourceState, WatchMode

_CN_TICKER = re.compile(r"^(?:SH|SZ|BJ)\d{6}$")
_TICKER_NAME = re.compile(r"\$([^$()]+)\(((?:SH|SZ|BJ)\d{6})\)\$")
_MARKET_RELATED_VIEWPOINT_SQL = """
(e.is_market_related = 1
 OR EXISTS (SELECT 1 FROM claims market_claim WHERE market_claim.version_id = e.version_id))
"""


def _row_dict(
    row: sqlite3.Row,
    *,
    redacted_columns: frozenset[str] = frozenset(),
) -> dict[str, object]:
    return {
        str(key): redact_text(str(row[key]))
        if key in redacted_columns and row[key] is not None
        else row[key]
        for key in row.keys()
    }


def _status_summary(
    feed_state: FeedState,
    source_state: SourceState,
    watch_mode: WatchMode,
) -> dict[str, str]:
    summary = {
        "human_label": (
            f"列表观察：{_feed_label(feed_state)}；来源：{_source_label(source_state)}；"
            f"监控：{_watch_label(watch_mode)}"
        ),
    }
    if source_state is SourceState.GONE_CONFIRMED:
        return summary | {
            "deletion_signal_level": "strong",
            "deletion_signal_label": "强信号：来源页明确显示已移除，不归因移除主体",
        }
    if source_state is SourceState.UNAVAILABLE:
        return summary | {
            "deletion_signal_level": "weak",
            "deletion_signal_label": "弱信号：直链当前不可访问，无法确认移除",
        }
    if feed_state is FeedState.ABSENT_CONFIRMED:
        return summary | {
            "deletion_signal_level": "weak",
            "deletion_signal_label": "弱信号：列表观察连续健康轮次缺席，未经直链确认",
        }
    if feed_state is FeedState.OUT_OF_SCOPE and watch_mode is WatchMode.INACTIVE:
        return summary | {
            "deletion_signal_level": "none",
            "deletion_signal_label": "无删帖信号：帖子已滑出监控窗口",
        }
    if feed_state is FeedState.PRESENT:
        return summary | {
            "deletion_signal_level": "none",
            "deletion_signal_label": "无删帖信号：最近列表观察为在场",
        }
    return summary | {
        "deletion_signal_level": "none",
        "deletion_signal_label": "无删帖信号：当前证据不足",
    }


def _feed_label(state: FeedState) -> str:
    return {
        FeedState.PRESENT: "在场",
        FeedState.ABSENT_CONFIRMED: "连续健康轮次缺席",
        FeedState.OUT_OF_SCOPE: "已滑出监控窗口",
        FeedState.UNKNOWN: "待确认",
    }[state]


def _source_label(state: SourceState) -> str:
    return {
        SourceState.REACHABLE: "直链可访问",
        SourceState.GONE_CONFIRMED: "来源页明确显示已移除",
        SourceState.UNAVAILABLE: "直链当前不可访问",
        SourceState.UNKNOWN: "未复查",
    }[state]


def _watch_label(mode: WatchMode) -> str:
    return {
        WatchMode.RECENT_WINDOW: "近期窗口",
        WatchMode.PINNED: "已钉住",
        WatchMode.INACTIVE: "停止持续监控",
    }[mode]


def _post_projection(row: sqlite3.Row) -> dict[str, object]:
    projection = _row_dict(row)
    projection["status"] = _status_summary(
        FeedState(str(row["feed_state"])),
        SourceState(str(row["source_state"])),
        WatchMode(str(row["watch_mode"])),
    )
    return projection
