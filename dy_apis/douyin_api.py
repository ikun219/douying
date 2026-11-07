# -*- coding: utf-8 -*-
"""抖音 API 统一入口 — 由 5 个领域 mixin 组装而成。

模块化拆分（dy_apis/ 目录内）：
  _http.py     : 共享 _SESSION + URL 常量
  _content.py  : 作品信息 / 评论 / 综合搜索 / 视频搜索 / 推荐流 / 收藏
  _user.py     : 用户信息 / 搜索用户 / 粉丝 / 关注 / 通知
  _live.py     : 直播搜索 / 直播信息 / 商品 / 榜单 / 点赞 / 弹幕
  _social.py   : 收藏 / 评论 / 私信 / 点赞视频
  _identity.py : 我的 UID / 设备 ID

所有 mixin 方法均为 @classmethod，跨域调用通过 cls.get_xxx() 完成。
外部调用 DouyinAPI.get_xxx(auth, ...) 保持完全兼容。
"""

import json
from google.protobuf.json_format import MessageToDict as _MessageToDict


def protobuf_to_dict(msg):
    return _MessageToDict(msg, preserving_proto_field_name=True)


import static.Response_pb2 as ResponseProto  # noqa: E402 (after protobuf_to_dict)

from ._content import ContentMixin
from ._user import UserMixin
from ._live import LiveMixin
from ._social import SocialMixin
from ._identity import IdentityMixin


class DouyinAPI(ContentMixin, UserMixin, LiveMixin, SocialMixin, IdentityMixin):
    douyin_url = 'https://www.douyin.com'
    live_url = 'https://live.douyin.com'
    creator = "https://creator.douyin.com"


if __name__ == '__main__':
    web_protect_str = r''
    keys_str = r''
    cookies_str = ''

    from builder.auth import DouyinAuth
    auth_ = DouyinAuth()
    auth_.perepare_auth(cookies_str, web_protect_str, keys_str)

    live_url = "https://live.douyin.com/852953608964"
    live_id = "852953608964"
    res = DouyinAPI.get_live_info(auth_, live_id)
    print(res)

    room_id = res['room_id']
    anchor_id = res['anchor_id']
    sec_anchor_id = res['sec_uid']
    DouyinAPI.get_rank_list(auth_, room_id, anchor_id, sec_anchor_id)
    # res = DouyinAPI.search_live(auth_, "三角洲")

    # my_uid = DouyinAPI.get_my_uid(auth_)
    # print(my_uid)
    # my_sec_uid = DouyinAPI.get_my_sec_uid(auth_)
    # print(my_sec_uid)
    # print(DouyinAPI.get_user_info(auth_, "https://www.douyin.com/user/MS4wLjABAAAA7BDbZk0LjnEMcDDsLag5mDrMc157hD3x0SMhH1HaCM8"))
    # print(DouyinAPI.digg(auth_, "7433523124836060416", "1"))
    # print(DouyinAPI.digg(auth_, "7212619184386182435", "1"))
    # user_info = DouyinAPI.get_user_info(auth_, "https://www.douyin.com/user/MS4wLjABAAAAHXtdycTLMSe5Ld_468-9HKR1HUUrk4ywq-xMCM-E9w_cDIrhmynrQUalv061ZSpn?from_tab_name=main")
    # print(user_info)
    # conversation_id, conversation_short_id, ticket = DouyinAPI.create_conversation(auth_, to_user_id)
    # print(conversation_id, conversation_short_id, ticket)
    # DouyinAPI.send_msg(auth_, conversation_id, conversation_short_id, ticket, content)
    # print(DouyinAPI.get_user_all_work_info(auth_,"https://www.douyin.com/user/MS4wLjABAAAA8nC7nKxMrRtBwEqFzRgRBSxhBcw89VL0ysN-IXvhlKU?vid=7378825215213718818"))
    # print(DouyinAPI.get_work_info(auth_, "https://www.douyin.com/video/7212619184386182435"))
    # print(DouyinAPI.get_work_all_out_comment(auth_, "https://www.douyin.com/video/7212619184386182435"))
    # print(DouyinAPI.get_work_inner_comment(auth_, {
    #     'aweme_id': '7212619184386182435',
    #     'cid': '7237287000211325744',
    # }, '0'))
    # print(DouyinAPI.get_work_all_inner_comment(auth_, {
    #     'aweme_id': '7212619184386182435',
    #     'cid': '7237287000211325744',
    # }))
    # print(DouyinAPI.get_work_all_comment(auth_, "https://www.douyin.com/video/7212619184386182435"))
    # print(DouyinAPI.search_general_work(auth_, "美女", sort_type='2'))
    # print(DouyinAPI.search_some_general_work(auth_, "美女", sort_type='2', publish_time='0', num=30))
    # print(DouyinAPI.get_all_live_production(auth_, "https://live.douyin.com/84255891276"))

    # room_info = DouyinAPI.get_live_info(auth_, '60503986163')
    # room_id = room_info['room_id']
    # print(DouyinAPI.get_live_production(auth_, "https://live.douyin.com/84255891276", room_id, author_id, '0'))
    # print(DouyinAPI.collect_aweme(auth_, "7377676120549772554", '1'))
    # print(DouyinAPI.move_collect_aweme(auth_, "7207861673711930656", "tt", "7379252593215919891"))
    # print(DouyinAPI.remove_collect_aweme(auth_, "7376244589235113250", "tt", "7379252593215919891"))
    # print(DouyinAPI.get_live_production_detail(auth_, "https://live.douyin.com/552370739330", "3622058061409408240", "MS4wLjABAAAATfhR-kvE-AWqZaNaomCLFqgDKzvBwMS87FUGVjS_u7Y", "7379220637308504843"))
    # print(DouyinAPI.get_collect_list(auth_))
    # print(DouyinAPI.search_user(auth_, "巴旦木公主"))
    # print(DouyinAPI.search_some_user(auth_, "巴旦木公主", 30))
    # print(DouyinAPI.search_live(auth_, "馨馨baby😐ᵇᵃᵇʸ"))
    # print(DouyinAPI.get_user_favorite(auth_, "MS4wLjABAAAA99bTJ_GOw3odYmsXOe7i7xuEv0iQf2X_Kg_VUyVP0U8"))
    # print(DouyinAPI.get_some_user_follower_list(auth_, "3074704605975950", "MS4wLjABAAAA0L4jpkJDeuFO9AM-dQK1B649tmr7GIw-sQtyPasP_Z45QnUjIQgUOLIs8Kw8Gp-u", 40))
    # print(DouyinAPI.get_some_user_following_list(auth_, "3074704605975950", "MS4wLjABAAAA0L4jpkJDeuFO9AM-dQK1B649tmr7GIw-sQtyPasP_Z45QnUjIQgUOLIs8Kw8Gp-u", 40))
    # print(DouyinAPI.search_some_video_work(auth_, "巴旦木公主", 32))
    # print(DouyinAPI.get_feed(auth_))
    # print(DouyinAPI.publish_comment(auth_, "7356193166732709139"))
    # print(DouyinAPI.get_upload_auth_key(auth_))

    #     print(f"diggLiveRoom")
    #     print(DouyinAPI.sendMsgInRoom(auth_, room_id, "666"))

    #     room_id = room_info['room_id']
    #     print(DouyinAPI.diggLiveRoom(auth_, room_id, '10'))