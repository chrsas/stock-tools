from __future__ import annotations

from probe.normalization_fixture import load_normalization_pair
from probe.normalize_text import content_hash, content_text


def test_feed_and_show_share_display_text_and_hash() -> None:
    pair = load_normalization_pair()

    assert content_text(pair["feed_text"]) == pair["expected_content_text"]
    assert content_text(pair["show_text"]) == pair["expected_content_text"]
    assert content_hash(pair["feed_text"]) == content_hash(pair["show_text"])


def test_display_text_collapses_whitespace_but_hash_ignores_it() -> None:
    assert content_text("<p>alpha \n beta</p>") == "alpha beta"
    assert content_hash("<p>alpha \n beta</p>") == content_hash("<p>alphabeta</p>")


def test_escaped_literal_stays_distinct_from_decoded_entity() -> None:
    pair = load_normalization_pair()

    assert content_text(pair["escaped_literal_text"]) == pair["expected_escaped_literal_text"]
    assert content_hash(pair["escaped_literal_text"]) != content_hash(pair["decoded_entity_text"])
