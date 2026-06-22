"""Collection and enrichment execution: the long-running jobs the web triggers."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx

from kol_archive.config import load_config
from kol_archive.database import connect_database
from kol_archive.enrich import enrich_targets, load_enrich_settings
from kol_archive.maintenance import redact_text
from kol_archive.models import EnrichmentTarget
from kol_archive.obs import http_client, trace_scope
from kol_archive.service import Archive

from .settings import EnrichmentStatus

if TYPE_CHECKING:
    from .settings import ArchiveHttpServer

LOGGER = logging.getLogger("kol_archive.web")


def _set_collection_status(
    server: ArchiveHttpServer, *, running: bool, phase: str, healthy: bool | None
) -> None:
    now = datetime.now(tz=UTC).isoformat()
    with server.collection_status_lock:
        status = server.collection_status
        if running and not status.running:
            status.started_at = now
            status.finished_at = None
            status.logs = []
        status.running = running
        status.phase = phase
        status.updated_at = now
        status.healthy = healthy
        if not status.logs or status.logs[-1]["message"] != phase:
            status.logs.append({"at": now, "message": phase})
        if not running:
            status.finished_at = now


def _start_background_collection(
    server: ArchiveHttpServer,
) -> tuple[dict[str, object], HTTPStatus]:
    """Kick off a collection on a worker thread and return at once.

    The shared collect/enrich lock is the mutex and the idempotency guard: if it
    cannot be taken right now a collection (or enrichment) is already in flight, so
    report 进行中 without starting a second. On success the lock is handed to the
    worker, which releases it when the run ends, while the request returns
    immediately. This is what keeps the multi-minute pass off the request thread so
    the browser never waits it out and aborts the socket mid-write.
    """
    if not server.collect_lock.acquire(blocking=False):
        return {"ok": True, "started": False, "message": "采集正在进行中，请稍候。"}, HTTPStatus.OK
    # Mark running synchronously before the thread starts so an immediate status poll
    # already reflects the run, and a second click arriving before the worker spins
    # up still sees ``running`` and refuses.
    _set_collection_status(server, running=True, phase="正在启动采集", healthy=None)
    thread = threading.Thread(
        target=_collection_worker, args=(server,), name="web-collection", daemon=True
    )
    thread.start()
    return (
        {"ok": True, "started": True, "message": "采集已开始，正在后台运行。"},
        HTTPStatus.ACCEPTED,
    )


def _collection_worker(server: ArchiveHttpServer) -> None:
    """Run the collection holding the lock the launcher acquired, then release it.

    Wrapped in a fresh trace scope so the background pass gets its own request id in
    the logs, mirroring the automation loop. No client is waiting, so a failure is
    only logged; ``_execute_collection_locked`` already recorded the failure phase in
    ``collection_status`` (which the frontend polls) before re-raising.
    """
    try:
        with trace_scope():
            _execute_collection_locked(server)
    except Exception:
        LOGGER.warning("background web collection failed")
    finally:
        server.collect_lock.release()


def _execute_collection(
    server: ArchiveHttpServer,
) -> tuple[dict[str, object] | None, tuple[HTTPStatus, str] | None]:
    """Run one collection pass synchronously, acquiring and releasing the lock.

    Kept for the automation loop, which already runs on its own background thread
    and wants the blocking call plus the CONFLICT result when the lock is busy. The
    web path uses :func:`_start_background_collection` instead so the request thread
    is never tied up for the duration of a run.
    """
    if not server.collect_lock.acquire(blocking=False):
        return None, (HTTPStatus.CONFLICT, "采集正在进行中，请稍候。")
    try:
        return _execute_collection_locked(server)
    finally:
        server.collect_lock.release()


def _execute_collection_locked(
    server: ArchiveHttpServer,
) -> tuple[dict[str, object] | None, tuple[HTTPStatus, str] | None]:
    """Run one collection pass assuming the shared collect/enrich lock is held.

    The caller owns the lock and is responsible for releasing it. Splitting
    acquisition out lets the web launcher keep the lock across the hand-off to a
    background thread while the request returns at once, without the release and
    re-acquire that a second click could otherwise slip through.
    """
    _set_collection_status(server, running=True, phase="正在启动采集", healthy=None)
    result: Any | None = None
    failure_response: tuple[HTTPStatus, str] | None = None
    try:
        # Deferred import: kol_archive.cli.collect pulls in the CLI package, which
        # imports this module back. Importing it lazily avoids the load-time cycle.
        from kol_archive.browser import BrowserError
        from kol_archive.cli.collect import RunLockError, execute_run_once

        try:
            result = execute_run_once(
                server.config_dir,
                progress=lambda phase: _set_collection_status(
                    server,
                    running=True,
                    phase=phase,
                    healthy=None,
                ),
            )
        except RunLockError:
            _set_collection_status(
                server,
                running=False,
                phase="采集未启动，另一处采集正在运行",
                healthy=False,
            )
            failure_response = (
                HTTPStatus.CONFLICT,
                "采集正在进行中（已被其他进程占用），请稍候。",
            )
        except BrowserError:
            _set_collection_status(
                server,
                running=False,
                phase="采集失败，专用雪球浏览器未就绪",
                healthy=False,
            )
            failure_response = (
                HTTPStatus.SERVICE_UNAVAILABLE,
                "采集失败：已自动尝试启动专用雪球浏览器但未能就绪。"
                "请看刚弹出的 Edge 窗口是否卡在滑块/登录，处理完后再点一次采集。",
            )
    except Exception:
        _set_collection_status(
            server, running=False, phase="采集失败，请查看服务日志", healthy=False
        )
        raise
    if failure_response is not None:
        return None, failure_response
    assert result is not None
    message = "采集完成。" if result.healthy else f"采集完成，但有告警：{result.reason}"
    _set_collection_status(server, running=False, phase=message, healthy=result.healthy)
    LOGGER.info(
        "collection finished healthy=%s reason=%s",
        result.healthy,
        result.reason or "-",
    )
    # The DB is the enrichment queue; collection just wrote new current versions, so
    # wake the resident worker to drain them instead of enriching inline here.
    auto_enrich_started = _nudge_enrichment(server)
    return (
        {
            "ok": True,
            "healthy": result.healthy,
            "reason": result.reason,
            "message": message,
            "auto_enrich_started": auto_enrich_started,
        },
        None,
    )


def _enrichment_detail(
    target: EnrichmentTarget,
    *,
    status: str,
    error: Exception | None = None,
) -> dict[str, object]:
    original_text = re.sub(r"\s+", " ", target.original_text).strip()
    detail: dict[str, object] = {
        "post_id": target.post_id,
        "version_id": target.version_id,
        "status": status,
        "excerpt": original_text[:120],
    }
    if error is not None:
        detail["error_type"] = type(error).__name__
        detail["error"] = redact_text(str(error)).strip()[:500] or "未提供错误详情"
    return detail


def _set_enrichment_status(
    server: ArchiveHttpServer,
    *,
    running: bool,
    author_uid: str,
    phase: str,
    processed: int,
    total: int,
    enriched: int,
    failed: int,
    details: list[dict[str, object]],
) -> None:
    now = datetime.now(tz=UTC).isoformat()
    with server.enrichment_status_lock:
        previous = server.enrichment_status
        logs = [] if running and not previous.running else list(previous.logs)
        if not logs or logs[-1]["message"] != phase:
            logs.append({"at": now, "message": phase})
        server.enrichment_status = EnrichmentStatus(
            running=running,
            author_uid=author_uid,
            phase=phase,
            processed=processed,
            total=total,
            enriched=enriched,
            failed=failed,
            details=list(details),
            logs=logs,
        )


def _execute_author_enrichment(
    server: ArchiveHttpServer, author_uid: str, observed_since: str | None
) -> tuple[dict[str, object] | None, tuple[HTTPStatus, str] | None]:
    if not server.enrichment_lock.acquire(blocking=False):
        return None, (HTTPStatus.CONFLICT, "富化正在进行中，请稍候。")
    _set_enrichment_status(
        server,
        running=True,
        author_uid=author_uid,
        phase="正在准备富化",
        processed=0,
        total=0,
        enriched=0,
        failed=0,
        details=[],
    )
    settings = None
    targets: list[EnrichmentTarget] = []
    enriched = failed = 0
    details: list[dict[str, object]] = []
    try:
        config = load_config(server.config_dir)
        settings = replace(
            load_enrich_settings(config),
            prompt_version=server.enrich_prompt_version,
        )
        connection = connect_database(server.db_path)
        archive = Archive(connection)
        try:
            author_row = connection.execute(
                "SELECT id FROM authors WHERE platform = 'xueqiu' AND platform_uid = ?",
                (author_uid,),
            ).fetchone()
            if author_row is None:
                raise ValueError("author not found")
            targets = archive.enrichment_targets(
                settings.prompt_version,
                author_id=int(author_row["id"]),
                current_only=True,
                observed_since=observed_since,
            )
            _set_enrichment_status(
                server,
                running=True,
                author_uid=author_uid,
                phase=f"准备富化 {len(targets)} 条发言",
                processed=0,
                total=len(targets),
                enriched=0,
                failed=0,
                details=[],
            )
            with http_client(timeout=30.0) as client:
                for index, (target, result, error) in enumerate(
                    enrich_targets(settings, targets, client=client), start=1
                ):
                    try:
                        if error is not None:
                            raise error
                        assert result is not None
                        if (
                            archive.add_enrichment(
                                target,
                                result,
                                settings.model,
                                settings.prompt_version,
                                datetime.now(tz=UTC).isoformat(),
                            )
                            is not None
                        ):
                            enriched += 1
                            details.append(_enrichment_detail(target, status="success"))
                    except (httpx.HTTPError, sqlite3.Error, ValueError) as failure:
                        failed += 1
                        details.append(
                            _enrichment_detail(
                                target,
                                status="failed",
                                error=failure,
                            )
                        )
                        LOGGER.warning(
                            "web enrichment failed version_id=%s type=%s",
                            target.version_id,
                            type(failure).__name__,
                        )
                    _set_enrichment_status(
                        server,
                        running=True,
                        author_uid=author_uid,
                        phase=f"正在富化 {index}/{len(targets)}",
                        processed=index,
                        total=len(targets),
                        enriched=enriched,
                        failed=failed,
                        details=details,
                    )
            _set_enrichment_status(
                server,
                running=False,
                author_uid=author_uid,
                phase=f"富化完成，成功 {enriched} 条，失败 {failed} 条",
                processed=len(targets),
                total=len(targets),
                enriched=enriched,
                failed=failed,
                details=details,
            )
        finally:
            connection.close()
    except Exception:
        with server.enrichment_status_lock:
            status = server.enrichment_status
            processed = status.processed
            total = status.total
            enriched = status.enriched
            failed = status.failed
            details = status.details
        _set_enrichment_status(
            server,
            running=False,
            author_uid=author_uid,
            phase=f"富化中止，已处理 {processed}/{total}，成功 {enriched} 条，失败 {failed} 条",
            processed=processed,
            total=total,
            enriched=enriched,
            failed=failed,
            details=details,
        )
        raise
    finally:
        server.enrichment_lock.release()
    assert settings is not None
    return (
        {
            "ok": True,
            "prompt_version": settings.prompt_version,
            "candidates": len(targets),
            "enriched": enriched,
            "failed": failed,
            "details": details,
            "message": f"富化完成，成功 {enriched} 条，失败 {failed} 条。",
        },
        None,
    )


def _nudge_enrichment(server: ArchiveHttpServer) -> bool:
    """Wake the resident enrichment worker after new versions land.

    Returns whether auto-enrichment is active, so the caller can report it. This is
    only a nudge: the worker drains the full pending queue regardless of who wrote
    the rows, so there is no per-collection scope to pass along.
    """
    with server.automation_settings_lock:
        if not server.automation_active or not server.automation_settings.auto_enrich:
            return False
    server.enrich_wake.set()
    return True


def _drain_pending_enrichments(server: ArchiveHttpServer) -> tuple[int, str]:
    """Enrich every current version still missing a label for the prompt version.

    The DB is the queue: this selects the pending set and drains it author by
    author, regardless of which collector produced the rows or when. Idempotent via
    ``UNIQUE(version_id, prompt_version)``, so a partial drain (e.g. the shared
    collect/enrich lock is busy) just leaves the rest for the next wake or poll.
    """
    connection = connect_database(server.db_path)
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT a.platform_uid
            FROM authors a
            JOIN posts p ON p.author_id = a.id
            JOIN post_versions v ON v.id = p.current_version_id
            LEFT JOIN enrichments e
              ON e.version_id = v.id AND e.prompt_version = ?
            WHERE e.id IS NULL
            ORDER BY a.id
            """,
            (server.enrich_prompt_version,),
        ).fetchall()
    finally:
        connection.close()
    pending_author_count = len(rows)
    if pending_author_count == 0:
        return 0, "empty"
    for row in rows:
        author_uid = str(row["platform_uid"])
        try:
            _, failure_response = _execute_author_enrichment(server, author_uid, None)
        except Exception:
            LOGGER.warning("resident enrichment failed author_uid=%s", author_uid)
            continue
        if failure_response is not None:
            # CONFLICT means the shared collect/enrich lock is busy; every remaining
            # author would hit the same wall, so stop and let the next wake retry.
            if failure_response[0] == HTTPStatus.CONFLICT:
                return pending_author_count, "lock_busy"
            LOGGER.warning(
                "resident enrichment failed author_uid=%s status=%s",
                author_uid,
                failure_response[0],
            )
    return pending_author_count, "completed"
