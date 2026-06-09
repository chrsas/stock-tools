"""雪球正文归一化：展示文本保留词间空格，哈希文本移除全部空白。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class _ImageExtractor(HTMLParser):
    """Collect ``<img>`` source URLs in document order from raw post HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        mapping = {name: value for name, value in attrs}
        # Prefer the canonical ``src``; fall back to lazy-load attributes Xueqiu
        # sometimes uses so a deferred image is not silently dropped.
        for key in ("src", "data-src", "data-original"):
            value = mapping.get(key)
            if value:
                self.sources.append(value.strip())
                return


def content_text(raw_html: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw_html or "")
    parser.close()
    text = "".join(parser.parts)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def content_hash(raw_html: str) -> str:
    canonical = re.sub(r"\s+", "", content_text(raw_html))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_image_url(url: str) -> str:
    """Drop the query/fragment so signed-but-equivalent image URLs compare equal.

    Xueqiu serves images behind a CDN whose query string can carry a rotating
    signature/token. Two requests for the same picture therefore differ only in
    volatile, credential-bearing query parameters. The manifest must be stable
    across those rotations (otherwise every poll would look like an image swap),
    so identity is the scheme+host+path only.
    """
    split = urlsplit(url.strip())
    return urlunsplit((split.scheme, split.netloc, split.path, "", ""))


def extract_image_urls(raw_html: str) -> list[str]:
    """Image sources in document order, raw (query/signature preserved)."""
    parser = _ImageExtractor()
    parser.feed(raw_html or "")
    parser.close()
    return parser.sources


def image_manifest_hash(normalized_urls: list[str]) -> str:
    """Stable digest over the ordered, normalized image-URL list of a version.

    Folded into version-change detection alongside :func:`content_hash` because
    the latter strips all HTML tags — image URLs are invisible to it, so an edit
    that only swaps a chart would otherwise leave no new version. An empty list
    hashes to a real constant (not a sentinel) so that losing the last image is
    still a detectable change rather than an un-comparable ``NULL``.
    """
    return hashlib.sha256("\n".join(normalized_urls).encode("utf-8")).hexdigest()
