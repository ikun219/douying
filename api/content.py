# -*- coding: utf-8 -*-
"""抖音内容接口 — 搜索/用户/作品。

导出:
    search_videos(query, count)   — 搜索视频
    search_users(query, count)    — 搜索用户
    get_user_info(user_url)       — 获取用户信息
    get_user_works(user_url)      — 获取用户作品列表
    get_work_info(url)            — 获取单个作品信息
    get_feed(count)               — 获取推荐流

CLI:
    python -m douyin.api.content search <关键词>
    python -m douyin.api.content user <用户主页URL>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # douyin/
sys.path.insert(0, str(HERE))

# 缓存 auth 对象（减少重复读 storage_state）
_auth = None


def _get_auth():
    """获取 DouyinAuth（懒加载，缓存）。"""
    global _auth
    if _auth is not None:
        return _auth
    from builder.auth import DouyinAuth
    _auth = DouyinAuth()
    return _auth


# --------------------------------------------------------------- API 封装
def search_videos(query: str, count: int = 16, sort_type: str = "0", publish_time: str = "0") -> list:
    """搜索视频。

    Args:
        query: 搜索关键词
        count: 返回数量（默认 16）
        sort_type: "0"=综合 "1"=最多点赞 "2"=最新
        publish_time: "0"=不限 "1"=一天内 "7"=一周内 "180"=半年内

    Returns [{aweme_id, desc, author:{nickname, uid}, ...}, ...]
    """
    from dy_apis.douyin_api import DouyinAPI
    auth = _get_auth()
    try:
        return DouyinAPI.search_some_video_work(auth, query, num=count, sort_type=sort_type, publish_time=publish_time)
    except Exception:
        return []


def search_users(query: str, count: int = 10) -> list:
    """搜索用户。

    Returns [{user_info:{uid, nickname, unique_id, avatar_thumb, ...}, ...}, ...]
    """
    from dy_apis.douyin_api import DouyinAPI
    auth = _get_auth()
    try:
        return DouyinAPI.search_some_user(auth, query, num=count)
    except Exception:
        return []


def get_user_info(user_url: str) -> dict:
    """获取用户信息。

    Args:
        user_url: 用户主页 URL（如 https://www.douyin.com/user/MS4wLjAB...）

    Returns {uid, nickname, unique_id, avatar, follower_count, ...}
    """
    from dy_apis.douyin_api import DouyinAPI
    auth = _get_auth()
    try:
        return DouyinAPI.get_user_info(auth, user_url)
    except Exception:
        return {}


def get_user_works(user_url: str) -> list:
    """获取用户全部作品。

    Returns [{aweme_id, desc, create_time, statistics:{digg_count,comment_count,share_count}, ...}, ...]
    """
    from dy_apis.douyin_api import DouyinAPI
    auth = _get_auth()
    try:
        return DouyinAPI.get_user_all_work_info(auth, user_url)
    except Exception:
        return []


def get_work_info(url: str) -> dict:
    """获取单个作品信息。

    Args:
        url: 视频 URL

    Returns {aweme_id, desc, author, statistics, ...}
    """
    from dy_apis.douyin_api import DouyinAPI
    auth = _get_auth()
    try:
        return DouyinAPI.get_work_info(auth, url)
    except Exception:
        return {}


def get_feed(count: str = "20") -> list:
    """获取推荐流。

    Returns [{aweme_id, desc, author:{nickname}, ...}, ...]
    """
    from dy_apis.douyin_api import DouyinAPI
    auth = _get_auth()
    try:
        return DouyinAPI.get_feed(auth, count=count)
    except Exception:
        return []


def get_comments(aweme_url: str) -> list:
    """获取作品评论。

    Returns [{cid, text, user:{nickname}, digg_count, ...}, ...]
    """
    from dy_apis.douyin_api import DouyinAPI
    auth = _get_auth()
    try:
        return DouyinAPI.get_work_all_comment(auth, aweme_url)
    except Exception:
        return []


# --------------------------------------------------------------- CLI
def main():
    if len(sys.argv) < 2:
        print("用法:", file=sys.stderr)
        print("  python -m douyin.api.content search <关键词>", file=sys.stderr)
        print("  python -m douyin.api.content user <用户主页URL>", file=sys.stderr)
        print("  python -m douyin.api.content works <用户主页URL>", file=sys.stderr)
        print("  python -m douyin.api.content feed", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        results = search_videos(query)
        _print_results(results, ["aweme_id", "desc"])
    elif cmd == "user":
        url = sys.argv[2] if len(sys.argv) > 2 else ""
        info = get_user_info(url)
        print(json.dumps(info, ensure_ascii=False, indent=2))
    elif cmd == "works":
        url = sys.argv[2] if len(sys.argv) > 2 else ""
        works = get_user_works(url)
        _print_results(works, ["aweme_id", "desc"])
    elif cmd == "feed":
        feed = get_feed()
        _print_results(feed, ["aweme_id", "desc", "author"])
    else:
        print(f"未知命令: {cmd}", file=sys.stderr)
        sys.exit(1)


def _print_results(items: list, keys: list[str], limit: int = 10):
    for i, item in enumerate(items[:limit]):
        vals = {}
        for k in keys:
            if k == "author" and isinstance(item.get("author"), dict):
                vals[k] = item["author"].get("nickname", "?")
            elif k == "desc":
                vals[k] = (item.get(k, "") or "")[:60]
            else:
                vals[k] = item.get(k, "?")
        print(f"  {i+1:2}. " + " | ".join(f"{k}={v}" for k, v in vals.items()))
    if len(items) > limit:
        print(f"  ... ({len(items)} total, showing {limit})")


if __name__ == "__main__":
    main()