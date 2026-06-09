"""OCR derivation: searchable text transcribed from stored image bytes.

A derived layer, deliberately kept out of ``post_versions.content_text``: OCR is a
machine transcription that may misread, so it is recorded as its own material —
tagged with the engine and version that produced it — rather than mixed into the
verbatim evidence. WinOCR is preferred on Windows (offline, no extra cost);
Tesseract is the cross-platform fallback. Both deps are optional and imported
lazily so the archive runs (and tests pass) without either installed.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from kol_archive.service import Archive

LOGGER = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


@runtime_checkable
class OcrEngine(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def recognize(self, image_bytes: bytes) -> str: ...


def _load_image(image_bytes: bytes):  # type: ignore[no-untyped-def]
    from PIL import Image  # type: ignore[import-not-found]  # noqa: PLC0415

    return Image.open(io.BytesIO(image_bytes))


class WinOcrEngine:
    """Windows.Media.Ocr via the ``winocr`` package (offline, free, Windows-only)."""

    name = "winocr"

    def __init__(self, lang: str = "zh-Hans") -> None:
        import winocr  # type: ignore[import-not-found]  # noqa: PLC0415

        self._winocr = winocr
        self._lang = lang
        self.version = str(getattr(winocr, "__version__", "unknown"))

    def recognize(self, image_bytes: bytes) -> str:
        result = self._winocr.recognize_pil_sync(_load_image(image_bytes), self._lang)
        return str(getattr(result, "text", "") or "").strip()


class TesseractEngine:
    """Cross-platform fallback via ``pytesseract`` (requires a tesseract binary)."""

    name = "tesseract"

    def __init__(self, lang: str = "chi_sim+eng") -> None:
        import pytesseract  # type: ignore[import-not-found]  # noqa: PLC0415

        self._pytesseract = pytesseract
        self._lang = lang
        self.version = str(pytesseract.get_tesseract_version())

    def recognize(self, image_bytes: bytes) -> str:
        text = self._pytesseract.image_to_string(_load_image(image_bytes), lang=self._lang)
        return str(text or "").strip()


def select_engine() -> OcrEngine:
    """WinOCR first, then Tesseract; raise if neither is usable on this host."""
    errors: list[str] = []
    for factory in (WinOcrEngine, TesseractEngine):
        try:
            return factory()
        except Exception as error:  # noqa: BLE001 — probing optional backends
            errors.append(f"{factory.__name__}: {error}")
    raise RuntimeError("no OCR engine available — " + "; ".join(errors))


def run_ocr(
    archive: Archive,
    engine: OcrEngine,
    *,
    post_id: int | None = None,
    limit: int | None = None,
    clock: Callable[[], str] = utc_now,
) -> list[int]:
    """Transcribe every stored image lacking OCR for this engine+version.

    Idempotent and resumable: already-transcribed images are skipped by the target
    query, and a recognition error on one image is logged and skipped (left pending
    for a retry) rather than aborting the batch.
    """
    targets = archive.ocr_targets(engine.name, engine.version, post_id=post_id, limit=limit)
    row_ids: list[int] = []
    for image in targets:
        try:
            text = engine.recognize(image.image_bytes)
        except Exception:  # noqa: BLE001 — a bad image must not sink the batch
            LOGGER.warning("OCR failed for image_id=%s", image.image_id, exc_info=True)
            continue
        row_id = archive.add_image_ocr(image, engine.name, engine.version, text, clock())
        if row_id is not None:
            row_ids.append(row_id)
    return row_ids
