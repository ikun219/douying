"""
抖音 IM (私信) Protobuf 接口。

用法:
    from api.im import create_conversation, send_msg
    from api._session import get_auth

    auth = get_auth()
    conv = create_conversation(auth, to_user_id=123456789)
    send_msg(auth, *conv, "你好~")
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DOUYIN = _HERE.parent
if str(_DOUYIN) not in sys.path:
    sys.path.insert(0, str(_DOUYIN))

from dy_apis.douyin_api import DouyinAPI


def create_conversation(auth, to_user_id: int) -> tuple[str, int, str]:
    """创建私信对话 (Protobuf)。返回 (conversation_id, conversation_short_id, ticket)。"""
    return DouyinAPI.create_conversation(auth, to_user_id)


def send_msg(auth, conversation_id: str, conversation_short_id: int, ticket: str, content: str) -> bool:
    """发送私信 (Protobuf)。返回 True=成功。"""
    return DouyinAPI.send_msg(auth, conversation_id, conversation_short_id, ticket, content)