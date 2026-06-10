"""Command-line entry points for the local archive."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from kol_archive.browser import (
    DEFAULT_CDP_URL,
    DEFAULT_LANDING_URL,
    DEFAULT_PROFILE_DIR,
    create_xueqiu_browser_client,
    start_dedicated_browser,
)
from kol_archive.collector import (
    HEADERS,
    CollectorSettings,
    XueqiuCollector,
    create_xueqiu_client,
)
from kol_archive.config import load_config, resolve_cookie
from kol_archive.database import connect_database, initialize_database
from kol_archive.enrich import load_enrich_settings, request_enrichment
from kol_archive.image_enrich import load_vision_settings, run_image_enrichment
from kol_archive.images import ImageDownloader, ImageDownloadSettings
from kol_archive.kline import (
    DEFAULT_BAR_COUNT,
    discover_tickers,
    fetch_and_store,
    validated_symbol,
)
from kol_archive.maintenance import (
    create_verified_backup,
    export_archive,
    restore_backup,
    verify_backup,
)
from kol_archive.models import ArchiveSettings, QueueReason
from kol_archive.ocr import run_ocr, select_engine
from kol_archive.presentation import (
    author_scorecards,
    build_evidence_card,
    list_attention_queue,
    list_filtered_timeline,
    list_timeline,
)
from kol_archive.prices import import_prices_csv, import_ticker_names_csv
from kol_archive.rewrite import load_rewrite_settings, request_rewrite
from kol_archive.service import Archive
from kol_archive.time import parse_utc_timestamp
from kol_archive.web import load_web_settings, serve_archive

LOGGER = logging.getLogger(__name__)


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(path)
    try:
        initialize_database(connection)
    finally:
        connection.close()


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _backup_retention_count(storage: dict[str, Any]) -> int:
    value = storage.get("backup_retention_count")
    return 30 if value is None else int(value)


def _db_path_from_config(config: dict[str, Any]) -> Path:
    storage = _section(config, "storage")
    return Path(str(storage.get("db_path") or "data/kol.sqlite3"))


def _browser_section(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("browser") or {}
    if not isinstance(value, dict):
        raise ValueError("browser must be a mapping")
    return value


def _backfill_section(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("backfill") or {}
    if not isinstance(value, dict):
        raise ValueError("backfill must be a mapping")
    return value


def _build_collector(archive: Archive, client: Any, polling: dict[str, Any]) -> XueqiuCollector:
    return XueqiuCollector(
        archive,
        client,
        CollectorSettings(
            request_min_interval_seconds=float(polling.get("request_min_interval_seconds") or 2.5),
            request_jitter_seconds=float(polling.get("request_jitter_seconds") or 1.5),
            max_feed_pages=int(polling.get("max_feed_pages", 20)),
        ),
    )


def _build_collector_client(config: dict[str, Any]) -> Any:
    """Pick the data path per config: dedicated browser (CDP) or httpx direct."""
    browser = _browser_section(config)
    if bool(browser.get("enabled", True)):
        cdp_url = str(browser.get("cdp_url") or DEFAULT_CDP_URL)
        landing_url = str(browser.get("landing_url") or DEFAULT_LANDING_URL)
        LOGGER.info("data path=browser cdp_url=%s", cdp_url)
        return create_xueqiu_browser_client(cdp_url, landing_url=landing_url)
    cookie, cookie_source = resolve_cookie(config)
    LOGGER.info("data path=httpx credential source=%s present=%s", cookie_source, bool(cookie))
    return create_xueqiu_client(cookie)


def _resolve_db_path(path: Path | None, config: dict[str, Any]) -> Path:
    return _db_path_from_config(config) if path is None else path


def _configured_db_path(path: Path | None, config_dir: Path) -> Path:
    return path if path is not None else _db_path_from_config(load_config(config_dir))


def _connect_existing_archive(path: Path) -> tuple[sqlite3.Connection, Archive]:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite archive does not exist: {path}")
    connection = connect_database(path)
    initialize_database(connection)
    return connection, Archive(connection)


def _configure_stdout_utf8() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        # CLI JSON stays UTF-8 for Windows consoles and downstream redirection.
        reconfigure(encoding="utf-8")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _current_version_id(archive: Archive, post_id: int, version_id: int | None) -> int:
    return archive.current_version_id(post_id) if version_id is None else version_id


def run_once(conf_dir: Path) -> None:
    config = load_config(conf_dir)
    storage = _section(config, "storage")
    monitoring = _section(config, "monitoring")
    polling = _section(config, "polling")
    db_path = Path(str(storage.get("db_path") or "data/kol.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(db_path)
    initialize_database(connection)
    archive = Archive(
        connection,
        ArchiveSettings(
            absent_threshold_n=int(monitoring.get("absent_threshold_n") or 3),
            recent_feed_absent_ttl_days=int(monitoring.get("recent_feed_absent_ttl_days") or 7),
        ),
    )
    backfill = _backfill_section(config)
    backfill_on_add = bool(backfill.get("on_add_enabled", True))
    on_add_pages_value = backfill.get("on_add_pages")
    on_add_pages = 5 if on_add_pages_value is None else int(on_add_pages_value)
    # 0 explicitly disables auto-backfill; a negative count is a misconfiguration, not a
    # silent off switch (mirrors backfill_feed's max_pages validation).
    if on_add_pages < 0:
        raise ValueError("backfill.on_add_pages must not be negative (use 0 to disable)")
    client = None
    try:
        client = _build_collector_client(config)
        collector = _build_collector(archive, client, polling)
        now = datetime.now(tz=UTC)
        archive.expire_rechecks(now.isoformat())
        window_started_at = (
            now - timedelta(days=int(monitoring.get("window_days") or 30))
        ).isoformat()
        accounts = config.get("accounts") or []
        if not accounts:
            raise ValueError("at least one account must be configured")
        want_backfill = backfill_on_add and on_add_pages > 0
        feed_blocked = False
        for account in accounts:
            uid = str(account.get("uid") or "").strip()
            if not uid:
                continue
            note = str(account.get("note") or "") or None
            author_id = archive.ensure_author("xueqiu", uid, now.isoformat(), note)
            previous_covered_to = archive.last_live_covered_to(author_id)
            live_run_id = collector.poll_feed(
                author_id, uid, window_started_at, previous_covered_to=previous_covered_to
            )
            # If the live poll hit rate limiting / login expiry / transport errors, the
            # session as a whole has hit a wall — every later request shares the same
            # cookies and endpoint host, so stop the account loop (and, below, skip the
            # shared probe pass) instead of marching on to the next account.
            if archive.feed_run_blocked(live_run_id):
                feed_blocked = True
                break
            # Pull a few pages of history beyond the live window so the archive has a
            # baseline (recorded as ingest_mode=backfill). Page past the live run's last
            # page so we reach genuinely older posts instead of re-requesting the recent
            # window, and keep retrying on later runs until the baseline reaches its
            # planned depth — a rate-limited or failed first attempt must not leave the
            # account un-backfilled.
            #
            # Only resume from a parse-clean live run: a degraded last page may not be the
            # real end of the timeline, so its page count would start the backfill on an
            # out-of-range page. Skip and leave the baseline pending for a later clean run.
            if (
                want_backfill
                and archive.baseline_backfill_pending(author_id)
                and archive.feed_run_parse_clean(live_run_id)
            ):
                backfill_run_id = collector.backfill_feed(
                    author_id,
                    uid,
                    max_pages=on_add_pages,
                    start_page=archive.feed_run_pages(live_run_id) + 1,
                )
                # The backfill hits the same endpoint; if it tripped the wall, treat the
                # session as blocked too — stop the loop and skip the probe pass.
                if archive.feed_run_blocked(backfill_run_id):
                    feed_blocked = True
                    break
        # Direct-link probes hit the same host and session as the feed; if any feed poll
        # was blocked this run, skip the probe pass too (same rule as the backfill gate).
        if not feed_blocked:
            collector.probe_due_posts()
    finally:
        if client is not None:
            client.close()
        connection.close()
    if bool(storage.get("backup_after_run", True)):
        result = create_verified_backup(
            db_path,
            Path(str(storage.get("backup_dir") or "data/backups")),
            retention_count=_backup_retention_count(storage),
        )
        LOGGER.info(
            "verified snapshot created path=%s removed_snapshots=%s",
            result.snapshot_path,
            len(result.removed_snapshots),
        )


def run_backfill(
    conf_dir: Path,
    *,
    uid: str,
    pages: int | None,
    until: str | None,
) -> None:
    """Manually pull deeper history for one account (ingest_mode=backfill)."""
    if not uid.strip():
        raise ValueError("uid must not be empty")
    config = load_config(conf_dir)
    storage = _section(config, "storage")
    polling = _section(config, "polling")
    monitoring = _section(config, "monitoring")
    backfill = _backfill_section(config)
    command_pages_value = backfill.get("command_pages")
    command_pages = 10 if command_pages_value is None else int(command_pages_value)
    max_pages = pages if pages is not None else command_pages
    # Validate inputs before any side effects (DB creation, client build, author insert).
    # backfill_feed re-checks these, but only after we have already touched the filesystem.
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    if until is not None:
        parse_utc_timestamp(until)
    db_path = Path(str(storage.get("db_path") or "data/kol.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(db_path)
    initialize_database(connection)
    archive = Archive(
        connection,
        ArchiveSettings(
            absent_threshold_n=int(monitoring.get("absent_threshold_n") or 3),
            recent_feed_absent_ttl_days=int(monitoring.get("recent_feed_absent_ttl_days") or 7),
        ),
    )
    client = None
    try:
        client = _build_collector_client(config)
        collector = _build_collector(archive, client, polling)
        author_id = archive.ensure_author("xueqiu", uid.strip(), datetime.now(tz=UTC).isoformat())
        run_id = collector.backfill_feed(author_id, uid.strip(), max_pages=max_pages, until=until)
    finally:
        if client is not None:
            client.close()
        connection.close()
    LOGGER.info(
        "backfill complete uid=%s run_id=%s max_pages=%s until=%s", uid, run_id, max_pages, until
    )


def run_login(conf_dir: Path, *, uid: str | None, minimized: bool) -> None:
    """Launch the dedicated browser so the user can clear the slider / log in once."""
    config = load_config(conf_dir)
    browser = _browser_section(config)
    cdp_url = str(browser.get("cdp_url") or DEFAULT_CDP_URL)
    profile_dir = Path(str(browser.get("profile_dir") or DEFAULT_PROFILE_DIR))
    edge_path = str(browser.get("edge_path") or "") or None
    target_uid = uid or next(
        (str(a.get("uid")).strip() for a in (config.get("accounts") or []) if a.get("uid")),
        None,
    )
    landing = (
        f"https://xueqiu.com/u/{target_uid}"
        if target_uid
        else str(browser.get("landing_url") or DEFAULT_LANDING_URL)
    )
    process = start_dedicated_browser(
        profile_dir=profile_dir,
        cdp_url=cdp_url,
        url=landing,
        edge_path=edge_path,
        minimized=minimized,
    )
    LOGGER.info(
        "dedicated browser launched pid=%s cdp_url=%s profile_dir=%s url=%s",
        process.pid,
        cdp_url,
        profile_dir,
        landing,
    )
    print("# 专用雪球浏览器已启动")
    print(f"- CDP: {cdp_url}")
    print(f"- Profile: {profile_dir}")
    print(f"- 打开页面: {landing}")
    print("- 请在弹出的浏览器窗口里完成登录并手动拖动滑块，直到能看到时间线。")
    print("- 之后保持该窗口开着，运行 `python -m kol_archive run-once` 即可采集。")


def _init_db_command(args: argparse.Namespace) -> None:
    init_db(args.path)


def _login_command(args: argparse.Namespace) -> None:
    run_login(args.config_dir, uid=args.uid, minimized=args.minimized)


def _run_once_command(args: argparse.Namespace) -> None:
    run_once(args.config_dir)


def _backfill_command(args: argparse.Namespace) -> None:
    run_backfill(args.config_dir, uid=args.uid, pages=args.pages, until=args.until)


def _backup_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    storage = _section(config, "storage")
    result = create_verified_backup(
        _resolve_db_path(args.path, config),
        args.backup_dir or Path(str(storage.get("backup_dir") or "data/backups")),
        retention_count=(
            _backup_retention_count(storage)
            if args.retention_count is None
            else args.retention_count
        ),
    )
    LOGGER.info(
        "verified snapshot created path=%s removed_snapshots=%s",
        result.snapshot_path,
        len(result.removed_snapshots),
    )


def _verify_backup_command(args: argparse.Namespace) -> None:
    verify_backup(args.path)
    LOGGER.info("snapshot restore verification passed path=%s", args.path)


def _restore_backup_command(args: argparse.Namespace) -> None:
    restore_backup(args.snapshot_path, args.target_path)
    LOGGER.info("snapshot restored path=%s", args.target_path)


def _export_command(args: argparse.Namespace) -> None:
    result = export_archive(_configured_db_path(args.path, args.config_dir), args.output_dir)
    LOGGER.info("credential-safe export created path=%s", result.bundle_dir)


def _enrich_prompt_version(config: dict[str, Any], override: str | None) -> str:
    if override:
        return override
    llm = config.get("llm") or {}
    if isinstance(llm, dict):
        configured = str(llm.get("enrich_prompt_version") or "").strip()
        if configured:
            return configured
    return "enrich-v2"


def _timeline_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = _connect_existing_archive(_resolve_db_path(args.path, config))
    try:
        if args.filtered:
            prompt_version = _enrich_prompt_version(config, args.prompt_version)
            timeline = list_filtered_timeline(connection, prompt_version, limit=args.limit)
        else:
            timeline = list_timeline(connection, limit=args.limit)
        _print_json(timeline)
    finally:
        connection.close()


def _queue_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = _connect_existing_archive(_resolve_db_path(args.path, config))
    try:
        prompt_version = _enrich_prompt_version(config, args.prompt_version)
        queue = list_attention_queue(connection, prompt_version, limit=args.limit)
        if args.tier3_only:
            queue = [item for item in queue if int(cast(int, item.get("tier") or 0)) >= 3]
        _print_json(queue)
    finally:
        connection.close()


def _scorecards_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = _connect_existing_archive(_resolve_db_path(args.path, config))
    try:
        prompt_version = _enrich_prompt_version(config, args.prompt_version)
        _print_json(author_scorecards(connection, prompt_version))
    finally:
        connection.close()


def _show_post_command(args: argparse.Namespace) -> None:
    connection, _ = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        _print_json(build_evidence_card(connection, args.post_id))
    finally:
        connection.close()


def _pin_command(args: argparse.Namespace) -> None:
    connection, archive = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        reason = None if args.confirm_reason is None else QueueReason(args.confirm_reason)
        archive.pin_post(args.post_id, datetime.now(tz=UTC).isoformat(), confirm_reason=reason)
        _print_json({"post_id": args.post_id, "watch_mode": "pinned"})
    finally:
        connection.close()


def _unpin_command(args: argparse.Namespace) -> None:
    connection, archive = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        if args.window_days < 1:
            raise ValueError("window days must be positive")
        now = datetime.now(tz=UTC)
        archive.unpin_post_for_window(
            args.post_id,
            now.isoformat(),
            (now - timedelta(days=args.window_days)).isoformat(),
        )
        row = connection.execute(
            "SELECT watch_mode FROM posts WHERE id = ?",
            (args.post_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown post id: {args.post_id}")
        _print_json({"post_id": args.post_id, "watch_mode": row["watch_mode"]})
    finally:
        connection.close()


def _add_attention_command(args: argparse.Namespace) -> None:
    connection, archive = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        version_id = _current_version_id(archive, args.post_id, args.version_id)
        attention_id = archive.add_attention(
            args.post_id,
            version_id,
            datetime.now(tz=UTC).isoformat(),
            args.reason,
            args.expectation,
        )
        _print_json(
            {
                "attention_id": attention_id,
                "post_id": args.post_id,
                "version_id": version_id,
                "watch_mode": "pinned",
            }
        )
    finally:
        connection.close()


def _rewrite_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    settings = load_rewrite_settings(config)
    connection, archive = _connect_existing_archive(_resolve_db_path(args.path, config))
    try:
        version_id = _current_version_id(archive, args.post_id, args.version_id)
        source = archive.rewrite_source(args.post_id, version_id)
        suggestion = request_rewrite(settings, source.original_text)
        exercise_id = archive.add_rewrite_exercise(
            source,
            suggestion.rewritten_claim,
            suggestion.rationale,
            settings.model,
            settings.prompt_version,
            datetime.now(tz=UTC).isoformat(),
        )
        _print_json(
            {
                "rewrite_exercise_id": exercise_id,
                "post_id": args.post_id,
                "version_id": version_id,
                "llm_rewritten_claim": suggestion.rewritten_claim,
                "llm_rationale": suggestion.rationale,
                "watch_mode": "pinned",
            }
        )
    finally:
        connection.close()


def _enrich_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    settings = load_enrich_settings(config)
    if args.prompt_version:
        settings = replace(settings, prompt_version=args.prompt_version)
    connection, archive = _connect_existing_archive(_resolve_db_path(args.path, config))
    try:
        targets = archive.enrichment_targets(
            settings.prompt_version, post_id=args.post_id, limit=args.limit
        )
        enriched = skipped = failed = 0
        for target in targets:
            try:
                result = request_enrichment(settings, target.original_text)
            except (httpx.HTTPError, ValueError) as error:
                # One bad version (LLM/network/parse failure) must not abort the
                # batch; it stays pending so a later run retries it.
                failed += 1
                LOGGER.warning("enrichment failed for version %s: %s", target.version_id, error)
                continue
            enrichment_id = archive.add_enrichment(
                target,
                result,
                settings.model,
                settings.prompt_version,
                datetime.now(tz=UTC).isoformat(),
            )
            if enrichment_id is None:
                skipped += 1
            else:
                enriched += 1
        _print_json(
            {
                "prompt_version": settings.prompt_version,
                "model": settings.model,
                "candidates": len(targets),
                "enriched": enriched,
                "skipped": skipped,
                "failed": failed,
            }
        )
    finally:
        connection.close()


def _import_prices_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = _connect_existing_archive(_resolve_db_path(args.path, config))
    try:
        summary = import_prices_csv(connection, args.csv_path)
        _print_json({"rows": summary.rows, "tickers": summary.tickers, "names": summary.names})
    finally:
        connection.close()


def _import_ticker_names_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = _connect_existing_archive(_resolve_db_path(args.path, config))
    try:
        summary = import_ticker_names_csv(connection, args.csv_path)
        _print_json({"rows": summary.rows})
    finally:
        connection.close()


def _images_section(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("images") or {}
    if not isinstance(value, dict):
        raise ValueError("images must be a mapping")
    return value


def _download_images_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    images = _images_section(config)
    cookie, _ = resolve_cookie(config)
    settings = ImageDownloadSettings(
        request_min_interval_seconds=float(images.get("request_min_interval_seconds") or 1.0),
        request_jitter_seconds=float(images.get("request_jitter_seconds") or 1.0),
        max_image_bytes=int(images.get("max_image_bytes") or 8 * 1024 * 1024),
        max_batch_bytes=int(images.get("max_batch_bytes") or 256 * 1024 * 1024),
    )
    connection, archive = _connect_existing_archive(_resolve_db_path(args.path, config))
    # Images are static CDN assets, fetched directly (not through the feed's WAF
    # path); a dead/blocked link is recorded as a failed attempt, not raised.
    client = httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True)
    if cookie:
        client.headers["cookie"] = cookie
    try:
        downloader = ImageDownloader(archive, client, settings)
        row_ids = downloader.download_pending(post_id=args.post_id, limit=args.limit)
        _print_json({"download_attempts": len(row_ids)})
    finally:
        client.close()
        connection.close()


def _fetch_kline_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    prices_config = config.get("prices") or {}
    benchmark = (args.benchmark or str(prices_config.get("benchmark_ticker") or "SH000300")).upper()
    connection, _ = _connect_existing_archive(_resolve_db_path(args.path, config))
    client = None
    try:
        tickers = (
            [validated_symbol(ticker) for ticker in args.ticker]
            if args.ticker
            else discover_tickers(connection)
        )
        # The benchmark must share dates with each asset for the snapshot join, so always
        # pull it alongside whatever assets we fetch.
        if benchmark not in tickers:
            tickers.append(benchmark)
        if not tickers:
            note = "no tracked tickers; pass --ticker"
            _print_json({"tickers": 0, "bars": 0, "failures": [], "note": note})
            return
        client = _build_collector_client(config)
        summary = fetch_and_store(connection, client, tickers, count=args.count)
        _print_json(
            {"tickers": summary.tickers, "bars": summary.bars, "failures": list(summary.failures)}
        )
    finally:
        if client is not None:
            client.close()
        connection.close()


def _ocr_images_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, archive = _connect_existing_archive(_resolve_db_path(args.path, config))
    try:
        engine = select_engine()
        row_ids = run_ocr(archive, engine, post_id=args.post_id, limit=args.limit)
        _print_json(
            {
                "engine": engine.name,
                "engine_version": engine.version,
                "ocr_added": len(row_ids),
            }
        )
    finally:
        connection.close()


def _enrich_images_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    settings = load_vision_settings(config)
    if args.prompt_version:
        settings = replace(settings, prompt_version=args.prompt_version)
    connection, archive = _connect_existing_archive(_resolve_db_path(args.path, config))
    try:
        row_ids = run_image_enrichment(archive, settings, post_id=args.post_id, limit=args.limit)
        _print_json(
            {
                "model": settings.model,
                "prompt_version": settings.prompt_version,
                "described": len(row_ids),
            }
        )
    finally:
        connection.close()


def _review_rewrite_command(args: argparse.Namespace) -> None:
    connection, archive = _connect_existing_archive(_configured_db_path(args.path, args.config_dir))
    try:
        archive.review_rewrite_exercise(args.exercise_id, args.verdict)
        _print_json({"rewrite_exercise_id": args.exercise_id, "my_verdict": args.verdict})
    finally:
        connection.close()


def _serve_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    serve_archive(
        _resolve_db_path(args.path, config),
        args.config_dir,
        load_web_settings(config, bind_host=args.host, port=args.port),
    )


def main() -> None:
    _configure_stdout_utf8()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="KOL evidence archive")
    subparsers = parser.add_subparsers(required=True)
    init_parser = subparsers.add_parser("init-db", help="initialize a SQLite archive")
    init_parser.add_argument("path", type=Path)
    init_parser.set_defaults(handler=_init_db_command)
    login_parser = subparsers.add_parser(
        "login", help="launch the dedicated browser to clear the slider / log in once"
    )
    login_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    login_parser.add_argument(
        "--uid", help="open this account's page (defaults to first configured)"
    )
    login_parser.add_argument("--minimized", action="store_true", help="start the window minimized")
    login_parser.set_defaults(handler=_login_command)
    run_parser = subparsers.add_parser("run-once", help="poll feeds and probe due posts")
    run_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    run_parser.set_defaults(handler=_run_once_command)
    backfill_parser = subparsers.add_parser(
        "backfill", help="pull deeper history for one account (ingest_mode=backfill)"
    )
    backfill_parser.add_argument("--uid", required=True, help="account uid to backfill")
    backfill_parser.add_argument(
        "--pages", type=int, help="max pages to page back (default from config)"
    )
    backfill_parser.add_argument(
        "--until", help="page back until reaching posts at or before this ISO timestamp"
    )
    backfill_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    backfill_parser.set_defaults(handler=_backfill_command)
    backup_parser = subparsers.add_parser("backup", help="create and verify a SQLite snapshot")
    backup_parser.add_argument("--path", type=Path)
    backup_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    backup_parser.add_argument("--backup-dir", type=Path)
    backup_parser.add_argument("--retention-count", type=int)
    backup_parser.set_defaults(handler=_backup_command)
    verify_parser = subparsers.add_parser(
        "verify-backup", help="restore a snapshot temporarily and validate it"
    )
    verify_parser.add_argument("path", type=Path)
    verify_parser.set_defaults(handler=_verify_backup_command)
    restore_parser = subparsers.add_parser("restore-backup", help="restore a verified snapshot")
    restore_parser.add_argument("snapshot_path", type=Path)
    restore_parser.add_argument("target_path", type=Path)
    restore_parser.set_defaults(handler=_restore_backup_command)
    export_parser = subparsers.add_parser("export", help="export credential-safe JSON and CSV")
    export_parser.add_argument("--path", type=Path)
    export_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    export_parser.add_argument("--output-dir", type=Path, default=Path("data/exports"))
    export_parser.set_defaults(handler=_export_command)
    timeline_parser = subparsers.add_parser("timeline", help="show the raw observed timeline")
    timeline_parser.add_argument("--path", type=Path)
    timeline_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    timeline_parser.add_argument("--limit", type=int, default=50)
    timeline_parser.add_argument(
        "--filtered",
        action="store_true",
        help="show only posts whose current version hit an enrichment label",
    )
    timeline_parser.add_argument(
        "--prompt-version", help="enrichment prompt version for --filtered (default from config)"
    )
    timeline_parser.set_defaults(handler=_timeline_command)
    queue_parser = subparsers.add_parser(
        "queue", help="show the pending-attention queue (label hits not yet pinned/reasoned)"
    )
    queue_parser.add_argument("--path", type=Path)
    queue_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    queue_parser.add_argument("--limit", type=int, default=50)
    queue_parser.add_argument(
        "--tier3-only", action="store_true", help="only versions hitting all three labels"
    )
    queue_parser.add_argument(
        "--prompt-version", help="enrichment prompt version (default from config)"
    )
    queue_parser.set_defaults(handler=_queue_command)
    scorecards_parser = subparsers.add_parser(
        "scorecards",
        help="per-author label counts + genre mix (diagnostic summary; no hit-rate, no ranking)",
    )
    scorecards_parser.add_argument("--path", type=Path)
    scorecards_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    scorecards_parser.add_argument(
        "--prompt-version", help="enrichment prompt version (default from config)"
    )
    scorecards_parser.set_defaults(handler=_scorecards_command)
    show_parser = subparsers.add_parser("show-post", help="show one post evidence card")
    show_parser.add_argument("post_id", type=int)
    show_parser.add_argument("--path", type=Path)
    show_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    show_parser.set_defaults(handler=_show_post_command)
    pin_parser = subparsers.add_parser("pin", help="pin one archived post")
    pin_parser.add_argument("post_id", type=int)
    pin_parser.add_argument("--path", type=Path)
    pin_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    pin_parser.add_argument("--confirm-reason", choices=[reason.value for reason in QueueReason])
    pin_parser.set_defaults(handler=_pin_command)
    unpin_parser = subparsers.add_parser("unpin", help="unpin one archived post")
    unpin_parser.add_argument("post_id", type=int)
    unpin_parser.add_argument("--path", type=Path)
    unpin_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    unpin_parser.add_argument("--window-days", type=int, default=30)
    unpin_parser.set_defaults(handler=_unpin_command)
    attention_parser = subparsers.add_parser(
        "add-attention", help="record a reason and pin the selected observed version"
    )
    attention_parser.add_argument("post_id", type=int)
    attention_parser.add_argument("--reason", required=True)
    attention_parser.add_argument("--expectation")
    attention_parser.add_argument("--version-id", type=int)
    attention_parser.add_argument("--path", type=Path)
    attention_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    attention_parser.set_defaults(handler=_add_attention_command)
    rewrite_parser = subparsers.add_parser(
        "rewrite", help="request one LLM rewrite exercise and pin the selected observed version"
    )
    rewrite_parser.add_argument("post_id", type=int)
    rewrite_parser.add_argument("--version-id", type=int)
    rewrite_parser.add_argument("--path", type=Path)
    rewrite_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    rewrite_parser.set_defaults(handler=_rewrite_command)
    enrich_parser = subparsers.add_parser(
        "enrich", help="batch-label observed versions with the LLM (post_type + labels)"
    )
    enrich_parser.add_argument("--post-id", type=int, help="restrict to one post's versions")
    enrich_parser.add_argument("--limit", type=int, help="cap versions labelled this run")
    enrich_parser.add_argument(
        "--prompt-version", help="override llm.enrich_prompt_version for this run"
    )
    enrich_parser.add_argument("--path", type=Path)
    enrich_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    enrich_parser.set_defaults(handler=_enrich_command)
    import_prices_parser = subparsers.add_parser(
        "import-prices", help="import ticker,date,close[,name] CSV rows"
    )
    import_prices_parser.add_argument("csv_path", type=Path)
    import_prices_parser.add_argument("--path", type=Path)
    import_prices_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    import_prices_parser.set_defaults(handler=_import_prices_command)
    import_names_parser = subparsers.add_parser(
        "import-ticker-names", help="import a locally maintained ticker,name CSV"
    )
    import_names_parser.add_argument("csv_path", type=Path)
    import_names_parser.add_argument("--path", type=Path)
    import_names_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    import_names_parser.set_defaults(handler=_import_ticker_names_command)
    fetch_kline_parser = subparsers.add_parser(
        "fetch-kline", help="fetch daily OHLC bars from Xueqiu via the dedicated browser"
    )
    fetch_kline_parser.add_argument(
        "--ticker",
        action="append",
        help="ticker to fetch (repeatable); default: every tracked ticker",
    )
    fetch_kline_parser.add_argument(
        "--benchmark",
        help="benchmark ticker to include (default: prices.benchmark_ticker or SH000300)",
    )
    fetch_kline_parser.add_argument(
        "--count", type=int, default=DEFAULT_BAR_COUNT, help="daily bars to pull per ticker"
    )
    fetch_kline_parser.add_argument("--path", type=Path)
    fetch_kline_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    fetch_kline_parser.set_defaults(handler=_fetch_kline_command)
    download_images_parser = subparsers.add_parser(
        "download-images", help="fetch and store image bytes for archived versions"
    )
    download_images_parser.add_argument("--post-id", type=int, help="restrict to one post")
    download_images_parser.add_argument("--limit", type=int, help="cap images fetched this run")
    download_images_parser.add_argument("--path", type=Path)
    download_images_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    download_images_parser.set_defaults(handler=_download_images_command)
    ocr_images_parser = subparsers.add_parser(
        "ocr-images", help="transcribe stored images (winocr, tesseract fallback)"
    )
    ocr_images_parser.add_argument("--post-id", type=int, help="restrict to one post")
    ocr_images_parser.add_argument("--limit", type=int, help="cap images transcribed this run")
    ocr_images_parser.add_argument("--path", type=Path)
    ocr_images_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    ocr_images_parser.set_defaults(handler=_ocr_images_command)
    enrich_images_parser = subparsers.add_parser(
        "enrich-images", help="describe stored images with a vision model (inference, not evidence)"
    )
    enrich_images_parser.add_argument("--post-id", type=int, help="restrict to one post")
    enrich_images_parser.add_argument("--limit", type=int, help="cap images described this run")
    enrich_images_parser.add_argument(
        "--prompt-version", help="override llm.vision_prompt_version for this run"
    )
    enrich_images_parser.add_argument("--path", type=Path)
    enrich_images_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    enrich_images_parser.set_defaults(handler=_enrich_images_command)
    review_parser = subparsers.add_parser(
        "review-rewrite", help="record a rewrite exercise verdict"
    )
    review_parser.add_argument("exercise_id", type=int)
    review_parser.add_argument("--verdict", choices=["valid", "too_vague", "wrong"], required=True)
    review_parser.add_argument("--path", type=Path)
    review_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    review_parser.set_defaults(handler=_review_rewrite_command)
    serve_parser = subparsers.add_parser("serve", help="serve the local web archive")
    serve_parser.add_argument("--path", type=Path)
    serve_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)
    serve_parser.set_defaults(handler=_serve_command)
    args = parser.parse_args()

    args.handler(args)


if __name__ == "__main__":
    main()
