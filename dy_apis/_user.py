import json
import re
import urllib
import uuid

from builder.header import HeaderBuilder, HeaderType
from builder.params import Params
from builder.proto import ProtoBuilder
from utils.dy_util import splice_url, generate_a_bogus, generate_msToken, trans_cookies
from ._http import _SESSION, DOUYIN_URL, LIVE_URL


class UserMixin:
    @classmethod
    def get_user_info(cls, auth, user_url: str, **kwargs) -> dict:
        """
        获取用户信息.
        :param auth: DouyinAuth object.
        :param user_url: 用户主页URL.
        :return: 用户信息.
        """
        api = f"/aweme/v1/web/user/profile/other/"
        user_id = user_url.split("/")[-1].split("?")[0]
        headers = HeaderBuilder().build(HeaderType.GET)
        headers.set_referer(user_url)
        params = Params()
        params.add_param("device_platform", 'webapp')
        params.add_param("aid", '6383')
        params.add_param("channel", 'channel_pc_web')
        params.add_param("publish_video_strategy_type", '2')
        params.add_param("source", 'channel_pc_web')
        params.add_param("sec_user_id", user_id)
        params.add_param("personal_center_strategy", '1')
        params.add_param("update_version_code", '170400')
        params.add_param("pc_client_type", '1')
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
        params.with_web_id(auth, user_url)
        params.add_param("msToken", auth.msToken)
        params.add_param('verifyFp', auth.cookie['s_v_web_id'])
        params.add_param('fp', auth.cookie['s_v_web_id'])
        params.with_a_bogus()
        resp = _SESSION.get(f'{cls.douyin_url}{api}', headers=headers.get(), cookies=auth.cookie,
                            params=params.get(), verify=False)
        return json.loads(resp.text)

    @classmethod
    def search_some_user(cls, auth, query: str, num: int, **kwargs) -> list:
        """
        搜索指定数量用户.
        :param auth: DouyinAuth object.
        :param query: 搜索关键字.
        :param num: 搜索结果数量.
        :return: 用户列表.
        """
        offset = "0"
        count = "25"
        user_list = []
        while True:
            res_json = cls.search_user(auth, query, offset, count)
            users = res_json["user_list"]
            user_list.extend(users)
            if res_json["has_more"] != 1 or len(user_list) >= num:
                break
            offset = str(int(offset) + int(count))
        if len(user_list) > num:
            user_list = user_list[:num]
        return user_list


    @classmethod
    def search_user(cls, auth, query: str, offset: str = '0', num: str = '25', douyin_user_fans="", douyin_user_type="", **kwargs):
        """
        搜索用户.
        :param auth: DouyinAuth object.
        :param query:  搜索关键字.
        :param offset:  搜索结果偏移量.
        :param num:  搜索结果数量.
        :param douyin_user_fans: 粉丝数量 空字符串 (0_1k 1000以下) (1k_1w 1000-10000) (1w_10w 10000-100000) (10w_100w 10w-100w粉丝) (100w_ 100w以上)
        :param douyin_user_type: 用户类型 空字符串 不限 common_user 普通用户 enterprise_user 企业用户 personal_user 个人认证用户
        :return: JSON数据.
        """
        api = "/aweme/v1/web/discover/search"
        headers = HeaderBuilder().build(HeaderType.GET)
        refer = f'https://www.douyin.com/search/{urllib.parse.quote(query)}?aid={uuid.uuid4()}&type=general'
        headers.set_referer(refer)
        params = Params()
        params.add_param("device_platform", 'webapp')
        params.add_param("aid", '6383')
        params.add_param("channel", 'channel_pc_web')
        params.add_param("search_channel", 'aweme_user_web')
        params.add_param("search_filter_value", r'{"douyin_user_fans":["%s"],"douyin_user_type":["%s"]}' % (
            douyin_user_fans, douyin_user_type))
        params.add_param("keyword", query)
        params.add_param("search_source", 'switch_tab')
        params.add_param("query_correct_type", '1')
        params.add_param("is_filter_search", '1')
        # params.add_param("from_group_id", '7378456704385600820')
        params.add_param("offset", offset)
        params.add_param("count", num)
        params.add_param("need_filter_settings", '1' if offset == '0' else '0')
        params.add_param("list_type", 'single')
        params.add_param("update_version_code", '170400')
        params.add_param("pc_client_type", '1')
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
        params.add_param("round_trip_time", '150')
        params.with_web_id(auth, refer)
        params.add_param("msToken", auth.msToken)
        params.with_a_bogus()
        resp = _SESSION.get(f'{cls.douyin_url}{api}', headers=headers.get(), cookies=auth.cookie,
                            params=params.get(), verify=False)
        return resp.json()

    @classmethod
    def get_user_follower_list(cls, auth, user_id: str, sec_id: str, max_time: str = '0', count: str = '20', **kwargs):
        """
        获取用户的粉丝列表
        :param auth: DouyinAuth object.
        :param user_id: 用户ID.
        :param sec_id: 用户sec_id.
        :param max_time: 最大时间戳.
        :param count: 数量.
        :return:  JSON.
        """
        api = "/aweme/v1/web/user/follower/list/"
        headers = HeaderBuilder().build(HeaderType.GET)
        refer = f"https://www.douyin.com/user/{sec_id}"
        headers.set_referer(refer)
        params = Params()
        params.add_param("device_platform", 'webapp')
        params.add_param("aid", '6383')
        params.add_param("channel", 'channel_pc_web')
        params.add_param("user_id", user_id)
        params.add_param("sec_user_id", sec_id)
        params.add_param("offset", '0')
        params.add_param("min_time", '0')
        params.add_param("max_time", max_time)
        params.add_param("count", count)
        params.add_param("source_type", '2' if max_time == '0' else '1')
        params.add_param("gps_access", '0')
        params.add_param("address_book_access", '0')
        params.add_param("update_version_code", '170400')
        params.add_param("pc_client_type", '1')
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
        params.add_param("round_trip_time", '150')
        params.with_web_id(auth, refer)
        params.add_param("msToken", auth.msToken)
        params.with_a_bogus()
        params.add_param("verifyFp", auth.cookie['s_v_web_id'])
        params.add_param("fp", auth.cookie['s_v_web_id'])
        res = _SESSION.get(f'{cls.douyin_url}{api}', headers=headers.get(), params=params.get(),
                           cookies=auth.cookie, verify=False)
        return res.json()

    @classmethod
    def get_some_user_follower_list(cls, auth, user_id: str, sec_id: str, num: int, **kwargs) -> list:
        """
        获取用户的前num个粉丝列表
        :param auth: DouyinAuth object.
        :param user_id: 用户ID.
        :param sec_id: 用户sec_id.
        :param num: 要获取的数量
        :return: 粉丝列表.
        """
        max_time = "0"
        count = "20"
        follower_list = []
        while True:
            res_json = cls.get_user_follower_list(auth, user_id, sec_id, max_time, count)
            followers = res_json["followers"]
            follower_list.extend(followers)
            if res_json["has_more"] != 1 or len(follower_list) >= num:
                break
            max_time = res_json["min_time"]
        if len(follower_list) > num:
            follower_list = follower_list[:num]
        return follower_list

    @classmethod
    def get_user_following_list(cls, auth, user_id: str, sec_id: str, max_time: str = '0', count: str = '20', **kwargs):
        """
        获取用户的关注列表
        :param auth: DouyinAuth object.
        :param user_id: 用户ID.
        :param sec_id: 用户sec_id.
        :param max_time: 最大时间戳.
        :param count: 数量.
        :return:
        """
        api = "/aweme/v1/web/user/following/list/"
        headers = HeaderBuilder().build(HeaderType.GET)
        refer = f"https://www.douyin.com/user/{sec_id}"
        headers.set_referer(refer)
        params = Params()
        params.add_param("device_platform", 'webapp')
        params.add_param("aid", '6383')
        params.add_param("channel", 'channel_pc_web')
        params.add_param("user_id", user_id)
        params.add_param("sec_user_id", sec_id)
        params.add_param("offset", '0')
        params.add_param("min_time", '0')
        params.add_param("max_time", max_time)
        params.add_param("count", count)
        params.add_param("source_type", '2' if max_time == '0' else '1')
        params.add_param("gps_access", '0')
        params.add_param("address_book_access", '0')
        params.add_param("is_top", '1')
        params.add_param("update_version_code", '170400')
        params.add_param("pc_client_type", '1')
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
        params.add_param("round_trip_time", '150')
        params.with_web_id(auth, refer)
        params.add_param("msToken", auth.msToken)
        params.with_a_bogus()
        params.add_param("verifyFp", auth.cookie['s_v_web_id'])
        params.add_param("fp", auth.cookie['s_v_web_id'])
        res = _SESSION.get(f'{cls.douyin_url}{api}', headers=headers.get(), params=params.get(),
                           cookies=auth.cookie, verify=False)
        return res.json()

    @classmethod
    def get_some_user_following_list(cls, auth, user_id: str, sec_id: str, num: int, **kwargs) -> list:
        """
        获取用户的前num个关注列表
        :param auth: DouyinAuth object.
        :param user_id: 用户ID.
        :param sec_id: 用户sec_id.
        :param num: 要获取的数量
        :return: 关注列表.
        """
        max_time = "0"
        count = "20"
        following_list = []
        while True:
            res_json = cls.get_user_following_list(auth, user_id, sec_id, max_time, count)
            followings = res_json["followings"]
            following_list.extend(followings)
            if res_json["has_more"] != 1 or len(following_list) >= num:
                break
            max_time = res_json["min_time"]
        if len(following_list) > num:
            following_list = following_list[:num]
        return following_list

    @classmethod
    def get_notice_list(cls, auth, min_time='0', max_time='0', count='10', notice_group='700', **kwargs):
        """
        获得通知
        :param auth: DouyinAuth object.
        :param min_time: 最小时间戳.
        :param max_time: 最大时间戳.
        :param count: 数量.
        :param notice_group: 消息类型 700 全部消息 401 粉丝 601 @我的 2 评论 3 点赞 520 弹幕
        :return: JSON.
        """
        api = "/aweme/v1/web/notice/"
        headers = HeaderBuilder().build(HeaderType.GET)
        refer = "https://www.douyin.com/?recommend=1"
        headers.set_referer(refer)
        params = Params()
        params.add_param("device_platform", 'webapp')
        params.add_param("aid", '6383')
        params.add_param("channel", 'channel_pc_web')
        params.add_param("is_new_notice", '1')
        params.add_param("is_mark_read", '1')
        params.add_param("notice_group", notice_group)
        params.add_param("count", count)
        params.add_param("min_time", min_time)
        params.add_param("max_time", max_time)
        params.add_param("update_version_code", '170400')
        params.add_param("pc_client_type", '1')
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
        params.add_param("round_trip_time", '50')
        params.with_web_id(auth, refer)
        params.add_param("msToken", auth.msToken)
        params.with_a_bogus()
        params.add_param("verifyFp", auth.cookie['s_v_web_id'])
        params.add_param("fp", auth.cookie['s_v_web_id'])
        res = _SESSION.get(f'{cls.douyin_url}{api}', headers=headers.get(), params=params.get(),
                           cookies=auth.cookie, verify=False)
        return res.json()

    @classmethod
    def get_some_notice_list(cls, auth, num: int = 20, notice_group='700', **kwargs) -> list:
        """
        获得前num条通知
        :param auth: DouyinAuth object.
        :param num: 数量.
        :param notice_group: 消息类型 | 700 全部消息 401 粉丝 601 @我的 2 评论 3 点赞 520 弹幕
        :return:
        """
        min_time = "0"
        max_time = "0"
        count = "10"
        notice_list = []
        while True:
            res_json = cls.get_notice_list(auth, min_time, max_time, count, notice_group)
            notices = res_json["notice_list_v2"]
            notice_list.extend(notices)
            if res_json["has_more"] != 1 or len(notice_list) >= num:
                break
            min_time = res_json["min_time"]
            max_time = res_json["max_time"]
        if len(notice_list) > num:
            notice_list = notice_list[:num]
        return notice_list
