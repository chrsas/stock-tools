"""离线校验 feed / show 正文归一化规则。"""
from __future__ import annotations

import json
from pathlib import Path

from normalize_text import content_hash, content_text

FIXTURE = Path(__file__).parent / "fixtures" / "normalization_pair.json"


def main() -> None:
    pair = json.loads(FIXTURE.read_text(encoding="utf-8"))
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
