"""LLM claim extraction, review projections, and deterministic settlement helpers."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, cast

import httpx

from kol_archive.market import A_SHARE_TIMEZONE, OUTCOME_METHOD_VERSION, common_close_returns
from kol_archive.models import ClaimProposalResult
from kol_archive.obs import http_client
from kol_archive.time import parse_utc_timestamp

SYSTEM_PROMPT = """你在从一条 KOL 发言原文中抽取可供人工确认的可证伪市场命题。
只允许抽取原文明示的内容，不得补造标的、方向、期限、目标价或置信措辞。

输出 JSON 对象，且仅含 claims 数组。每项仅含：
- ticker：原文明示的 A 股代码，格式 SH/SZ/BJ 加 6 位数字。
- direction：long、short 或 neutral。
- horizon_days：原文明示的自然日观察期限；没有则为 null。
- target_price：原文明示的目标价；没有则为 null。
- confidence_phrasing：原文明示的置信措辞；没有则为 null。
- evidence_snippet：原文中逐字摘录、足以支持该命题的一小段。

无法形成可证伪命题时返回 {"claims": []}。不得把相关标的自动解释为看多或看空。"""

_TICKER = re.compile(r"(?:SH|SZ|BJ)\d{6}")
_NUMBER = re.compile(r"(?<!\d)(?:\d+(?:\.\d+)?)(?!\d)")
_DIRECTION_TERMS = {
    "long": ("看多", "看好", "做多", "买入", "上涨", "上行", "增持"),
    "short": ("看空", "看淡", "做空", "卖出", "下跌", "下行", "减持"),
    "neutral": ("中性", "震荡", "观望"),
}
CLAIM_SETTLEMENT_MAX_LAG_DAYS = 14


@dataclass(frozen=True)
class ClaimSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    prompt_version: str = "claim-v1"


def load_claim_settings(config: dict[str, Any]) -> ClaimSettings:
    llm = config.get("llm") or {}
    if not isinstance(llm, dict):
        raise ValueError("llm must be a mapping")
    if str(llm.get("provider") or "openai_compatible").strip() != "openai_compatible":
        raise ValueError("llm.provider must be openai_compatible")
    api_key_env = str(llm.get("api_key_env") or "LLM_API_KEY").strip()
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(f"LLM API key environment variable is missing: {api_key_env}")
    model = str(llm.get("model") or "").strip()
    if not model:
        raise ValueError("llm.model must be configured")
    return ClaimSettings(
        base_url=str(llm.get("base_url") or "https://api.openai.com/v1").rstrip("/"),
        model=model,
        api_key=api_key,
        prompt_version=str(llm.get("claim_prompt_version") or "claim-v1").strip() or "claim-v1",
    )


def _strip_whitespace(value: str) -> str:
    return "".join(value.split())


def is_verbatim_excerpt(excerpt: str, original_text: str) -> bool:
    return bool(excerpt.strip()) and _strip_whitespace(excerpt) in _strip_whitespace(original_text)


def _optional_number(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"LLM claim field must be a number or null: {field_name}")
    return float(value)


def _matching_numbers(text: str, expected: float) -> list[re.Match[str]]:
    without_tickers = _TICKER.sub("", text)
    return [
        match for match in _NUMBER.finditer(without_tickers) if float(match.group()) == expected
    ]


def _has_explicit_horizon(text: str, expected: int) -> bool:
    without_tickers = _TICKER.sub("", text)
    return any(
        re.match(r"\s*(?:个\s*)?(?:自然日|交易日|天|日)", without_tickers[match.end() :])
        for match in _matching_numbers(text, float(expected))
    )


def _has_explicit_target_price(text: str, expected: float) -> bool:
    without_tickers = _TICKER.sub("", text)
    for match in _matching_numbers(text, expected):
        prefix = without_tickers[max(0, match.start() - 12) : match.start()]
        suffix = without_tickers[match.end() : match.end() + 8]
        if re.search(r"(?:目标价|目标|看到|看至|看高至|涨到|跌到)\s*(?:为|到|至)?\s*$", prefix):
            return True
        if re.match(r"\s*(?:元|块)(?:钱)?", suffix):
            return True
    return False


def validate_claim_proposal_result(result: ClaimProposalResult, original_text: str) -> None:
    if not _TICKER.fullmatch(result.ticker):
        raise ValueError(f"LLM claim ticker is invalid: {result.ticker}")
    if result.ticker not in original_text:
        raise ValueError("LLM claim ticker is not explicit in the original text")
    if result.direction not in {"long", "short", "neutral"}:
        raise ValueError("LLM claim direction must be long, short, or neutral")
    if not any(term in original_text for term in _DIRECTION_TERMS[result.direction]):
        raise ValueError("LLM claim direction is not explicit in the original text")
    if not is_verbatim_excerpt(result.evidence_snippet, original_text):
        raise ValueError("LLM claim evidence_snippet must be a verbatim non-empty excerpt")
    if result.horizon_days is not None and not _has_explicit_horizon(
        original_text, result.horizon_days
    ):
        raise ValueError("LLM claim horizon_days is not explicit in the original text")
    if result.target_price is not None and not _has_explicit_target_price(
        original_text, result.target_price
    ):
        raise ValueError("LLM claim target_price is not explicit in the original text")
    if result.confidence_phrasing is not None and not is_verbatim_excerpt(
        result.confidence_phrasing, original_text
    ):
        raise ValueError("LLM claim confidence_phrasing is not explicit in the original text")


def _parse_result(payload: dict[str, Any], original_text: str) -> list[ClaimProposalResult]:
    try:
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(str(content))
        claims = parsed["claims"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("LLM claim response must contain a JSON claims array") from error
    if not isinstance(claims, list):
        raise ValueError("LLM claim response claims must be an array")
    results: list[ClaimProposalResult] = []
    seen: set[str] = set()
    for item in claims:
        if not isinstance(item, dict):
            raise ValueError("LLM claim array items must be objects")
        ticker = str(item.get("ticker") or "").strip().upper()
        direction = str(item.get("direction") or "").strip()
        evidence = str(item.get("evidence_snippet") or "").strip()
        horizon = item.get("horizon_days")
        if horizon is not None and (
            isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0
        ):
            raise ValueError("LLM claim horizon_days must be a positive integer or null")
        target_price = _optional_number(item.get("target_price"), "target_price")
        if target_price is not None and target_price <= 0:
            raise ValueError("LLM claim target_price must be positive")
        if ticker in seen:
            raise ValueError(f"LLM claim response contains duplicate ticker: {ticker}")
        seen.add(ticker)
        confidence = str(item.get("confidence_phrasing") or "").strip() or None
        result = ClaimProposalResult(
            ticker=ticker,
            direction=direction,
            horizon_days=horizon,
            target_price=target_price,
            confidence_phrasing=confidence,
            evidence_snippet=evidence,
        )
        validate_claim_proposal_result(result, original_text)
        results.append(result)
    return results


def request_claim_proposals(
    settings: ClaimSettings,
    original_text: str,
    *,
    client: httpx.Client | None = None,
) -> list[ClaimProposalResult]:
    owned_client = client is None
    active_client = client or http_client(timeout=30.0)
    try:
        response = active_client.post(
            f"{settings.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            json={
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": original_text},
                ],
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("LLM claim response body must be a JSON object")
        return _parse_result(cast(dict[str, Any], payload), original_text)
    finally:
        if owned_client:
            active_client.close()


def list_claim_proposals(
    connection: sqlite3.Connection,
    *,
    review_state: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if review_state is not None and review_state not in {"pending", "accepted", "rejected"}:
        raise ValueError("claim proposal review_state must be pending, accepted, or rejected")
    where = "" if review_state is None else "WHERE cp.review_state = ?"
    params: tuple[object, ...] = (limit,) if review_state is None else (review_state, limit)
    rows = connection.execute(
        f"""
        SELECT
            cp.*, p.id AS post_id, p.platform_post_id, p.url, p.source_state, v.content_text,
            v.first_observed_at, a.platform_uid AS author_platform_uid,
            COALESCE(json_extract(v.raw_payload, '$.user.screen_name'), a.notes)
                AS author_display_name,
            tn.name AS ticker_name
        FROM claim_proposals cp
        JOIN post_versions v ON v.id = cp.version_id
        JOIN posts p ON p.id = v.post_id
        JOIN authors a ON a.id = p.author_id
        LEFT JOIN ticker_names tn ON tn.ticker = cp.ticker
        {where}
        ORDER BY CASE cp.review_state WHEN 'pending' THEN 0 ELSE 1 END, cp.created_at, cp.id
        LIMIT ?
        """,
        params,
    ).fetchall()
    counts = connection.execute(
        """
        SELECT
            SUM(review_state = 'pending') AS pending,
            SUM(review_state = 'accepted') AS accepted,
            SUM(review_state = 'rejected') AS rejected
        FROM claim_proposals
        """
    ).fetchone()
    return {
        "items": [dict(row) for row in rows],
        "counts": {
            "pending": int(counts["pending"] or 0),
            "accepted": int(counts["accepted"] or 0),
            "rejected": int(counts["rejected"] or 0),
        },
        "filters": {"review_state": review_state},
    }


def common_close_claim_outcome(
    connection: sqlite3.Connection,
    ticker: str,
    benchmark_ticker: str,
    claim_made_at: str,
    horizon_days: int,
) -> dict[str, object] | None:
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    target_date = (
        parse_utc_timestamp(claim_made_at).astimezone(A_SHARE_TIMEZONE).date()
        + timedelta(days=horizon_days)
    ).isoformat()
    outcome = common_close_returns(
        connection, ticker, benchmark_ticker, claim_made_at, end_date=target_date
    )
    if outcome is None:
        return None
    resolved_date = date.fromisoformat(str(outcome["end_date"]))
    latest_acceptable_date = date.fromisoformat(target_date) + timedelta(
        days=CLAIM_SETTLEMENT_MAX_LAG_DAYS
    )
    if resolved_date > latest_acceptable_date:
        return None
    return {
        **outcome,
        "resolved_at": outcome["end_date"],
        "notes": f"共同收盘起点 {outcome['start_date']}，目标自然日 {target_date}",
        "outcome_method_version": OUTCOME_METHOD_VERSION,
    }
