"""
共享 HTTP Session + cookie 加载 + DouyinAuth 工厂。

取代散落各文件的独立 requests 调用，统一管理：
- requests.Session（连接复用，池化）
- cookie 加载（从 cookies.json / storage_state.json / env 三重读取）
- DouyinAuth 初始化（整合 web_protect / keys_str）
- HeaderBuilder / Params 快捷构造

用法:
    from api._session import get_auth, get_session

    auth = get_auth()           # 自动加载 cookie → DouyinAuth
    session = get_session()     # 模块级连接池 Session
    headers = auth.build_headers(HeaderType.GET)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

# 确保 douyin/ 可在 sys.path 找到 builder / dy_apis / utils
_HERE = Path(__file__).resolve().parent  # douyin/api/
_DOUYIN = _HERE.parent                   # douyin/
if str(_DOUYIN) not in sys.path:
    sys.path.insert(0, str(_DOUYIN))

from builder.header import HeaderBuilder, HeaderType
from builder.params import Params

requests.packages.urllib3.disable_warnings()

# --------------------------------------------------------------- session
_SESSION = requests.Session()


def get_session() -> requests.Session:
    return _SESSION


# --------------------------------------------------------------- auth 工厂
class DouyinAuth:
    """与 builder.auth.DouyinAuth 兼容的轻量认证对象。"""

    def __init__(self):
        self.cookie: dict[str, str] = {}
        self.cookie_str: str = ""
        self.msToken: str = ""
        self.uid: str = ""
        # 风控凭据（可选）
        self.web_protect: str = ""
        self.keys_str: str = ""

    def prepare(self, cookie_dict: dict[str, str], web_protect: str = "", keys_str: str = ""):
        self.cookie = cookie_dict
        self.cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
        self.msToken = cookie_dict.get("msToken", "")
        self.uid = cookie_dict.get("uid", "")
        self.web_protect = web_protect
        self.keys_str = keys_str

    def build_headers(self, ht: HeaderType = HeaderType.GET) -> HeaderBuilder:
        h = HeaderBuilder().build(ht)
        if self.cookie_str:
            h.set_cookie(self.cookie_str)
        return h

    def build_params(self) -> Params:
        p = Params()
        return p


# --------------------------------------------------------------- cookie 加载
_COOKIES_FILE = _DOUYIN / "cookies.json"
_STATE_FILE = _DOUYIN / "storage_state.json"
_AUTH_CACHE: DouyinAuth | None = None


def _load_from_cookies_json() -> dict[str, str] | None:
    if not _COOKIES_FILE.exists():
        return None
    try:
        data = json.loads(_COOKIES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _load_from_storage_state() -> dict[str, str] | None:
    if not _STATE_FILE.exists():
        return None
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        cookies_list = data.get("cookies", [])
        return {c["name"]: c["value"] for c in cookies_list if "name" in c and "value" in c}
    except Exception:
        pass
    return None


def _load_from_env() -> dict[str, str] | None:
    cookie_str = os.environ.get("DY_COOKIES", "")
    if not cookie_str:
        return None
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies or None


def load_cookies(force: bool = False) -> dict[str, str]:
    """从 cookies.json / storage_state.json / env 三重读取 cookie。

    优先级：cookies.json > storage_state.json > DY_COOKIES 环境变量。
    force=True 强制重读（忽略缓存）。
    """
    for loader in (_load_from_cookies_json, _load_from_storage_state, _load_from_env):
        cookies = loader()
        if cookies and len(cookies) >= 3:
            return cookies
    return {}


def get_auth(force: bool = False) -> DouyinAuth:
    """获取或创建 DouyinAuth 对象（模块级缓存）。

    force=True 强制重建。
    """
    global _AUTH_CACHE
    if _AUTH_CACHE is not None and not force:
        return _AUTH_CACHE

    auth = DouyinAuth()
    cookies = load_cookies(force=force)
    if cookies:
        auth.prepare(cookies)
    _AUTH_CACHE = auth
    return auth


# --------------------------------------------------------------- 探测
def is_authed() -> bool:
    """是否已有可用 cookie。"""
    cookies = load_cookies()
    return "sessionid" in cookies