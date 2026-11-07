"""
douyin.api — 抖音 API 统一入口。

用法:
    from douyin.api import (
        # 登录与认证
        login, verify, whoami, get_auth, get_session, is_authed,
        # 消息 (发送)
        send_text, send_voice, send_meme,
        # 内容 (搜索/用户/视频流)
        search_videos, search_users, get_user_info, get_user_works, get_work_info, get_feed, get_comments,
        # 社交 (点赞/收藏/粉丝/关注)
        collect_aweme, digg, digg_live_room, search_user,
        get_follower_list, get_following_list,
        # 直播
        get_live_info, get_live_production, get_all_live_production, get_live_production_detail,
        # IM (私信)
        create_conversation, send_msg,
        # 底层构造器
        HeaderBuilder, HeaderType, Params,
    )
"""

from ._builder import HeaderBuilder, HeaderType, Params
from ._session import get_auth, get_session, is_authed
from .auth import login, verify, whoami
from .content import get_comments, get_feed, get_user_info, get_user_works, get_work_info, search_users, search_videos
from .im import create_conversation, send_msg
from .live import get_all_live_production, get_live_info, get_live_production, get_live_production_detail
from .message import send_meme, send_text, send_voice
from .social import (
    collect_aweme,
    digg,
    digg_live_room,
    get_follower_list,
    get_following_list,
    search_user,
)

__all__ = [
    # auth
    "login", "verify", "whoami",
    # session
    "get_auth", "get_session", "is_authed",
    # message
    "send_text", "send_voice", "send_meme",
    # content
    "search_videos", "search_users", "get_user_info", "get_user_works", "get_work_info", "get_feed",
    # social
    "collect_aweme", "digg", "digg_live_room",
    "search_user", "get_follower_list", "get_following_list",
    # live
    "get_live_info", "get_live_production", "get_all_live_production", "get_live_production_detail",
    # IM
    "create_conversation", "send_msg",
    # builder
    "HeaderBuilder", "HeaderType", "Params",
]