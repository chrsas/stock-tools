"""UTC timestamp parsing and comparison helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def timestamp_at_or_before(left: str, right: str) -> bool:
    return parse_utc_timestamp(left) <= parse_utc_timestamp(right)


def timestamp_in_closed_range(value: str, start: str, end: str) -> bool:
    parsed = parse_utc_timestamp(value)
    return parse_utc_timestamp(start) <= parsed <= parse_utc_timestamp(end)
