# -*- coding: utf-8 -*-
"""获取视频信息（标题/封面/作者/链接），用于"分享视频给主人"。

requests 直连（x-tt-argus + uifid + verifyFp/fp），无需浏览器。
用法: python get_video_info.py <aweme_id>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

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


def get_video_info(aweme_id: str) -> dict:
    """返回 {aweme_id, title, cover, author, url, duration, play_url}。"""
    ck_str, ck = _load()
    uifid = ck.get("UIFID") or ck.get("UIFID_TEMP", "")
    fp = ck.get("s_v_web_id", "")
    headers = {
        "User-Agent": DEFAULT_UA,
        "Cookie": ck_str,
        "Host": "www.douyin.com",
        "Referer": "https://www.douyin.com/",
        "x-tt-argus": "1",
        "uifid": uifid,
    }
    params = {"aweme_id": aweme_id, "uifid": uifid, "verifyFp": fp, "fp": fp}
    r = requests.get(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/",
        params=params,
        headers=headers,
        timeout=30,
    )
    aw = (r.json() or {}).get("aweme_detail") or {}
    v = aw.get("video") or {}
    cover = ""
    for c in (v.get("cover") or {}).get("url_list", []) or []:
        cover = c
        break
    play_url = ""
    for u in (v.get("play_addr") or {}).get("url_list", []) or []:
        play_url = u
        break
    # 图文作品没有 video.play_addr，图片在 aw['images'] 里（每条 {url_list:[...]}）
    images = []
    for img in aw.get("images") or []:
        if not isinstance(img, dict):
            continue
        for u in (img.get("url_list") or [])[:1]:
            images.append(u)
    return {
        "aweme_id": aweme_id,
        "title": aw.get("desc", ""),
        "cover": cover,
        "author": (aw.get("author") or {}).get("nickname", ""),
        "url": f"https://www.douyin.com/video/{aweme_id}",
        "duration": int(v.get("duration") or 0),  # 毫秒
        "play_url": play_url,
        "images": images,  # 图文作品用：图片直链列表（最多 9 张够用）
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: get_video_info.py <aweme_id>")
        return
    r = get_video_info(sys.argv[1])
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
