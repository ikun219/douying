"""
抖音社交互动接口 (关注/粉丝/点赞/收藏)。

用法:
    from api.social import collect_aweme, digg, digg_live_room, get_follower_list, get_following_list, search_user
    from api._session import get_auth

    auth = get_auth()
    result = digg(auth, "7123456789012345678")
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DOUYIN = _HERE.parent
if str(_DOUYIN) not in sys.path:
    sys.path.insert(0, str(_DOUYIN))

from dy_apis.douyin_api import DouyinAPI


# --------------------------------------------------------------- 收藏
def collect_aweme(auth, aweme_id: str, action: str = "1") -> dict:
    """收藏或取消收藏视频。

    :param auth: DouyinAuth 对象 (来自 _session.get_auth())
    :param aweme_id: 视频 aweme_id
    :param action: "1"=收藏, "0"=取消收藏
    :return: 抖音 API 响应 JSON
    """
    return DouyinAPI.collect_aweme(auth, aweme_id, action)


# --------------------------------------------------------------- 点赞
def digg(auth, aweme_id: str, action: str = "1") -> bool:
    """点赞或取消点赞视频。

    :param auth: DouyinAuth 对象
    :param aweme_id: 视频 aweme_id
    :param action: "1"=点赞, "0"=取消点赞
    :return: True=成功
    """
    return DouyinAPI.digg(auth, aweme_id, action)


# --------------------------------------------------------------- 直播间点赞
def digg_live_room(auth, room_id: str, count: str = "1"):
    """点赞直播间。

    :param auth: DouyinAuth 对象
    :param room_id: 直播间 room_id
    :param count: 点赞次数
    """
    return DouyinAPI.diggLiveRoom(auth, room_id, count)


# --------------------------------------------------------------- 搜索用户
def search_user(auth, query: str, offset: str = "0", num: str = "25",
                follower_order: str = "", user_type: str = "") -> dict:
    """搜索抖音用户。

    :param auth: DouyinAuth 对象
    :param query: 搜索关键字
    :param offset: 翻页游标
    :param num: 每页数量
    :return: 抖音 API 响应 JSON
    """
    return DouyinAPI.search_user(auth, query, offset, num, follower_order, user_type)


# --------------------------------------------------------------- 粉丝列表
def get_follower_list(auth, user_id: str, sec_id: str, max_time: str = "0", count: str = "20") -> dict:
    """获取用户粉丝列表。

    :param auth: DouyinAuth 对象
    :param user_id: 用户 user_id (数字)
    :param sec_id: 用户 sec_id (字符串)
    :param max_time: 翻页游标
    :param count: 每页数量
    :return: 抖音 API 响应 JSON
    """
    return DouyinAPI.get_user_follower_list(auth, user_id, sec_id, max_time, count)


# --------------------------------------------------------------- 关注列表
def get_following_list(auth, user_id: str, sec_id: str, max_time: str = "0", count: str = "20") -> dict:
    """获取用户关注列表。

    :param auth: DouyinAuth 对象
    :param user_id: 用户 user_id (数字)
    :param sec_id: 用户 sec_id (字符串)
    :param max_time: 翻页游标
    :param count: 每页数量
    :return: 抖音 API 响应 JSON
    """
    return DouyinAPI.get_user_following_list(auth, user_id, sec_id, max_time, count)