import json
import re
import urllib
import uuid

from builder.header import HeaderBuilder, HeaderType
from builder.params import Params
from builder.proto import ProtoBuilder
from utils.dy_util import splice_url, generate_a_bogus, generate_msToken, trans_cookies
from ._http import _SESSION, DOUYIN_URL, LIVE_URL


class SocialMixin:
    @classmethod
    def collect_aweme(cls, auth, aweme_id: str, action: str = '1', **kwargs):
        """
        收藏或取消收藏视频.
        :param auth: DouyinAuth object.
        :param aweme_id: 视频ID.
        :param action: 1: 收藏, 0: 取消收藏.
        :return: 响应JSON.
        """
        api = '/aweme/v1/web/aweme/collect/'
        headers = HeaderBuilder().build(HeaderType.FORM)
        refer = "https://www.douyin.com/?recommend=1"
        headers.set_referer(refer)
        headers.with_bd(api, auth)
        headers.with_csrf(auth.cookie_str)
        headers.set_header("origin", cls.douyin_url)
        params = Params()
        params.add_param("device_platform", "webapp")
        params.add_param("aid", "6383")
        params.add_param("channel", "channel_pc_web")
        params.add_param("pc_client_type", "1")
        params.add_param("update_version_code", "170400")
        params.add_param("version_code", "170400")
        params.add_param("version_name", "17.4.0")
        params.add_param("cookie_enabled", "true")
        params.add_param("screen_width", "1707")
        params.add_param("screen_height", "960")
        params.add_param("browser_language", "zh-CN")
        params.add_param("browser_platform", "Win32")
        params.add_param("browser_name", "Edge")
        params.add_param("browser_version", "125.0.0.0")
        params.add_param("browser_online", "true")
        params.add_param("engine_name", "Blink")
        params.add_param("engine_version", "125.0.0.0")
        params.add_param("os_name", "Windows")
        params.add_param("os_version", "10")
        params.add_param("cpu_core_num", "32")
        params.add_param("device_memory", "8")
        params.add_param("platform", "PC")
        params.add_param("downlink", "10")
        params.add_param("effective_type", "4g")
        params.add_param("round_trip_time", "50")
        params.with_web_id(auth, refer)
        params.add_param("verifyFp", auth.cookie['s_v_web_id'])
        params.add_param("fp", auth.cookie['s_v_web_id'])
        params.add_param("msToken", auth.msToken)
        data = {
            "action": action,
            "aweme_id": aweme_id,
            "aweme_type": "0",
        }
        params.with_a_bogus(data)
        res = _SESSION.post(f'{cls.douyin_url}{api}', headers=headers.get(), params=params.get(),
                            cookies=auth.cookie, data=data, verify=False)
        return res.json()

    @classmethod
    def move_collect_aweme(cls, auth, aweme_id: str, collect_name: str, collect_id: str, **kwargs):
        """
        移动视频到指定收藏夹（需要先收藏视频）
        :param collect_name: 收藏夹名称
        :param collect_id: 收藏夹ID
        :param auth: DouyinAuth object.
        :param aweme_id: 视频ID.
        :return: 响应JSON.
        """
        api = '/aweme/v1/web/collects/video/move/'
        headers = HeaderBuilder().build(HeaderType.FORM)
        refer = "https://www.douyin.com/?recommend=1"
        headers.set_referer(refer)
        headers.with_bd(api, auth)
        headers.with_csrf(auth.cookie_str)
        headers.set_header("origin", cls.douyin_url)
        params = Params()
        params.add_param("aid", "6383")
        params.add_param("browser_language", "zh-CN")
        params.add_param("browser_name", "Edge")
        params.add_param("browser_online", "true")
        params.add_param("browser_platform", "Win32")
        params.add_param("browser_version", "125.0.0.0")
        params.add_param("channel", "channel_pc_web")
        params.add_param("collects_name", collect_name)
        params.add_param("cookie_enabled", "true")
        params.add_param("cpu_core_num", "32")
        params.add_param("device_memory", "8")
        params.add_param("device_platform", "webapp")
        params.add_param("downlink", "10")
        params.add_param("effective_type", "4g")
        params.add_param("engine_name", "Blink")
        params.add_param("engine_version", "125.0.0.0")
        params.add_param("item_ids", aweme_id)
        params.add_param("item_type", "2")
        params.add_param("move_collects_list", collect_id)
        params.add_param("os_name", "Windows")
        params.add_param("os_version", "10")
        params.add_param("pc_client_type", "1")
        params.add_param("platform", "PC")
        params.add_param("round_trip_time", "50")
        params.add_param("screen_height", "960")
        params.add_param("screen_width", "1707")
        params.add_param("to_collects_id", collect_id)
        params.add_param("update_collects_sort", "true")
        params.add_param("update_version_code", "170400")
        params.add_param("version_code", "170400")
        params.add_param("version_name", "17.4.0")
        params.with_web_id(auth, refer)
        params.add_param("verifyFp", auth.cookie['s_v_web_id'])
        params.add_param("fp", auth.cookie['s_v_web_id'])
        params.add_param("msToken", auth.msToken)
        params.with_a_bogus()
        res = _SESSION.post(f'{cls.douyin_url}{api}', headers=headers.get(), params=params.get(),
                            cookies=auth.cookie, verify=False)
        return res.json()

    @classmethod
    def remove_collect_aweme(cls, auth, aweme_id: str, collect_name: str, collect_id: str, **kwargs):
        """
        从指定收藏夹中移除视频（需要先收藏视频）
        :param collect_name: 收藏夹名称
        :param collect_id: 收藏夹ID
        :param auth: DouyinAuth object.
        :param aweme_id: 视频ID.
        :return: 响应JSON.
        """
        api = '/aweme/v1/web/collects/video/move/'
        headers = HeaderBuilder().build(HeaderType.FORM)
        refer = "https://www.douyin.com/user/self?showTab=favorite_collection"
        headers.set_referer(refer)
        headers.with_bd(api, auth)
        headers.with_csrf(auth.cookie_str)
        headers.set_header("origin", cls.douyin_url)
        params = Params()
        params.add_param("aid", "6383")
        params.add_param("browser_language", "zh-CN")
        params.add_param("browser_name", "Edge")
        params.add_param("browser_online", "true")
        params.add_param("browser_platform", "Win32")
        params.add_param("browser_version", "125.0.0.0")
        params.add_param("channel", "channel_pc_web")
        params.add_param("collects_name", collect_name)
        params.add_param("cookie_enabled", "true")
        params.add_param("cpu_core_num", "32")
        params.add_param("device_memory", "8")
        params.add_param("device_platform", "webapp")
        params.add_param("downlink", "10")
        params.add_param("effective_type", "4g")
        params.add_param("engine_name", "Blink")
        params.add_param("engine_version", "125.0.0.0")
        params.add_param("from_collects_id", collect_id)
        params.add_param("item_ids", aweme_id)
        params.add_param("item_type", "2")
        params.add_param("os_name", "Windows")
        params.add_param("os_version", "10")
        params.add_param("pc_client_type", "1")
        params.add_param("platform", "PC")
        params.add_param("round_trip_time", "50")
        params.add_param("screen_height", "960")
        params.add_param("screen_width", "1707")
        params.add_param("update_version_code", "170400")
        params.add_param("version_code", "170400")
        params.add_param("version_name", "17.4.0")
        params.with_web_id(auth, refer)
        params.add_param("verifyFp", auth.cookie['s_v_web_id'])
        params.add_param("fp", auth.cookie['s_v_web_id'])
        params.add_param("msToken", auth.msToken)
        params.with_a_bogus()
        res = _SESSION.post(f'{cls.douyin_url}{api}', headers=headers.get(), params=params.get(),
                            cookies=auth.cookie, verify=False)
        return res.json()

    @classmethod
    def get_collect_list(cls, auth, **kwargs):
        """
        获取我的收藏夹列表
        :param auth: DouyinAuth object.
        :return: JSON.
        """
        api = "/aweme/v1/web/collects/list/"
        headers = HeaderBuilder().build(HeaderType.GET)
        refer = "https://www.douyin.com/?recommend=1"
        headers.set_referer(refer)
        params = Params()
        params.add_param("device_platform", "webapp")
        params.add_param("aid", "6383")
        params.add_param("channel", "channel_pc_web")
        params.add_param("cursor", "0")
        params.add_param("count", "20")
        params.add_param("update_version_code", "170400")
        params.add_param("pc_client_type", "1")
        params.add_param("version_code", "170400")
        params.add_param("version_name", "17.4.0")
        params.add_param("cookie_enabled", "true")
        params.add_param("screen_width", "1707")
        params.add_param("screen_height", "960")
        params.add_param("browser_language", "zh-CN")
        params.add_param("browser_platform", "Win32")
        params.add_param("browser_name", "Edge")
        params.add_param("browser_version", "125.0.0.0")
        params.add_param("browser_online", "true")
        params.add_param("engine_name", "Blink")
        params.add_param("engine_version", "125.0.0.0")
        params.add_param("os_name", "Windows")
        params.add_param("os_version", "10")
        params.add_param("cpu_core_num", "32")
        params.add_param("device_memory", "8")
        params.add_param("platform", "PC")
        params.add_param("downlink", "5.95")
        params.add_param("effective_type", "4g")
        params.add_param("round_trip_time", "200")
        params.with_web_id(auth, refer)
        params.add_param("msToken", auth.msToken)
        params.with_a_bogus()
        params.add_param("verifyFp", auth.cookie['s_v_web_id'])
        params.add_param("fp", auth.cookie['s_v_web_id'])
        res = _SESSION.get(f'{cls.douyin_url}{api}', headers=headers.get(), params=params.get(),
                           cookies=auth.cookie, verify=False)
        return res.json()
    @classmethod
    def publish_comment(cls, auth, aweme_id: str, content: str = '', reply_id="", **kwargs):
        """
        发布评论
        :param auth: DouyinAuth object.
        :param aweme_id: 视频ID.
        :param content: 评论内容.
        :param reply_id: 回复评论ID.
        :return: JSON.
        """
        api = "/aweme/v1/web/comment/publish"
        headers = HeaderBuilder().build(HeaderType.FORM)
        refer = f"https://www.douyin.com/discover?modal_id={aweme_id}"
        headers.set_header("Origin", cls.douyin_url)
        headers.with_bd(api, auth)
        headers.with_csrf(auth.cookie_str)
        headers.set_referer(refer)
        params = Params()
        params.add_param("app_name", 'aweme')
        params.add_param("enter_from", 'discover')
        params.add_param("previous_page", 'discover')
        params.add_param("device_platform", 'webapp')
        params.add_param("aid", '6383')
        params.add_param("channel", 'channel_pc_web')
        params.add_param("pc_client_type", '1')
        params.add_param("update_version_code", '170400')
        params.add_param("version_code", '170400')
        params.add_param("version_name", '17.4.0')
        params.add_param("cookie_enabled", 'true')
        params.add_param("screen_width", '1707')
        params.add_param("screen_height", '960')
        params.add_param("browser_language", 'zh-CN')
        params.add_param("browser_platform", 'Win32')
        params.add_param("browser_name", 'Edge')
        params.add_param("browser_version", '125.0.0.0')
        params.add_param("browser_online", 'true')
        params.add_param("engine_name", 'Blink')
        params.add_param("engine_version", '125.0.0.0')
        params.add_param("os_name", 'Windows')
        params.add_param("os_version", '10')
        params.add_param("cpu_core_num", '32')
        params.add_param("device_memory", '8')
        params.add_param("platform", 'PC')
        params.add_param("downlink", '10')
        params.add_param("effective_type", '4g')
        params.add_param("round_trip_time", '100')
        params.with_web_id(auth, refer)
        params.add_param("msToken", auth.msToken)
        data = {
            "aweme_id": aweme_id,
            "comment_send_celltime": random.randint(1000, 20000),
            "comment_video_celltime": random.randint(1000, 20000),
        }
        if reply_id != "":
            data["reply_id"] = reply_id
        data["text"] = content
        data["text_extra"] = []
        params.with_a_bogus(data)
        params.add_param("verifyFp", auth.cookie['s_v_web_id'])
        params.add_param("fp", auth.cookie['s_v_web_id'])
        res = _SESSION.post(f'{cls.douyin_url}{api}', headers=headers.get(), params=params.get(),
                            cookies=auth.cookie, data=data, verify=False)
        return res.json()

    @classmethod
    def create_conversation(cls, auth, to_user_id: int, **kwargs):
        """
        创建私信对话.
        :param auth: DouyinAuth object.
        :param to_user_id: 私信对话接收者ID.
        :return: 私信对话ID.
        """
        url = "https://imapi.douyin.com/v2/conversation/create"
        requestProto = ProtoBuilder.build_create_conversation_request(auth, to_user_id, auth.get_uid())
        headers = HeaderBuilder().build(HeaderType.PROTOBUF)
        headers.set_header('referer', 'https://www.douyin.com/')

        resp = _SESSION.post(
            url,
            headers=headers.get(),
            cookies=auth.cookie,
            data=requestProto.SerializeToString(),
            verify=False
        )
        responseProto = ResponseProto.Response()
        responseProto.ParseFromString(resp.content)
        resp_json = protobuf_to_dict(responseProto)
        conversation = resp_json['body']['create_conversation_v2_body']['conversation_info_list'][0]
        conversation_id = conversation['conversation_id']
        conversation_short_id = int(conversation['conversation_short_id'])
        ticket = conversation['ticket']
        return conversation_id, conversation_short_id, ticket

    @classmethod
    def get_conversation_list(cls, auth, aweme_id: str, **kwargs) -> list:
        pass

    @classmethod
    def send_msg(cls, auth, conversation_id, conversation_short_id, ticket, content: str, **kwargs) -> bool:
        """
        发送私信.
        :param auth: DouyinAuth object.
        :param conversation_id: 私信对话ID.
        :param conversation_short_id: 私信对话短ID.
        :param ticket: 私信对话票据.
        :param content: 私信内容.
        :return: True 发送成功 False 发送失败
        """
        url = 'https://imapi.douyin.com/v1/message/send'
        headers = HeaderBuilder().build(HeaderType.PROTOBUF)
        headers.set_header('referer', 'https://www.douyin.com/')
        requestProto = ProtoBuilder.build_send_message_request(auth, conversation_id, conversation_short_id, ticket,
                                                               content)
        params = {
            'verifyFp': auth.cookie['s_v_web_id'],
            'fp': auth.cookie['s_v_web_id'],
            'msToken': generate_msToken()
        }
        query = splice_url(params)
        abogus = generate_a_bogus(query)
        params['a_bogus'] = abogus
        resp = _SESSION.post(url, params=params, headers=headers.get(), verify=False, cookies=auth.cookie,
                             data=requestProto.SerializeToString())
        responseProto = ResponseProto.Response()
        responseProto.ParseFromString(resp.content)
        resp_json = protobuf_to_dict(responseProto)
        print(resp_json)
    @classmethod
    def digg(cls, auth, aweme_id: str, digg_type: str = '1', **kwargs) -> bool:
        """
        点赞视频.
        :param auth: DouyinAuth object.
        :param aweme_id: 视频ID.
        :param digg_type: 点赞类型, 1: 点赞, 0: 取消点赞.
        :return: 0 点赞成功 1 取消点赞成功
        """
        api = '/aweme/v1/web/commit/item/digg/'
        url = f'{cls.douyin_url}{api}'
        refer = f'{cls.douyin_url}/discover?modal_id={aweme_id}'
        headers = HeaderBuilder.build(HeaderType.FORM)
        headers.set_header("Host", cls.douyin_url.split("https://")[-1])
        headers.with_bd(api, auth)
        headers.with_csrf(auth.cookie_str)
        headers.set_header("origin", cls.douyin_url)
        headers.set_header("referer", refer)
        params = Params()
        params.with_platform()
        params.with_ms_token()
        params.with_web_id(auth, refer)
        params.add_param('verifyFp', auth.cookie['s_v_web_id'])
        params.add_param('fp', auth.cookie['s_v_web_id'])
        params.with_a_bogus()
        data = {
            'aweme_id': aweme_id,
            'item_type': '0',
            'type': digg_type,
        }
        resp = _SESSION.post(url, params=params.get(), headers=headers.get(), cookies=auth.cookie, data=data,
                             verify=False)
        print(resp.text)
        resp_json = json.loads(resp.text)
        return resp_json['is_digg'] == 0
