"""人工离线快速校验入口；pytest 套件复用同一 fixture 覆盖回归断言。

用法: python -m probe.verify_normalization
"""

from __future__ import annotations

from probe.normalization_fixture import load_normalization_pair
from probe.normalize_text import content_hash, content_text


def main() -> None:
    pair = load_normalization_pair()
    feed_text = content_text(pair["feed_text"])
    show_text = content_text(pair["show_text"])
    feed_hash = content_hash(pair["feed_text"])
    show_hash = content_hash(pair["show_text"])

    assert feed_text == pair["expected_content_text"]
    assert show_text == pair["expected_content_text"]
    assert feed_hash == show_hash
    escaped_literal = content_text(pair["escaped_literal_text"])
    assert escaped_literal == pair["expected_escaped_literal_text"]
    assert content_hash(pair["escaped_literal_text"]) != content_hash(pair["decoded_entity_text"])
    print(f"normalization verified: text_len={len(feed_text)} hash={feed_hash}")


if __name__ == "__main__":
    main()
