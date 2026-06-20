"""Vision-model image enrichment (phase 3, image layer).

Mirrors :mod:`kol_archive.enrich` — same OpenAI-compatible transport and the same
idempotent, ``UNIQUE``-keyed batch shape — but judges an image instead of text.
Two deliberate differences keep it honest as *inference, not evidence*:

* the bytes sent are the archived BLOB re-encoded as a ``data:`` URI, never the
  live source URL, so an expired/replaced/auth-gated link cannot change what was
  judged after the fact; and
* the system prompt forbids inventing numbers or conclusions the image does not
  visibly show — the description is a reading aid for attention filtering, and is
  surfaced and exported as an inferred label, not as evidence text.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from kol_archive.enrich import load_enrich_settings
from kol_archive.obs import http_client
from kol_archive.service import Archive

LOGGER = logging.getLogger(__name__)

USER_PROMPT = "请客观描述这张图片中可见的内容。"

SYSTEM_PROMPT = """你在协助用户描述一条 KOL 发言中附带的图片，用于过滤注意力，不替用户下判断。
只描述图片中清晰可见的内容（如图表类型、坐标轴与图例文字、截图中的文字、表格、人物或场景），
可指出"这是一张某类图表/某应用截图"。

严禁补造图中未显示的数字、结论、标的或信息来源；看不清或无法确定的，直接说明看不清，不要猜测。
用简洁中文输出一段描述即可，不要加入你自己的投资判断或评价。"""

# Magic-byte sniffing so the data: URI carries a truthful mime even when the CDN's
# Content-Type was missing or wrong at download time.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


@dataclass(frozen=True)
class VisionSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    prompt_version: str = "vision-v1"


def load_vision_settings(config: dict[str, Any]) -> VisionSettings:
    """Reuse the text-enrichment llm block; allow a vision-specific model/prompt."""
    base = load_enrich_settings(config)
    llm = config.get("llm") or {}
    model = str(llm.get("vision_model") or base.model).strip()
    prompt_version = str(llm.get("vision_prompt_version") or "vision-v1").strip()
    return VisionSettings(
        base_url=base.base_url, model=model, api_key=base.api_key, prompt_version=prompt_version
    )


def _sniff_mime(image_bytes: bytes, fallback: str | None) -> str:
    for magic, mime in _MAGIC:
        if image_bytes.startswith(magic):
            return mime
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return fallback or "image/jpeg"


def _data_uri(image_bytes: bytes, mime_type: str | None) -> str:
    mime = _sniff_mime(image_bytes, mime_type)
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def request_image_description(
    settings: VisionSettings,
    image_bytes: bytes,
    mime_type: str | None,
    *,
    client: httpx.Client | None = None,
) -> str:
    owned_client = client is None
    active_client = client or http_client(timeout=60.0)
    try:
        response = active_client.post(
            f"{settings.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            json={
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": USER_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": _data_uri(image_bytes, mime_type)},
                            },
                        ],
                    },
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("vision response body must be a JSON object")
        return _parse_description(cast(dict[str, Any], payload))
    finally:
        if owned_client:
            active_client.close()


def _parse_description(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("vision response is missing choices[0].message.content") from error
    description = str(content or "").strip()
    if not description:
        raise ValueError("vision response description must not be empty")
    return description


def run_image_enrichment(
    archive: Archive,
    settings: VisionSettings,
    *,
    post_id: int | None = None,
    limit: int | None = None,
    client: httpx.Client | None = None,
    clock: Callable[[], str] | None = None,
) -> list[int]:
    """Describe every stored image lacking a verdict for this model+prompt_version.

    Idempotent (target query excludes already-described images) and fault-tolerant:
    a per-image request failure is logged and skipped so one bad image does not
    abort the batch.
    """
    now = clock or (lambda: datetime.now(tz=UTC).isoformat())
    targets = archive.image_enrichment_targets(
        settings.model, settings.prompt_version, post_id=post_id, limit=limit
    )
    owned_client = client is None
    active_client = client or http_client(timeout=60.0)
    row_ids: list[int] = []
    try:
        for image in targets:
            try:
                description = request_image_description(
                    settings, image.image_bytes, image.mime_type, client=active_client
                )
            except httpx.HTTPError, ValueError:
                LOGGER.warning("vision enrichment failed for image_id=%s", image.image_id)
                continue
            row_id = archive.add_image_enrichment(
                image, settings.model, settings.prompt_version, USER_PROMPT, description, now()
            )
            if row_id is not None:
                row_ids.append(row_id)
    finally:
        if owned_client:
            active_client.close()
    return row_ids
