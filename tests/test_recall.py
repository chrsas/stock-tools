from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from kol_archive.database import connect_database, initialize_database
from kol_archive.recall import (
    RetrievalQuery,
    TermGroup,
    build_recall_page,
    parse_term_group,
    parse_window_bound,
    retrieve,
)


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect_database(tmp_path / "recall.sqlite3")
    initialize_database(connection)
    return connection


def _author(connection: sqlite3.Connection, uid: str) -> int:
    existing = connection.execute(
        "SELECT id FROM authors WHERE platform = 'xueqiu' AND platform_uid = ?", (uid,)
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    return _lastrowid(
        connection.execute(
            """
            INSERT INTO authors(platform, platform_uid, live_monitoring_started_at, notes)
            VALUES ('xueqiu', ?, '2025-01-01T00:00:00+00:00', ?)
            """,
            (uid, f"作者{uid}"),
        )
    )


def _post(
    connection: sqlite3.Connection,
    uid: str,
    text: str,
    observed_at: str,
    *,
    ingest_mode: str = "live",
    removed: bool = False,
) -> tuple[int, int]:
    author_id = _author(connection, uid)
    post_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO posts(
                author_id, platform, platform_post_id, first_seen_at, feed_state,
                source_state, watch_mode, ingest_mode, posted_at_claimed
            ) VALUES (?, 'xueqiu', ?, ?, 'present', ?, 'recent_window', ?, ?)
            """,
            (
                author_id,
                f"post-{uid}-{observed_at}",
                observed_at,
                "gone_confirmed" if removed else "reachable",
                ingest_mode,
                observed_at,
            ),
        )
    )
    version_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO post_versions(
                post_id, content_text, content_hash, first_observed_at, ingest_mode, raw_payload
            ) VALUES (?, ?, ?, ?, ?, '{}')
            """,
            (post_id, text, f"hash-{post_id}", observed_at, ingest_mode),
        )
    )
    if removed:
        connection.execute(
            """
            INSERT INTO post_events(post_id, dimension, from_value, to_value, detected_at)
            VALUES (?, 'source_state', 'reachable', 'gone_confirmed', ?)
            """,
            (post_id, observed_at),
        )
    return post_id, version_id


# 2025-06 美伊冲突窗口（北京时间）。
WINDOW_FROM = "2025-06-10"
WINDOW_TO = "2025-06-30"


def _event_market() -> tuple[TermGroup, TermGroup]:
    return (
        TermGroup("event", ("美伊", "伊朗", "霍尔木兹")),
        TermGroup("market", ("油价", "原油", "布油")),
    )


def test_window_bound_reads_dates_as_beijing_local() -> None:
    # 2025-06-10 北京时间 00:00 == 2025-06-09 16:00 UTC。
    assert parse_window_bound("2025-06-10").startswith("2025-06-09T16:00:00")
    assert parse_window_bound("2025-06-30", end_of_day=True).startswith("2025-06-30T15:59:59")
    # 带时区的完整时间戳按自身偏移解析。
    assert parse_window_bound("2025-06-10T00:00:00+00:00").startswith("2025-06-10T00:00:00")


def test_cross_group_and_excludes_market_only_noise(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _post(connection, "a", "伊朗局势紧张，油价可能冲高", "2025-06-15T01:00:00+00:00")
        # 只谈油价、与冲突无关 —— 组间 AND 应排除。
        _post(connection, "b", "夏季出行旺季，油价季节性走强", "2025-06-16T01:00:00+00:00")
        event, market = _event_market()

        result = retrieve(
            connection,
            RetrievalQuery((event, market), WINDOW_FROM, WINDOW_TO),
        )

        hits = cast(list[dict[str, object]], result["hits"])
        assert [hit["author_platform_uid"] for hit in hits] == ["a"]
        coverage = cast(dict[str, object], result["coverage"])
        assert coverage["version_count"] == 1
        assert coverage["author_count"] == 1
        # 各组各自窗内命中数仍可见：market 组命中 2，event 组命中 1。
        groups = {g["label"]: g for g in cast(list[dict[str, object]], coverage["groups"])}
        assert groups["market"]["version_count"] == 2
        assert groups["event"]["version_count"] == 1
    finally:
        connection.close()


def test_any_group_relaxes_to_or(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _post(connection, "a", "伊朗局势紧张，油价可能冲高", "2025-06-15T01:00:00+00:00")
        _post(connection, "b", "夏季出行旺季，油价季节性走强", "2025-06-16T01:00:00+00:00")
        event, market = _event_market()

        result = retrieve(
            connection,
            RetrievalQuery((event, market), WINDOW_FROM, WINDOW_TO, require_all_groups=False),
        )

        hits = cast(list[dict[str, object]], result["hits"])
        assert {hit["author_platform_uid"] for hit in hits} == {"a", "b"}
    finally:
        connection.close()


def test_window_filters_out_of_range_and_backfill(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _post(connection, "a", "伊朗与油价", "2025-06-15T01:00:00+00:00")
        # 窗外（5 月）。
        _post(connection, "b", "伊朗与油价", "2025-05-15T01:00:00+00:00")
        # 窗内但 backfill，非 live。
        _post(connection, "c", "伊朗与油价", "2025-06-17T01:00:00+00:00", ingest_mode="backfill")
        event, market = _event_market()

        result = retrieve(
            connection,
            RetrievalQuery((event, market), WINDOW_FROM, WINDOW_TO),
        )

        hits = cast(list[dict[str, object]], result["hits"])
        assert [hit["author_platform_uid"] for hit in hits] == ["a"]
    finally:
        connection.close()


def test_matches_across_stance_framework_and_ocr(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        # 正文含 event 词、不含 market 词；market 侧靠立场摘要「原油」命中。
        _, stance_version = _post(connection, "a", "伊朗风险评估", "2025-06-15T01:00:00+00:00")
        connection.execute(
            """
            INSERT INTO enrichments(
                post_id, version_id, post_type, label_first_hand_info,
                label_transferable_framework, label_reasoned_non_consensus,
                rationale, evidence_snippet, stance_summary, is_market_related,
                model, prompt_version, created_at
            ) VALUES (
                (SELECT post_id FROM post_versions WHERE id = ?), ?, '观点', 0, 0, 0,
                '依据', '片段', '看多原油', 1, 'm', 'enrich-v2', '2025-06-15T02:00:00+00:00'
            )
            """,
            (stance_version, stance_version),
        )
        # 正文不含 market 词，framework summary 含「布油」。
        _, fw_version = _post(connection, "b", "霍尔木兹海峡评估", "2025-06-16T01:00:00+00:00")
        connection.execute(
            """
            INSERT INTO framework_extractions(
                post_id, version_id, topic, summary, input_variables, logic_chain,
                conclusion_shape, evidence_snippet, model, prompt_version, created_at
            ) VALUES (
                (SELECT post_id FROM post_versions WHERE id = ?), ?, '地缘', '布油定价框架',
                '["供给"]', '链', '形状', '片段', 'm', 'enrich-v3', '2025-06-16T02:00:00+00:00'
            )
            """,
            (fw_version, fw_version),
        )
        # 正文不含 market 词，OCR 含「油价」。
        _, ocr_version = _post(connection, "c", "美伊冲突图表", "2025-06-17T01:00:00+00:00")
        image_id = _lastrowid(
            connection.execute(
                """
                INSERT INTO post_images(
                    version_id, source_url, normalized_url, ordinal, sha256, mime_type,
                    byte_size, image_bytes, downloaded_at, download_status
                ) VALUES (?, 'u', 'u', 0, 's', 'image/png', 1, X'00', ?, 'ok')
                """,
                (ocr_version, "2025-06-17T02:00:00+00:00"),
            )
        )
        connection.execute(
            """
            INSERT INTO image_ocr(
                image_id, image_sha256, engine, engine_version, ocr_text, created_at
            ) VALUES (?, 's', 'eng', '1', '油价走势图', '2025-06-17T03:00:00+00:00')
            """,
            (image_id,),
        )
        event, market = _event_market()

        result = retrieve(
            connection,
            RetrievalQuery((event, market), WINDOW_FROM, WINDOW_TO),
        )

        hits = cast(list[dict[str, object]], result["hits"])
        assert {hit["author_platform_uid"] for hit in hits} == {"a", "b", "c"}
        by_author = {hit["author_platform_uid"]: hit for hit in hits}
        assert by_author["a"]["stance_summary"] == "看多原油"
        assert by_author["b"]["framework_topics"] == ["地缘"]
    finally:
        connection.close()


def test_ticker_filter_narrows_and_selection_flags_removal(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _, kept = _post(connection, "a", "伊朗与油价", "2025-06-15T01:00:00+00:00")
        connection.execute(
            "INSERT INTO version_tickers(version_id, ticker) VALUES (?, 'SH601857')", (kept,)
        )
        # 命中分组但无该标的 —— 被 ticker 过滤掉。
        _post(connection, "b", "伊朗与油价", "2025-06-16T01:00:00+00:00")
        # 命中分组、有标的、且后来删帖 —— 计入 selection。
        _, removed = _post(
            connection, "c", "伊朗与油价后续", "2025-06-18T01:00:00+00:00", removed=True
        )
        connection.execute(
            "INSERT INTO version_tickers(version_id, ticker) VALUES (?, 'SH601857')", (removed,)
        )
        event, market = _event_market()

        result = retrieve(
            connection,
            RetrievalQuery((event, market), WINDOW_FROM, WINDOW_TO, tickers=("sh601857",)),
        )

        hits = cast(list[dict[str, object]], result["hits"])
        assert {hit["author_platform_uid"] for hit in hits} == {"a", "c"}
        selection = cast(dict[str, object], result["selection"])
        assert selection["removed_post_count"] == 1
        removed_post = next(hit for hit in hits if hit["author_platform_uid"] == "c")
        assert removed_post["removed"] is True
        assert selection["removed_post_ids"] == [removed_post["post_id"]]
    finally:
        connection.close()


def test_coverage_and_selection_ignore_limit(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        # 4 命中版本、3 个作者、2 条后来删帖；limit=1 只截断明细。
        _post(connection, "a", "伊朗与油价 1", "2025-06-12T01:00:00+00:00")
        _post(connection, "a", "伊朗与油价 2", "2025-06-13T01:00:00+00:00")
        _post(connection, "b", "伊朗与油价 3", "2025-06-14T01:00:00+00:00", removed=True)
        _post(connection, "c", "伊朗与油价 4", "2025-06-15T01:00:00+00:00", removed=True)
        event, market = _event_market()

        result = retrieve(
            connection,
            RetrievalQuery((event, market), WINDOW_FROM, WINDOW_TO, limit=1),
        )

        assert len(cast(list[dict[str, object]], result["hits"])) == 1
        coverage = cast(dict[str, object], result["coverage"])
        assert coverage["version_count"] == 4
        assert coverage["author_count"] == 3
        assert coverage["post_count"] == 4
        selection = cast(dict[str, object], result["selection"])
        assert selection["removed_post_count"] == 2
        assert len(cast(list[int], selection["removed_post_ids"])) == 2
    finally:
        connection.close()


def _recall_page(connection: sqlite3.Connection, values: dict[str, list[str]]) -> dict[str, object]:
    return build_recall_page(
        connection, values, prompt_version="enrich-v2", benchmark_ticker="SH000300"
    )


def test_parse_term_group_and_errors() -> None:
    assert parse_term_group("market=油价, 原油 ,") == TermGroup("market", ("油价", "原油"))
    with pytest.raises(ValueError, match="label=词1,词2"):
        parse_term_group("油价,原油")  # missing label=
    with pytest.raises(ValueError, match="未提供检索词"):
        parse_term_group("market=")


def test_build_recall_page_runs_retrieval_and_echoes_form(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _post(connection, "a", "伊朗局势紧张，油价可能冲高", "2025-06-15T01:00:00+00:00")
        _post(connection, "b", "夏季出行旺季，油价季节性走强", "2025-06-16T01:00:00+00:00")

        page = _recall_page(
            connection,
            {
                "q": ["美伊冲突油价"],
                "group": ["event=美伊,伊朗,霍尔木兹", "market=油价,原油,布油"],
                "from": [WINDOW_FROM],
                "to": [WINDOW_TO],
            },
        )

        assert page["view"] == "recall"
        assert page["has_results"] is True
        hits = cast(list[dict[str, object]], page["hits"])
        assert [hit["author_platform_uid"] for hit in hits] == ["a"]
        form = cast(dict[str, object], page["form"])
        assert form["question"] == "美伊冲突油价"
        assert form["date_from"] == WINDOW_FROM
        assert form["require_all_groups"] is True
        assert form["groups"] == [
            {"label": "event", "terms": ["美伊", "伊朗", "霍尔木兹"]},
            {"label": "market", "terms": ["油价", "原油", "布油"]},
        ]
    finally:
        connection.close()


def test_build_recall_page_without_groups_returns_form_only(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        page = _recall_page(connection, {"q": ["还没填检索词"]})
        assert page["has_results"] is False
        assert "error" not in page
        assert cast(dict[str, object], page["form"])["groups"] == []
    finally:
        connection.close()


def test_build_recall_page_requires_window_when_groups_present(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        page = _recall_page(connection, {"group": ["event=伊朗"], "from": [WINDOW_FROM]})
        assert page["has_results"] is False
        assert "时间窗" in str(page["error"])
    finally:
        connection.close()


def test_build_recall_page_flags_invalid_groups(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        page = _recall_page(connection, {"group": ["没有等号"]})
        assert page["has_results"] is False
        form = cast(dict[str, object], page["form"])
        assert form["invalid_groups"] == ["没有等号"]
        assert "label=词1,词2" in str(page["error"])
    finally:
        connection.close()


def test_build_recall_page_any_group_and_ticker_params(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _post(connection, "a", "伊朗局势紧张", "2025-06-15T01:00:00+00:00")
        _post(connection, "b", "油价季节性走强", "2025-06-16T01:00:00+00:00")

        page = _recall_page(
            connection,
            {
                "group": ["event=伊朗", "market=油价"],
                "from": [WINDOW_FROM],
                "to": [WINDOW_TO],
                "any": ["1"],
                "ticker": ["sh601857, sh000300"],
                "limit": ["5"],
            },
        )

        assert page["has_results"] is True
        coverage = cast(dict[str, object], page["coverage"])
        assert coverage["require_all_groups"] is False
        form = cast(dict[str, object], page["form"])
        assert form["require_all_groups"] is False
        assert form["tickers"] == ["SH601857", "SH000300"]
        assert form["limit"] == 5
    finally:
        connection.close()


def test_topic_briefs_table_is_append_only(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        connection.execute(
            """
            INSERT INTO topic_briefs(
                question, groups, tickers, date_from, date_to, require_all_groups,
                coverage, selection, cited_version_ids, brief_text, model,
                prompt_version, created_at
            ) VALUES (
                '美伊冲突油价', '[{"label":"event","terms":["伊朗"]}]', '[]',
                '2025-06-10T00:00:00+00:00', '2025-06-30T15:59:59+00:00', 1,
                '{}', '{}', '[1]', '简报正文', 'm', 'brief-v1', '2025-07-01T00:00:00+00:00'
            )
            """
        )
        row = connection.execute("SELECT id FROM topic_briefs").fetchone()
        brief_id = int(row["id"])
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE topic_briefs SET brief_text = '改写' WHERE id = ?", (brief_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM topic_briefs WHERE id = ?", (brief_id,))
    finally:
        connection.close()


def test_literal_underscore_term_is_not_a_wildcard(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _post(connection, "a", "伊朗 WTI_原油 期货", "2025-06-15T01:00:00+00:00")
        _post(connection, "b", "伊朗 WTIX原油 现货", "2025-06-16T01:00:00+00:00")

        result = retrieve(
            connection,
            RetrievalQuery(
                (TermGroup("event", ("伊朗",)), TermGroup("market", ("WTI_原油",))),
                WINDOW_FROM,
                WINDOW_TO,
            ),
        )

        hits = cast(list[dict[str, object]], result["hits"])
        # '_' 必须按字面匹配，不能当成 LIKE 通配命中 b。
        assert [hit["author_platform_uid"] for hit in hits] == ["a"]
    finally:
        connection.close()
