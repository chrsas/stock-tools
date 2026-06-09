"""Image-byte fixation: download a version's images before the links rot.

A separate, idempotent batch pass (like probing and enrichment), not part of the
atomic ingest write — fetching bytes is slow and fails independently of parsing.
Each attempt appends one ``post_images`` row; the manifest membership it works
from is re-derived from the version's stored payload, so this never mutates
evidence. Bytes are kept so OCR and the vision model later read a frozen copy
rather than the live (rotatable, expiring) source URL.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from kol_archive.models import ImageDownloadResult, ImageDownloadTarget
from kol_archive.service import Archive

LOGGER = logging.getLogger(__name__)

# Trust the response Content-Type only when it actually claims an image; some
# CDNs answer an expired/blocked link with an HTML error body at HTTP 200.
_IMAGE_MIME_PREFIX = "image/"


@dataclass(frozen=True)
class ImageDownloadSettings:
    request_min_interval_seconds: float = 1.0
    request_jitter_seconds: float = 1.0
    max_image_bytes: int = 8 * 1024 * 1024
    max_batch_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.request_min_interval_seconds < 0 or self.request_jitter_seconds < 0:
            raise ValueError("download interval/jitter must not be negative")
        if self.max_image_bytes < 1:
            raise ValueError("max_image_bytes must be positive")
        if self.max_batch_bytes < 1:
            raise ValueError("max_batch_bytes must be positive")


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


class ImageDownloader:
    def __init__(
        self,
        archive: Archive,
        client: httpx.Client,
        settings: ImageDownloadSettings | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.archive = archive
        self.client = client
        self.settings = settings or ImageDownloadSettings()
        self.sleep = sleep
        self.clock = clock

    def download_pending(
        self, *, post_id: int | None = None, limit: int | None = None
    ) -> list[int]:
        """Fetch every pending image, appending one row per attempt.

        Stops early once the cumulative successful payload would exceed the batch
        budget, so a single pass cannot balloon the archive; the remaining targets
        stay pending for a later run. Failures are recorded, not raised, so one
        dead link does not abort the batch.
        """
        targets = self.archive.image_download_targets(post_id=post_id, limit=limit)
        row_ids: list[int] = []
        batch_bytes = 0
        for index, target in enumerate(targets):
            result = self._fetch(target)
            if result.download_status == "ok" and result.byte_size is not None:
                if batch_bytes + result.byte_size > self.settings.max_batch_bytes:
                    break
                batch_bytes += result.byte_size
            row_ids.append(self.archive.record_image_download(target, result, self.clock()))
            if index + 1 < len(targets):
                self._wait()
        return row_ids

    def _fetch(self, target: ImageDownloadTarget) -> ImageDownloadResult:
        try:
            with self.client.stream("GET", target.source_url) as response:
                if response.status_code != 200:
                    return ImageDownloadResult(
                        download_status="failed", notes=f"http_{response.status_code}"
                    )
                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                declared_size = _content_length(response)
                if declared_size is not None and declared_size > self.settings.max_image_bytes:
                    return ImageDownloadResult(
                        download_status="failed", byte_size=declared_size, notes="too_large"
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.settings.max_image_bytes:
                        return ImageDownloadResult(
                            download_status="failed", byte_size=len(body), notes="too_large"
                        )
        except httpx.HTTPError:
            return ImageDownloadResult(download_status="failed", notes="http_error")
        if not body:
            return ImageDownloadResult(download_status="failed", notes="empty_body")
        mime_type = content_type if content_type.startswith(_IMAGE_MIME_PREFIX) else None
        image_bytes = bytes(body)
        return ImageDownloadResult(
            download_status="ok",
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            mime_type=mime_type,
            byte_size=len(image_bytes),
            image_bytes=image_bytes,
            notes=None if mime_type else "non_image_content_type",
        )

    def _wait(self) -> None:
        delay = self.settings.request_min_interval_seconds + random.uniform(
            0, self.settings.request_jitter_seconds
        )
        self.sleep(delay)


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None
