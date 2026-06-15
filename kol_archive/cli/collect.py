"""Collection commands: login, run-once, backfill, and run-health alerting."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from kol_archive.alerts import load_alert_settings, record_run_health
from kol_archive.browser import (
    DEFAULT_CDP_URL,
    DEFAULT_LANDING_URL,
    DEFAULT_PROFILE_DIR,
    BrowserError,
    create_xueqiu_browser_client,
    start_dedicated_browser,
)
from kol_archive.collector import CollectorSettings, XueqiuCollector, create_xueqiu_client
from kol_archive.config import load_config, resolve_cookie
from kol_archive.database import connect_database, initialize_database
from kol_archive.maintenance import create_verified_backup
from kol_archive.models import ArchiveSettings
from kol_archive.notifications import (
    NotificationPayload,
    load_notification_settings,
    send_notification,
)
from kol_archive.service import Archive
from kol_archive.time import parse_utc_timestamp
from kol_archive.watchlist import (
    mark_watchlist_alert_sent,
    pending_watchlist_alerts,
    stage_watchlist_alerts,
    watchlist_match_link,
    watchlist_match_title,
)

from .common import (
    backup_retention_count,
    connect_existing_archive,
    init_db,
    resolve_db_path,
    section,
)

LOGGER = logging.getLogger(__name__)


class RunLockError(RuntimeError):
    """Raised when another process already holds the run-once lock."""


# OS-level advisory locks auto-release when the holding process exits, so a crashed
# run-once never strands the lock the way an existence-based lock file would.
if sys.platform == "win32":
    import msvcrt

    def _acquire_run_lock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _release_run_lock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire_run_lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release_run_lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _run_once_file_lock(db_path: Path) -> Iterator[None]:
    """Cross-process mutex so scheduled and web-triggered run-once never overlap.

    The lock sits next to the archive because a single run-once shares that SQLite
    file, the dedicated CDP page and the backup directory — none of which tolerate a
    concurrent writer, whether it comes from this process or another.
    """
    lock_path = db_path.parent / "run-once.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            _acquire_run_lock(handle)
        except OSError as error:
            raise RunLockError("另一处 run-once 采集正在进行（已被其他进程占用）。") from error
        try:
            yield
        finally:
            _release_run_lock(handle)
    finally:
        handle.close()


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


def _collection_failure_reason(db_path: Path, started_at: str) -> str | None:
    if not db_path.is_file():
        return "run-once 执行失败"
    connection = connect_database(db_path)
    try:
        row = connection.execute(
            """
            SELECT
                EXISTS(
                    SELECT 1 FROM fetch_runs
                    WHERE started_at >= ? AND login_state = 'expired'
                    UNION ALL
                    SELECT 1 FROM probe_runs
                    WHERE started_at >= ? AND login_state = 'expired'
                ) AS has_expired_login,
                EXISTS(
                    SELECT 1 FROM fetch_runs
                    WHERE started_at >= ?
                      AND (
                        status = 'failed' OR login_state != 'valid'
                        OR rate_limited = 1 OR http_error_count > 0
                      )
                    UNION ALL
                    SELECT 1 FROM probe_runs
                    WHERE started_at >= ?
                      AND (status = 'failed' OR login_state != 'valid' OR rate_limited = 1)
                ) AS has_failure
            """,
            (started_at, started_at, started_at, started_at),
        ).fetchone()
    finally:
        connection.close()
    if row is not None and bool(row["has_expired_login"]):
        return "登录状态连续失效"
    if row is not None and bool(row["has_failure"]):
        return "run-once 连续失败"
    return None


def _record_run_health_safely(
    config: dict[str, Any],
    *,
    healthy: bool,
    reason: str | None,
) -> None:
    try:
        alert_settings = load_alert_settings(config)
        try:
            notification_settings = load_notification_settings(config)
        except Exception:
            LOGGER.warning("notification configuration invalid")
            notification_settings = None
        record_run_health(
            alert_settings,
            healthy=healthy,
            reason=reason,
            private_link=(
                "" if notification_settings is None else notification_settings.private_base_url
            ),
            notify=lambda payload: (
                False
                if notification_settings is None
                else send_notification(notification_settings, payload)
            ),
        )
    except Exception:
        LOGGER.warning("run health state update failed")


def run_once(conf_dir: Path) -> None:
    _run_once_with_config(load_config(conf_dir))


def _run_once_with_config(config: dict[str, Any]) -> None:
    storage = section(config, "storage")
    monitoring = section(config, "monitoring")
    polling = section(config, "polling")
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
            retention_count=backup_retention_count(storage),
        )
        LOGGER.info(
            "verified snapshot created path=%s removed_snapshots=%s",
            result.snapshot_path,
            len(result.removed_snapshots),
        )


@dataclass(frozen=True)
class RunOnceResult:
    """Outcome of a completed run-once pass (collection did not hard-fail)."""

    healthy: bool
    reason: str | None


def execute_run_once(conf_dir: Path) -> RunOnceResult:
    """Run the full run-once pass: collect, evaluate health, alert, record state.

    Shared by the CLI ``run-once`` command and the web "立即采集" button. Hard
    failures (e.g. :class:`BrowserError`) record unhealthy state and re-raise; a
    pass that finishes with degraded runs returns ``healthy=False`` plus a reason.
    """
    config = load_config(conf_dir)
    db_path = resolve_db_path(None, config)
    with _run_once_file_lock(db_path):
        started_at = datetime.now(tz=UTC).isoformat()
        try:
            _run_once_with_config(config)
        except Exception as error:
            reason = "CDP 连接失败" if isinstance(error, BrowserError) else "run-once 执行失败"
            _record_run_health_safely(config, healthy=False, reason=reason)
            raise
        collection_reason = _collection_failure_reason(db_path, started_at)
        _send_watchlist_alerts(config, db_path, started_at)
        _record_run_health_safely(
            config, healthy=collection_reason is None, reason=collection_reason
        )
        return RunOnceResult(healthy=collection_reason is None, reason=collection_reason)


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
    storage = section(config, "storage")
    polling = section(config, "polling")
    monitoring = section(config, "monitoring")
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


def _send_watchlist_alerts(config: dict[str, Any], db_path: Path, observed_since: str) -> None:
    try:
        settings = load_notification_settings(config)
        if not settings.enabled:
            return
        if not os.environ.get(settings.webhook_url_env):
            LOGGER.warning("watchlist alert processing skipped: notification credential missing")
            return
        connection: sqlite3.Connection
        connection, _ = connect_existing_archive(db_path)
        try:
            stage_watchlist_alerts(
                connection,
                observed_since,
                datetime.now(tz=UTC).isoformat(),
            )
            for match in pending_watchlist_alerts(connection):
                try:
                    sent = send_notification(
                        settings,
                        NotificationPayload(
                            title=watchlist_match_title(match),
                            count=1,
                            link=watchlist_match_link(settings.private_base_url, match),
                        ),
                    )
                except Exception:
                    LOGGER.warning("watchlist notification failed alert_id=%s", match.alert_id)
                    continue
                if sent:
                    mark_watchlist_alert_sent(
                        connection,
                        match.alert_id,
                        datetime.now(tz=UTC).isoformat(),
                    )
        finally:
            connection.close()
    except Exception:
        LOGGER.warning("watchlist alert processing failed")


def _init_db_command(args: argparse.Namespace) -> None:
    init_db(args.path)


def _login_command(args: argparse.Namespace) -> None:
    run_login(args.config_dir, uid=args.uid, minimized=args.minimized)


def _run_once_command(args: argparse.Namespace) -> None:
    try:
        execute_run_once(args.config_dir)
    except RunLockError as error:
        # A scheduled run and a manual/web run colliding is expected concurrency, not a
        # failure: skip cleanly so Task Scheduler doesn't flag the run as errored.
        LOGGER.warning("run-once skipped: %s", error)
        print(f"# 跳过：{error}")


def _backfill_command(args: argparse.Namespace) -> None:
    run_backfill(args.config_dir, uid=args.uid, pages=args.pages, until=args.until)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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
