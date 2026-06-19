"""The extracted-framework library projection."""

from __future__ import annotations

import json
import sqlite3

from kol_archive.maintenance import redact_text
from kol_archive.models import FeedState, SourceState

from .common import _feed_label, _source_label


def framework_library(
    connection: sqlite3.Connection,
    framework_prompt_version: str,
    *,
    topic: str | None = None,
    variable: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    """The extracted-framework library, browsable by topic/variable.

    Every entry links back to its source ``version_id`` and carries the source
    post's current state labels: a framework stays usable after the original
    post is gone, but the reader must see that its source is no longer readable
    at the origin (charter 4/7 — neutral wording, no attribution).
    """
    if not framework_prompt_version.strip():
        raise ValueError("prompt_version must not be empty")
    if limit < 1:
        raise ValueError("framework library limit must be positive")
    rows = connection.execute(
        """
        SELECT
            f.id, f.post_id, f.version_id, f.topic, f.summary, f.input_variables,
            f.logic_chain, f.conclusion_shape, f.applicability_conditions,
            f.invalidation_conditions, f.evidence_snippet, f.model,
            f.prompt_version, f.created_at,
            v.first_observed_at AS version_first_observed_at,
            v.content_text,
            p.feed_state, p.source_state, p.watch_mode, p.current_version_id, p.url,
            a.platform_uid AS author_platform_uid,
            COALESCE(
                json_extract(v.raw_payload, '$.user.screen_name'), a.notes
            ) AS author_display_name
        FROM framework_extractions f
        JOIN post_versions v ON v.id = f.version_id
        JOIN posts p ON p.id = f.post_id
        JOIN authors a ON a.id = p.author_id
        WHERE f.prompt_version = ?
        ORDER BY f.topic, f.created_at DESC, f.id DESC
        """,
        (framework_prompt_version.strip(),),
    ).fetchall()
    items: list[dict[str, object]] = []
    topic_counts: dict[str, int] = {}
    variable_counts: dict[str, int] = {}
    for row in rows:
        variables = [str(item) for item in json.loads(str(row["input_variables"]))]
        row_topic = str(row["topic"])
        topic_counts[row_topic] = topic_counts.get(row_topic, 0) + 1
        for name in variables:
            variable_counts[name] = variable_counts.get(name, 0) + 1
        if topic is not None and row_topic != topic:
            continue
        if variable is not None and variable not in variables:
            continue
        source_state = SourceState(str(row["source_state"]))
        feed_state = FeedState(str(row["feed_state"]))
        is_current = row["current_version_id"] == row["version_id"]
        source_readable = source_state in {SourceState.REACHABLE, SourceState.UNKNOWN} and (
            feed_state in {FeedState.PRESENT, FeedState.UNKNOWN, FeedState.OUT_OF_SCOPE}
        )
        items.append(
            {
                "id": row["id"],
                "post_id": row["post_id"],
                "version_id": row["version_id"],
                "topic": row_topic,
                "summary": row["summary"],
                "input_variables": variables,
                "logic_chain": row["logic_chain"],
                "conclusion_shape": row["conclusion_shape"],
                "applicability_conditions": row["applicability_conditions"],
                "invalidation_conditions": row["invalidation_conditions"],
                "evidence_snippet": row["evidence_snippet"],
                "content_text": row["content_text"],
                "model": row["model"],
                "prompt_version": row["prompt_version"],
                "created_at": row["created_at"],
                "version_first_observed_at": row["version_first_observed_at"],
                "author_platform_uid": row["author_platform_uid"],
                "author_display_name": redact_text(str(row["author_display_name"]))
                if row["author_display_name"] is not None
                else None,
                "url": row["url"],
                "source_status_label": (
                    f"列表观察：{_feed_label(feed_state)}；来源：{_source_label(source_state)}"
                    + ("" if is_current else "；非当前版本（原帖其后有内容变化）")
                ),
                "source_readable": source_readable,
                "is_current_version": is_current,
            }
        )
        if len(items) >= limit:
            break
    return {
        "items": items,
        "topics": [
            {"topic": name, "count": count}
            for name, count in sorted(topic_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "variables": [
            {"variable": name, "count": count}
            for name, count in sorted(variable_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "prompt_version": framework_prompt_version.strip(),
        "topic": topic,
        "variable": variable,
    }
