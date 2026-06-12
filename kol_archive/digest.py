"""Neutral, evidence-linked change digests."""

from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from difflib import unified_diff
from pathlib import Path

from kol_archive.presentation import version_descriptive_market_snapshots


@dataclass(frozen=True)
class DigestEvent:
    event_id: int
    kind: str
    post_id: int
    author_id: int
    platform_post_id: str
    author_name: str
    detected_at: str
    first_seen_at: str
    last_observed_at: str | None
    excerpt: str
    diff: str | None = None
    image_paths: tuple[str, ...] = ()
    market_snapshot: dict[str, object] | None = None


@dataclass(frozen=True)
class DigestResult:
    title: str
    start_at: str
    end_at: str
    events: tuple[DigestEvent, ...]
    deletion_count: int
    edit_count: int
    image_change_count: int
    deletion_wave: bool
    markdown_path: Path
    html_path: Path


def _event_rows(connection: sqlite3.Connection, start_at: str, end_at: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            e.id AS event_id,
            e.dimension,
            e.to_value,
            e.detected_at,
            e.from_version_id,
            e.to_version_id,
            from_v.id AS from_v_id,
            from_v.content_text AS from_v_text,
            from_v.content_hash AS from_v_hash,
            from_v.image_manifest_hash AS from_v_image_hash,
            to_v.id AS to_v_id,
            to_v.content_text AS to_v_text,
            to_v.content_hash AS to_v_hash,
            to_v.image_manifest_hash AS to_v_image_hash,
            p.id AS post_id,
            p.author_id,
            p.platform_post_id,
            p.first_seen_at,
            p.current_version_id,
            COALESCE(
                json_extract(current_v.raw_payload, '$.user.screen_name'),
                a.notes,
                a.platform_uid
            ) AS author_name,
            current_v.content_text AS current_text,
            (
                SELECT MAX(s.observed_at)
                FROM version_sightings s
                WHERE s.version_id = p.current_version_id
            ) AS last_observed_at
        FROM post_events e
        JOIN posts p ON p.id = e.post_id
        JOIN authors a ON a.id = p.author_id
        LEFT JOIN post_versions current_v ON current_v.id = p.current_version_id
        LEFT JOIN post_versions from_v ON from_v.id = e.from_version_id
        LEFT JOIN post_versions to_v ON to_v.id = e.to_version_id
        WHERE e.detected_at >= ? AND e.detected_at <= ?
          AND (
            (e.dimension = 'source_state' AND e.to_value = 'gone_confirmed')
            OR (
                e.dimension = 'content'
                AND e.from_version_id IS NOT NULL
                AND e.to_version_id IS NOT NULL
            )
          )
        ORDER BY e.detected_at DESC, e.id DESC
        """,
        (start_at, end_at),
    ).fetchall()


def _excerpt(text: object, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _content_kind(row: sqlite3.Row) -> str:
    if (
        row["from_v_hash"] == row["to_v_hash"]
        and row["from_v_image_hash"] != row["to_v_image_hash"]
    ):
        return "image_change"
    return "edit"


def _content_diff(row: sqlite3.Row) -> str | None:
    if row["from_v_hash"] == row["to_v_hash"]:
        return None
    return "".join(
        unified_diff(
            str(row["from_v_text"]).splitlines(keepends=True),
            str(row["to_v_text"]).splitlines(keepends=True),
            fromfile=f"observed-version-{row['from_v_id']}",
            tofile=f"observed-version-{row['to_v_id']}",
        )
    )


def _image_rows_by_version(
    connection: sqlite3.Connection, version_ids: set[int]
) -> dict[int, list[sqlite3.Row]]:
    if not version_ids:
        return {}
    placeholders = ",".join("?" for _ in version_ids)
    rows = connection.execute(
        f"""
        SELECT version_id, mime_type, image_bytes
        FROM (
            SELECT
                version_id,
                mime_type,
                image_bytes,
                ROW_NUMBER() OVER (PARTITION BY version_id ORDER BY ordinal, id DESC) AS image_rank
            FROM post_images
            WHERE download_status = 'ok' AND version_id IN ({placeholders})
        )
        WHERE image_rank <= 3
        ORDER BY version_id, image_rank
        """,
        tuple(sorted(version_ids)),
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["version_id"]), []).append(row)
    return grouped


def _image_suffix(mime_type: object) -> str:
    return {
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(str(mime_type).lower(), ".bin")


def _export_images(rows: list[sqlite3.Row], assets_dir: Path, event_id: int) -> tuple[str, ...]:
    paths: list[str] = []
    for index, row in enumerate(rows, start=1):
        suffix = _image_suffix(row["mime_type"])
        filename = f"event-{event_id}-image-{index}{suffix}"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / filename).write_bytes(bytes(row["image_bytes"]))
        paths.append(f"assets/{filename}")
    return tuple(paths)


def _event_image_version_id(row: sqlite3.Row) -> int | None:
    value = (
        row["current_version_id"] if row["dimension"] == "source_state" else row["to_version_id"]
    )
    return None if value is None else int(value)


def collect_digest_events(
    connection: sqlite3.Connection,
    start_at: str,
    end_at: str,
    assets_dir: Path,
    *,
    prompt_version: str,
    benchmark_ticker: str,
) -> tuple[DigestEvent, ...]:
    rows = _event_rows(connection, start_at, end_at)
    image_version_ids = {
        version_id for row in rows if (version_id := _event_image_version_id(row)) is not None
    }
    images_by_version = _image_rows_by_version(connection, image_version_ids)
    snapshots_by_version = version_descriptive_market_snapshots(
        connection, image_version_ids, prompt_version, benchmark_ticker
    )
    events: list[DigestEvent] = []
    for row in rows:
        kind = "deletion" if row["dimension"] == "source_state" else _content_kind(row)
        version_id = row["current_version_id"] if kind == "deletion" else row["to_version_id"]
        text = row["current_text"] if kind == "deletion" else row["to_v_text"]
        events.append(
            DigestEvent(
                event_id=int(row["event_id"]),
                kind=kind,
                post_id=int(row["post_id"]),
                author_id=int(row["author_id"]),
                platform_post_id=str(row["platform_post_id"]),
                author_name=str(row["author_name"]),
                detected_at=str(row["detected_at"]),
                first_seen_at=str(row["first_seen_at"]),
                last_observed_at=(
                    None if row["last_observed_at"] is None else str(row["last_observed_at"])
                ),
                excerpt=_excerpt(text),
                diff=_content_diff(row),
                image_paths=_export_images(
                    images_by_version.get(int(version_id), []) if version_id is not None else [],
                    assets_dir,
                    int(row["event_id"]),
                ),
                market_snapshot=(
                    snapshots_by_version.get(int(version_id)) if version_id is not None else None
                ),
            )
        )
    return tuple(events)


def _kind_label(kind: str) -> str:
    return {
        "deletion": "删除事件",
        "edit": "编辑事件",
        "image_change": "仅图片变更",
    }[kind]


def _markdown_text(value: object) -> str:
    return re.sub(r"([\\`*_[\]{}()#+\-.!>|])", r"\\\1", html.escape(str(value)))


def _markdown_diff(diff: str) -> list[str]:
    longest_run = max((len(match) for match in re.findall(r"`+", diff)), default=0)
    fence = "`" * max(3, longest_run + 1)
    return [f"{fence}diff", diff.rstrip(), fence, ""]


def _percent(value: object) -> str:
    return f"{float(str(value)):+.2%}"


def _market_text(snapshot: dict[str, object]) -> str:
    ticker = str(snapshot["ticker"])
    ticker_name = str(snapshot.get("ticker_name") or "").strip()
    label = f"{ticker_name}（{ticker}）" if ticker_name else ticker
    return (
        f"描述性市场变化：{label} {_percent(snapshot['raw_return'])}，"
        f"{snapshot['benchmark_ticker']} {_percent(snapshot['benchmark_return'])}，"
        f"超额 {_percent(snapshot['excess_return'])}；"
        f"{snapshot['start_date']} 至 {snapshot['end_date']}，"
        f"口径 {snapshot['method_version']}"
    )


def _render_markdown(
    title: str, start_at: str, end_at: str, events: tuple[DigestEvent, ...], wave: bool
) -> str:
    lines = [f"# {title}", "", f"统计窗口：{start_at} 至 {end_at}", ""]
    if not events:
        lines.extend(["无变更", ""])
        return "\n".join(lines)
    if wave:
        lines.extend(["平台级删帖密集期", ""])
    for event in events:
        lines.extend(
            [
                f"## {_kind_label(event.kind)} · {_markdown_text(event.author_name)}",
                "",
                f"帖子：{_markdown_text(event.platform_post_id)}（归档 ID {event.post_id}）",
                f"首次观察：{_markdown_text(event.first_seen_at)}",
                f"最后观察：{_markdown_text(event.last_observed_at or '暂无')}",
                f"检测到变更：{_markdown_text(event.detected_at)}",
                "",
                _markdown_text(event.excerpt or "无正文摘录"),
                "",
            ]
        )
        for image_path in event.image_paths:
            lines.extend([f"![存档图片缩略]({image_path})", ""])
        if event.market_snapshot:
            lines.extend([_markdown_text(_market_text(event.market_snapshot)), ""])
        if event.diff:
            lines.extend(_markdown_diff(event.diff))
    return "\n".join(lines)


def _render_html(
    title: str, start_at: str, end_at: str, events: tuple[DigestEvent, ...], wave: bool
) -> str:
    blocks: list[str] = []
    if not events:
        blocks.append("<p>无变更</p>")
    if wave:
        blocks.append("<p><strong>平台级删帖密集期</strong></p>")
    for event in events:
        images = "".join(
            f'<img src="{html.escape(path)}" alt="存档图片缩略">' for path in event.image_paths
        )
        diff = f"<pre>{html.escape(event.diff)}</pre>" if event.diff else ""
        market = (
            f"<p>{html.escape(_market_text(event.market_snapshot))}</p>"
            if event.market_snapshot
            else ""
        )
        blocks.append(
            "<article>"
            f"<h2>{html.escape(_kind_label(event.kind))} · {html.escape(event.author_name)}</h2>"
            f"<p>帖子：{html.escape(event.platform_post_id)}（归档 ID {event.post_id}）</p>"
            f"<p>首次观察：{html.escape(event.first_seen_at)}<br>"
            f"最后观察：{html.escape(event.last_observed_at or '暂无')}<br>"
            f"检测到变更：{html.escape(event.detected_at)}</p>"
            f"<p>{html.escape(event.excerpt or '无正文摘录')}</p>{images}{market}{diff}</article>"
        )
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px/1.6 system-ui;max-width:920px;margin:32px auto;padding:0 20px}"
        "article{border-top:1px solid #bbb;padding:18px 0}img{max-width:240px;max-height:180px;"
        "object-fit:contain;margin:8px}pre{white-space:pre-wrap;background:#f4f4f4;padding:12px}</style>"
        f"<h1>{html.escape(title)}</h1><p>统计窗口：{html.escape(start_at)} 至 "
        f"{html.escape(end_at)}</p>{''.join(blocks)}</html>"
    )


def generate_digest(
    connection: sqlite3.Connection,
    start_at: str,
    end_at: str,
    output_dir: Path,
    *,
    wave_min_accounts: int = 3,
    prompt_version: str,
    benchmark_ticker: str,
) -> DigestResult:
    if wave_min_accounts < 0:
        raise ValueError("wave_min_accounts must not be negative")
    end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
    bundle_dir = output_dir / end.strftime("%Y%m%dT%H%M%SZ")
    assets_dir = bundle_dir / "assets"
    events = collect_digest_events(
        connection,
        start_at,
        end_at,
        assets_dir,
        prompt_version=prompt_version,
        benchmark_ticker=benchmark_ticker,
    )
    deletion_authors = {event.author_id for event in events if event.kind == "deletion"}
    wave = len(deletion_authors) >= wave_min_accounts
    title = f"KOL 变更摘要 {end.strftime('%Y-%m-%d')}"
    markdown_path = bundle_dir / "digest.md"
    html_path = bundle_dir / "digest.html"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(
        _render_markdown(title, start_at, end_at, events, wave), encoding="utf-8"
    )
    html_path.write_text(_render_html(title, start_at, end_at, events, wave), encoding="utf-8")
    return DigestResult(
        title=title,
        start_at=start_at,
        end_at=end_at,
        events=events,
        deletion_count=sum(event.kind == "deletion" for event in events),
        edit_count=sum(event.kind == "edit" for event in events),
        image_change_count=sum(event.kind == "image_change" for event in events),
        deletion_wave=wave,
        markdown_path=markdown_path,
        html_path=html_path,
    )
