"""Persistent run-health streak tracking for collection alerts."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kol_archive.notifications import NotificationPayload

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertSettings:
    failure_streak: int
    state_path: Path


@dataclass(frozen=True)
class RunHealthState:
    failure_streak: int = 0
    alerted: bool = False


def load_alert_settings(config: dict[str, object]) -> AlertSettings:
    section = config.get("alerts") or {}
    if not isinstance(section, dict):
        raise ValueError("alerts must be a mapping")
    configured_failure_streak = section.get("failure_streak")
    configured_state_path = section.get("state_path")
    failure_streak = int(2 if configured_failure_streak is None else configured_failure_streak)
    state_path_text = str(
        "data/alerts/run-health.json" if configured_state_path is None else configured_state_path
    )
    if failure_streak < 1:
        raise ValueError("alerts.failure_streak must be positive")
    if not state_path_text.strip():
        raise ValueError("alerts.state_path must not be empty")
    state_path = Path(state_path_text)
    return AlertSettings(failure_streak=failure_streak, state_path=state_path)


def _read_state(path: Path) -> RunHealthState:
    if not path.exists():
        return RunHealthState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("run health state must be a mapping")
        return RunHealthState(
            failure_streak=max(0, int(raw.get("failure_streak") or 0)),
            alerted=bool(raw.get("alerted", False)),
        )
    except OSError, ValueError, TypeError, json.JSONDecodeError:
        LOGGER.warning("run health state unreadable; resetting path=%s", path)
        return RunHealthState()


def _write_state(path: Path, state: RunHealthState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"failure_streak": state.failure_streak, "alerted": state.alerted},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def record_run_health(
    settings: AlertSettings,
    *,
    healthy: bool,
    reason: str | None,
    private_link: str,
    notify: Callable[[NotificationPayload], bool],
) -> RunHealthState:
    current = _read_state(settings.state_path)
    if healthy:
        next_state = RunHealthState()
        _write_state(settings.state_path, next_state)
        return next_state
    streak = current.failure_streak + 1
    alerted = current.alerted
    if streak >= settings.failure_streak and not alerted:
        title = f"采集健康告警：{reason or 'run-once 连续失败'}"
        try:
            alerted = notify(NotificationPayload(title=title, count=streak, link=private_link))
        except Exception:
            LOGGER.warning("collection health notification failed")
    next_state = RunHealthState(failure_streak=streak, alerted=alerted)
    _write_state(settings.state_path, next_state)
    return next_state
