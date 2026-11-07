import json
import re
import urllib
import uuid

from builder.header import HeaderBuilder, HeaderType
from builder.params import Params
from builder.proto import ProtoBuilder
from utils.dy_util import splice_url, generate_a_bogus, generate_msToken, trans_cookies
from ._http import _SESSION, DOUYIN_URL, LIVE_URL


class IdentityMixin:
    @classmethod
    def get_my_uid(cls, auth, **kwargs) -> int:
        """
        获取自己的用户ID.
        :param auth: DouyinAuth object.
        :return: 用户ID.
        """
        url = 'https://www.douyin.com/aweme/v1/web/query/user/'
        headers = HeaderBuilder().build(HeaderType.GET)
        refer = 'https://www.douyin.com/'
        headers.set_header('referer', refer)
        params = Params()
        params.with_platform()
        params.with_web_id(auth, refer)
        params.with_ms_token()
        params.add_param('verifyFp', auth.cookie['s_v_web_id'])
        params.add_param('fp', auth.cookie['s_v_web_id'])
        params.with_a_bogus()
        resp = _SESSION.get(url, params=params.get(), verify=False, headers=headers.get(), cookies=auth.cookie)
        resp_json = json.loads(resp.text)
        return int(resp_json['user_uid'])

    @classmethod
    def get_my_sec_uid(cls, auth, **kwargs) -> str:
        """
        获取自己的SECID.
        :param auth: DouyinAuth object.
        :return: SECID.
        """
        headers = HeaderBuilder().build(HeaderType.GET)
        url = "https://www.douyin.com/user/self"
        params = {
            "from_tab_name": "main"
        }
        response = _SESSION.get(url, headers=headers.get(), cookies=auth.cookie, params=params)
        sec_uid = re.findall(r'\\"secUid\\":\\"(.*?)\\"', response.text)[0]
        return sec_uid

    @classmethod
    def get_device_id(cls, auth, **kwargs) -> str:
        """
        获取设备ID.
        :param auth: DouyinAuth object.
        :return: 设备ID.
        """
        url = "https://www.douyin.com/aweme/v1/web/query/user"
        headers = HeaderBuilder().build(HeaderType.GET)
        refer = "https://www.douyin.com/discover"
        headers.set_header("referer", refer)
        params = Params()
        (params
         .with_platform()
         .add_param("publish_video_strategy_type", "2")
         .with_web_id(auth, refer)
         .with_ms_token()
         .add_param('verifyFp', auth.cookie['s_v_web_id'])
         .add_param('fp', auth.cookie['s_v_web_id'])
         .with_a_bogus()
         )
        resp = _SESSION.get(url, params=params.get(), verify=False, headers=headers.get(), cookies=auth.cookie)
        resp_json = json.loads(resp.text)
        return resp_json['id']

