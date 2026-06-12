"""Image evidence layer: downloads, OCR, and vision-model descriptions."""

from __future__ import annotations

import json

from kol_archive.models import (
    ImageDownloadResult,
    ImageDownloadTarget,
    StoredImage,
)
from probe.normalize_text import extract_image_urls, normalize_image_url

from .base import ArchiveBase, _required_lastrowid


class ImagesMixin(ArchiveBase):
    def image_download_targets(
        self, *, post_id: int | None = None, limit: int | None = None
    ) -> list[ImageDownloadTarget]:
        """Images in stored versions' manifests with no successful download yet.

        The manifest is re-parsed from each version's stored ``raw_payload`` (the
        URLs are not separately persisted — ``post_images`` is the download log,
        not the manifest). A ``normalized_url`` already recorded ``ok`` for the
        version is skipped, which makes the pass idempotent and resumable; a failed
        attempt does not satisfy this, so failures are retried on the next run.
        Ordered oldest version first for steady forward progress.
        """
        query = """
            SELECT v.id AS version_id, v.raw_payload
            FROM post_versions v
            ORDER BY v.first_observed_at, v.id
        """
        params: list[object] = []
        if post_id is not None:
            query = """
                SELECT v.id AS version_id, v.raw_payload
                FROM post_versions v
                WHERE v.post_id = ?
                ORDER BY v.first_observed_at, v.id
            """
            params = [post_id]
        targets: list[ImageDownloadTarget] = []
        for row in self.connection.execute(query, params).fetchall():
            raw_payload = row["raw_payload"]
            if raw_payload is None:
                continue
            text = json.loads(str(raw_payload)).get("text")
            if not isinstance(text, str):
                continue
            version_id = int(row["version_id"])
            downloaded = {
                str(done["normalized_url"])
                for done in self.connection.execute(
                    "SELECT normalized_url FROM post_images "
                    "WHERE version_id = ? AND download_status = 'ok'",
                    (version_id,),
                )
            }
            for ordinal, source_url in enumerate(extract_image_urls(text)):
                normalized_url = normalize_image_url(source_url)
                if normalized_url in downloaded:
                    continue
                targets.append(
                    ImageDownloadTarget(
                        version_id=version_id,
                        source_url=source_url,
                        normalized_url=normalized_url,
                        ordinal=ordinal,
                    )
                )
                if limit is not None and len(targets) >= limit:
                    return targets
        return targets

    def record_image_download(
        self, target: ImageDownloadTarget, result: ImageDownloadResult, downloaded_at: str
    ) -> int:
        """Append one image-fetch outcome (success or failure) to ``post_images``.

        Append-only by design: a re-download of the same ``normalized_url`` adds a
        new row rather than mutating the old one, so a byte swap behind an unchanged
        URL stays visible. When the new bytes differ from the latest prior ``ok``
        download of the same image, the new row is flagged ``bytes_changed`` so the
        substitution is discoverable without scanning blobs.
        """
        if result.download_status not in ("ok", "failed"):
            raise ValueError("download_status must be 'ok' or 'failed'")
        notes = result.notes
        if result.download_status == "ok":
            if result.sha256 is None or result.image_bytes is None:
                raise ValueError("a successful download requires sha256 and image_bytes")
            prior = self.connection.execute(
                """
                SELECT sha256 FROM post_images
                WHERE version_id = ? AND normalized_url = ? AND download_status = 'ok'
                ORDER BY id DESC LIMIT 1
                """,
                (target.version_id, target.normalized_url),
            ).fetchone()
            if prior is not None and str(prior["sha256"]) != result.sha256:
                notes = "bytes_changed" if not notes else f"{notes};bytes_changed"
        with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO post_images(
                    version_id, source_url, normalized_url, ordinal, sha256, mime_type,
                    byte_size, image_bytes, downloaded_at, download_status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target.version_id,
                    target.source_url,
                    target.normalized_url,
                    target.ordinal,
                    result.sha256,
                    result.mime_type,
                    result.byte_size,
                    result.image_bytes,
                    downloaded_at,
                    result.download_status,
                    notes,
                ),
            )
        return _required_lastrowid(cursor)

    def _stored_images_missing(
        self,
        derivation_table: str,
        join_predicate: str,
        params_tail: list[object],
        *,
        post_id: int | None,
        limit: int | None,
    ) -> list[StoredImage]:
        query = f"""
            SELECT i.id AS image_id, i.version_id, v.post_id, i.sha256, i.mime_type, i.image_bytes
            FROM post_images i
            JOIN post_versions v ON v.id = i.version_id
            LEFT JOIN {derivation_table} d ON {join_predicate}
            WHERE i.download_status = 'ok' AND d.id IS NULL
        """
        params: list[object] = list(params_tail)
        if post_id is not None:
            query += " AND v.post_id = ?"
            params.append(post_id)
        query += " ORDER BY i.id"
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            query += " LIMIT ?"
            params.append(limit)
        return [
            StoredImage(
                image_id=int(row["image_id"]),
                version_id=int(row["version_id"]),
                post_id=int(row["post_id"]),
                sha256=str(row["sha256"]),
                mime_type=None if row["mime_type"] is None else str(row["mime_type"]),
                image_bytes=bytes(row["image_bytes"]),
            )
            for row in self.connection.execute(query, params).fetchall()
        ]

    def ocr_targets(
        self,
        engine: str,
        engine_version: str,
        *,
        post_id: int | None = None,
        limit: int | None = None,
    ) -> list[StoredImage]:
        """Downloaded images with no OCR yet for this engine+version (idempotent)."""
        if not engine.strip() or not engine_version.strip():
            raise ValueError("engine and engine_version must not be empty")
        return self._stored_images_missing(
            "image_ocr",
            "d.image_id = i.id AND d.engine = ? AND d.engine_version = ?",
            [engine.strip(), engine_version.strip()],
            post_id=post_id,
            limit=limit,
        )

    def add_image_ocr(
        self,
        image: StoredImage,
        engine: str,
        engine_version: str,
        ocr_text: str,
        created_at: str,
    ) -> int | None:
        """Persist OCR text for one image; ``None`` if already present (rerun)."""
        if not engine.strip() or not engine_version.strip():
            raise ValueError("engine and engine_version must not be empty")
        with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO image_ocr(
                    image_id, image_sha256, engine, engine_version, ocr_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    image.image_id,
                    image.sha256,
                    engine.strip(),
                    engine_version.strip(),
                    ocr_text,
                    created_at,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return _required_lastrowid(cursor)

    def image_enrichment_targets(
        self,
        model: str,
        prompt_version: str,
        *,
        post_id: int | None = None,
        limit: int | None = None,
    ) -> list[StoredImage]:
        """Downloaded images with no vision verdict yet for model+prompt_version."""
        if not model.strip() or not prompt_version.strip():
            raise ValueError("model and prompt_version must not be empty")
        return self._stored_images_missing(
            "image_enrichments",
            "d.image_id = i.id AND d.model = ? AND d.prompt_version = ?",
            [model.strip(), prompt_version.strip()],
            post_id=post_id,
            limit=limit,
        )

    def add_image_enrichment(
        self,
        image: StoredImage,
        model: str,
        prompt_version: str,
        prompt: str,
        description: str,
        created_at: str,
    ) -> int | None:
        """Persist one vision-model description (inference, not evidence).

        ``None`` if a verdict already exists for ``UNIQUE(image_id, model,
        prompt_version)`` so reruns are idempotent.
        """
        if not model.strip() or not prompt_version.strip():
            raise ValueError("model and prompt_version must not be empty")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if not description.strip():
            raise ValueError("description must not be empty")
        with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO image_enrichments(
                    image_id, image_sha256, model, prompt_version, prompt, description, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image.image_id,
                    image.sha256,
                    model.strip(),
                    prompt_version.strip(),
                    prompt.strip(),
                    description.strip(),
                    created_at,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return _required_lastrowid(cursor)
