#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""汐缘 抖音私信自动回复机器人（常驻）—— 线程编排 + 启动入口。

消息路由 / 媒体处理 / 轮询 / 看门狗 → core/router.py
记忆 / 上下文组装            → core/memory.py
工具函数 / 表情包 / 智能体   → core/tools.py
LLM 调用                     → llm_providers.py
抖音 API 收发                → dy_apis/ + api/
"""

import sys, io, os, json, hashlib, time, traceback, threading, asyncio, random, faulthandler

faulthandler.enable()
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'dy_apis'))  # 必须在 HERE 之前：dy_apis 里也有 builder/，会盖掉真正的 builder
sys.path.insert(0, os.path.dirname(HERE))           # 项目根 (llm_providers.py)
sys.path.insert(0, HERE)

import requests
requests.packages.urllib3.disable_warnings()
import websocket
from websocket import WebSocketApp

from builder.auth import DouyinAuth
from builder.header import HeaderBuilder
from builder.params import Params
from dy_apis.douyin_api import DouyinAPI
from llm_providers import deepseek_chat, DEEPSEEK_KEY, DEEPSEEK_MODEL, DEEPSEEK_URL

from core.memory import MemoryStore
from core.tools import (
    init as tools_init,
    _ark_conf,
    load_persona, load_mood_line,
    acquire_single_instance, send_to_master_qq,
)
from core.text_util import _clean_reply
from core.router import MessageRouter

# ═══════════════════════════ 常量 ═══════════════════════════

BRAIN_URL     = 'http://127.0.0.1:6220/chat'
BRAIN_ENABLE  = True
BRAIN_TIMEOUT = 150

PERSONA_FILE   = os.path.join(HERE, 'persona.txt')
MOOD_FILE      = os.path.join(HERE, '..', 'astrbot', 'data', 'plugin_data',
                              'astrbot_plugin_inner_mood', 'mood.json')
LOG_FILE       = os.path.join(HERE, 'reply_bot.log')
ARK_CFG_PATH   = os.path.join(HERE, 'ark_config.json')

ARK_API_BASE = 'https://ark.cn-beijing.volces.com/api/v3'

MASTER_MAP = {'3900658741936829': '老大'}

LIVINGMEMORY_DB     = os.path.join(HERE, '..', 'astrbot', 'data', 'plugin_data',
                                   'astrbot_plugin_livingmemory', 'conversations.db')
QQ_SESSION          = '汐缘:FriendMessage:1905347904'
DOUYIN_MEMORY_FILE  = os.path.join(HERE, 'douyin_memory.jsonl')
OUTBOX_FILE         = os.path.join(HERE, 'outbox.jsonl')
RESTART_NOTIFY_FILE = os.path.join(HERE, 'restart_notify.json')

# 本进程 python.exe 路径（handle_restart 固定用 .venv 解释器，避免串到系统 python）
HERE_PY = os.path.join(HERE, '.venv', 'Scripts', 'python.exe')

QQ_TOOL = {
    'type': 'function',
    'function': {
        'name': 'send_qq_to_master',
        'description': (
            '把一条消息真正发到老大（主人）的 QQ。'
            '当老大说"在QQ发给我""QQ上发一句""发我QQ"时调用。'
            '注意：只有调用这个工具才算真的发了，不调用就别声称已经发了。'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'message': {'type': 'string', 'description': '要发到 QQ 的消息内容'},
            },
            'required': ['message'],
        },
    },
}


# ═══════════════════════════ 模块级日志 ═══════════════════════════

def log(*a):
    line = ' '.join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S ') + line + '\n')
    except Exception:
        pass


# ═══════════════════════════ ReplyBot ═══════════════════════════

class ReplyBot:
    appKey = "e1bd35ec9db7b8d846de66ed140b1ad9"
    fpId = '9'

    def __init__(self, auth):
        # ---- 全局工具初始化（tools.py 需要 HERE / log / 路径） ----
        tools_init(
            session=requests.Session(),
            log_fn=log,
            root_dir=HERE,
            outbox_file=OUTBOX_FILE,
            mood_file=MOOD_FILE,
            persona_file=PERSONA_FILE,
            log_file=LOG_FILE,
        )

        self.log = log
        self.auth = auth
        self.my_uid = auth.get_uid()
        self.processed = set()
        self.last_frame_ts = time.time()
        self._last_content_ts = time.time()
        self.stranger_hist = {}       # uid -> [回复时间戳]，防刷屏
        self._video_dedup = {}        # itemId/aweme_id -> 处理时间戳
        self._recent_videos = {}      # sender_str -> {title_norm, title, desc, ts}
        self._http_evt = threading.Event()
        self._ws_text_ts = {}         # (发送者, 文本) -> 时间戳（防 WS/HTTP 双重回话）

        # 文件路径（router 通过 self.bot 访问）
        self._HERE = HERE
        self._HERE_PY = HERE_PY
        self._RESTART_NOTIFY_FILE = RESTART_NOTIFY_FILE
        self._shared_watched = os.path.join(HERE, 'douyin_watched.jsonl')
        self._HTTP_STATE = os.path.join(HERE, 'http_poll_state.json')
        self.conv_file = os.path.join(HERE, 'conversations.json')
        self.conv_cache = {}
        try:
            self.conv_cache = json.load(open(self.conv_file, encoding='utf-8'))
        except Exception:
            self.conv_cache = {}

        self.persona = load_persona()

        # 记忆存储
        self.memory = MemoryStore(DOUYIN_MEMORY_FILE, LIVINGMEMORY_DB, QQ_SESSION, log)

        # 消息路由器（协作者模式：self 作为 bot 传入）
        self.router = MessageRouter(self)

        # 连接复用 Session（_brain_reply + ARK 路径用）
        self._http = requests.Session()

        # WS 连接参数
        deviceId  = DouyinAPI.get_device_id(auth=auth)
        accessKey = f'{self.fpId}{self.appKey}{deviceId}f8a69f1719916z'
        accessKey = hashlib.md5(accessKey.encode('UTF-8')).hexdigest()
        params = Params()
        (params
         .add_param("aid", "6383")
         .add_param("device_platform", "douyin_pc")
         .add_param("fpid", self.fpId)
         .add_param("device_id", deviceId)
         .add_param("token", self.auth.cookie["sessionid"])
         .add_param("access_key", accessKey))
        self.url = f"wss://frontier-im.douyin.com/ws/v2?{params.toString()}"
        log(f"[init] my_uid={self.my_uid} device_id={deviceId}")

    # ═══════════════════ 记忆/限流（thin wrappers） ═══════════════════

    def _write_message(self, role, content):
        self.memory.write_message(role, content)

    def _allow_stranger(self, uid):
        """非老大私信限流：同一人 5 秒/条、每小时最多 60 条，防刷屏。"""
        now = time.time()
        rec = self.stranger_hist.setdefault(uid, [])
        rec[:] = [t for t in rec if now - t < 3600]
        if len(rec) >= 60:
            return False
        if rec and now - rec[-1] < 5:
            return False
        rec.append(now)
        return True

    # ═══════════════════ 发送 ═══════════════════

    def send_reply(self, sender_uid, text):
        uid = str(sender_uid)
        try:
            from send_text import send_text_sync, send_text_to_uid
            if uid in MASTER_MAP:
                r = send_text_sync(text, conv_name='潮汐')
            else:
                r = send_text_to_uid(text, uid)
            if r.get("ok"):
                log(f"[reply -> {uid}] {text} (browser)")
                return True
            log(f"[send browser fail] {str(r)[:160]}")
            return False
        except Exception as e:
            log(f"[send browser err] {e}")
            traceback.print_exc()
            return False

    # ═══════════════════ 思考（薄 coordinator） ═══════════════════

    def _brain_reply(self, text, sender_id=None, sender_label=None,
                     is_master=True, use_pro=False):
        """调用主脑，返回 (reply_text, image_paths) 或 None。"""
        if not BRAIN_ENABLE:
            return None
        payload = {
            'text': text,
            'sender_id': str(sender_id or ''),
            'sender_name': str(sender_label or ''),
            'is_master': bool(is_master),
            'use_pro': bool(use_pro),
        }
        try:
            r = self._http.post(BRAIN_URL, json=payload, timeout=BRAIN_TIMEOUT)
        except Exception as e:
            log(f"[brain] 连不上主脑（{e}），回退本地模型")
            return None
        if r.status_code != 200:
            log(f"[brain] HTTP {r.status_code} {r.text[:120]}，回退本地模型")
            return None
        try:
            d = r.json()
        except Exception:
            log('[brain] 返回不是 JSON，回退本地模型')
            return None
        if not d.get('ok'):
            log(f"[brain] 主脑没给出回复：{d.get('error')}，回退本地模型")
            return None
        reply = _clean_reply(d.get('reply') or '')
        images = d.get('images') or []
        if reply:
            log(f"[brain] 主脑回复（{len(reply)} 字{' + ' + str(len(images)) + ' 张表情包' if images else ''}）")
            return (reply, images)
        return None

    def generate_reply(self, text, sender_label=None, is_master=True,
                       use_pro=False, sender_id=None):
        """返回 (reply_text, image_paths) 或 None。"""
        # 先问主脑
        result = self._brain_reply(text, sender_id=sender_id,
                                   sender_label=sender_label,
                                   is_master=is_master, use_pro=use_pro)
        if result:
            return result

        # ---- 本地 LLM 兜底 ----
        who = f"\n\n[正在和你私信的人是：{sender_label}]" if sender_label else ""

        hist = self.memory.read_recent_qq() if is_master else ''
        hist_block = f"\n\n[最近和老大在QQ上的聊天记录，供你记忆连贯]\n{hist}" if hist else ""

        vids = self.memory.recent_video_lines() if is_master else ''
        vid_block = (
            '\n\n[老大以前发给你的抖音视频/分享（你是真看过的，用来回忆；'
            '如果老大这会儿发来新的视频，以新的那条为准，绝不许拿旧的顶替）]\n' + vids
            if vids else ''
        )

        system = self.persona + load_mood_line() + who + hist_block + vid_block
        messages = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': text},
        ]

        pro = _ark_conf() if use_pro else {}
        if use_pro and pro.get('key'):
            url = ARK_API_BASE.rstrip('/') + '/chat/completions'
            key = pro['key']
            payload = {
                'model': pro.get('model') or 'doubao-seed-2-1-pro-260915',
                'messages': messages,
                'max_tokens': 400,
                'temperature': 0.7,
            }
            log(f"[llm] 条件路由 → 豆包 {payload['model']}")
            payload['messages'] = [
                {'role': 'system',
                 'content': system + '\n\n[只输出你要发出去的那一句话。不要写"回应思路""最终回复"这类标注，不要解释。]'},
                {'role': 'user', 'content': text},
            ]
        else:
            try:
                msg = deepseek_chat(messages, tools=[QQ_TOOL] if is_master else None)
                if msg is None:
                    return None
                for _ in range(3):
                    tool_calls = msg.get('tool_calls') or []
                    if not tool_calls:
                        return (_clean_reply(msg.get('content') or ''), [])
                    messages.append(msg)
                    for tc in tool_calls:
                        fn = tc.get('function', {})
                        name = fn.get('name')
                        try:
                            args = json.loads(fn.get('arguments') or '{}')
                        except Exception:
                            args = {}
                        if name == 'send_qq_to_master':
                            ok = send_to_master_qq(args.get('message', ''))
                            result = '已真发到老大QQ' if ok else '发送失败'
                        else:
                            result = f'未知工具 {name}'
                        messages.append({'role': 'tool',
                                         'tool_call_id': tc.get('id', ''),
                                         'content': result})
                    msg = deepseek_chat(messages, tools=[QQ_TOOL] if is_master else None)
                return None
            except Exception as e:
                log(f"[deepseek err] {e}")
                return None

        # 豆包路径（ARK API）
        try:
            for _ in range(4):
                r = self._http.post(
                    url,
                    headers={'Authorization': f'Bearer {key}',
                             'Content-Type': 'application/json'},
                    json=payload, timeout=60,
                )
                if r.status_code != 200:
                    log(f"[deepseek] HTTP {r.status_code} {r.text[:160]}")
                    return None
                d = r.json()
                msg = d['choices'][0]['message']
                tool_calls = msg.get('tool_calls') or []
                if not tool_calls:
                    return (_clean_reply(msg.get('content') or ''), [])
                messages.append(msg)
                for tc in tool_calls:
                    fn = tc.get('function', {})
                    name = fn.get('name')
                    try:
                        args = json.loads(fn.get('arguments') or '{}')
                    except Exception:
                        args = {}
                    if name == 'send_qq_to_master':
                        ok = send_to_master_qq(args.get('message', ''))
                        result = '已真发到老大QQ' if ok else '发送失败'
                    else:
                        result = f'未知工具 {name}'
                    messages.append({'role': 'tool',
                                     'tool_call_id': tc.get('id', ''),
                                     'content': result})
            return None
        except Exception as e:
            log(f"[deepseek err] {e}")
            return None

    # ═══════════════════ WebSocket 回调 ═══════════════════

    def on_open(self, ws):
        log("[ws] connected")
        self._notify_restart_done()
        if not getattr(self, "_warm_done", False):
            self._warm_done = True
            self._warm_sender()

    def on_message(self, ws, message):
        """全部消息路由交给 MessageRouter。"""
        self.router.on_message(ws, message)

    def on_error(self, ws, error):
        log(f"[ws error] {error}")

    def on_close(self, ws, code, msg):
        log(f"[ws closed] {code} {msg}")

    # ═══════════════════ 辅助 ═══════════════════

    def _warm_sender(self):
        """提前拉起常驻浏览器，第一条私信不用等 11 秒冷启动。"""
        try:
            if os.environ.get("SEND_TEXT_PREWARM", "1") == "0":
                log("[send_text] 预热已关闭（SEND_TEXT_PREWARM=0）")
                return
            from send_text import warm_sender_async
            warm_sender_async()
            log("[send_text] 已在后台预热常驻浏览器")
        except Exception as e:
            log(f"[send_text] 预热启动失败: {e}")

    def _notify_restart_done(self):
        """重启后首次连上 WS，给老大回一句确认（只回一次，发完删标记）。"""
        try:
            if os.path.exists(RESTART_NOTIFY_FILE):
                flag = json.load(open(RESTART_NOTIFY_FILE, encoding='utf-8'))
                if time.time() - float(flag.get('ts', 0)) < 180:
                    self.send_reply(flag.get('uid'), '重启成功啦，我回来啦～')
                os.remove(RESTART_NOTIFY_FILE)
        except Exception:
            pass

    @staticmethod
    def _heartbeat_writer():
        """每 10 秒写 heartbeat.json，供 AstrBotPanel.hta 检测进程死活"""
        hb_path = os.path.join(HERE, 'heartbeat.json')
        while True:
            try:
                with open(hb_path, 'w', encoding='utf-8') as f:
                    json.dump({'ts': time.time(), 'pid': os.getpid()}, f)
            except Exception:
                pass
            time.sleep(10)

    # ═══════════════════ 启动 ═══════════════════

    def start(self):
        # 心跳
        threading.Thread(target=self._heartbeat_writer,
                         name="dy-heartbeat", daemon=True).start()
        # 风控 watchdog
        try:
            import risk_guard
            threading.Thread(target=risk_guard.run_watchdog,
                             name="dy-risk-guard", daemon=True).start()
        except Exception:
            pass
        # 沉默重连 watchdog
        threading.Thread(target=self.router.poll.silence_watchdog,
                         name="dy-silence-wd", daemon=True).start()
        # 常驻有头浏览器
        try:
            import headed_browser
            threading.Thread(target=headed_browser.start,
                             name="dy-headed-browser", daemon=True).start()
        except Exception:
            pass
        # 评论回复通知 watchdog
        threading.Thread(target=self.router.poll.notification_watchdog,
                         name="dy-notify-wd", daemon=True).start()
        # HTTP 轮询兜底
        threading.Thread(target=self.router.poll.http_poll_loop,
                         name="dy-http-poll", daemon=True).start()

        while True:
            try:
                self.ws = WebSocketApp(
                    url=self.url,
                    header={
                        'Pragma': 'no-cache',
                        'Accept-Language':
                            'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
                        'User-Agent': HeaderBuilder.ua,
                        'Cache-Control': 'no-cache',
                        'Sec-WebSocket-Protocol': 'binary, base64, pbbp2',
                        'Sec-WebSocket-Extensions':
                            'permessage-deflate; client_max_window_bits',
                    },
                    cookie=self.auth.cookie_str,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    on_open=self.on_open,
                )
                self.ws.run_forever(
                    origin='https://www.douyin.com',
                    ping_interval=20,
                    ping_timeout=10,
                )
            except KeyboardInterrupt:
                log("[exit] KeyboardInterrupt")
                break
            except Exception as e:
                log(f"[run err] {e}")
            log("[ws] reconnect in 5s ...")
            time.sleep(5)


# ═══════════════════════════ 启动辅助 ═══════════════════════════

def load_auth():
    cookie_str = ''
    for l in open(os.path.join(HERE, '.env'), encoding='utf-8'):
        l = l.strip()
        if l.startswith('DY_COOKIES='):
            cookie_str = l[len('DY_COOKIES='):]
    keys_str = json.load(open(os.path.join(HERE, 'auth_keys.json'),
                               encoding='utf-8'))['keys_str']
    auth = DouyinAuth()
    auth.perepare_auth(cookie_str, '', keys_str)
    auth.ticket = ''
    auth.ts_sign = ''
    auth.client_cert = ''
    return auth


if __name__ == '__main__':
    if not acquire_single_instance():
        log("[dup] 已有 reply_bot 在运行，本实例退出（避免重复启动）")
        sys.exit(0)
    auth = load_auth()
    bot = ReplyBot(auth)
    bot.start()