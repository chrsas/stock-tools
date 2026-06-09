"""Small server-rendered web interface for the local single-user archive."""

from __future__ import annotations

import logging
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, unquote, urlparse

from kol_archive.config import load_config
from kol_archive.database import connect_database
from kol_archive.maintenance import redact_text
from kol_archive.models import FeedState, SourceState, WatchMode
from kol_archive.presentation import (
    author_profile,
    author_recent_viewpoint_clusters,
    author_viewpoint_overview,
    build_evidence_card,
    list_attention_queue,
    list_filtered_timeline,
    list_pinned_versions,
    list_timeline,
)
from kol_archive.rewrite import load_rewrite_settings, request_rewrite
from kol_archive.service import Archive

LOGGER = logging.getLogger(__name__)
MAX_FORM_BYTES = 64 * 1024
REWRITE_VERDICTS = ("valid", "too_vague", "wrong")


@dataclass(frozen=True)
class WebSettings:
    bind_host: str = "127.0.0.1"
    port: int = 8765
    timeline_limit: int = 50
    window_days: int = 30
    enrich_prompt_version: str = "enrich-v1"


class ArchiveHttpServer(ThreadingHTTPServer):
    db_path: Path
    config_dir: Path
    csrf_token: str
    timeline_limit: int
    window_days: int
    enrich_prompt_version: str


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def load_web_settings(
    config: dict[str, Any],
    *,
    bind_host: str | None = None,
    port: int | None = None,
) -> WebSettings:
    web = _section(config, "web")
    monitoring = _section(config, "monitoring")
    llm = _section(config, "llm")
    settings = WebSettings(
        bind_host=str(bind_host or web.get("bind_host") or "127.0.0.1").strip(),
        port=int(port if port is not None else web.get("port") or 8765),
        timeline_limit=int(web.get("timeline_limit") or 50),
        window_days=int(monitoring.get("window_days") or 30),
        enrich_prompt_version=str(llm.get("enrich_prompt_version") or "enrich-v1").strip()
        or "enrich-v1",
    )
    if not settings.bind_host or settings.bind_host in {"0.0.0.0", "::", "[::]"}:
        raise ValueError("web.bind_host must be a loopback or explicit tailnet address")
    if not 1 <= settings.port <= 65535:
        raise ValueError("web.port must be between 1 and 65535")
    if settings.timeline_limit < 1:
        raise ValueError("web.timeline_limit must be positive")
    if settings.window_days < 1:
        raise ValueError("monitoring.window_days must be positive")
    return settings


def create_server(
    db_path: Path,
    config_dir: Path,
    settings: WebSettings,
    *,
    csrf_token: str | None = None,
) -> ArchiveHttpServer:
    if not db_path.is_file():
        raise FileNotFoundError(f"SQLite archive does not exist: {db_path}")
    server = ArchiveHttpServer((settings.bind_host, settings.port), ArchiveRequestHandler)
    server.db_path = db_path
    server.config_dir = config_dir
    server.csrf_token = csrf_token or secrets.token_urlsafe(32)
    server.timeline_limit = settings.timeline_limit
    server.window_days = settings.window_days
    server.enrich_prompt_version = settings.enrich_prompt_version
    return server


def serve_archive(
    db_path: Path,
    config_dir: Path,
    settings: WebSettings,
) -> None:
    server = create_server(db_path, config_dir, settings)
    host, port = cast(tuple[str, int], server.server_address)
    LOGGER.info("web archive listening http://%s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _cell(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("human_label", "")
    return escape(_text(value))


def _original_post_link(value: object) -> str:
    url = _text(value).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    href = escape(url, quote=True)
    return (
        f'<a class="secondary" href="{href}" target="_blank" '
        'rel="noopener noreferrer">打开雪球原帖</a>'
    )


def _snowball_user_url(uid: object) -> str:
    raw = _text(uid).strip()
    if not raw:
        return ""
    return f"https://xueqiu.com/u/{quote(raw, safe='')}"


def _snowball_user_link(uid: object) -> str:
    href = _snowball_user_url(uid)
    if not href:
        return ""
    return (
        f'<a class="secondary" href="{escape(href, quote=True)}" target="_blank" '
        'rel="noopener noreferrer">雪球主页</a>'
    )


def _local_author_link(uid: object) -> str:
    raw = _text(uid).strip()
    if not raw:
        return ""
    return f'<a class="secondary" href="/authors/{quote(raw, safe="")}">本地作者页</a>'


def _author_name(item: dict[str, object]) -> str:
    return (
        _text(item.get("author_display_name")).strip()
        or _text(item.get("author_name")).strip()
        or _text(item.get("author_platform_uid")).strip()
        or "未知作者"
    )


def _avatar_url(value: object) -> str:
    candidates = [part.strip() for part in _text(value).split(",") if part.strip()]
    if not candidates:
        return ""
    raw = next((part for part in candidates if "50x50" in part), candidates[0])
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return raw
    if parsed.scheme:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    if any(char.isspace() for char in raw):
        return ""
    key = raw.lstrip("/")
    if not key.startswith(("community/", "avatar/", "cube/", "users/")):
        return ""
    return f"https://xqimg.imedao.com/{key}"


def _avatar_img(item: dict[str, object]) -> str:
    src = _avatar_url(item.get("author_avatar_url"))
    name = _author_name(item)
    if not src:
        return '<span class="avatar placeholder"></span>'
    return f'<img class="avatar" src="{escape(src, quote=True)}" alt="{escape(name, quote=True)}">'


def _author_badge(item: dict[str, object]) -> str:
    uid = _cell(item.get("author_platform_uid"))
    return (
        '<div class="author-badge">'
        f"{_avatar_img(item)}"
        "<div>"
        f'<div class="author-name">{escape(_author_name(item))}</div>'
        f'<div class="muted small">uid {uid}</div>'
        "</div></div>"
    )


def _post_title(item: dict[str, object]) -> str:
    platform_post_id = _text(item.get("platform_post_id")).strip()
    if platform_post_id:
        return f"雪球 {platform_post_id}"
    return f"本地记录 {item['post_id']}"


def _post_identity(item: dict[str, object]) -> str:
    parts = [
        f"原帖 {_cell(item.get('platform_post_id'))}",
        f"发布 {_fmt_ts_text(item.get('posted_at_claimed'))}",
        f"本地记录 {_cell(item.get('post_id'))}",
    ]
    return " · ".join(part for part in parts if not part.endswith(" "))


def _fmt_ts_text(value: object) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")


def _relative_ts_text(value: object) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if moment.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(tz=moment.tzinfo)
    delta = now - moment
    seconds = int(delta.total_seconds())
    if 0 <= seconds < 60:
        return "刚刚"
    if 60 <= seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if 3600 <= seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return _fmt_ts_text(value)


def _fmt_ts(value: object) -> str:
    raw = _text(value)
    if not raw:
        return ""
    label = _relative_ts_text(value)
    absolute = _fmt_ts_text(value)
    return (
        f'<time datetime="{escape(raw, quote=True)}" title="{escape(absolute, quote=True)}">'
        f"{escape(label)}</time>"
    )


def _csrf_input(token: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{escape(token)}">'


def _table(rows: list[dict[str, object]], *, empty: str = "暂无记录") -> str:
    if not rows:
        return f"<p>{escape(empty)}</p>"
    columns = list(rows[0])
    heading = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(row.get(column))}</td>" for column in columns) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="table-wrap"><table><thead><tr>{heading}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <script>
    (() => {{
      const systemTheme = matchMedia("(prefers-color-scheme: dark)");
      const saved = localStorage.getItem("kol-theme");
      const preference = saved === "light" || saved === "dark" ? saved : "system";
      const applyTheme = (nextPreference) => {{
        document.documentElement.dataset.theme =
          nextPreference === "system"
            ? systemTheme.matches ? "dark" : "light"
            : nextPreference;
        document.documentElement.dataset.themePreference = nextPreference;
      }};
      window.kolTheme = {{ applyTheme, preference, systemTheme }};
      applyTheme(preference);
      systemTheme.addEventListener("change", () => {{
        if (window.kolTheme.preference === "system") applyTheme("system");
      }});
    }})();
  </script>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: system-ui, sans-serif;
      --page: light-dark(#f5f6f8, #0f141b);
      --surface: light-dark(#fff, #171e27);
      --surface-soft: light-dark(#fbfcfe, #1b2430);
      --surface-code: light-dark(#f7f8fa, #111821);
      --text: light-dark(#18202b, #e6edf5);
      --text-soft: light-dark(#5d6878, #aab7c7);
      --text-faint: light-dark(#8190a4, #8998ab);
      --link: light-dark(#075985, #7dd3fc);
      --border: light-dark(#d8dee8, #344152);
      --border-soft: light-dark(#e4e8ef, #2c3746);
      --accent-soft: light-dark(#e0f2fe, #16384a);
      --active-soft: light-dark(#eaf6fb, #17394a);
      --neutral-soft: light-dark(#edf1f5, #293442);
      --snippet: light-dark(#f7f9fb, #1c2733);
      --snippet-text: light-dark(#26384d, #d8e3ef);
      --snippet-border: light-dark(#b8c7d8, #53677c);
      --avatar: light-dark(#dbe4ee, #293747);
      --track: light-dark(#eef3f7, #293544);
      --success-bg: light-dark(#e6f6ee, #153b2c);
      --success: light-dark(#15803d, #6ee7a8);
      --warn-bg: light-dark(#fff3d6, #493515);
      --warn: light-dark(#9a5b00, #f6c76b);
      --control-border: light-dark(#cbd5e1, #435266);
      --hover-border: light-dark(#7ba9c4, #6f91aa);
    }}
    :root[data-theme="light"] {{ color-scheme: light; }}
    :root[data-theme="dark"] {{
      color-scheme: dark;
    }}
    body {{ margin: 0; background: var(--page); color: var(--text); }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 16px; }}
    a {{ color: var(--link); }}
    article, section {{ background: var(--surface); border: 1px solid var(--border);
      border-radius: 10px;
      margin: 12px 0; padding: 14px; }}
    h1, h2, h3 {{ margin: 0 0 10px; line-height: 1.25; }}
    h1 {{ font-size: 1.5rem; }} h2 {{ font-size: 1.2rem; }} h3 {{ font-size: 1rem; }}
    p {{ margin: 8px 0; overflow-wrap: anywhere; }}
    .muted {{ color: var(--text-soft); word-break: break-all; }}
    .chip {{ display: inline-block; background: var(--accent-soft); color: var(--link);
      border-radius: 999px;
      padding: 2px 10px; margin-right: 6px; font-size: .82rem; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; font-size: .88rem; }}
    th, td {{ border-bottom: 1px solid var(--border-soft); padding: 8px; text-align: left;
      vertical-align: top; white-space: pre-wrap; }}
    th {{ color: var(--text-soft); }}
    pre {{ overflow-x: auto; background: var(--surface-code); border-radius: 6px; padding: 10px;
      white-space: pre-wrap; word-break: break-word; }}
    form {{ display: grid; gap: 8px; margin: 10px 0; }}
    label {{ display: grid; gap: 5px; }}
    input, textarea, select, button {{ box-sizing: border-box; font: inherit; max-width: 100%;
      min-height: 40px; padding: 8px; }}
    input, textarea, select {{ background: var(--surface); color: var(--text);
      border: 1px solid var(--control-border); border-radius: 5px; }}
    textarea {{ min-height: 84px; }}
    button {{ cursor: pointer; width: fit-content; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    .actions form {{ display: inline; margin: 0; }}
    .topbar {{ display: flex; justify-content: space-between; align-items: baseline;
      flex-wrap: wrap; gap: 10px; }}
    .nav {{ display: flex; flex-wrap: wrap; gap: 12px; font-size: .9rem; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 12px 0; }}
    .filter {{ border: 1px solid var(--control-border); background: var(--surface);
      color: var(--text);
      border-radius: 999px; padding: 6px 12px; font-size: .85rem; }}
    .filter.active {{ background: #075985; border-color: #075985; color: #fff; }}
    .filter.active:hover {{ text-decoration: none; }}
    .overview-grid {{ display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 14px;
      align-items: start; }}
    .author-list {{ position: sticky; top: 12px; }}
    .author-option {{ display: block; border: 1px solid var(--border); border-radius: 9px;
      padding: 11px; margin-bottom: 8px; background: var(--surface); color: inherit;
      text-decoration: none; }}
    .author-option:hover {{ border-color: var(--hover-border); }}
    .author-option.active {{ border-color: var(--link); background: var(--active-soft); }}
    .author-option .author-badge {{ margin-bottom: 7px; }}
    .toolcount {{ color: var(--text-soft); font-size: .82rem; }}
    .layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 14px;
      align-items: start; }}
    .panel {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
      padding: 14px; margin: 0 0 12px; }}
    .sectit {{ display: flex; justify-content: space-between; gap: 8px; align-items: baseline;
      flex-wrap: wrap; margin-bottom: 8px; }}
    .candidate {{ border: 1px solid var(--border); border-radius: 9px; padding: 13px;
      margin: 10px 0; }}
    .chead {{ display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap;
      align-items: start; }}
    .author-badge {{ display: flex; align-items: center; gap: 9px; min-width: 0; }}
    .avatar {{ width: 34px; height: 34px; border-radius: 50%; object-fit: cover; flex: none;
      background: var(--avatar); border: 1px solid var(--control-border); }}
    .avatar.placeholder::after {{ content: ""; display: block; width: 100%; height: 100%; }}
    .author-name {{ font-weight: 700; line-height: 1.25; }}
    .small {{ font-size: .8rem; }}
    .link-row {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }}
    .who {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline; min-width: 0; }}
    .who .name {{ font-weight: 700; font-size: 1rem; }}
    .who .uid {{ color: var(--text-faint); font-size: .8rem; }}
    .status {{ display: inline-flex; align-items: center; gap: 6px; border-radius: 999px;
      background: var(--success-bg); color: var(--success); padding: 4px 10px;
      font-weight: 700; font-size: .8rem;
      white-space: nowrap; }}
    .status.warn {{ background: var(--warn-bg); color: var(--warn); }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; background: currentColor; flex: none; }}
    .meta-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; }}
    .pill {{ display: inline-flex; align-items: center; border-radius: 999px;
      background: var(--accent-soft);
      color: var(--link); padding: 3px 9px; font-size: .8rem; }}
    .pill.gray {{ background: var(--neutral-soft); color: var(--text-soft); }}
    .snippet {{ margin: 9px 0; padding: 9px 12px; border-left: 3px solid var(--snippet-border);
      border-radius: 0 6px 6px 0; background: var(--snippet); color: var(--snippet-text);
      font-size: .9rem; }}
    .evidence-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px;
      margin: 10px 0; }}
    .fact {{ border: 1px solid var(--border-soft); background: var(--surface-soft);
      border-radius: 7px; padding: 8px;
      min-width: 0; }}
    .fact b {{ display: block; font-size: .8rem; margin-bottom: 4px; }}
    .fact span {{ display: block; color: var(--text-soft); font-size: .8rem; line-height: 1.4;
      overflow-wrap: anywhere; }}
    .qactions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; align-items: stretch; }}
    .qactions form {{ display: inline; margin: 0; }}
    .primary {{ background: #075985; border: 1px solid #075985; color: #fff; border-radius: 7px;
      padding: 7px 11px; font-size: .85rem; min-height: 36px; }}
    .secondary {{ background: var(--surface); border: 1px solid var(--control-border);
      color: var(--link); border-radius: 7px;
      padding: 7px 11px; font-size: .85rem; }}
    .side-item {{ border: 1px solid var(--border-soft); border-radius: 7px; padding: 9px;
      margin-bottom: 8px; }}
    .side-item b {{ display: block; font-size: .92rem; }}
    .side-item .who-line {{ color: var(--text-soft); font-size: .8rem; margin: 2px 0 6px; }}
    .bars {{ display: grid; gap: 5px; }}
    .barrow {{ display: grid; grid-template-columns: 72px minmax(0, 1fr) 28px; gap: 7px;
      align-items: center; font-size: .78rem; }}
    .bartrack {{ height: 11px; border-radius: 999px; background: var(--track); overflow: hidden;
      min-width: 0; }}
    .barfill {{ display: block; height: 100%; min-width: 3px; border-radius: 999px; }}
    .f0 {{ background: #0ea5e9; }} .f1 {{ background: #6366f1; }} .f2 {{ background: #d97706; }}
    .label-card {{ border: 1px solid var(--border-soft); background: var(--surface-soft);
      border-radius: 7px; padding: 9px;
      margin-bottom: 8px; }}
    .label-card b {{ display: block; font-size: .88rem; margin-bottom: 4px; }}
    .label-card p {{ margin: 0; color: var(--text-soft); font-size: .8rem; line-height: 1.45; }}
    .audit-row {{ display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 8px;
      font-size: .82rem; line-height: 1.45; margin-bottom: 6px; }}
    .audit-row span {{ color: var(--text-soft); }}
    .viewpoint {{ border-left: 4px solid #0ea5e9; }}
    .market-row {{ border-top: 1px solid var(--border-soft); padding-top: 8px; margin-top: 8px; }}
    .market-row strong {{ margin-right: 8px; }}
    .theme-control {{ display: flex; width: fit-content; align-items: center; gap: 6px;
      margin: 10px max(12px, calc((100% - 1180px) / 2 + 16px)) 0 auto;
      padding: 5px 7px; border: 1px solid var(--border);
      border-radius: 8px; background: var(--surface); color: var(--text-soft); font-size: .78rem;
      box-shadow: 0 2px 10px rgb(0 0 0 / 12%); }}
    .theme-control select {{ min-height: 30px; padding: 3px 6px; font-size: .78rem; }}
    @media (max-width: 860px) {{
      .layout, .overview-grid {{ grid-template-columns: 1fr; }}
      .author-list {{ position: static; }}
    }}
    @media (max-width: 640px) {{
      main {{ padding: 10px; }} article, section {{ padding: 11px; }}
      th, td {{ min-width: 128px; }} button {{ width: 100%; }}
      .actions, .actions form {{ display: grid; width: 100%; }}
      .evidence-grid {{ grid-template-columns: 1fr; }}
      .chead {{ flex-direction: column; }}
      .barrow {{ grid-template-columns: 64px minmax(0, 1fr) 26px; }}
      .theme-control {{ margin-right: 10px; }}
    }}
  </style>
</head>
<body>
<label class="theme-control">主题
  <select id="theme-select" aria-label="主题">
    <option value="system">跟随系统</option>
    <option value="light">浅色</option>
    <option value="dark">暗色</option>
  </select>
</label>
<main>{body}</main>
<script>
  (() => {{
    const select = document.getElementById("theme-select");
    select.value = document.documentElement.dataset.themePreference;
    select.addEventListener("change", () => {{
      const preference = select.value;
      window.kolTheme.preference = preference;
      window.kolTheme.applyTheme(preference);
      if (preference === "system") localStorage.removeItem("kol-theme");
      else localStorage.setItem("kol-theme", preference);
    }});
  }})();
</script>
</body>
</html>"""


_LABEL_CHIPS = (
    ("label_first_hand_info", "第一手信息"),
    ("label_transferable_framework", "可迁移框架"),
    ("label_reasoned_non_consensus", "有据非共识"),
)


def _timeline_article(item: dict[str, object], *, extra: str = "") -> str:
    status = cast(dict[str, str], item["status"])
    current_text = escape(_text(item.get("current_text")))
    original_link = _original_post_link(item.get("url"))
    links = "".join(
        [
            original_link,
            _snowball_user_link(item.get("author_platform_uid")),
            _local_author_link(item.get("author_platform_uid")),
        ]
    )
    links_html = f'\n  <div class="link-row">{links}</div>' if links else ""
    title = escape(_post_title(item))
    identity = escape(_post_identity(item))
    return f"""<article>
  {_author_badge(item)}
  <h2><a href="/posts/{item["post_id"]}">{title}</a></h2>
  <p class="muted">{identity}</p>{links_html}
  <p>{escape(status["human_label"])}</p>
  <p>{escape(status["deletion_signal_label"])}</p>{extra}
  <p class="muted">首次观察：{_cell(item.get("first_seen_at"))}<br>
  最后在场观察：{_cell(item.get("last_present_at"))}<br>
  当前版本首次观察：{_cell(item.get("current_version_first_observed_at"))}<br>
  当前版本最后观察：{_cell(item.get("current_version_last_observed_at"))}<br>
  检测到缺失：{_cell(item.get("last_feed_absence_detected_at"))}</p>
  <pre>{current_text}</pre>
</article>"""


def _timeline_html(connection: sqlite3.Connection, limit: int) -> str:
    items = list_timeline(connection, limit=limit)
    articles = [_timeline_article(item) for item in items]
    content = "".join(articles) or "<p>归档中暂无帖子。</p>"
    nav = (
        '<p><a href="/">博主最近观点</a> · <a href="/?view=queue">待处理队列</a>'
        ' · <a href="/?view=filtered">标签过滤流</a></p>'
    )
    return _layout("KOL 原始时间线", f"<h1>KOL 原始时间线</h1>{nav}{content}")


def _filtered_timeline_html(connection: sqlite3.Connection, prompt_version: str, limit: int) -> str:
    items = list_filtered_timeline(connection, prompt_version, limit=limit)
    articles = []
    for item in items:
        chips = " ".join(
            f'<span class="chip">{escape(label)}</span>'
            for key, label in _LABEL_CHIPS
            if item.get(key)
        )
        snippet = escape(_text(item.get("enrichment_evidence_snippet")))
        extra = (
            f"\n  <p>体裁：{_cell(item.get('post_type'))}　{chips}</p>"
            f'\n  <p class="muted">依据片段：{snippet}</p>'
        )
        articles.append(_timeline_article(item, extra=extra))
    content = "".join(articles) or (
        "<p>当前 prompt 版本下还没有命中标签的帖子。先运行 "
        "<code>python -m kol_archive enrich</code> 富化，再回来查看。</p>"
    )
    nav = (
        '<p><a href="/">博主最近观点</a> · <a href="/?view=queue">待处理队列</a>'
        ' · <a href="/?view=raw">原始时间线</a></p>'
    )
    heading = f'<h1>KOL 标签过滤流</h1><p class="muted">prompt 版本：{escape(prompt_version)}</p>'
    return _layout("KOL 标签过滤流", f"{heading}{nav}{content}")


_LABEL_GUIDE = (
    ("第一手信息", "来自作者自身观察、调研、交易复盘或可追溯经历，优先看原文是否给出具体场景。"),
    ("可迁移框架", "表达了可复用的判断方法、约束条件或推理结构，适合沉淀到关注理由里。"),
    ("有据非共识", "提出和常见叙事有差异的判断，并给出至少一段支撑证据或可验证线索。"),
)

_AUDIT_ROWS = (
    ("时间", "统一「首次观察 / 最后观察 / 检测到缺失」语义"),
    ("删除信号", "只展示 source_state，不推断移除主体"),
    ("版本", "队列行绑定 version_id，证据卡展示版本 diff"),
    ("队列", "纯推导，无新表；钉住或写关注理由后自然离队"),
)

# Persistent, in-page explanation of what each action does, so the operating
# vocabulary lives in the UI rather than only in the code.
_ACTION_GUIDE = (
    (
        "钉住",
        "把这条版本长期留观：移出待处理队列，并豁免它随时间被自动判为「停止持续监控」。"
        "认定重要、想继续盯它后续编辑时用。",
    ),
    (
        "关注理由",
        "写下你为何在意与预期后再钉住：同样离队，但额外留一条可回溯的判断记录"
        "（比纯钉住多了理由与预期）。",
    ),
    (
        "取消钉住",
        "恢复为按时间窗口观察；若它已滑出近期窗口，会回到「停止持续监控」。",
    ),
    (
        "改写训练",
        "让 LLM 把原文压成一句可证伪命题，供你校验后标 verdict，不改动原始证据。",
    ),
)

# Hover hints for the toolbar chips, so each count's meaning is one mouseover away.
_TOOLBAR_HINTS = {
    "pending": "命中标签、未钉住、未写关注理由的版本",
    "tier3": "第一手 / 可迁移框架 / 有据非共识 三个标签全部命中的强信号版本",
    "pinned": "已长期留观的版本，点开查看与取消钉住",
    "absent": "feed 连续健康轮次中确认缺席的帖子",
}


def _label_guide_panel() -> str:
    cards = "".join(
        f'<div class="label-card"><b>{escape(name)}</b><p>{escape(desc)}</p></div>'
        for name, desc in _LABEL_GUIDE
    )
    return (
        '<section class="panel"><div class="sectit"><h2>标签说明</h2>'
        '<span class="muted">给标签加上下文</span></div>' + cards + "</section>"
    )


def _action_guide_panel() -> str:
    cards = "".join(
        f'<div class="label-card"><b>{escape(name)}</b><p>{escape(desc)}</p></div>'
        for name, desc in _ACTION_GUIDE
    )
    return (
        '<section class="panel"><div class="sectit"><h2>操作说明</h2>'
        '<span class="muted">按钮各自做什么</span></div>' + cards + "</section>"
    )


def _audit_panel() -> str:
    rows = "".join(
        f'<div class="audit-row"><span>{escape(label)}</span><strong>{escape(text)}</strong></div>'
        for label, text in _AUDIT_ROWS
    )
    return (
        '<section class="panel"><div class="sectit"><h2>证据口径</h2></div>' + rows + "</section>"
    )


def _badge(text: str, *, warn: bool) -> str:
    cls = "status warn" if warn else "status"
    return f'<span class="{cls}"><span class="dot"></span>{escape(text)}</span>'


def _queue_status_badge(item: dict[str, object]) -> str:
    source_state = _text(item.get("source_state"))
    fidelity = _text(item.get("current_content_fidelity")) or "na"
    if source_state == SourceState.UNKNOWN.value:
        return _badge(f"未复查 · Track B 待跑 · {fidelity}", warn=True)
    label = {
        SourceState.REACHABLE.value: "直链可访问",
        SourceState.GONE_CONFIRMED.value: "来源已移除",
        SourceState.UNAVAILABLE.value: "直链不可访问",
    }.get(source_state, "未复查")
    return _badge(f"{label} · {fidelity}", warn=source_state == SourceState.UNAVAILABLE.value)


def _queue_card(item: dict[str, object], csrf_token: str, *, pinned: bool = False) -> str:
    post_id = item["post_id"]
    pills = "".join(
        f'<span class="pill">{escape(label)}</span>' for key, label in _LABEL_CHIPS if item.get(key)
    )
    pills += f'<span class="pill gray">{_cell(item.get("post_type"))}</span>'
    snippet = escape(_text(item.get("enrichment_evidence_snippet"))) or "（本条无依据片段）"
    version_count = int(cast(int, item.get("version_count") or 1))
    changed = version_count > 1
    obs_count = int(cast(int, item.get("current_version_observation_count") or 1))
    change = (
        f"检出编辑 · 共 {version_count} 个观察版本"
        if changed
        else f"暂无变动 · 首个入库版本 · 已观察 {obs_count} 次"
    )
    # A pinned post can lack a full version (e.g. preview-only); keep it visible
    # with an explicit placeholder rather than an empty body.
    current_text = escape(_text(item.get("current_text"))) or "（暂无完整正文版本）"
    title = _cell(_post_title(item))
    vid = _cell(item.get("current_version_id"))
    platform_post_id = _cell(item.get("platform_post_id"))
    posted_at = _fmt_ts(item.get("posted_at_claimed"))
    first_obs = _fmt_ts(item.get("current_version_first_observed_at"))
    last_obs = _fmt_ts(item.get("current_version_last_observed_at"))
    channel = _text(item.get("latest_evidence_channel"))
    channel_label = {"feed": "feed 轮询", "direct": "直链复查"}.get(channel, "未知来源")
    run_id = _cell(item.get("latest_evidence_run_id"))
    fidelity = _cell(item.get("current_content_fidelity"))
    who = (
        f'<div class="who">{_author_badge(item)}<span class="name">{title}</span>'
        f'<span class="uid">原帖 {platform_post_id} · 发布 {posted_at} · '
        f"本地记录 {post_id} · 版本 {vid}</span></div>"
    )
    obs_span = f"首次 {first_obs}<br>最后 {last_obs}" if changed else (first_obs or last_obs)
    facts = (
        f'<div class="fact"><b>当前版本观察</b>'
        f"<span>{obs_span}</span></div>"
        f'<div class="fact"><b>内容变动</b><span>{escape(change)}</span></div>'
        f'<div class="fact"><b>证据来源</b>'
        f"<span>{escape(channel_label)} run {run_id}<br>fidelity {fidelity}</span></div>"
    )
    if pinned:
        action_form = (
            f'<form method="post" action="/posts/{post_id}/unpin">{_csrf_input(csrf_token)}'
            f'<button class="secondary" type="submit">取消钉住</button></form>'
        )
    else:
        action_form = (
            f'<form method="post" action="/posts/{post_id}/pin">{_csrf_input(csrf_token)}'
            f'<button class="primary" type="submit">钉住当前版本</button></form>'
        )
    return f"""<article class="candidate">
  <div class="chead">
    <div>{who}<div class="meta-row">{pills}</div></div>
    {_queue_status_badge(item)}
  </div>
  <p class="snippet">依据片段「{snippet}」</p>
  <div class="evidence-grid">{facts}</div>
  <div class="qactions">{action_form}
    <a class="secondary" href="/posts/{post_id}">打开证据卡</a>
    {_snowball_user_link(item.get("author_platform_uid"))}
    {_local_author_link(item.get("author_platform_uid"))}
  </div>
  <details><summary>展开原文</summary><pre>{current_text}</pre></details>
</article>"""


def _queue_counts(connection: sqlite3.Connection, prompt_version: str) -> dict[str, int]:
    def scalar(query: str, *params: object) -> int:
        return int(connection.execute(query, params).fetchone()[0])

    pending = scalar(
        """
        SELECT COUNT(*) FROM posts p
        JOIN enrichments e ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE p.watch_mode != ?
          AND (e.label_first_hand_info = 1 OR e.label_transferable_framework = 1
               OR e.label_reasoned_non_consensus = 1)
          AND NOT EXISTS (SELECT 1 FROM attention_log al WHERE al.version_id = p.current_version_id)
        """,
        prompt_version,
        WatchMode.PINNED.value,
    )
    three = scalar(
        """
        SELECT COUNT(*) FROM posts p
        JOIN enrichments e ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE p.watch_mode != ?
          AND e.label_first_hand_info = 1 AND e.label_transferable_framework = 1
          AND e.label_reasoned_non_consensus = 1
          AND NOT EXISTS (SELECT 1 FROM attention_log al WHERE al.version_id = p.current_version_id)
        """,
        prompt_version,
        WatchMode.PINNED.value,
    )
    pinned = scalar("SELECT COUNT(*) FROM posts WHERE watch_mode = ?", WatchMode.PINNED.value)
    absent = scalar(
        "SELECT COUNT(*) FROM posts WHERE feed_state = ?", FeedState.ABSENT_CONFIRMED.value
    )
    return {"pending": pending, "three": three, "pinned": pinned, "absent": absent}


_HOME_NAV = (
    '<nav class="nav"><a href="/">博主最近观点</a>'
    '<a href="/?view=raw">原始时间线</a>'
    '<a href="/?view=filtered">全部过滤流</a>'
    '<a href="/?view=queue">待处理队列</a></nav>'
)


def _queue_toolbar(counts: dict[str, int], *, active: str) -> str:
    """The shared filter row for the queue and the pinned list.

    ``active`` is one of ``pending`` / ``tier3`` / ``pinned``; ``近期缺席`` is a
    plain count with no view of its own. Each chip carries a ``title`` hint so
    its meaning is one mouseover away.
    """

    def chip(name: str, href: str, label: str) -> str:
        cls = "filter" + (" active" if active == name else "")
        return (
            f'<a class="{cls}" href="{href}" title="{escape(_TOOLBAR_HINTS[name])}">'
            f"{escape(label)}</a>"
        )

    return (
        chip("pending", "/?view=queue", f"待处理 {counts['pending']}")
        + chip("tier3", "/?tier=3", f"只看三标签命中 {counts['three']}")
        + chip("pinned", "/?view=pinned", f"已钉住 {counts['pinned']}")
        + f'<span class="toolcount" title="{escape(_TOOLBAR_HINTS["absent"])}">'
        f"近期缺席 {counts['absent']}</span>"
    )


def _home_aside() -> str:
    return _label_guide_panel() + _action_guide_panel() + _audit_panel()


def _queue_html(
    connection: sqlite3.Connection,
    prompt_version: str,
    limit: int,
    csrf_token: str,
    *,
    tier3_only: bool,
) -> str:
    items = list_attention_queue(connection, prompt_version, limit=limit)
    if tier3_only:
        items = [item for item in items if int(cast(int, item.get("tier") or 0)) >= 3]
    counts = _queue_counts(connection, prompt_version)
    cards = "".join(_queue_card(item, csrf_token) for item in items) or (
        "<p>队列为空：当前 prompt 版本下没有未处置的命中标签版本。先运行 "
        "<code>python -m kol_archive enrich</code> 富化，或所有命中都已钉住/写过关注理由。</p>"
    )
    toolbar = _queue_toolbar(counts, active="tier3" if tier3_only else "pending")
    header = (
        '<div class="topbar"><div><h1>KOL 照妖镜 · 待处理注意力</h1>'
        '<p class="muted">命中标签、未钉住、未写关注理由的版本，按 tier（命中标签数）与新观察排序；'
        "不做跨账号排行。原始时间线与全部过滤流一键可达。</p></div>"
        f"{_HOME_NAV}</div>"
    )
    body = (
        f'{header}<div class="toolbar">{toolbar}</div>'
        f'<div class="layout"><section class="panel"><div class="sectit">'
        f"<h2>待处理的高信号版本</h2></div>{cards}</section>"
        f"<aside>{_home_aside()}</aside></div>"
    )
    return _layout("KOL 照妖镜 · 待处理注意力", body)


def _pinned_html(
    connection: sqlite3.Connection,
    prompt_version: str,
    limit: int,
    csrf_token: str,
) -> str:
    items = list_pinned_versions(connection, prompt_version, limit=limit)
    counts = _queue_counts(connection, prompt_version)
    cards = "".join(_queue_card(item, csrf_token, pinned=True) for item in items) or (
        "<p>还没有钉住任何版本。在待处理卡片或证据卡里「钉住」后，会出现在这里长期留观。</p>"
    )
    toolbar = _queue_toolbar(counts, active="pinned")
    header = (
        '<div class="topbar"><div><h1>KOL 照妖镜 · 已钉住</h1>'
        '<p class="muted">已长期留观的版本：它们已离开待处理队列，并豁免随时间被判为停止监控。'
        "在这里复查或「取消钉住」。</p></div>"
        f"{_HOME_NAV}</div>"
    )
    body = (
        f'{header}<div class="toolbar">{toolbar}</div>'
        f'<div class="layout"><section class="panel"><div class="sectit">'
        f"<h2>已钉住的版本</h2></div>{cards}</section>"
        f"<aside>{_home_aside()}</aside></div>"
    )
    return _layout("KOL 照妖镜 · 已钉住", body)


def _percent(value: object) -> str:
    if value is None:
        return "无"
    return f"{float(cast(float, value)) * 100:+.2f}%"


def _market_outcomes_html(viewpoint: dict[str, object]) -> str:
    outcomes = cast(list[dict[str, object]], viewpoint["market_outcomes"])
    if not outcomes:
        return '<p class="muted">尚未提取可证伪命题，暂时无法关联市场变化。</p>'
    rows = []
    for outcome in outcomes:
        ticker = escape(_text(outcome.get("ticker")))
        direction = escape(_text(outcome.get("direction")))
        horizon = outcome.get("horizon_days")
        horizon_text = "未设期限" if horizon is None else f"{horizon} 天"
        if outcome.get("resolved_at") is None:
            result = "等待结果"
        else:
            result = (
                f"标的变化 {_percent(outcome.get('raw_return'))} · "
                f"基准变化 {_percent(outcome.get('benchmark_return'))} · "
                f"超额变化 {_percent(outcome.get('excess_return'))}"
            )
        rows.append(
            f'<div class="market-row"><strong>{ticker} · {direction} · {horizon_text}</strong>'
            f'<span class="muted">{escape(result)}</span></div>'
        )
    return "".join(rows)


def _viewpoint_card(
    viewpoint: dict[str, object],
    *,
    compact: bool = False,
    role: str | None = None,
    show_actions: bool = True,
) -> str:
    post_id = viewpoint["post_id"]
    text = escape(_text(viewpoint.get("current_text")))
    snippet = escape(_text(viewpoint.get("enrichment_evidence_snippet"))) or "（无依据片段）"
    original_link = _original_post_link(viewpoint.get("url"))
    heading = f"{escape(role)} · " if role else ""
    actions = (
        f'<div class="link-row">{original_link}'
        f'<a class="secondary" href="/posts/{post_id}">打开证据卡</a></div>'
        if show_actions
        else ""
    )
    details = (
        f"<details><summary>展开原文</summary><pre>{text}</pre></details>"
        if compact
        else f"<pre>{text}</pre>"
    )
    return f"""<article class="viewpoint">
  <div class="sectit"><h3>{heading}<a href="/posts/{post_id}">{escape(_post_title(viewpoint))}</a>
  </h3><span class="muted">发布 {_fmt_ts(viewpoint.get("viewpoint_at"))}</span></div>
  <p class="snippet">观点依据「{snippet}」</p>
  {_market_outcomes_html(viewpoint)}
  {actions}
  {details}
</article>"""


def _viewpoint_cluster_card(cluster: dict[str, object]) -> str:
    viewpoints = cast(list[dict[str, object]], cluster["viewpoints"])
    statements = []
    for index, viewpoint in enumerate(viewpoints):
        text = _text(viewpoint.get("current_text")).lstrip()
        if text.startswith("回复"):
            role = "相关回复"
        elif index == len(viewpoints) - 1:
            role = "首次记录"
        else:
            role = "强化或更新"
        statements.append(_viewpoint_card(viewpoint, compact=True, role=role, show_actions=False))
    ticker = cluster.get("ticker")
    grouping = (
        f"依据原帖或转发原帖中的明确证券代码 {_cell(ticker)}，按 7 天连续强化周期聚合。"
        if ticker
        else "已有可证伪市场命题，暂未归并到单一证券代码。"
    )
    latest_snippet = escape(_text(viewpoints[0].get("enrichment_evidence_snippet")))
    return f"""<section class="panel">
  <div class="sectit"><h2>{escape(_text(cluster["title"]))}</h2>
  <span class="chip">{cluster["statement_count"]} 次相关发言</span></div>
  <p class="muted">{escape(grouping)}<br>
  首次记录 {_fmt_ts(cluster.get("first_at"))} · 最近强化 {_fmt_ts(cluster.get("latest_at"))}</p>
  <p class="snippet">最新依据「{latest_snippet}」</p>
  <details><summary>展开 {cluster["statement_count"]} 条相关发言</summary>
  {"".join(statements)}</details>
</section>"""


def _viewpoint_overview_html(
    connection: sqlite3.Connection, prompt_version: str, selected_uid: str | None
) -> str:
    authors = author_viewpoint_overview(connection, prompt_version)
    nav = (
        '<nav class="nav"><a href="/?view=queue">待处理队列</a>'
        '<a href="/?view=raw">原始时间线</a>'
        '<a href="/?view=filtered">全部过滤流</a></nav>'
    )
    header = (
        '<div class="topbar"><div><h1>KOL 照妖镜 · 博主最近观点</h1>'
        '<p class="muted">先选择博主，再查看最近 10 个有明确市场关联的观点及后续变化。</p></div>'
        f"{nav}</div>"
    )
    if not authors:
        return _layout("KOL 照妖镜 · 博主最近观点", f"{header}<p>暂无已监控博主。</p>")
    selected = next(
        (
            author
            for author in authors
            if _text(author.get("author_platform_uid")) == _text(selected_uid)
        ),
        authors[0],
    )
    author_options = []
    for author in authors:
        uid = author["author_platform_uid"]
        active = " active" if author is selected else ""
        author_options.append(
            f'<a class="author-option{active}" href="/?author={quote(_text(uid), safe="")}">'
            f"{_author_badge(author)}"
            f'<span class="muted small">观点发言 {author["viewpoint_count"]} · '
            f"已评估观点 {author['evaluated_viewpoint_count']}</span>"
            "</a>"
        )
    selected_uid_value = selected["author_platform_uid"]
    selected_clusters = author_recent_viewpoint_clusters(
        connection, _text(selected_uid_value), prompt_version, limit=10
    )
    cards = "".join(_viewpoint_cluster_card(cluster) for cluster in selected_clusters)
    if not cards:
        cards = "<p>最近还没有具备明确市场关联的观点发言。</p>"
    market_gate_note = (
        "仅保留含明确 A 股证券代码或已有可证伪市场命题的观点；同一代码在 7 天连续周期内合并展示。"
    )
    selected_panel = f"""<section>
  <div class="sectit"><div>{_author_badge(selected)}</div>
  <div class="link-row">{_local_author_link(selected_uid_value)}
  {_snowball_user_link(selected_uid_value)}</div></div>
  <h2>最近 {len(selected_clusters)} 个观点簇</h2>
  <p class="muted">{market_gate_note}</p>
  {cards}
</section>"""
    body = (
        f'{header}<div class="overview-grid">'
        f'<aside class="author-list" aria-label="博主列表">'
        f'<section class="panel"><h2>选择博主</h2>{"".join(author_options)}</section></aside>'
        f"<div>{selected_panel}</div></div>"
    )
    return _layout("KOL 照妖镜 · 博主最近观点", body)


def _author_html(profile: dict[str, object]) -> str:
    author = cast(dict[str, object], profile["author"])
    posts = cast(list[dict[str, object]], profile["posts"])
    viewpoint_clusters = cast(list[dict[str, object]], profile["viewpoint_clusters"])
    uid = _cell(author.get("author_platform_uid"))
    title = _author_name(author)
    description = escape(_text(author.get("author_description")))
    counts = (
        f"归档 {int(cast(int, author.get('post_count') or 0))} · "
        f"实时 {int(cast(int, author.get('live_post_count') or 0))} · "
        f"钉住 {int(cast(int, author.get('pinned_count') or 0))}"
    )
    posts_html = "".join(_timeline_article(item) for item in posts) or "<p>暂无帖子。</p>"
    viewpoints_html = "".join(_viewpoint_cluster_card(item) for item in viewpoint_clusters) or (
        "<p>最近还没有具备明确市场关联的观点发言。</p>"
    )
    market_gate_note = (
        "仅保留含明确 A 股证券代码或已有可证伪市场命题的观点；同一代码下的多次发言合并展示。"
    )
    snowball_link = _snowball_user_link(author.get("author_platform_uid"))
    body = f"""<p><a href="/">返回博主最近观点</a> · <a href="/?view=queue">待处理队列</a>
· <a href="/?view=raw">原始时间线</a></p>
<section>
  {_author_badge(author)}
  <h1>{escape(title)}</h1>
  <p class="muted">
    uid {uid} · 监控开始 {_fmt_ts(author.get("live_monitoring_started_at"))} · {escape(counts)}
  </p>
  <div class="link-row">{snowball_link}</div>
  <p>{description}</p>
</section>
<section>
  <h2>最近 10 个观点簇与市场变化</h2>
  <p class="muted">{market_gate_note}</p>
  {viewpoints_html}
</section>
<section>
  <h2>最近帖子</h2>
  {posts_html}
</section>"""
    return _layout(f"作者 {title}", body)


def _version_sections(versions: list[dict[str, object]]) -> str:
    blocks = []
    for version in versions:
        diff = version.get("diff_from_prior_observed_version")
        blocks.append(
            f"""<article>
  <h3>观察版本 {version["version_id"]}</h3>
  <p class="muted">首次观察：{_cell(version.get("first_observed_at"))}<br>
  最后观察：{_cell(version.get("last_observed_at"))}</p>
  <pre>{_cell(version.get("content_text"))}</pre>
  <h3>相对上一观察版本 diff</h3>
  <pre>{_cell(diff) if diff else "首个观察版本"}</pre>
</article>"""
        )
    return "".join(blocks) or "<p>暂无完整正文版本。</p>"


def _post_html(card: dict[str, Any], csrf_token: str) -> str:
    post = cast(dict[str, object], card["post"])
    status = cast(dict[str, str], post["status"])
    post_id = int(str(post["id"]))
    original_link = _original_post_link(post.get("url"))
    links = "".join(
        [
            original_link,
            _snowball_user_link(post.get("author_platform_uid")),
            _local_author_link(post.get("author_platform_uid")),
        ]
    )
    links_html = f'\n  <div class="link-row">{links}</div>' if links else ""
    title = _post_title(cast(dict[str, object], {**post, "post_id": post_id}))
    identity = escape(_post_identity(cast(dict[str, object], {**post, "post_id": post_id})))
    version_id = post.get("current_version_id")
    version_input = (
        f'<input type="hidden" name="version_id" value="{escape(str(version_id))}">'
        if version_id is not None
        else ""
    )
    rewrite_form = (
        f"""<form method="post" action="/posts/{post_id}/rewrite">
  {_csrf_input(csrf_token)}{version_input}
  <button type="submit">生成单条改写训练</button>
</form>"""
        if version_id is not None
        else "<p>当前帖子没有完整正文版本，无法创建关注理由或改写训练。</p>"
    )
    attention_form = (
        f"""<form method="post" action="/posts/{post_id}/attention">
  {_csrf_input(csrf_token)}{version_input}
  <label>关注理由<textarea name="reason" required></textarea></label>
  <label>我的预期<textarea name="expectation"></textarea></label>
  <button type="submit">记录关注理由并钉住</button>
</form>"""
        if version_id is not None
        else ""
    )
    verdict_options = "".join(
        f'<option value="{value}">{value}</option>' for value in REWRITE_VERDICTS
    )
    verdict_forms = "".join(
        f"""<form method="post" action="/rewrite-exercises/{item["rewrite_exercise_id"]}/verdict">
  {_csrf_input(csrf_token)}
  <input type="hidden" name="post_id" value="{post_id}">
  <select name="verdict">{verdict_options}</select>
  <button type="submit">记录 verdict</button>
</form>"""
        for item in cast(list[dict[str, object]], card["rewrite_exercises"])
    )
    versions = cast(list[dict[str, object]], card["versions"])
    feed_observations = cast(list[dict[str, object]], card["feed_observations"])
    direct_probes = cast(list[dict[str, object]], card["direct_probes"])
    events = cast(list[dict[str, object]], card["events"])
    attention_log = cast(list[dict[str, object]], card["attention_log"])
    rewrite_exercises = cast(list[dict[str, object]], card["rewrite_exercises"])
    enrichments = cast(list[dict[str, object]], card["enrichments"])
    body = f"""<p><a href="/">返回博主最近观点</a> · <a href="/?view=queue">待处理队列</a></p>
<h1>证据卡片：{escape(title)}</h1>
<section>
  {_author_badge(post)}
  <p class="muted">{identity}</p>
  <p>{escape(status["human_label"])}</p>{links_html}
  <p>{escape(status["deletion_signal_label"])}</p>
  <p class="muted">首次观察：{_cell(post.get("first_seen_at"))}<br>
  最后在场观察：{_cell(post.get("last_present_at"))}<br>
  当前版本首次观察：{_cell(post.get("current_version_first_observed_at"))}<br>
  当前版本最后观察：{_cell(post.get("current_version_last_observed_at"))}<br>
  检测到缺失：{_cell(post.get("last_feed_absence_detected_at"))}</p>
</section>
<section>
  <h2>操作</h2>
  <div class="actions">
    <form method="post" action="/posts/{post_id}/pin">
      {_csrf_input(csrf_token)}<button type="submit">钉住</button>
    </form>
    <form method="post" action="/posts/{post_id}/unpin">
      {_csrf_input(csrf_token)}<button type="submit">取消钉住</button>
    </form>
  </div>
  {attention_form}
  {rewrite_form}
</section>
<section><h2>观察版本</h2>{_version_sections(versions)}</section>
<section><h2>feed 观察</h2>{_table(feed_observations)}</section>
<section><h2>直链复查</h2>{_table(direct_probes)}</section>
<section><h2>状态变迁</h2>{_table(events)}</section>
<section><h2>关注理由</h2>{_table(attention_log)}</section>
<section><h2>改写训练</h2>{_table(rewrite_exercises)}{verdict_forms}</section>
<section><h2>LLM 富化标签</h2>{_table(enrichments)}</section>"""
    return _layout(f"证据卡片 {title}", body)


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    server: ArchiveHttpServer

    def log_message(self, format_string: str, *args: object) -> None:
        LOGGER.info("web request " + format_string, *args)

    def _render_home(self, connection: sqlite3.Connection, query: str) -> str:
        view = self._query_value(query, "view")
        prompt_version = self.server.enrich_prompt_version
        limit = self.server.timeline_limit
        if view == "raw":
            return _timeline_html(connection, limit)
        if view == "filtered":
            return _filtered_timeline_html(connection, prompt_version, limit)
        if view == "pinned":
            return _pinned_html(connection, prompt_version, limit, self.server.csrf_token)
        if view == "queue" or self._query_value(query, "tier") == "3":
            return _queue_html(
                connection,
                prompt_version,
                limit,
                self.server.csrf_token,
                tier3_only=self._query_value(query, "tier") == "3",
            )
        return _viewpoint_overview_html(
            connection, prompt_version, self._query_value(query, "author")
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._with_connection(
                    lambda connection: self._send_html(
                        HTTPStatus.OK, self._render_home(connection, parsed.query)
                    )
                )
                return
            author_uid = self._author_uid(path)
            if author_uid is not None:
                self._with_connection(
                    lambda connection: self._send_html(
                        HTTPStatus.OK,
                        _author_html(
                            author_profile(
                                connection,
                                author_uid,
                                prompt_version=self.server.enrich_prompt_version,
                            )
                        ),
                    )
                )
                return
            post_id = self._post_id(path, suffix="")
            if post_id is not None:
                self._with_connection(
                    lambda connection: self._send_html(
                        HTTPStatus.OK,
                        _post_html(
                            build_evidence_card(connection, post_id),
                            self.server.csrf_token,
                        ),
                    )
                )
                return
        except ValueError as error:
            self._send_text(HTTPStatus.NOT_FOUND, redact_text(str(error)))
            return
        except Exception as error:
            LOGGER.error("web read failed type=%s", type(error).__name__)
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, "页面读取失败。")
            return
        if self._is_mutation_path(path):
            self._send_text(HTTPStatus.METHOD_NOT_ALLOWED, "状态修改只接受 POST 请求。")
            return
        self._send_text(HTTPStatus.NOT_FOUND, "页面不存在。")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        form = self._read_form()
        if form is None:
            return
        if not self._valid_csrf(form):
            self._send_text(HTTPStatus.FORBIDDEN, "CSRF token 校验失败。")
            return
        try:
            routes: list[tuple[str, Callable[[int, dict[str, list[str]]], None]]] = [
                ("/pin", self._pin),
                ("/unpin", self._unpin),
                ("/attention", self._attention),
                ("/rewrite", self._rewrite),
            ]
            for suffix, action in routes:
                post_id = self._post_id(path, suffix=suffix)
                if post_id is not None:
                    action(post_id, form)
                    return
            exercise_id = self._exercise_id(path)
            if exercise_id is not None:
                self._verdict(exercise_id, form)
                return
        except ValueError as error:
            self._send_text(HTTPStatus.BAD_REQUEST, redact_text(str(error)))
            return
        except Exception as error:
            LOGGER.error("web action failed type=%s", type(error).__name__)
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, "操作失败。")
            return
        self._send_text(HTTPStatus.NOT_FOUND, "页面不存在。")

    def _with_connection(self, callback: Callable[[sqlite3.Connection], object]) -> None:
        connection = connect_database(self.server.db_path)
        try:
            callback(connection)
        finally:
            connection.close()

    def _with_archive(self, callback: Callable[[Archive], object]) -> None:
        self._with_connection(lambda connection: callback(Archive(connection)))

    def _pin(self, post_id: int, form: dict[str, list[str]]) -> None:
        del form
        self._with_archive(
            lambda archive: archive.pin_post(post_id, datetime.now(tz=UTC).isoformat())
        )
        self._redirect_post(post_id)

    def _unpin(self, post_id: int, form: dict[str, list[str]]) -> None:
        del form
        now = datetime.now(tz=UTC)
        self._with_archive(
            lambda archive: archive.unpin_post_for_window(
                post_id,
                now.isoformat(),
                (now - timedelta(days=self.server.window_days)).isoformat(),
            )
        )
        self._redirect_post(post_id)

    def _attention(self, post_id: int, form: dict[str, list[str]]) -> None:
        reason = self._required_form_value(form, "reason")
        expectation = self._form_value(form, "expectation")
        version_id = self._version_id(form)
        self._with_archive(
            lambda archive: archive.add_attention(
                post_id,
                version_id,
                datetime.now(tz=UTC).isoformat(),
                reason,
                expectation,
            )
        )
        self._redirect_post(post_id)

    def _rewrite(self, post_id: int, form: dict[str, list[str]]) -> None:
        version_id = self._version_id(form)
        # Allow ignored local LLM settings to take effect without restarting the web server.
        config = load_config(self.server.config_dir)

        def rewrite(archive: Archive) -> None:
            settings = load_rewrite_settings(config)
            source = archive.rewrite_source(post_id, version_id)
            suggestion = request_rewrite(settings, source.original_text)
            archive.add_rewrite_exercise(
                source,
                suggestion.rewritten_claim,
                suggestion.rationale,
                settings.model,
                settings.prompt_version,
                datetime.now(tz=UTC).isoformat(),
            )

        self._with_archive(rewrite)
        self._redirect_post(post_id)

    def _verdict(self, exercise_id: int, form: dict[str, list[str]]) -> None:
        verdict = self._required_form_value(form, "verdict")
        post_id = int(self._required_form_value(form, "post_id"))
        self._with_archive(lambda archive: archive.review_rewrite_exercise(exercise_id, verdict))
        self._redirect_post(post_id)

    def _read_form(self) -> dict[str, list[str]] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_text(HTTPStatus.BAD_REQUEST, "请求体长度无效。")
            return None
        if not 0 <= length <= MAX_FORM_BYTES:
            self._send_text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体过大。")
            return None
        try:
            return parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            self._send_text(HTTPStatus.BAD_REQUEST, "表单编码无效。")
            return None

    def _valid_csrf(self, form: dict[str, list[str]]) -> bool:
        token = self._form_value(form, "csrf_token")
        return (
            token is not None
            and token.isascii()
            and self.server.csrf_token.isascii()
            and secrets.compare_digest(token, self.server.csrf_token)
        )

    @staticmethod
    def _query_value(query: str, key: str) -> str | None:
        values = parse_qs(query).get(key)
        return None if not values else values[0].strip() or None

    @staticmethod
    def _form_value(form: dict[str, list[str]], key: str) -> str | None:
        values = form.get(key)
        return None if not values else values[0].strip() or None

    def _required_form_value(self, form: dict[str, list[str]], key: str) -> str:
        value = self._form_value(form, key)
        if value is None:
            raise ValueError(f"missing form field: {key}")
        return value

    def _version_id(self, form: dict[str, list[str]]) -> int:
        return int(self._required_form_value(form, "version_id"))

    @staticmethod
    def _author_uid(path: str) -> str | None:
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "authors" or not parts[1]:
            return None
        return unquote(parts[1])

    @staticmethod
    def _post_id(path: str, *, suffix: str) -> int | None:
        parts = path.strip("/").split("/")
        expected = ["posts", "", *([suffix.strip("/")] if suffix else [])]
        if len(parts) != len(expected) or parts[0] != "posts":
            return None
        if suffix and parts[2] != suffix.strip("/"):
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    @staticmethod
    def _exercise_id(path: str) -> int | None:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "rewrite-exercises" or parts[2] != "verdict":
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    @staticmethod
    def _is_mutation_path(path: str) -> bool:
        return any(
            path.endswith(suffix)
            for suffix in ("/pin", "/unpin", "/attention", "/rewrite", "/verdict")
        )

    def _redirect_post(self, post_id: int) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/posts/{quote(str(post_id))}")
        self.end_headers()

    def _send_html(self, status: HTTPStatus, html: str) -> None:
        self._send_bytes(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _send_text(self, status: HTTPStatus, text: str) -> None:
        self._send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
