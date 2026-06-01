"""阶段 1a 雪球平台探针。

只读、温和采集。把每一步的原始响应落盘到 probe/raw/，供书面决策记录引用。
不写任何业务库；这个脚本的产物是「事实」，不是功能。

用法:
    python -m probe.probe_xueqiu bootstrap          # 引导 cookie，记录 cookie 名称
    python -m probe.probe_xueqiu discover           # 用公开热门流发现真实 uid/status_id
    python -m probe.probe_xueqiu timeline <uid>     # 抓 user_timeline 多页，落盘
    python -m probe.probe_xueqiu show <status_id>   # 直链 show.json + HTML 页
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

RAW = Path(__file__).parent / "raw"
RAW.mkdir(exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://xueqiu.com/",
}


def dump(name: str, obj: object) -> Path:
    p = RAW / name
    if isinstance(obj, (dict, list)):
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        p.write_text(str(obj), encoding="utf-8")
    print(f"  -> saved {p.relative_to(Path(__file__).parent.parent)} ({p.stat().st_size} bytes)")
    return p


def new_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=20.0, follow_redirects=False)


def bootstrap(client: httpx.Client) -> dict[str, str]:
    """访问首页获取 guest cookie，仅记录 cookie 名称。"""
    r = client.get("https://xueqiu.com/")
    print(f"GET / -> {r.status_code}, history={[h.status_code for h in r.history]}")
    cookies = dict(client.cookies)
    print(f"cookies received: {sorted(cookies)}")
    dump(
        "00_bootstrap_cookies.json",
        {"cookie_names": sorted(cookies), "cookie_count": len(cookies)},
    )
    return cookies


def cmd_bootstrap() -> None:
    with new_client() as c:
        bootstrap(c)


def cmd_discover() -> None:
    with new_client() as c:
        bootstrap(c)
        # 公开热门时间线，用来发现真实 uid + status_id 作为后续探测样本
        for path in [
            "https://xueqiu.com/statuses/hot/listV2.json?since_id=-1&max_id=-1&size=15",
            "https://xueqiu.com/v4/statuses/public_timeline_by_category.json?since_id=-1&max_id=-1&count=15&category=-1",
        ]:
            try:
                r = c.get(path)
                print(f"GET {path}\n  -> {r.status_code}, len={len(r.content)}")
                fname = "01_discover_" + path.split("/")[-1].split("?")[0] + ".json"
                try:
                    dump(fname, r.json())
                except Exception:
                    dump(fname.replace(".json", ".txt"), r.text[:4000])
            except Exception as e:
                print(f"  !! {type(e).__name__}: {e}")
            time.sleep(1.5)


def cmd_timeline(uid: str) -> None:
    with new_client() as c:
        bootstrap(c)
        for page in (1, 2):
            url = f"https://xueqiu.com/v4/statuses/user_timeline.json?user_id={uid}&page={page}"
            r = c.get(url)
            print(
                f"GET user_timeline uid={uid} page={page} -> {r.status_code}, len={len(r.content)}"
            )
            try:
                dump(f"02_timeline_{uid}_p{page}.json", r.json())
            except Exception:
                dump(f"02_timeline_{uid}_p{page}.txt", r.text[:4000])
            time.sleep(2.0)


def cmd_show(status_id: str) -> None:
    with new_client() as c:
        bootstrap(c)
        # 1) JSON 直链
        url = f"https://xueqiu.com/statuses/show.json?id={status_id}"
        r = c.get(url)
        print(f"GET show.json id={status_id} -> {r.status_code}, len={len(r.content)}")
        try:
            dump(f"03_show_{status_id}.json", r.json())
        except Exception:
            dump(f"03_show_{status_id}.txt", r.text[:4000])
        time.sleep(2.0)
        # 2) 几个必然不存在 / 异常的 id，观察区分能力
        for bad in ("1", "99999999999999"):
            rb = c.get(f"https://xueqiu.com/statuses/show.json?id={bad}")
            print(f"GET show.json id={bad} (probe-removed) -> {rb.status_code}")
            try:
                dump(f"03_show_bad_{bad}.json", rb.json())
            except Exception:
                dump(f"03_show_bad_{bad}.txt", rb.text[:2000])
            time.sleep(1.5)


def require_arg(value: str | None, command: str) -> str:
    if value is None:
        raise SystemExit(f"{command} requires an argument")
    return value


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "bootstrap"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    commands: dict[str, Callable[[], Any]] = {
        "bootstrap": cmd_bootstrap,
        "discover": cmd_discover,
        "timeline": lambda: cmd_timeline(require_arg(arg, "timeline")),
        "show": lambda: cmd_show(require_arg(arg, "show")),
    }
    try:
        commands[cmd]()
    except KeyError:
        raise SystemExit(f"unknown command: {cmd}") from None
