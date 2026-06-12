from __future__ import annotations

import sqlite3
from pathlib import Path

from kol_archive.__main__ import _digest_settings
from kol_archive.database import connect_database, initialize_database
from kol_archive.digest import DigestResult, collect_digest_events, generate_digest

START = "2026-06-01T00:00:00+00:00"
END = "2026-06-08T00:00:00+00:00"
PROMPT_VERSION = "enrich-v2"
BENCHMARK_TICKER = "SH000300"


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("insert did not produce a row id")
    return cursor.lastrowid


def _archive(tmp_path: Path) -> sqlite3.Connection:
    connection = connect_database(tmp_path / "digest.sqlite3")
    initialize_database(connection)
    return connection


def _author(connection: sqlite3.Connection, uid: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO authors(platform, platform_uid, live_monitoring_started_at, notes)
        VALUES ('xueqiu', ?, ?, ?)
        """,
        (uid, START, f"作者 {uid}"),
    )
    return _lastrowid(cursor)


def _post(
    connection: sqlite3.Connection, author_id: int, post_id: str, text: str
) -> tuple[int, int]:
    cursor = connection.execute(
        """
        INSERT INTO posts(
            author_id, platform, platform_post_id, first_seen_at, feed_state,
            source_state, watch_mode, ingest_mode
        ) VALUES (?, 'xueqiu', ?, ?, 'present', 'reachable', 'pinned', 'live')
        """,
        (author_id, post_id, START),
    )
    archive_post_id = _lastrowid(cursor)
    cursor = connection.execute(
        """
        INSERT INTO post_versions(
            post_id, content_text, content_hash, image_manifest_hash, first_observed_at,
            ingest_mode, raw_payload
        ) VALUES (?, ?, ?, 'images-a', ?, 'live', '{}')
        """,
        (archive_post_id, text, f"hash-{text}", START),
    )
    version_id = _lastrowid(cursor)
    connection.execute(
        """
        UPDATE posts
        SET current_version_id = ?, current_content_hash = ?,
            current_image_manifest_hash = 'images-a'
        WHERE id = ?
        """,
        (version_id, f"hash-{text}", archive_post_id),
    )
    return archive_post_id, version_id


def _event(
    connection: sqlite3.Connection,
    post_id: int,
    dimension: str,
    to_value: str,
    *,
    from_version_id: int | None = None,
    to_version_id: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO post_events(
            post_id, dimension, from_value, to_value, detected_at, from_version_id, to_version_id
        ) VALUES (?, ?, NULL, ?, '2026-06-05T00:00:00+00:00', ?, ?)
        """,
        (post_id, dimension, to_value, from_version_id, to_version_id),
    )


def _enrich_viewpoint(
    connection: sqlite3.Connection, post_id: int, version_id: int, prompt_version: str = "enrich-v2"
) -> None:
    connection.execute(
        """
        INSERT INTO enrichments(
            post_id, version_id, post_type, label_first_hand_info,
            label_transferable_framework, label_reasoned_non_consensus,
            is_market_related, rationale, evidence_snippet, stance_summary,
            model, prompt_version, created_at
        ) VALUES (?, ?, '观点', 0, 0, 0, 1, '理由', '片段', '立场', 'test', ?, ?)
        """,
        (post_id, version_id, prompt_version, END),
    )


def _price(connection: sqlite3.Connection, ticker: str, date: str, close: float) -> None:
    connection.execute(
        "INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)",
        (ticker, date, close),
    )


def _generate(
    connection: sqlite3.Connection, output_dir: Path, *, wave_min_accounts: int = 3
) -> DigestResult:
    return generate_digest(
        connection,
        START,
        END,
        output_dir,
        prompt_version=PROMPT_VERSION,
        benchmark_ticker=BENCHMARK_TICKER,
        wave_min_accounts=wave_min_accounts,
    )


def test_digest_writes_no_change_files(tmp_path: Path) -> None:
    connection = _archive(tmp_path)
    result = _generate(connection, tmp_path / "digests")

    assert result.events == ()
    assert "无变更" in result.markdown_path.read_text(encoding="utf-8")
    assert "无变更" in result.html_path.read_text(encoding="utf-8")
    assert not (result.markdown_path.parent / "assets").exists()


def test_digest_classifies_edit_and_image_only_change(tmp_path: Path) -> None:
    connection = _archive(tmp_path)
    author_id = _author(connection, "one")
    post_id, first_version = _post(connection, author_id, "post-1", "原始正文")
    cursor = connection.execute(
        """
        INSERT INTO post_versions(
            post_id, content_text, content_hash, image_manifest_hash, first_observed_at,
            ingest_mode, raw_payload
        ) VALUES (?, '编辑正文', 'hash-edited', 'images-a', ?, 'live', '{}')
        """,
        (post_id, END),
    )
    edited_version = _lastrowid(cursor)
    cursor = connection.execute(
        """
        INSERT INTO post_versions(
            post_id, content_text, content_hash, image_manifest_hash, first_observed_at,
            ingest_mode, raw_payload
        ) VALUES (?, '编辑正文', 'hash-edited', 'images-b', ?, 'live', '{}')
        """,
        (post_id, END),
    )
    image_version = _lastrowid(cursor)
    connection.execute(
        """
        INSERT INTO post_images(
            version_id, source_url, normalized_url, ordinal, sha256, mime_type,
            byte_size, image_bytes, downloaded_at, download_status
        ) VALUES (?, 'https://example.test/a.webp', 'https://example.test/a.webp',
            0, 'sha-webp', 'image/webp', 3, ?, ?, 'ok')
        """,
        (image_version, b"img", END),
    )
    connection.execute(
        """
        INSERT INTO post_images(
            version_id, source_url, normalized_url, ordinal, sha256, mime_type,
            byte_size, image_bytes, downloaded_at, download_status
        ) VALUES (?, 'https://example.test/b.gif', 'https://example.test/b.gif',
            1, 'sha-gif', 'image/gif', 3, ?, ?, 'ok')
        """,
        (image_version, b"gif", END),
    )
    _event(
        connection,
        post_id,
        "content",
        str(first_version),
        to_version_id=first_version,
    )
    _event(
        connection,
        post_id,
        "content",
        str(edited_version),
        from_version_id=first_version,
        to_version_id=edited_version,
    )
    _event(
        connection,
        post_id,
        "content",
        str(image_version),
        from_version_id=edited_version,
        to_version_id=image_version,
    )

    result = _generate(connection, tmp_path / "digests")
    markdown = result.markdown_path.read_text(encoding="utf-8")

    assert result.edit_count == 1
    assert result.image_change_count == 1
    assert "编辑事件" in markdown
    assert "仅图片变更" in markdown
    assert "```diff" in markdown
    assert "assets/event-" in markdown
    assert len(list((result.markdown_path.parent / "assets").glob("*.webp"))) == 1
    assert len(list((result.markdown_path.parent / "assets").glob("*.gif"))) == 1


def test_digest_marks_cross_account_deletion_wave_with_neutral_copy(tmp_path: Path) -> None:
    connection = _archive(tmp_path)
    for uid in ("one", "two", "three"):
        post_id, _ = _post(connection, _author(connection, uid), f"post-{uid}", "存档正文")
        _event(connection, post_id, "source_state", "gone_confirmed")

    result = _generate(connection, tmp_path / "digests", wave_min_accounts=3)
    markdown = result.markdown_path.read_text(encoding="utf-8")

    assert result.deletion_count == 3
    assert result.deletion_wave is True
    assert "平台级删帖密集期" in markdown
    assert "删除事件" in markdown
    for forbidden in ("改口", "心虚", "作者删除"):
        assert forbidden not in markdown


def test_digest_escapes_markdown_and_uses_longer_diff_fence(tmp_path: Path) -> None:
    connection = _archive(tmp_path)
    author_id = _author(connection, "markdown")
    connection.execute("UPDATE authors SET notes = '# 作者 [链接](坏)' WHERE id = ?", (author_id,))
    post_id, first_version = _post(connection, author_id, "post-[one]", "原始\n```\n上下文")
    cursor = connection.execute(
        """
        INSERT INTO post_versions(
            post_id, content_text, content_hash, image_manifest_hash, first_observed_at,
            ingest_mode, raw_payload
        ) VALUES (?, '# 摘要 [链接](坏)\\n```\\n上下文', 'hash-markdown', 'images-a',
            ?, 'live', '{}')
        """,
        (post_id, END),
    )
    edited_version = _lastrowid(cursor)
    _event(
        connection,
        post_id,
        "content",
        str(edited_version),
        from_version_id=first_version,
        to_version_id=edited_version,
    )

    result = _generate(connection, tmp_path / "digests")
    markdown = result.markdown_path.read_text(encoding="utf-8")

    assert "## 编辑事件 · \\# 作者 \\[链接\\]\\(坏\\)" in markdown
    assert "\\# 摘要 \\[链接\\]\\(坏\\)" in markdown
    assert "帖子：post\\-\\[one\\]" in markdown
    assert "````diff" in markdown
    assert "\n````\n" in markdown


def test_digest_configuration_preserves_explicit_falsy_values() -> None:
    output_dir, wave_min_accounts = _digest_settings(
        {"digest": {"output_dir": "", "wave_min_accounts": 0}}, None
    )

    assert output_dir == Path(".")
    assert wave_min_accounts == 0


def test_digest_event_collection_query_count_stays_bounded_with_market_versions(
    tmp_path: Path,
) -> None:
    connection = _archive(tmp_path)
    for index, uid in enumerate(("one", "two", "three"), start=1):
        post_id, first_version = _post(connection, _author(connection, uid), f"post-{uid}", "原始")
        ticker = f"SH60000{index}"
        cursor = connection.execute(
            """
            INSERT INTO post_versions(
                post_id, content_text, content_hash, image_manifest_hash, first_observed_at,
                ingest_mode, raw_payload
            ) VALUES (?, ?, ?, 'images-a', '2026-06-06T00:00:00+00:00', 'live', '{}')
            """,
            (post_id, f"$测试{index}({ticker})$ 观点", f"hash-{index}"),
        )
        version_id = _lastrowid(cursor)
        _enrich_viewpoint(connection, post_id, version_id)
        _event(
            connection,
            post_id,
            "content",
            str(version_id),
            from_version_id=first_version,
            to_version_id=version_id,
        )
        for symbol, before, after in (
            (ticker, 10.0, 11.0),
            (BENCHMARK_TICKER, 100.0, 105.0),
        ):
            connection.execute(
                "INSERT OR IGNORE INTO prices(ticker, date, close) VALUES (?, ?, ?)",
                (symbol, "2026-06-05", before),
            )
            connection.execute(
                "INSERT OR IGNORE INTO prices(ticker, date, close) VALUES (?, ?, ?)",
                (symbol, "2026-06-08", after),
            )
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    events = collect_digest_events(
        connection,
        START,
        END,
        tmp_path / "assets",
        prompt_version=PROMPT_VERSION,
        benchmark_ticker=BENCHMARK_TICKER,
    )

    connection.set_trace_callback(None)
    reads = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
    assert len(events) == 3
    assert all(event.market_snapshot is not None for event in events)
    assert len(reads) == 3


def test_digest_includes_existing_descriptive_market_snapshot(tmp_path: Path) -> None:
    connection = _archive(tmp_path)
    author_id = _author(connection, "market")
    post_id, first_version = _post(connection, author_id, "post-market", "原始正文")
    cursor = connection.execute(
        """
        INSERT INTO post_versions(
            post_id, content_text, content_hash, image_manifest_hash, first_observed_at,
            ingest_mode, raw_payload
        ) VALUES (?, '$测试股份(SH600000)$ 后续观点', 'hash-market', 'images-a',
            '2026-06-06T00:00:00+00:00', 'live', '{}')
        """,
        (post_id,),
    )
    market_version = _lastrowid(cursor)
    _enrich_viewpoint(connection, post_id, market_version)
    _event(
        connection,
        post_id,
        "content",
        str(market_version),
        from_version_id=first_version,
        to_version_id=market_version,
    )
    for ticker, before, after in (
        ("SH600000", 10.0, 11.0),
        ("SH000300", 100.0, 105.0),
    ):
        _price(connection, ticker, "2026-06-05", before)
        _price(connection, ticker, "2026-06-08", after)

    result = _generate(connection, tmp_path / "digests")
    markdown = result.markdown_path.read_text(encoding="utf-8")
    rendered_html = result.html_path.read_text(encoding="utf-8")

    assert result.events[0].market_snapshot is not None
    assert "series" not in result.events[0].market_snapshot
    assert "描述性市场变化：测试股份（SH600000） \\+10\\.00%" in markdown
    assert "SH000300 \\+5\\.00%" in markdown
    assert "超额 \\+5\\.00%" in markdown
    assert "描述性市场变化：测试股份（SH600000） +10.00%" in rendered_html


def test_digest_omits_market_snapshot_without_prices(tmp_path: Path) -> None:
    connection = _archive(tmp_path)
    author_id = _author(connection, "market")
    post_id, first_version = _post(connection, author_id, "post-market", "原始正文")
    cursor = connection.execute(
        """
        INSERT INTO post_versions(
            post_id, content_text, content_hash, image_manifest_hash, first_observed_at,
            ingest_mode, raw_payload
        ) VALUES (?, '$测试股份(SH600000)$ 后续观点', 'hash-market', 'images-a',
            '2026-06-06T00:00:00+00:00', 'live', '{}')
        """,
        (post_id,),
    )
    market_version = _lastrowid(cursor)
    _enrich_viewpoint(connection, post_id, market_version)
    _event(
        connection,
        post_id,
        "content",
        str(market_version),
        from_version_id=first_version,
        to_version_id=market_version,
    )

    result = _generate(connection, tmp_path / "digests")

    assert result.events[0].market_snapshot is None
    assert "描述性市场变化" not in result.markdown_path.read_text(encoding="utf-8")
