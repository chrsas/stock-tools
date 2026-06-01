"""雪球正文归一化：展示文本保留词间空格，哈希文本移除全部空白。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def content_text(raw_html: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw_html or "")
    parser.close()
    text = "".join(parser.parts)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def content_hash(raw_html: str) -> str:
    canonical = re.sub(r"\s+", "", content_text(raw_html))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
