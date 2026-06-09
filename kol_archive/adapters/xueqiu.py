"""Pure parsing helpers for Xueqiu timeline and direct-link responses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kol_archive.models import (
    ContentFidelity,
    IngestMode,
    LoginState,
    NormalizedPost,
    PostImage,
    ProbeResult,
    RunStatus,
)
from kol_archive.time import timestamp_at_or_before
from probe.normalize_text import (
    content_hash,
    content_text,
    extract_image_urls,
    image_manifest_hash,
    normalize_image_url,
)

ADAPTER_VERSION = "xueqiu-3"
LOGIN_EXPIRED_CODE = "10022"
NOT_FOUND_CODE = "20210"


def epoch_ms_to_utc(value: object) -> str:
    if not isinstance(value, int):
        raise ValueError("epoch milliseconds must be an integer")
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()


def _int_field(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) else None


def _error_code(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    value = payload.get("error_code")
    return str(value) if value is not None else None


def response_failure_note(http_status: int, payload_issue: str | None = None) -> str:
    if http_status != 200:
        return f"http_{http_status}"
    return payload_issue or "response_not_json"


@dataclass(frozen=True)
class FeedPage:
    posts: list[NormalizedPost]
    parse_failure_count: int
    page: int
    max_page: int
    total: int
    covered_from: str | None
    covered_to: str | None

    def covers_window(self, window_started_at: str) -> bool:
        return self.page >= self.max_page or (
            self.covered_from is not None
            and timestamp_at_or_before(self.covered_from, window_started_at)
        )


@dataclass(frozen=True)
class ProbeParse:
    status: RunStatus
    login_state: LoginState
    rate_limited: bool
    result: ProbeResult
    content_fidelity: ContentFidelity
    observed_post: NormalizedPost | None
    notes: str | None = None


def normalize_status(
    payload: dict[str, Any],
    *,
    author_id: int,
    observed_at: str,
    ingest_mode: IngestMode,
) -> tuple[NormalizedPost | None, bool]:
    try:
        platform_post_id = str(_int_field(payload, "id"))
        user_id = _int_field(payload, "user_id")
        posted_at_claimed = epoch_ms_to_utc(payload.get("created_at"))
    except ValueError:
        return None, True

    raw_meta = {
        "edited_at": _optional_int(payload, "edited_at"),
        "is_column": bool(payload.get("is_column")),
        "mark": _optional_int(payload, "mark"),
        "truncated": bool(payload.get("truncated")),
    }
    images: tuple[PostImage, ...] = ()
    manifest_hash: str | None = None
    if bool(payload.get("is_column")) or bool(payload.get("truncated")):
        fidelity = ContentFidelity.PREVIEW
        display_text = None
        digest = None
        failed = False
    else:
        raw_text = payload.get("text")
        if not isinstance(raw_text, str) or not raw_text:
            fidelity = ContentFidelity.NA
            display_text = None
            digest = None
            failed = True
        else:
            fidelity = ContentFidelity.FULL
            display_text = content_text(raw_text)
            digest = content_hash(raw_text)
            failed = False
            images = _parse_images(raw_text)
            manifest_hash = image_manifest_hash([image.normalized_url for image in images])

    return (
        NormalizedPost(
            platform_post_id=platform_post_id,
            author_id=author_id,
            observed_at=observed_at,
            content_fidelity=fidelity,
            content_text=display_text,
            content_hash=digest,
            image_manifest_hash=manifest_hash,
            images=images,
            posted_at_claimed=posted_at_claimed,
            url=f"https://xueqiu.com/{user_id}/{platform_post_id}",
            ingest_mode=ingest_mode,
            raw_payload=payload,
            raw_meta=raw_meta,
        ),
        failed,
    )


def _parse_images(raw_text: str) -> tuple[PostImage, ...]:
    """Ordered image manifest for a full-fidelity post, parsed from its HTML.

    ``normalized_url`` (query stripped) is the manifest/dedupe key; ``source_url``
    keeps the signed URL actually needed to fetch the bytes. ``ordinal`` preserves
    document order so a reordered set of images is itself a detectable change.
    """
    images: list[PostImage] = []
    for ordinal, source_url in enumerate(extract_image_urls(raw_text)):
        images.append(
            PostImage(
                source_url=source_url,
                normalized_url=normalize_image_url(source_url),
                ordinal=ordinal,
            )
        )
    return tuple(images)


def parse_feed_page(
    payload: dict[str, Any],
    *,
    author_id: int,
    observed_at: str,
    ingest_mode: IngestMode = IngestMode.LIVE,
) -> FeedPage:
    raw_statuses = payload.get("statuses")
    if not isinstance(raw_statuses, list):
        raise ValueError("timeline statuses must be a list")
    posts: list[NormalizedPost] = []
    parse_failure_count = 0
    covered_times: list[str] = []
    for raw_status in raw_statuses:
        if not isinstance(raw_status, dict):
            parse_failure_count += 1
            continue
        post, failed = normalize_status(
            raw_status,
            author_id=author_id,
            observed_at=observed_at,
            ingest_mode=ingest_mode,
        )
        parse_failure_count += int(failed)
        if post is None:
            continue
        posts.append(post)
        if raw_status.get("mark") != 1 and post.posted_at_claimed is not None:
            covered_times.append(post.posted_at_claimed)
    return FeedPage(
        posts=posts,
        parse_failure_count=parse_failure_count,
        page=_int_field(payload, "page"),
        max_page=_int_field(payload, "maxPage"),
        total=_int_field(payload, "total"),
        covered_from=min(covered_times, default=None),
        covered_to=max(covered_times, default=None),
    )


def parse_probe_response(
    http_status: int,
    payload: dict[str, Any] | None,
    *,
    author_id: int,
    observed_at: str,
    ingest_mode: IngestMode = IngestMode.LIVE,
    payload_issue: str | None = None,
) -> ProbeParse:
    error_code = _error_code(payload)
    if http_status == 429:
        return ProbeParse(
            RunStatus.PARTIAL,
            LoginState.UNKNOWN,
            True,
            ProbeResult.UNKNOWN,
            ContentFidelity.NA,
            None,
            "http_429",
        )
    if error_code == LOGIN_EXPIRED_CODE:
        return ProbeParse(
            RunStatus.PARTIAL,
            LoginState.EXPIRED,
            False,
            ProbeResult.UNKNOWN,
            ContentFidelity.NA,
            None,
            f"error_code={LOGIN_EXPIRED_CODE}",
        )
    if error_code == NOT_FOUND_CODE or http_status == 404:
        return ProbeParse(
            RunStatus.OK,
            LoginState.VALID,
            False,
            ProbeResult.NOT_FOUND,
            ContentFidelity.NA,
            None,
            f"error_code={error_code}" if error_code else "http_404",
        )
    if http_status != 200 or payload is None:
        return ProbeParse(
            RunStatus.FAILED,
            LoginState.UNKNOWN,
            False,
            ProbeResult.UNKNOWN,
            ContentFidelity.NA,
            None,
            response_failure_note(http_status, payload_issue),
        )
    if (
        bool(payload.get("is_private"))
        or bool(payload.get("is_refused"))
        or payload.get("legal_user_visible") is False
    ):
        return ProbeParse(
            RunStatus.OK,
            LoginState.VALID,
            False,
            ProbeResult.RESTRICTED,
            ContentFidelity.NA,
            None,
        )
    if bool(payload.get("explicitly_removed")):
        return ProbeParse(
            RunStatus.OK,
            LoginState.VALID,
            False,
            ProbeResult.EXPLICITLY_REMOVED,
            ContentFidelity.NA,
            None,
        )
    post, failed = normalize_status(
        payload,
        author_id=author_id,
        observed_at=observed_at,
        ingest_mode=ingest_mode,
    )
    if failed or post is None:
        return ProbeParse(
            RunStatus.PARTIAL,
            LoginState.VALID,
            False,
            ProbeResult.UNKNOWN,
            ContentFidelity.NA,
            None,
            "content_parse_failed",
        )
    return ProbeParse(
        RunStatus.OK,
        LoginState.VALID,
        False,
        ProbeResult.REACHABLE,
        post.content_fidelity,
        post,
    )
