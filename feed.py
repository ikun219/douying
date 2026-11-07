# -*- coding: utf-8 -*-
"""抖音推荐流刷视频（requests 直连，已通）。

关键：x-tt-argus 头 + a_bogus + 空 seed 即可拿到推荐流 JSON（aweme_list）。
用法：
  python feed.py [条数=6]
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "utils"))

STATE = Path(__file__).resolve().parent / "storage_state.json"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
)


def _load() -> tuple[str, dict]:
    d = json.loads(STATE.read_text(encoding="utf-8"))
    cookies = d["cookies"]
    ck_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    ck = {c["name"]: c["value"] for c in cookies}
    return ck_str, ck


def _parse_aweme(aw: dict) -> dict:
    cover = ""
    for c in (aw.get("video") or {}).get("cover", {}).get("url_list", []) or []:
        cover = c
        break
    stats = aw.get("statistics") or {}
    author = aw.get("author") or {}
    si = aw.get("share_info") or {}
    aid = aw.get("aweme_id")
    dur = aw.get("duration") or (aw.get("video") or {}).get("duration") or 0
    try:
        dur = int(dur)
    except Exception:
        dur = 0
    return {
        "aweme_id": aid,
        "desc": aw.get("desc", ""),
        "author": author.get("nickname", ""),
        "author_sec_uid": author.get("sec_uid", ""),
        "author_uid": str(author.get("uid") or ""),
        "cover": cover,
        "url": f"https://www.douyin.com/video/{aid}",
        "duration_ms": dur,
        "create_time": aw.get("create_time") or 0,
        "digg_count": stats.get("digg_count") or 0,
        "comment_count": stats.get("comment_count") or 0,
        "collect_count": stats.get("collect_count") or 0,
        "share_count": stats.get("share_count") or 0,
        # 官方分享链接（带 share_sign）；feed 里常常是空的，空就让调用方自己兜底
        "share_url": si.get("share_url") or "",
        # 抖音口令（"复制此链接，打开Dou音搜索…"）
        "share_desc": (si.get("share_link_desc") or "").replace("%s", si.get("share_url") or "").strip(),
    }


def get_feed(pull_type: int = 0) -> list[dict]:
    """拉一页推荐流（约 6 条），返回 [{aweme_id, desc, author, cover, url, ...}]。"""
    from dy_util import generate_a_bogus  # noqa: E402

    ck_str, ck = _load()
    uifid = ck.get("UIFID") or ck.get("UIFID_TEMP", "")
    fp = ck.get("s_v_web_id", "")

    base = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "version_code": "190600",
        "version_name": "19.6.0",
        "pull_type": str(pull_type),
        "webid": "7701340071136069120",
        "uifid": uifid,
        "verifyFp": fp,
        "fp": fp,
        "pc_libra_divert": "Windows",
        "support_h265": "1",
        "support_dash": "0",
        "xigua_user": "0",
        "Seo-Flag": "0",
        "use_lite_type": "1",
        "awemePcRecRawData": json.dumps(
            {"is_xigua_user": 0, "danmaku_switch_status": 0, "is_client": False},
            separators=(",", ":"),
        ),
    }
    q = urllib.parse.urlencode(base)
    ab = generate_a_bogus(q, "")

    headers = {
        "User-Agent": DEFAULT_UA,
        "Cookie": ck_str,
        "Host": "www.douyin.com",
        "Origin": "https://www.douyin.com/",
        "Referer": "https://www.douyin.com/jingxuan",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/plain, */*",
        "x-tt-argus": "1",
        "uifid": uifid,
        "x-secsdk-csrf-token": "DOWNGRADE",
    }
    url = f"https://www.douyin.com/aweme/v2/web/module/feed/?{q}&a_bogus={ab}"
    r = requests.post(
        url,
        data={"encoded_pre_item_ids": "", "refresh_index": "2"},
        headers=headers,
        timeout=30,
    )
    data = r.json()
    return [_parse_aweme(a) for a in (data.get("aweme_list") or [])]


def get_feed_tab(pages: int = 3) -> list[dict]:
    """拉「精选/推荐」tab 流（每页约 6 条，去重）。

    比 get_feed() 好在：**statistics 是全的**（赞/评/藏/转都有），能算互动比值；
    get_feed() 的 module/feed 只给 digg_count，其余全是 null。
    """
    import time as _time

    ck_str, ck = _load()
    uifid = ck.get("UIFID") or ck.get("UIFID_TEMP", "")
    fp = ck.get("s_v_web_id", "")
    headers = {
        "User-Agent": DEFAULT_UA,
        "Cookie": ck_str,
        "Host": "www.douyin.com",
        "Origin": "https://www.douyin.com/",
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "x-tt-argus": "1",
        "uifid": uifid,
    }
    bag: dict[str, dict] = {}
    for idx in range(1, max(1, pages) + 1):
        common = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "1",
            "version_code": "190600",
            "version_name": "19.6.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "125.0.0.0",
            "engine_name": "Blink",
            "engine_version": "125.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "50",
            "webid": "7701340071136069120",
            "uifid": uifid,
            "verifyFp": fp,
            "fp": fp,
            "count": "20",
            "refresh_index": str(idx),
        }
        url = "https://www.douyin.com/aweme/v1/web/tab/feed/?" + urllib.parse.urlencode(common)
        try:
            r = requests.get(url, headers=headers, timeout=30)
            data = r.json()
        except Exception:
            continue
        for a in data.get("aweme_list") or []:
            aid = str(a.get("aweme_id") or "")
            if aid:
                bag[aid] = a
        _time.sleep(1.2)
    return [_parse_aweme(a) for a in bag.values()]


def main() -> None:
    items = get_feed_tab()
    print(f"推荐流 {len(items)} 条：")
    for it in items:
        print(
            f"  {it['aweme_id']}  赞{it['digg_count']} 评{it['comment_count']}"
            f" 藏{it['collect_count']}  @{it['author']}  {it['desc'][:40]}"
        )


if __name__ == "__main__":
    main()
