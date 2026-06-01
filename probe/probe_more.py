"""补充探针：截断/长文、HTML 直链页、removed/not_found 区分。单次会话，温和。

用法: python -m probe.probe_more
"""

from __future__ import annotations

import time

import httpx

from probe.normalize_text import content_hash, content_text
from probe.probe_xueqiu import bootstrap, dump, new_client


def scan_truncation(c: httpx.Client, uids: list[str]) -> list[tuple[str, int, int, bool]]:
    print("\n=== 扫描截断/长文 ===")
    hits: list[tuple[str, int, int, bool]] = []
    for uid in uids:
        url = f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page=1"
        r = c.get(url)
        if r.status_code != 200:
            print(f"  uid={uid} -> {r.status_code} (skip)")
            time.sleep(1.5)
            continue
        statuses = r.json().get("statuses", [])
        truncated = [status for status in statuses if status.get("truncated")]
        longest = max((len(status.get("text") or "") for status in statuses), default=0)
        print(
            f"  uid={uid}: {len(statuses)} posts, truncated={len(truncated)}, "
            f"max text_len={longest}"
        )
        for status in truncated:
            status_id = status.get("id")
            if isinstance(status_id, int):
                hits.append((uid, status_id, len(status.get("text") or ""), True))
        time.sleep(1.8)
    print("  truncated hits (uid,id,text_len):", hits[:5])
    return hits


def probe_html(c: httpx.Client, target_path: str, label: str) -> httpx.Response:
    url = "https://xueqiu.com" + target_path
    r = c.get(url, follow_redirects=False)
    location = r.headers.get("location")
    print(
        f"  HTML {label} {url} -> {r.status_code}"
        + (f" redirect->{location}" if location else f" len={len(r.content)}")
    )
    dump(f"04_html_{label}.txt", r.text[:6000])
    return r


def probe_truncated_pair(c: httpx.Client, uid: str, status_id: int) -> None:
    """对一条 truncated 帖：比较 feed.text 与 show.text 的归一化内容。"""
    print(f"\n=== 截断帖双轨对比 uid={uid} id={status_id} ===")
    timeline = c.get(
        f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page=1"
    ).json()
    feed = next(
        (status for status in timeline.get("statuses", []) if status.get("id") == status_id),
        None,
    )
    time.sleep(1.5)
    show = c.get(f"https://xueqiu.com/statuses/show.json?id={status_id}").json()
    dump(f"05_trunc_feed_{status_id}.json", feed or {})
    dump(f"05_trunc_show_{status_id}.json", show)
    print(
        f"  feed.truncated={feed.get('truncated') if feed else 'N/A'} "
        f"feed.text_len={len(feed.get('text') or '') if feed else 0} "
        f"show.text_len={len(show.get('text') or '')} show.truncated={show.get('truncated')}"
    )
    if feed:
        feed_text, show_text = feed.get("text") or "", show.get("text") or ""
        print(
            f"  normalized_text_equal={content_text(feed_text) == content_text(show_text)} "
            f"content_hash_equal={content_hash(feed_text) == content_hash(show_text)}"
        )


if __name__ == "__main__":
    with new_client() as client:
        bootstrap(client)
        print("\n=== HTML 直链页 ===")
        probe_html(client, "/7377966687/391984427", "real")
        time.sleep(1.5)
        probe_html(client, "/7377966687/1", "notfound")
        time.sleep(1.5)
        hits = scan_truncation(client, ["9797395123", "2616724341", "1729819940", "2464917225"])
        if hits:
            uid, status_id, *_ = hits[0]
            probe_truncated_pair(client, uid, status_id)
        else:
            print("\n(本轮未发现 truncated 帖；截断阈值需更长文样本)")
