"""Shared loader for the offline normalization regression fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, cast

FIXTURE = Path(__file__).parent / "fixtures" / "normalization_pair.json"


class NormalizationPair(TypedDict):
    feed_text: str
    show_text: str
    expected_content_text: str
    escaped_literal_text: str
    expected_escaped_literal_text: str
    decoded_entity_text: str


def load_normalization_pair() -> NormalizationPair:
    return cast(NormalizationPair, json.loads(FIXTURE.read_text(encoding="utf-8")))
