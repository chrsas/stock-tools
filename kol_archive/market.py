"""Deterministic market-relation checks derived from archived post evidence."""

from __future__ import annotations

import json
import re

_A_SHARE_TICKER = re.compile(r"(?:SH|SZ|BJ)\d{6}")


def has_explicit_market_relation(content_text: str, raw_payload: str | None) -> bool:
    """Return whether archived text or stockCorrelation names an A-share ticker."""
    if _A_SHARE_TICKER.search(content_text):
        return True
    if not raw_payload:
        return False
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return False

    def visit(value: object) -> bool:
        if isinstance(value, dict):
            correlation = value.get("stockCorrelation")
            if isinstance(correlation, list):
                for item in correlation:
                    if _A_SHARE_TICKER.fullmatch(str(item)):
                        return True
                    if isinstance(item, dict) and any(
                        _A_SHARE_TICKER.fullmatch(str(item.get(key) or ""))
                        for key in ("symbol", "ticker", "code")
                    ):
                        return True
            return any(visit(child) for child in value.values())
        if isinstance(value, list):
            return any(visit(child) for child in value)
        return False

    return visit(payload)
