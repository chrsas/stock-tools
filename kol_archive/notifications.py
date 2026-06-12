"""Minimal third-party notification payloads."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class NotificationSettings:
    enabled: bool
    webhook_url_env: str
    private_base_url: str
    timeout_seconds: float


@dataclass(frozen=True)
class NotificationPayload:
    title: str
    count: int
    link: str

    def as_json(self) -> dict[str, object]:
        return {"title": self.title, "count": self.count, "link": self.link}


def load_notification_settings(config: dict[str, Any]) -> NotificationSettings:
    section = config.get("notifications") or {}
    if not isinstance(section, dict):
        raise ValueError("notifications must be a mapping")
    webhook_url_env = section.get("webhook_url_env")
    private_base_url = section.get("private_base_url")
    timeout_seconds = section.get("timeout_seconds")
    settings = NotificationSettings(
        enabled=bool(section.get("enabled", False)),
        webhook_url_env=str(
            "KOL_NOTIFICATION_WEBHOOK_URL" if webhook_url_env is None else webhook_url_env
        ).strip(),
        private_base_url=str("" if private_base_url is None else private_base_url)
        .strip()
        .rstrip("/"),
        timeout_seconds=float(10 if timeout_seconds is None else timeout_seconds),
    )
    if not settings.webhook_url_env:
        raise ValueError("notifications.webhook_url_env must not be empty")
    if settings.enabled and not settings.private_base_url:
        raise ValueError(
            "notifications.private_base_url is required when notifications are enabled"
        )
    if settings.timeout_seconds <= 0:
        raise ValueError("notifications.timeout_seconds must be positive")
    return settings


def send_notification(
    settings: NotificationSettings,
    payload: NotificationPayload,
    *,
    client: httpx.Client | None = None,
) -> bool:
    if not settings.enabled:
        return False
    webhook_url = os.environ.get(settings.webhook_url_env)
    if not webhook_url:
        raise ValueError("notification credential environment variable is missing")
    if payload.count < 0:
        raise ValueError("notification count must not be negative")
    owned_client = client is None
    active_client = client or httpx.Client(timeout=settings.timeout_seconds)
    try:
        response = active_client.post(webhook_url, json=payload.as_json())
        response.raise_for_status()
    finally:
        if owned_client:
            active_client.close()
    return True
