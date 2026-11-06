"""
抖音直播接口。

用法:
    from api.live import get_live_info, get_live_production, get_all_live_production, get_live_production_detail
    from api._session import get_auth

    auth = get_auth()
    info = get_live_info(auth, "123456789")
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DOUYIN = _HERE.parent
if str(_DOUYIN) not in sys.path:
    sys.path.insert(0, str(_DOUYIN))

from dy_apis.douyin_api import DouyinAPI


def get_live_info(auth, live_id: str) -> dict | None:
    """获取直播间信息（room_id, user_id, room_status, room_title 等）。

    :param auth: DouyinAuth 对象
    :param live_id: 直播间 ID（URL 末尾部分）
    :return: 直播间信息字典，或 None
    """
    return DouyinAPI.get_live_info(auth, live_id)


def get_live_production(auth, url: str, room_id: str, author_id: str, offset: str = "0") -> dict:
    """获取直播间商品列表（单页）。

    :param auth: DouyinAuth 对象
    :param url: 直播间完整 URL
    :param room_id: 直播间 room_id
    :param author_id: 主播 author_id
    :param offset: 翻页游标
    :return: JSON 商品列表
    """
    return DouyinAPI.get_live_production(auth, url, room_id, author_id, offset)


def get_all_live_production(auth, url: str) -> list:
    """获取直播间所有商品（自动翻页直到 next_offset=-1）。

    :param auth: DouyinAuth 对象
    :param url: 直播间完整 URL
    :return: 所有商品列表
    """
    return DouyinAPI.get_all_live_production(auth, url)


def get_live_production_detail(auth, url: str, promotion_id: str, author_id: str, room_id: str) -> dict:
    """获取直播间单个商品详情。

    :param auth: DouyinAuth 对象
    :param url: 直播间完整 URL
    :param promotion_id: 商品 ec_promotion_id
    :param author_id: 主播 sec_author_id
    :param room_id: 直播间 live_room_id
    :return: JSON 商品详情
    """
    return DouyinAPI.get_live_production_detail(auth, url, promotion_id, author_id, room_id)