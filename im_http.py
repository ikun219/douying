#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""im_http.py —— 用 Cookie 直连抖音 IM 的 protobuf 接口，纯 HTTP 读私信。

为什么要有它（2026-10-28）：
    抖音网页版 IM 的推送走 WS，那条连接会「静默死掉」——TCP 还 Established、
    心跳也正常，但服务端不再推任何帧；而浏览器 DOM 里判「谁发的」只能靠头像落点猜、
    拿不到时间戳。「分享卡片」在 WS 里甚至是以**自己的 uid** 推下来的，会被当成回显丢掉。

    imapi.douyin.com 的 protobuf 接口只认 Cookie：一次请求 ~0.4 秒拿到全部已建立会话
    + 每会话最近 20 条消息，每条带**真实毫秒时间戳**、**发送者 uid**、
    卡片还带 `itemId` / `content_title` / `cover_url`（`aweType == 800`）。

    只读：本模块没有任何发送代码，发消息仍然走 send_text.py（真实网页前端）。

协议信封是对照页面真实请求构造的（思路来自 NEWJO123/dsh-douyin-scroll 的 tools/im-http.py，
字段语义用我们自己的会话实测校准过）。
"""
from __future__ import annotations

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
IM_HOST = "https://imapi.douyin.com"
WWW = "https://www.douyin.com"
COMMON = "device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1"


# ────────────────────────── protobuf 最小实现 ──────────────────────────
def _ev(n):
    o = bytearray()
    v = int(n)
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            o.append(b | 0x80)
        else:
            o.append(b)
            return bytes(o)


def _enc_v(fn, n):
    return _ev((fn << 3) | 0) + _ev(n)


def _enc_ld(fn, d):
    b = d if isinstance(d, bytes) else str(d).encode('utf-8')
    return _ev((fn << 3) | 2) + _ev(len(b)) + b


def _read_varint(b, i):
    shift = 0
    r = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << shift
        if not (x & 0x80):
            return r, i
        shift += 7


def _parse(b):
    """宽容解析：遇到不认识的字段/坏字节就停，不抛。"""
    m = {}
    i, n = 0, len(b)
    while i < n:
        try:
            tag, i = _read_varint(b, i)
        except IndexError:
            break
        fn, wt = tag >> 3, tag & 7
        if not fn:
            break
        try:
            if wt == 0:
                val, i = _read_varint(b, i)
            elif wt == 1:
                val = b[i:i + 8]
                i += 8
            elif wt == 5:
                val = b[i:i + 4]
                i += 4
            elif wt == 2:
                ln, i = _read_varint(b, i)
                val = b[i:i + ln]
                i += ln
            else:
                break
        except IndexError:
            break
        m.setdefault(fn, []).append(val)
    return m


def _g1(m, k, d=None):
    v = m.get(k)
    return v[0] if v else d


def _s(v):
    return v.decode('utf-8', 'replace') if isinstance(v, bytes) else ('' if v is None else str(v))


_SEQ = [10000 + int(time.time() * 1000) % 89]


def _build_envelope(cmd, body):
    _SEQ[0] += 1

    def kv(k, v):
        return _enc_ld(15, _enc_ld(1, k) + _enc_ld(2, v))

    return b''.join([
        _enc_v(1, cmd), _enc_v(2, _SEQ[0]), _enc_ld(3, '0.1.8'), _enc_ld(4, b''),
        _enc_v(5, 3), _enc_v(6, 1),
        _enc_ld(7, '0d50935:feat/pc-im-group'), _enc_ld(8, body), _enc_ld(9, '0'),
        _enc_ld(11, 'douyin_pc'), _enc_ld(14, '360000'),
        kv('session_aid', '6383'), kv('session_did', '0'), kv('app_name', 'douyin_pc'),
        kv('priority_region', 'cn'), kv('user_agent', UA), kv('cookie_enabled', 'true'),
        kv('browser_language', 'zh-CN'), kv('browser_platform', 'Win32'),
        kv('browser_name', 'Mozilla'),
        kv('browser_version', '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                             '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'),
        kv('browser_online', 'true'), kv('screen_width', '800'), kv('screen_height', '600'),
        kv('timezone_name', 'Asia/Shanghai'), kv('deviceId', '0'), kv('is-retry', '0'),
        _enc_v(18, 1), _enc_ld(21, 'douyin_web'), _enc_ld(22, 'web_sdk'),
    ])


# ────────────────────────── Cookie ──────────────────────────
def load_cookie() -> str:
    """优先用浏览器落盘的最新 storage_state（登录态以浏览器为准）。"""
    for name in ('storage_state.json', 'auth_cookies.json'):
        p = os.path.join(HERE, name)
        try:
            d = json.load(open(p, encoding='utf-8'))
        except Exception:
            continue
        cs = d.get('cookies') if isinstance(d, dict) else None
        if not cs:
            continue
        return '; '.join('%s=%s' % (c['name'], c['value'])
                         for c in cs if isinstance(c, dict) and c.get('name'))
    raise RuntimeError('找不到可用的 cookie（storage_state.json）')


def _session(cookie):
    try:
        from curl_cffi import requests as creq
        s = creq.Session(impersonate='chrome')
    except ImportError:
        import requests
        s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Cookie': cookie,
                      'Referer': WWW + '/', 'Origin': WWW})
    return s


def peer_uid_of(conv_id: str, my_uid: str) -> str:
    parts = str(conv_id or '').split(':')
    uids = [p for p in parts if p.isdigit() and len(p) >= 8]
    other = next((u for u in uids if u != my_uid), None)
    return other or (uids[-1] if uids else '')


def fetch(my_uid: str = '', limit: int = 25, timeout: int = 15) -> dict:
    """拉一次全部会话 + 每会话最近消息。

    返回 {ok, my_uid, conversations: [{conv_id, type, peer_uid, messages: [...]}]}
    messages 按时间旧→新，每条 {ts, from_uid, text, awe, item_id, title, cover_url}。
    """
    cookie = load_cookie()
    s = _session(cookie)
    if not my_uid:
        r = s.get(WWW + '/aweme/v1/web/user/profile/self/?' + COMMON, timeout=timeout)
        my_uid = str(((r.json() or {}).get('user') or {}).get('uid') or '')
        if not my_uid:
            return {'ok': False, 'error': '查登录态失败（Cookie 可能过期）'}

    r = s.post(IM_HOST + '/v1/message/get_message_by_init',
               data=_build_envelope(2043, _enc_ld(2043, b'')),
               headers={'Content-Type': 'application/x-protobuf',
                        'Accept': 'application/x-protobuf'},
               timeout=timeout)
    if r.status_code != 200:
        return {'ok': False, 'error': f'IM HTTP {r.status_code}', 'my_uid': my_uid}
    env = _parse(r.content)
    status = _s(_g1(env, 4))
    if status != 'OK':
        return {'ok': False, 'error': f'IM 状态 {status or "(空)"}', 'my_uid': my_uid}

    wrap = _parse(_g1(env, 6) or b'')
    sub = _parse(_g1(wrap, 2043) or b'')
    convs = []
    for blob in sub.get(1) or []:
        if not isinstance(blob, bytes):
            continue
        conv = _parse(blob)
        core = _parse(_g1(conv, 1) or b'')
        conv_id = _s(_g1(core, 1))
        msgs = []
        for m in conv.get(2) or []:
            if not isinstance(m, bytes):
                continue
            f = _parse(m)
            from_uid = _s(_g1(f, 7))
            raw = _s(_g1(f, 8))
            ts = int(_g1(f, 10) or 0)
            try:
                j = json.loads(raw)
            except Exception:
                j = {}
            text = str(j.get('text') or '').strip()
            title = str(j.get('content_title') or '').strip()
            cover = ''
            cu = j.get('cover_url')
            if isinstance(cu, dict):
                urls = cu.get('url_list') or []
                cover = urls[0] if urls else ''
            msgs.append({
                'ts': ts,
                'from_uid': from_uid,
                'text': text,
                'title': title,
                'awe': int(j.get('aweType') or 0),
                'item_id': str(j.get('itemId') or ''),
                'cover_url': cover,
                'from_me': from_uid == my_uid,
            })
        msgs.sort(key=lambda x: x['ts'] or 0)
        convs.append({
            'conv_id': conv_id,
            'type': int(_g1(core, 3) or 0),
            'peer_uid': peer_uid_of(conv_id, my_uid),
            'messages': msgs[-limit:],
        })
    return {'ok': True, 'my_uid': my_uid, 'conversations': convs}
