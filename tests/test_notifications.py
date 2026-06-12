from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import pytest

from kol_archive.__main__ import (
    _collection_failure_reason,
    _record_run_health_safely,
    _run_once_command,
)
from kol_archive.alerts import AlertSettings, load_alert_settings, record_run_health
from kol_archive.database import connect_database, initialize_database
from kol_archive.notifications import (
    NotificationPayload,
    NotificationSettings,
    load_notification_settings,
    send_notification,
)


def test_notification_payload_is_minimal_and_credential_stays_outside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    monkeypatch.setenv("TEST_WEBHOOK_URL", "https://secret.example/token-value")
    settings = NotificationSettings(
        enabled=True,
        webhook_url_env="TEST_WEBHOOK_URL",
        private_base_url="http://100.64.0.1:8765",
        timeout_seconds=1,
    )
    payload = NotificationPayload(
        title="KOL 变更摘要 2026-06-12",
        count=3,
        link="http://100.64.0.1:8765",
    )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert send_notification(settings, payload, client=client) is True

    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body == {
        "title": "KOL 变更摘要 2026-06-12",
        "count": 3,
        "link": "http://100.64.0.1:8765",
    }
    assert "token-value" not in requests[0].content.decode()


def test_run_health_alerts_at_threshold_and_resets_after_success(tmp_path: Path) -> None:
    settings = AlertSettings(failure_streak=2, state_path=tmp_path / "health.json")
    sent: list[NotificationPayload] = []

    def notify(payload: NotificationPayload) -> bool:
        sent.append(payload)
        return True

    first = record_run_health(
        settings,
        healthy=False,
        reason="CDP 连接失败",
        private_link="http://100.64.0.1:8765",
        notify=notify,
    )
    second = record_run_health(
        settings,
        healthy=False,
        reason="CDP 连接失败",
        private_link="http://100.64.0.1:8765",
        notify=notify,
    )
    third = record_run_health(
        settings,
        healthy=False,
        reason="CDP 连接失败",
        private_link="http://100.64.0.1:8765",
        notify=notify,
    )
    reset = record_run_health(
        settings,
        healthy=True,
        reason=None,
        private_link="http://100.64.0.1:8765",
        notify=notify,
    )

    assert first.failure_streak == 1
    assert second.failure_streak == 2
    assert third.failure_streak == 3
    assert second.alerted is True
    assert third.alerted is True
    assert sent == [
        NotificationPayload(
            title="采集健康告警：CDP 连接失败",
            count=2,
            link="http://100.64.0.1:8765",
        )
    ]
    assert reset.failure_streak == 0
    assert json.loads(settings.state_path.read_text(encoding="utf-8")) == {
        "failure_streak": 0,
        "alerted": False,
    }


def test_failed_notification_retries_on_next_unhealthy_run(tmp_path: Path) -> None:
    settings = AlertSettings(failure_streak=1, state_path=tmp_path / "health.json")
    attempts = 0

    def notify(payload: NotificationPayload) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts > 1

    first = record_run_health(
        settings,
        healthy=False,
        reason="run-once 执行失败",
        private_link="http://100.64.0.1:8765",
        notify=notify,
    )
    second = record_run_health(
        settings,
        healthy=False,
        reason="run-once 执行失败",
        private_link="http://100.64.0.1:8765",
        notify=notify,
    )

    assert first.alerted is False
    assert second.alerted is True
    assert attempts == 2


def test_invalid_notification_config_does_not_block_health_state(tmp_path: Path) -> None:
    state_path = tmp_path / "health.json"

    _record_run_health_safely(
        {
            "notifications": {"enabled": True, "private_base_url": ""},
            "alerts": {"failure_streak": 1, "state_path": str(state_path)},
        },
        healthy=False,
        reason="run-once 执行失败",
    )

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "failure_streak": 1,
        "alerted": False,
    }


def test_run_once_command_loads_config_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config: dict[str, object] = {"storage": {"db_path": str(tmp_path / "missing.sqlite3")}}
    loads = 0

    def load_once(config_dir: Path) -> dict[str, object]:
        nonlocal loads
        loads += 1
        return config

    monkeypatch.setattr("kol_archive.__main__.load_config", load_once)
    monkeypatch.setattr("kol_archive.__main__._run_once_with_config", lambda loaded: None)
    monkeypatch.setattr(
        "kol_archive.__main__._record_run_health_safely", lambda *args, **kwargs: None
    )

    _run_once_command(argparse.Namespace(config_dir=tmp_path))

    assert loads == 1


def test_notification_settings_reject_explicit_zero_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        load_notification_settings({"notifications": {"timeout_seconds": 0}})


def test_alert_settings_reject_explicit_zero_threshold() -> None:
    with pytest.raises(ValueError, match="failure_streak"):
        load_alert_settings({"alerts": {"failure_streak": 0}})
    with pytest.raises(ValueError, match="state_path"):
        load_alert_settings({"alerts": {"state_path": ""}})


def test_collection_failure_reason_prefers_expired_login(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.sqlite3"
    connection = connect_database(db_path)
    initialize_database(connection)
    author = connection.execute(
        """
        INSERT INTO authors(platform, platform_uid, live_monitoring_started_at)
        VALUES ('xueqiu', 'one', '2026-06-01T00:00:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO fetch_runs(
            author_id, platform, started_at, finished_at, status, login_state,
            pages_fetched, pagination_complete, rate_limited, http_error_count,
            ingest_mode, adapter_version
        ) VALUES (?, 'xueqiu', '2026-06-12T01:00:00+00:00', '2026-06-12T01:01:00+00:00',
            'partial', 'expired', 0, 0, 0, 0, 'live', 'test')
        """,
        (author.lastrowid,),
    )
    connection.commit()
    connection.close()

    assert _collection_failure_reason(db_path, "2026-06-12T00:00:00+00:00") == "登录状态连续失效"
    assert _collection_failure_reason(db_path, "2026-06-12T02:00:00+00:00") is None


def test_collection_failure_reason_detects_failed_run(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.sqlite3"
    connection = connect_database(db_path)
    initialize_database(connection)
    author = connection.execute(
        """
        INSERT INTO authors(platform, platform_uid, live_monitoring_started_at)
        VALUES ('xueqiu', 'one', '2026-06-01T00:00:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO fetch_runs(
            author_id, platform, started_at, finished_at, status, login_state,
            pages_fetched, pagination_complete, rate_limited, http_error_count,
            ingest_mode, adapter_version
        ) VALUES (?, 'xueqiu', '2026-06-12T01:00:00+00:00', '2026-06-12T01:01:00+00:00',
            'failed', 'unknown', 0, 0, 0, 1, 'live', 'test')
        """,
        (author.lastrowid,),
    )
    connection.commit()
    connection.close()

    assert _collection_failure_reason(db_path, "2026-06-12T00:00:00+00:00") == "run-once 连续失败"


def test_collection_failure_reason_detects_failed_probe(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.sqlite3"
    connection = connect_database(db_path)
    initialize_database(connection)
    author = connection.execute(
        """
        INSERT INTO authors(platform, platform_uid, live_monitoring_started_at)
        VALUES ('xueqiu', 'one', '2026-06-01T00:00:00+00:00')
        """
    )
    post = connection.execute(
        """
        INSERT INTO posts(
            author_id, platform, platform_post_id, first_seen_at, feed_state,
            source_state, watch_mode, ingest_mode
        ) VALUES (?, 'xueqiu', 'post-1', '2026-06-01T00:00:00+00:00',
            'present', 'reachable', 'pinned', 'live')
        """,
        (author.lastrowid,),
    )
    connection.execute(
        """
        INSERT INTO probe_runs(
            post_id, started_at, finished_at, observed_at, status, login_state,
            rate_limited, result, content_fidelity, ingest_mode, adapter_version
        ) VALUES (?, '2026-06-12T01:00:00+00:00', '2026-06-12T01:01:00+00:00',
            '2026-06-12T01:00:00+00:00', 'failed', 'unknown', 0, 'unknown', 'na',
            'live', 'test')
        """,
        (post.lastrowid,),
    )
    connection.commit()
    connection.close()

    assert _collection_failure_reason(db_path, "2026-06-12T00:00:00+00:00") == "run-once 连续失败"
