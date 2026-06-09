"""Image evidence layer: manifest version-detection, download, OCR, vision."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from kol_archive.adapters.xueqiu import ADAPTER_VERSION, normalize_status
from kol_archive.database import connect_database, initialize_database
from kol_archive.image_enrich import VisionSettings, _data_uri, run_image_enrichment
from kol_archive.images import ImageDownloader, ImageDownloadSettings
from kol_archive.maintenance import export_archive
from kol_archive.models import (
    ArchiveSettings,
    FeedRun,
    ImageDownloadResult,
    IngestMode,
    LoginState,
    NormalizedPost,
    RunStatus,
    StoredImage,
)
from kol_archive.ocr import run_ocr
from kol_archive.service import Archive
from probe.normalize_text import (
    extract_image_urls,
    image_manifest_hash,
    normalize_image_url,
)

BASE_TIME = "2026-06-01T00:00:00+00:00"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"the-image-bytes"


@pytest.fixture
def archive() -> Iterator[Archive]:
    connection = connect_database(":memory:")
    initialize_database(connection)
    service = Archive(connection, ArchiveSettings(absent_threshold_n=3))
    service.add_author("xueqiu", "100", BASE_TIME)
    try:
        yield service
    finally:
        connection.close()


def _feed_run(finished_at: str = BASE_TIME) -> FeedRun:
    return FeedRun(
        author_id=1,
        platform="xueqiu",
        started_at=finished_at,
        finished_at=finished_at,
        status=RunStatus.OK,
        login_state=LoginState.VALID,
        pages_fetched=1,
        pagination_complete=True,
        covered_from="2026-05-01T00:00:00+00:00",
        covered_to="2026-06-02T00:00:00+00:00",
        rate_limited=False,
        http_error_count=0,
        ingest_mode=IngestMode.LIVE,
        adapter_version=ADAPTER_VERSION,
    )


def _status(text: str, *, observed_at: str = BASE_TIME, post_id: int = 1) -> NormalizedPost:
    payload = {"id": post_id, "user_id": 100, "created_at": 1_700_000_000_000, "text": text}
    post, failed = normalize_status(
        payload, author_id=1, observed_at=observed_at, ingest_mode=IngestMode.LIVE
    )
    assert not failed and post is not None
    return post


# ── L0: parsing + manifest ────────────────────────────────────────────────


def test_extract_image_urls_in_document_order() -> None:
    html = '<p>hi</p><img src="https://i.x/a.jpg?s=1"><img data-src="https://i.x/b.png">'
    assert extract_image_urls(html) == ["https://i.x/a.jpg?s=1", "https://i.x/b.png"]


def test_normalize_drops_signature_query() -> None:
    assert normalize_image_url("https://i.x/a.jpg?KID=k&Sign=z") == "https://i.x/a.jpg"


def test_manifest_hash_is_order_sensitive_and_empty_is_a_real_constant() -> None:
    assert image_manifest_hash(["a", "b"]) != image_manifest_hash(["b", "a"])
    # Empty list hashes to a stable, non-null constant so losing the last image
    # is a detectable change, not an un-comparable NULL.
    assert image_manifest_hash([]) == image_manifest_hash([])
    assert image_manifest_hash([]) != image_manifest_hash(["a"])


def test_adapter_sets_manifest_and_images() -> None:
    post = _status('<p>hi</p><img src="https://i.x/a.jpg?s=1">')
    assert [img.normalized_url for img in post.images] == ["https://i.x/a.jpg"]
    assert post.image_manifest_hash == image_manifest_hash(["https://i.x/a.jpg"])


# ── version determination ──────────────────────────────────────────────────


def _version_count(archive: Archive) -> int:
    return int(archive.connection.execute("SELECT COUNT(*) FROM post_versions").fetchone()[0])


def test_image_only_swap_forks_a_new_version(archive: Archive) -> None:
    archive.record_feed_run(_feed_run(), [_status('A<img src="https://i.x/a.jpg">')])
    # Same text, different image → content_hash identical, manifest differs.
    archive.record_feed_run(
        _feed_run(finished_at="2026-06-01T01:00:00+00:00"),
        [_status('A<img src="https://i.x/b.jpg">', observed_at="2026-06-01T01:00:00+00:00")],
    )
    assert _version_count(archive) == 2


def test_identical_image_does_not_fork(archive: Archive) -> None:
    archive.record_feed_run(_feed_run(), [_status('A<img src="https://i.x/a.jpg">')])
    archive.record_feed_run(
        _feed_run(finished_at="2026-06-01T01:00:00+00:00"),
        [_status('A<img src="https://i.x/a.jpg">', observed_at="2026-06-01T01:00:00+00:00")],
    )
    assert _version_count(archive) == 1


def test_legacy_null_manifest_does_not_spuriously_fork(archive: Archive) -> None:
    # Simulate a pre-feature row: a version + post with NULL manifest (INSERT is
    # allowed on the append-only table; only UPDATE/DELETE are blocked).
    conn = archive.connection
    # posts.current_version_id and post_versions.post_id reference each other, so
    # insert the post with a NULL current pointer, add the version, then point to it.
    conn.execute(
        "INSERT INTO posts(id, author_id, platform, platform_post_id, first_seen_at, "
        "current_version_id, current_content_hash, current_image_manifest_hash, "
        "absent_healthy_streak, feed_state, source_state, watch_mode, ingest_mode) "
        "VALUES (1, 1, 'xueqiu', '1', ?, NULL, ?, NULL, 0, 'present', 'reachable', "
        "'recent_window', 'live')",
        (BASE_TIME, "legacyhash"),
    )
    conn.execute(
        "INSERT INTO post_versions(id, post_id, content_text, content_hash, "
        "image_manifest_hash, first_observed_at, ingest_mode, raw_payload) "
        "VALUES (1, 1, 'A', 'legacyhash', NULL, ?, 'live', ?)",
        (BASE_TIME, json.dumps({"text": "A"})),
    )
    conn.execute("UPDATE posts SET current_version_id = 1 WHERE id = 1")
    # A poll now sees the same text but with an image. NULL prior manifest must
    # not be treated as a difference, so no new version is forked...
    post = _status("A", observed_at="2026-06-01T01:00:00+00:00")
    object.__setattr__(post, "content_hash", "legacyhash")  # align text identity
    archive.record_feed_run(_feed_run(finished_at="2026-06-01T01:00:00+00:00"), [post])
    assert _version_count(archive) == 1
    # ...but the projection now carries the manifest forward so future comparison works.
    row = conn.execute("SELECT current_image_manifest_hash FROM posts WHERE id = 1").fetchone()
    assert row["current_image_manifest_hash"] == post.image_manifest_hash


# ── L1: download log ────────────────────────────────────────────────────────


def _seed_version_with_images(archive: Archive, html: str) -> None:
    archive.record_feed_run(_feed_run(), [_status(html)])


def test_download_targets_derive_from_payload_and_skip_completed(archive: Archive) -> None:
    _seed_version_with_images(
        archive, '<img src="https://i.x/a.jpg?s=1"><img src="https://i.x/b.jpg">'
    )
    targets = archive.image_download_targets()
    assert {t.normalized_url for t in targets} == {"https://i.x/a.jpg", "https://i.x/b.jpg"}
    # Record one as downloaded; it drops out of the pending set.
    archive.record_image_download(
        targets[0],
        ImageDownloadResult(
            download_status="ok",
            sha256="h",
            mime_type="image/jpeg",
            byte_size=3,
            image_bytes=b"abc",
        ),
        BASE_TIME,
    )
    remaining = archive.image_download_targets()
    assert {t.normalized_url for t in remaining} == {"https://i.x/b.jpg"}


def test_failed_download_is_retried(archive: Archive) -> None:
    _seed_version_with_images(archive, '<img src="https://i.x/a.jpg">')
    target = archive.image_download_targets()[0]
    archive.record_image_download(
        target, ImageDownloadResult(download_status="failed", notes="http_404"), BASE_TIME
    )
    # A failed attempt does not satisfy the target — it stays pending.
    assert len(archive.image_download_targets()) == 1


def test_bytes_changed_note_on_resubstitution(archive: Archive) -> None:
    _seed_version_with_images(archive, '<img src="https://i.x/a.jpg">')
    target = archive.image_download_targets()[0]
    archive.record_image_download(
        target,
        ImageDownloadResult(download_status="ok", sha256="h1", byte_size=3, image_bytes=b"aaa"),
        BASE_TIME,
    )
    row_id = archive.record_image_download(
        target,
        ImageDownloadResult(download_status="ok", sha256="h2", byte_size=3, image_bytes=b"bbb"),
        "2026-06-02T00:00:00+00:00",
    )
    note = archive.connection.execute(
        "SELECT notes FROM post_images WHERE id = ?", (row_id,)
    ).fetchone()["notes"]
    assert note is not None and "bytes_changed" in note


def test_downloader_respects_size_and_batch_caps(archive: Archive) -> None:
    _seed_version_with_images(
        archive, '<img src="https://i.x/big.jpg"><img src="https://i.x/ok.jpg">'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = b"x" * (20 if "big" in str(request.url) else 4)
        return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = ImageDownloader(
        archive,
        client,
        ImageDownloadSettings(
            request_min_interval_seconds=0, request_jitter_seconds=0, max_image_bytes=10
        ),
        sleep=lambda _s: None,
    )
    downloader.download_pending()
    rows = archive.connection.execute(
        "SELECT normalized_url, download_status, notes FROM post_images ORDER BY id"
    ).fetchall()
    statuses = {r["normalized_url"]: (r["download_status"], r["notes"]) for r in rows}
    assert statuses["https://i.x/big.jpg"] == ("failed", "too_large")
    assert statuses["https://i.x/ok.jpg"][0] == "ok"


def test_downloader_rejects_large_content_length_before_reading_body(archive: Archive) -> None:
    _seed_version_with_images(archive, '<img src="https://i.x/huge.jpg">')

    class UnreadableStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("oversized declared body must not be read")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg", "content-length": "100"},
            stream=UnreadableStream(),
        )

    downloader = ImageDownloader(
        archive,
        httpx.Client(transport=httpx.MockTransport(handler)),
        ImageDownloadSettings(
            request_min_interval_seconds=0, request_jitter_seconds=0, max_image_bytes=10
        ),
        sleep=lambda _s: None,
    )

    downloader.download_pending()

    row = archive.connection.execute(
        "SELECT download_status, byte_size, notes FROM post_images"
    ).fetchone()
    assert tuple(row) == ("failed", 100, "too_large")


# ── L2: OCR ─────────────────────────────────────────────────────────────────


class _FakeEngine:
    name = "fake"
    version = "1"

    def __init__(self, text: str = "图中文字") -> None:
        self._text = text

    def recognize(self, image_bytes: bytes) -> str:
        return self._text


def _seed_downloaded_image(archive: Archive, *, sha256: str = "h1") -> None:
    _seed_version_with_images(archive, '<img src="https://i.x/a.jpg">')
    target = archive.image_download_targets()[0]
    archive.record_image_download(
        target,
        ImageDownloadResult(
            download_status="ok",
            sha256=sha256,
            mime_type="image/png",
            byte_size=len(PNG_BYTES),
            image_bytes=PNG_BYTES,
        ),
        BASE_TIME,
    )


def test_ocr_runs_then_is_idempotent(archive: Archive) -> None:
    _seed_downloaded_image(archive)
    engine = _FakeEngine()
    assert len(run_ocr(archive, engine)) == 1
    assert run_ocr(archive, engine) == []  # already transcribed for this engine+version
    row = archive.connection.execute(
        "SELECT ocr_text, engine, image_sha256 FROM image_ocr"
    ).fetchone()
    assert row["ocr_text"] == "图中文字"
    assert row["engine"] == "fake"
    assert row["image_sha256"] == "h1"


# ── L3: vision enrichment ────────────────────────────────────────────────────


def test_data_uri_sniffs_png_magic() -> None:
    uri = _data_uri(PNG_BYTES, None)
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == PNG_BYTES


def test_vision_enrichment_runs_then_idempotent(archive: Archive) -> None:
    _seed_downloaded_image(archive)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "一张K线截图"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = VisionSettings(
        base_url="https://llm.test/v1", model="vlm", api_key="k", prompt_version="vision-v1"
    )
    added = run_image_enrichment(archive, settings, client=client)
    assert len(added) == 1
    # The bytes sent are the stored BLOB as a data URI, never the source URL.
    content = captured[0]["messages"][1]["content"]
    image_part = next(part for part in content if part["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    # Idempotent rerun.
    assert run_image_enrichment(archive, settings, client=client) == []
    row = archive.connection.execute(
        "SELECT description, model, image_sha256 FROM image_enrichments"
    ).fetchone()
    assert row["description"] == "一张K线截图"
    assert row["image_sha256"] == "h1"


# ── export ───────────────────────────────────────────────────────────────────


def test_export_blobs_base64_and_redacts_url_and_description(
    archive: Archive, tmp_path: Path
) -> None:
    _seed_version_with_images(archive, '<img src="https://i.x/a.jpg?Sign=secret123">')
    target = archive.image_download_targets()[0]
    image_id = archive.record_image_download(
        target,
        ImageDownloadResult(
            download_status="ok",
            sha256="h1",
            mime_type="image/png",
            byte_size=len(PNG_BYTES),
            image_bytes=PNG_BYTES,
        ),
        BASE_TIME,
    )
    image = StoredImage(
        image_id=image_id,
        version_id=1,
        post_id=1,
        sha256="h1",
        mime_type="image/png",
        image_bytes=PNG_BYTES,
    )
    archive.add_image_ocr(image, "fake", "1", "图中文字", BASE_TIME)
    archive.add_image_enrichment(
        image, "vlm", "vision-v1", "描述这张图", "Bearer abc.def.ghi 一张图", BASE_TIME
    )

    result = export_archive(archive_db_path(archive, tmp_path), tmp_path / "out")
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    relations = payload["relations"]

    img_row = relations["post_images"][0]
    assert img_row["source_url"] == "https://i.x/a.jpg"  # signature query stripped
    assert base64.b64decode(img_row["image_bytes"]) == PNG_BYTES  # blob round-trips
    assert relations["image_ocr"][0]["ocr_text"] == "图中文字"  # evidence kept intact
    # VLM description scrubbed like notes (credential heuristics applied).
    assert "Bearer [REDACTED]" in relations["image_enrichments"][0]["description"]


def archive_db_path(archive: Archive, tmp_path: Path) -> Path:
    """Persist the in-memory archive to a file so export (which reopens it) can read it."""
    path = tmp_path / "kol.sqlite3"
    disk = connect_database(path)
    archive.connection.backup(disk)
    disk.close()
    return path
