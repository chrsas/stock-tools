from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kol_archive.cli.common import backup_retention_count
from kol_archive.database import connect_database, initialize_database
from kol_archive.maintenance import (
    create_verified_backup,
    export_archive,
    restore_backup,
    verify_backup,
)
from kol_archive.models import (
    ContentFidelity,
    FeedRun,
    IngestMode,
    LoginState,
    NormalizedPost,
    RunStatus,
)
from kol_archive.service import Archive

NOW = "2026-06-01T00:00:00+00:00"


def seed_archive(path: Path) -> None:
    connection = connect_database(path)
    initialize_database(connection)
    archive = Archive(connection)
    author_id = archive.add_author(
        "xueqiu",
        "100",
        NOW,
        "cookie=xq_a_token=cookie-secret; xqat=xqat-secret",
    )
    archive.record_feed_run(
        FeedRun(
            author_id=author_id,
            platform="xueqiu",
            started_at=NOW,
            finished_at=NOW,
            status=RunStatus.OK,
            login_state=LoginState.VALID,
            pages_fetched=1,
            pagination_complete=True,
            covered_from="2026-05-01T00:00:00+00:00",
            covered_to="2026-06-02T00:00:00+00:00",
            rate_limited=False,
            http_error_count=0,
            ingest_mode=IngestMode.LIVE,
            adapter_version="xueqiu-1",
        ),
        [
            NormalizedPost(
                platform_post_id="post-1",
                author_id=author_id,
                observed_at=NOW,
                content_fidelity=ContentFidelity.FULL,
                content_text="token=content-secret",
                content_hash="hash-a",
                raw_payload={
                    "api_key": "api-secret",
                    "headers": {"Authorization": "Bearer bearer-secret"},
                },
                raw_meta={"session_token": "meta-secret"},
            )
        ],
    )
    decision_id = archive.add_decision(
        "SH688303",
        "neutral",
        "thesis token=decision-secret",
        "cookie=invalidation-secret",
        NOW,
        position_note="api_key=position-secret",
    )
    archive.review_decision(
        decision_id,
        NOW,
        "Bearer review-secret",
        "password=lesson-secret",
    )
    connection.execute(
        """
        INSERT INTO watchlist(ticker, name, added_at, note)
        VALUES ('SH688303', '大全能源', ?, 'token=watchlist-secret')
        """,
        (NOW,),
    )
    connection.execute(
        """
        INSERT INTO topic_briefs(
            question, groups, tickers, date_from, date_to, require_all_groups,
            coverage, selection, cited_version_ids, brief_text, model,
            prompt_version, created_at
        ) VALUES (
            'question token=brief-question-secret',
            '[{"label":"event","terms":["token=brief-keyword-secret"]}]', '[]',
            '2025-06-10T00:00:00+00:00', '2025-06-30T15:59:59+00:00', 1,
            '{}', '{}', '[1]', 'brief body cookie=brief-text-secret', 'm',
            'brief-v1', ?
        )
        """,
        (NOW,),
    )
    connection.close()


def test_verified_snapshots_restore_and_prune_oldest_copy(tmp_path: Path) -> None:
    source_path = tmp_path / "archive.sqlite3"
    backup_dir = tmp_path / "backups"
    seed_archive(source_path)

    first = create_verified_backup(
        source_path,
        backup_dir,
        retention_count=2,
        now=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    second = create_verified_backup(
        source_path,
        backup_dir,
        retention_count=2,
        now=datetime(2026, 6, 1, 1, 0, tzinfo=UTC),
    )
    third = create_verified_backup(
        source_path,
        backup_dir,
        retention_count=2,
        now=datetime(2026, 6, 1, 2, 0, tzinfo=UTC),
    )

    assert not first.snapshot_path.exists()
    assert second.snapshot_path.exists()
    assert third.snapshot_path.exists()
    assert third.removed_snapshots == (first.snapshot_path,)
    verify_backup(third.snapshot_path)

    restored_path = tmp_path / "restored" / "archive.sqlite3"
    restore_backup(third.snapshot_path, restored_path)
    restored = sqlite3.connect(restored_path)
    try:
        assert restored.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()

    with pytest.raises(FileExistsError, match="already exists"):
        restore_backup(third.snapshot_path, restored_path)


def test_snapshot_collision_suffix_keeps_newer_copies(tmp_path: Path) -> None:
    source_path = tmp_path / "archive.sqlite3"
    backup_dir = tmp_path / "backups"
    now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    seed_archive(source_path)

    first = create_verified_backup(source_path, backup_dir, retention_count=2, now=now)
    second = create_verified_backup(source_path, backup_dir, retention_count=2, now=now)
    third = create_verified_backup(source_path, backup_dir, retention_count=2, now=now)

    assert not first.snapshot_path.exists()
    assert second.snapshot_path.exists()
    assert third.snapshot_path.exists()
    assert third.removed_snapshots == (first.snapshot_path,)


def test_zero_backup_retention_is_rejected_without_creating_snapshot(tmp_path: Path) -> None:
    source_path = tmp_path / "archive.sqlite3"
    backup_dir = tmp_path / "backups"
    seed_archive(source_path)

    with pytest.raises(ValueError, match="must be positive"):
        create_verified_backup(source_path, backup_dir, retention_count=0)

    assert not backup_dir.exists()
    assert backup_retention_count({"backup_retention_count": 0}) == 0


def test_export_writes_json_and_csv_with_credential_redaction(tmp_path: Path) -> None:
    source_path = tmp_path / "archive.sqlite3"
    seed_archive(source_path)

    result = export_archive(
        source_path,
        tmp_path / "exports",
        now=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )

    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    exported_text = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in result.bundle_dir.rglob("*")
        if path.is_file()
    )

    assert payload["credential_redaction_attempted"] is True
    assert payload["credential_redaction_mode"] == "heuristic_notes_and_raw_json"
    assert payload["relations"]["authors"][0]["notes"] == "cookie=[REDACTED]"
    assert payload["relations"]["post_versions"][0]["content_text"] == "token=content-secret"
    assert payload["relations"]["post_versions"][0]["raw_payload"] == {
        "api_key": "[REDACTED]",
        "headers": {"Authorization": "[REDACTED]"},
    }
    assert payload["relations"]["posts"][0]["raw_meta"] == {"session_token": "[REDACTED]"}
    assert payload["relations"]["watchlist"][0]["note"] == "token=[REDACTED]"
    brief = payload["relations"]["topic_briefs"][0]
    assert brief["question"] == "question token=[REDACTED]"
    assert brief["brief_text"] == "brief body cookie=[REDACTED]"
    assert brief["groups"] == [{"label": "event", "terms": ["token=[REDACTED]"]}]
    assert "content-secret" in exported_text
    for secret in (
        "cookie-secret",
        "xqat-secret",
        "api-secret",
        "bearer-secret",
        "meta-secret",
        "decision-secret",
        "invalidation-secret",
        "position-secret",
        "review-secret",
        "lesson-secret",
        "watchlist-secret",
        "brief-question-secret",
        "brief-keyword-secret",
        "brief-text-secret",
    ):
        assert secret not in exported_text
    assert (result.csv_dir / "posts.csv").is_file()
    assert (result.csv_dir / "version_sightings.csv").is_file()
