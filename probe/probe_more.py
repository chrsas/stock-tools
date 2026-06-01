"""补充探针：截断/长文、HTML 直链页、removed/not_found 区分。单次会话，温和。"""
from __future__ import annotations
import json, time
from pathlib import Path
import httpx
from probe_xueqiu import new_client, bootstrap, dump, RAW  # reuse
from normalize_text import content_hash, content_text

def scan_truncation(c, uids):
    print("\n=== 扫描截断/长文 ===")
    hits = []
    for uid in uids:
        url = f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page=1"
        r = c.get(url)
        if r.status_code != 200:
            print(f"  uid={uid} -> {r.status_code} (skip)"); time.sleep(1.5); continue
        sts = r.json().get("statuses", [])
        trunc = [s for s in sts if s.get("truncated")]
        longest = max((len(s.get("text") or "") for s in sts), default=0)
        print(f"  uid={uid}: {len(sts)} posts, truncated={len(trunc)}, max text_len={longest}")
        for s in trunc:
            hits.append((uid, s.get("id"), len(s.get("text") or ""), s.get("truncated")))
        time.sleep(1.8)
    print("  truncated hits (uid,id,text_len):", hits[:5])
    return hits

def probe_html(c, target_path, label):
    url = "https://xueqiu.com" + target_path
    r = c.get(url, follow_redirects=False)
    loc = r.headers.get("location")
    print(f"  HTML {label} {url} -> {r.status_code}"
          + (f" redirect->{loc}" if loc else f" len={len(r.content)}"))
    dump(f"04_html_{label}.txt", r.text[:6000])
    return r

def probe_truncated_pair(c, uid, sid):
    """对一条 truncated 帖：比较 feed.text 与 show.text 的归一化内容。"""
    print(f"\n=== 截断帖双轨对比 uid={uid} id={sid} ===")
    tl = c.get(f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page=1").json()
    feed = next((s for s in tl.get("statuses", []) if s.get("id") == sid), None)
    time.sleep(1.5)
    sh = c.get(f"https://xueqiu.com/statuses/show.json?id={sid}").json()
    dump(f"05_trunc_feed_{sid}.json", feed or {})
    dump(f"05_trunc_show_{sid}.json", sh)
    print(f"  feed.truncated={feed.get('truncated') if feed else 'N/A'} "
          f"feed.text_len={len(feed.get('text') or '') if feed else 0} "
          f"show.text_len={len(sh.get('text') or '')} show.truncated={sh.get('truncated')}")
    if feed:
        feed_text, show_text = feed.get("text") or "", sh.get("text") or ""
        print(f"  normalized_text_equal={content_text(feed_text) == content_text(show_text)} "
              f"content_hash_equal={content_hash(feed_text) == content_hash(show_text)}")

if __name__ == "__main__":
    with new_client() as c:
        bootstrap(c)
        # 1) HTML 直链页：真实存在 / 不存在
        print("\n=== HTML 直链页 ===")
        probe_html(c, "/7377966687/391984427", "real")
        time.sleep(1.5)
        probe_html(c, "/7377966687/1", "notfound")
        time.sleep(1.5)
        # 2) 扫截断（活跃账号）
        hits = scan_truncation(c, ["9797395123", "2616724341", "1729819940", "2464917225"])
        # 3) 若找到截断帖，做双轨对比
        if hits:
            uid, sid, *_ = hits[0]
            probe_truncated_pair(c, uid, sid)
        else:
            print("\n(本轮未发现 truncated 帖；截断阈值需更长文样本)")
