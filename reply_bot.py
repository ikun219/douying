#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""汐缘 抖音私信自动回复机器人（常驻）"""
import sys, io, os, json, hashlib, time, traceback, threading, asyncio, random, faulthandler, re
from concurrent.futures import ThreadPoolExecutor

faulthandler.enable()
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # 项目根 (llm_providers.py)
sys.path.insert(0, os.path.join(HERE, 'dy_apis'))

import requests
requests.packages.urllib3.disable_warnings()
_SESSION = requests.Session()  # 连接复用：省掉每次请求的 DNS/TCP/TLS 握手
import websocket
from websocket import WebSocketApp

from builder.auth import DouyinAuth
from builder.header import HeaderBuilder
from builder.params import Params
from dy_apis.douyin_api import DouyinAPI
from static import Live_pb2, Response_pb2
from llm_providers import deepseek_chat, stt_transcribe, vlm_describe_image, DEEPSEEK_KEY, DEEPSEEK_MODEL, DEEPSEEK_URL

# ---- 主脑：把「想」这件事交给 AstrBot（astrbot_plugin_douyin_brain） ----
# 抖音这边只保留「眼睛」（抽帧/看图/轮询）和「手」（playwright 发送）
BRAIN_URL = 'http://127.0.0.1:6220/chat'
BRAIN_ENABLE = True          # False = 退回抖音自己调模型的旧模式
BRAIN_TIMEOUT = 150
PERSONA_FILE = os.path.join(HERE, 'persona.txt')
MOOD_FILE = os.path.join(HERE, '..', 'astrbot', 'data', 'plugin_data', 'astrbot_plugin_inner_mood', 'mood.json')
LOG_FILE = os.path.join(HERE, 'reply_bot.log')

# 主人（老大）抖音账号白名单：只回复这些号，其他人私信一律忽略
MASTER_MAP = {
    '3900658741936829': '老大',   # 主人的抖音测试号
}

# 长期记忆互通：读 AstrBot livingmemory 的 conversations.db（QQ 记录共享）
# 写不进去（livingmemory 目录对子进程只读），改为写 sidecar 文件，由 AstrBot 插件 ingest
LIVINGMEMORY_DB = os.path.join(HERE, '..', 'astrbot', 'data', 'plugin_data', 'astrbot_plugin_livingmemory', 'conversations.db')
QQ_SESSION = '汐缘:FriendMessage:1905347904'
DOUYIN_MEMORY_FILE = os.path.join(HERE, 'douyin_memory.jsonl')

# QQ 桥接：往 outbox 写一行，AstrBot 的 douyin_bridge 插件会转发到老大 QQ
OUTBOX_FILE = os.path.join(HERE, 'outbox.jsonl')

# 重启标记：老大发"重启"后，新进程起来会先给老大回一句"重启成功"
RESTART_NOTIFY_FILE = os.path.join(HERE, 'restart_notify.json')

QQ_TOOL = {
    'type': 'function',
    'function': {
        'name': 'send_qq_to_master',
        'description': '把一条消息真正发到老大（主人）的 QQ。当老大说“在QQ发给我”“QQ上发一句”“发我QQ”时调用。注意：只有调用这个工具才算真的发了，不调用就别声称已经发了。',
        'parameters': {
            'type': 'object',
            'properties': {
                'message': {'type': 'string', 'description': '要发到 QQ 的消息内容'},
            },
            'required': ['message'],
        },
    },
}


# 单实例保护：同一时间只允许一个 reply_bot 在跑，否则同一条私信会被回两遍
_MUTEX_HANDLE = None


def acquire_single_instance() -> bool:
    """用 Windows 命名互斥体保证单实例；已有一个在跑就返回 False。
    重试几次：旧进程刚被杀时互斥体可能还没释放（TerminateProcess 异步清理）。"""
    global _MUTEX_HANDLE
    import ctypes, time
    for attempt in range(5):
        _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\xiyuan_reply_bot_singleton")
        if ctypes.windll.kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            return True
        # 互斥体还在：可能是旧进程没死透，关掉这个句柄等一下再试
        ctypes.windll.kernel32.CloseHandle(_MUTEX_HANDLE)
        _MUTEX_HANDLE = None
        time.sleep(1)
    print("[dup] 已有 reply_bot 在运行，本实例退出（避免重复启动）", flush=True)
    return False


def send_to_master_qq(message: str) -> bool:
    """把一条消息写进 outbox，由 douyin_bridge 插件转发到老大 QQ。"""
    try:
        line = json.dumps({'text': message, 'image_url': '', 'image_path': ''}, ensure_ascii=False)
        with open(OUTBOX_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        log(f"[qq -> 老大] {message[:100]}")
        return True
    except Exception as e:
        log(f"[qq send err] {e}")
        return False


def log(*a):
    line = ' '.join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S ') + line + '\n')
    except Exception:
        pass


# ---- 分享链接：老大把视频用「链接文字」发过来时，从 URL 里抠出 aweme_id ----
def _extract_douyin_url(text: str):
    """从文本里挑出一个抖音分享链接，返回 (url, aweme_id 或 None)。"""
    import re

    pats = [
        r'https?://v\.douyin\.com/[A-Za-z0-9_\-/]+',
        r'https?://www\.douyin\.com/video/\d+',
        r'https?://www\.douyin\.com/note/\d+',
        r'https?://www\.iesdouyin\.com/share/(?:video|note)/\d+',
        r'https?://www\.douyin\.com/discover\?modal_id=\d+',
    ]
    for p in pats:
        m = re.search(p, text)
        if m:
            return m.group(0), None
    return None, None


def _resolve_aweme_id(url: str):
    """短链 v.douyin.com/xxx 重定向到完整视频页，抠出 aweme_id。"""
    try:
        r = _SESSION.get(url, allow_redirects=False, timeout=15,
                         headers={'User-Agent': HeaderBuilder.ua})
        loc = r.headers.get('Location') or ''
        if not loc:
            loc = r.text[:3000] if 'douyin.com' not in r.headers.get('Location', '') else ''
        import re

        m = re.search(r'(?:/video/|/note/|modal_id=)(\d+)', loc)
        if m:
            return m.group(1)
        m = re.search(r'(?:/video/|/note/|modal_id=)(\d+)', url)
        if m:
            return m.group(1)
    except Exception as e:
        log(f'[share] 解析短链失败 {url} {e}')
    return None


def transcribe_audio_url(url: str, timeout: int = 120):
    """下载抖音语音消息 → SenseVoice 转文字。失败返回 None。"""
    try:
        r = _SESSION.get(url, timeout=timeout)
        if r.status_code != 200 or not r.content:
            log(f"[stt] 下载失败 {r.status_code}")
            return None
        return _stt_bytes(r.content, 'voice.mp3', 'audio/mpeg', timeout)
    except Exception as e:
        log(f"[stt err] {e}")
        return None


def _stt_bytes(content: bytes, filename: str, mime: str, timeout: int = 120):
    """把一段音频丢给 SenseVoice 转文字。"""
    return stt_transcribe(content, filename, mime, timeout)


def _find_url(obj, prefer=('image', 'jpeg', 'webp', 'png', 'gif', 'aweme'), skip=('avatar',)):
    """递归找出 content 里第一个像图片地址的 URL。

    skip 里的词一旦命中就跳过——视频消息里排在前面的是发件人的**头像**
    （aweme-avatar 的 100x100 图），以前会被当成视频封面，导致她对着头像瞎猜。
    """
    urls = []

    def walk(o):
        if isinstance(o, str):
            if o.startswith('http'):
                urls.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    urls = [u for u in urls if not any(k in u.lower() for k in skip)]
    for u in urls:
        low = u.lower()
        if any(k in low for k in prefer):
            return u
    return urls[0] if urls else None


PROMPT_VIDEO_COVER = (
    '这是一个短视频的封面图。按一行回答，不要客套，不超过50字：'
    '画面：（谁/什么、在做什么、场景，图上的文字原样写出）；风格'
    '：（真人/AI生成/动画/游戏，选一个）。'
)


def _video_cover_url(content: dict):
    """视频消息取真正的封面（封面大图），不要封面小图/头像。"""
    c = content or {}
    for key in ('cover_url', 'cover', 'origin_cover'):
        v = c.get(key)
        if isinstance(v, dict):
            u = (v.get('url_list') or [None])[0]
            if u:
                return u
        elif isinstance(v, str) and v.startswith('http'):
            return v
    return _find_url(c)


ARK_CFG_PATH = os.path.join(HERE, '..', 'astrbot', 'data', 'cmd_config.json')
ARK_API_BASE = 'https://ark.cn-beijing.volces.com/api/v3'
_ARK_CACHE = {}


def _ark_conf() -> dict:
    """从 AstrBot 配置里读豆包（火山方舟）的 key 和模型名。"""
    if _ARK_CACHE:
        return _ARK_CACHE
    try:
        import json as _json

        cfg = _json.load(open(ARK_CFG_PATH, encoding='utf-8-sig'))
        key = ''
        for s in cfg.get('provider_sources') or []:
            if s.get('id') == 'ark':
                k = s.get('key')
                if isinstance(k, (list, tuple)):
                    k = k[0] if k else ''
                if isinstance(k, str) and k.strip().startswith('['):
                    try:
                        k = _json.loads(k)[0]
                    except Exception:
                        k = k.strip("[]'\" ")
                key = str(k or '').strip()
                break
        model = ''
        for p in cfg.get('provider') or []:
            pid = str(p.get('id', ''))
            m = str(p.get('model') or '')
            # deepseek / bailian 模型不支持视觉（image_url），跳过
            if pid.startswith('ark/') and 'deepseek' not in m.lower() and 'bailian' not in m.lower():
                model = m
                break
        if not model:
            model = 'doubao-seed-2-1-pro-260915'
        _ARK_CACHE.update({'key': key, 'model': model})
        log(f"[ark] 豆包识图配置 model={_ARK_CACHE['model']} key={'有' if key else '无'}")
    except Exception as e:
        log(f'[ark] 读配置失败 {e}')
    return _ARK_CACHE

def describe_image_url(url: str, timeout: int = 90, prompt: str = None):
    """使用百炼 qwen3-vl-plus 识别图片。失败返回 None。

    url 也可以直接是本地文件路径（抽帧后的拼图就是本地文件）。
    """
    return vlm_describe_image(url, timeout=timeout, prompt=prompt)


# ---- 表情包/梗 记忆（C 方案：认过一次就记住）----
STICKER_MEM_FILE = os.path.join(HERE, 'sticker_memory.json')
_STICKER_MEM = None


def _sticker_mem() -> dict:
    global _STICKER_MEM
    if _STICKER_MEM is None:
        try:
            with open(STICKER_MEM_FILE, encoding='utf-8') as f:
                _STICKER_MEM = json.load(f) or {}
        except Exception:
            _STICKER_MEM = {}
    return _STICKER_MEM


def sticker_key(content: dict, url: str = None) -> str:
    """同一张表情包在抖音有稳定 id，优先用它；没有就退回图片 URL 指纹。"""
    content = content or {}
    for k in ('from_emoji_id', 'image_id', 'id', 'sticker_id'):
        v = str(content.get(k) or '').strip()
        if v and v not in ('0', 'None'):
            return f'{k}:{v}'
    if url:
        import hashlib

        return 'url:' + hashlib.md5(url.split('?')[0].encode()).hexdigest()[:16]
    return ''


def sticker_recall(key: str):
    if not key:
        return None
    return (_sticker_mem().get(key) or {}).get('desc')


def sticker_remember(key: str, desc: str, kind: str = '表情包'):
    if not key or not desc:
        return
    mem = _sticker_mem()
    rec = mem.get(key) or {'count': 0}
    rec['desc'] = desc
    rec['kind'] = kind
    rec['count'] = int(rec.get('count') or 0) + 1
    rec['last'] = int(time.time())
    mem[key] = rec
    try:
        with open(STICKER_MEM_FILE, 'w', encoding='utf-8') as f:
            json.dump(mem, f, ensure_ascii=False, indent=1)
        log(f"[meme-mem] 记住 {key} → {desc}（第{rec['count']}次）")
    except Exception as e:
        log(f"[meme-mem] 写入失败 {e}")


def _clean_reply(txt: str) -> str:
    """豆包 pro 偶尔会先写「回应思路：…」，只留最后真正要发的那句话。"""
    txt = str(txt or '').strip()
    if not txt:
        return ''
    for marker in ('最终回复：', '最终回复:', '回复：', '回复:'):
        if marker in txt:
            txt = txt.rsplit(marker, 1)[-1].strip()
            break
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    drop = ('回应思路', '思路：', '分析：', '解释：')
    lines = [ln for ln in lines if not ln.startswith(drop)]
    txt = ' '.join(lines) if lines else txt
    return ' '.join(txt.split())


def send_voice_message(text: str) -> bool:
    """文字 → TTS mp3 → 语音 mp4 → 发抖音私信（阻塞，约 20-40 秒，放线程里跑）。"""
    try:
        from voice import build_voice
        from send_voice import send_voice_dm
        mp4 = build_voice(text)
        r = asyncio.run(send_voice_dm(str(mp4), headless=True))
        ok = bool(r.get('ok'))
        log(f"[voice -> 老大] ok={ok} {text[:40]} {r.get('error', '')}")
        return ok
    except Exception as e:
        log(f"[voice err] {e}")
        return False


def send_voice_async(text: str):
    threading.Thread(target=send_voice_message, args=(text,), daemon=True).start()


MEME_PROB = float(os.environ.get('DOUYIN_MEME_PROB', '0.1'))

# 主人明确要表情包/图片时的确定性触发（不靠随机概率）
MEME_REQUEST_KEYS = (
    '表情包', '发表情', '来张图', '发张图', '发个图', '发图',
    '要表情', '来张表情', '张表情包', '丢个表情', '表情',
)


def _wants_meme(text: str) -> bool:
    """用户文本里是否明确要求表情包/图片。"""
    return any(k in text for k in MEME_REQUEST_KEYS)


def send_meme_message(hint: str) -> bool:
    """按情绪挑一张表情包 → 发抖音私信（阻塞约 20 秒，放线程里跑）。"""
    try:
        from meme import pick_meme, send_meme_dm
        img = pick_meme(hint, cute=True)
        if not img:
            log('[meme] 表情库为空')
            return False
        r = asyncio.run(send_meme_dm(str(img), headless=True))
        ok = bool(r.get('ok'))
        log(f"[meme -> 老大] ok={ok} {img.name} {r.get('error', '')}")
        return ok
    except Exception as e:
        log(f"[meme err] {e}")
        return False


def send_meme_async(hint: str):
    threading.Thread(target=send_meme_message, args=(hint,), daemon=True).start()


def load_persona():
    return open(PERSONA_FILE, encoding='utf-8').read()


_mood_cache = {'mtime': None, 'line': None}


def load_mood_line():
    # 按 mtime 缓存：mood.json 未变就复用上次结果，省掉每条消息的磁盘读
    try:
        mt = os.path.getmtime(MOOD_FILE)
        if _mood_cache['mtime'] != mt:
            with open(MOOD_FILE, encoding='utf-8') as f:
                m = json.load(f)
            _mood_cache['mtime'] = mt
            _mood_cache['line'] = f"\n\n[当前情绪：{m.get('summary', '还算平静')}；精力 {m.get('energy', 0.5):.2f}]"
        return _mood_cache['line']
    except Exception:
        return ''


class ReplyBot:
    appKey = "e1bd35ec9db7b8d846de66ed140b1ad9"
    fpId = '9'

    def __init__(self, auth):
        self.log = log  # 兼容模块级 log()，防止 scoping 问题
        self.auth = auth
        self.my_uid = auth.get_uid()
        self.processed = set()
        self.last_frame_ts = time.time()   # 最近一次收到任何推送帧的时间（看门狗用）
        self._last_content_ts = time.time()  # 只算内容帧（排除 50001 回声），防止 watchdog 被假帧骗
        self.stranger_hist = {}   # 非老大：uid -> [回复时间戳]，防止被刷屏
        self._video_dedup = {}    # itemId/aweme_id -> 处理时间戳，卡片和链接重复推时只处理一次
        self._recent_videos = {}  # sender_str -> {title_norm, title, desc, ts}：刚看懂的视频，
                                  # 供 _handle_media 判断"同一条图文作品的视频帧/图片帧"其实是同一内容
        self._http_evt = threading.Event()          # WS 收到帧时唤醒 HTTP 回源，做到秒级又不重复
        self._ws_text_ts = {}                       # (发送者, 文本) -> 时间戳，防 WS 与 HTTP 双重回话
        self._shared_watched = os.path.join(HERE, 'douyin_watched.jsonl')  # auto_browse 的观看记录
        self.HTTP_STATE = os.path.join(HERE, 'http_poll_state.json')
        self.conv_file = os.path.join(HERE, 'conversations.json')
        self.conv_cache = {}
        try:
            self.conv_cache = json.load(open(self.conv_file, encoding='utf-8'))
        except Exception:
            self.conv_cache = {}
        self.persona = load_persona()
        deviceId = DouyinAPI.get_device_id(auth=auth)
        accessKey = f'{self.fpId}{self.appKey}{deviceId}f8a69f1719916z'
        accessKey = hashlib.md5(accessKey.encode('UTF-8')).hexdigest()
        params = Params()
        (params
         .add_param("aid", "6383")
         .add_param("device_platform", "douyin_pc")
         .add_param("fpid", self.fpId)
         .add_param("device_id", deviceId)
         .add_param("token", self.auth.cookie["sessionid"])
         .add_param("access_key", accessKey)
         )
        self.url = f"wss://frontier-im.douyin.com/ws/v2?{params.toString()}"
        log(f"[init] my_uid={self.my_uid} device_id={deviceId}")

    # ---- 识别 auto_browse 刚分享出去的视频（避免"自己看自己"） ----
    def _auto_browse_just_shared(self, aweme_id):
        """auto_browse 最近 5 分钟内分享给老大的视频，不应被当作「老大发来的」。"""
        aid = str(aweme_id)
        cutoff = time.time() - 300
        try:
            with open(self._shared_watched, encoding='utf-8') as f:
                for line in reversed(list(f)):  # 从新到旧，命中即退
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    ts = float(r.get('ts') or 0)
                    if ts < cutoff:
                        break
                    if r.get('action') == 'shared' and str(r.get('aweme_id')) == aid:
                        return True
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f'[shared-check err] {e}')
        return False

    # ---- 长期记忆互通：读 QQ 记录 / 写回抖音私信 ----
    def _recent_video_lines(self, n=3, hours=72):
        """从 douyin_memory.jsonl 捞出老大最近发来的视频/分享，拼成给 LLM 看的清单。

        没有这段注入时，间隔几小时后她会一口咬定「你没发给我」。
        """
        import time
        import re

        rows = []
        try:
            with open(DOUYIN_MEMORY_FILE, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get('role') != 'user':
                        continue
                    c = (r.get('content') or '').strip()
                    if not c.startswith(('[抖音视频', '[抖音分享', '[抖音截图')):
                        continue
                    if c[:14].find('视频封面') >= 0:  # 封面兜底那条信息少，有正式记录时别重复
                        continue
                    rows.append((float(r.get('ts') or 0), c))
        except Exception as e:  # noqa: BLE001
            log(f'[video-mem err] {e}')
            return ''
        now = time.time()
        pick = [(t, c) for t, c in rows if 0 < now - t <= hours * 3600][-n:]
        if not pick:
            return ''
        # 去重：同一视频（标题/aweme_id 相同）只保留最新一条
        seen = set()
        dedup = []
        for t, c in reversed(pick):
            # 提取关键标识：aweme_id 优先，否则用标题前 60 字
            m = re.search(r'aweme_id[：:]s*(d+)', c)
            key = m.group(1) if m else c[:60]
            if key not in seen:
                seen.add(key)
                dedup.append((t, c))
        dedup.reverse()
        out = []
        for t, c in dedup:
            mins = (now - t) / 60.0
            ago = f'{mins:.0f} 分钟前' if mins < 90 else f'{mins / 60:.0f} 小时前'
            out.append(f'- {ago}：{c[:240]}')
        return chr(10).join(out)

    def _read_recent_qq(self, n=12):
        """读最近 n 条 QQ 聊天记录，拼成对话摘录注入 DeepSeek 上下文。"""
        try:
            import sqlite3
            con = sqlite3.connect(f'file:{LIVINGMEMORY_DB}?mode=ro', uri=True)
            rows = con.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (QQ_SESSION, n),
            ).fetchall()
            con.close()
            rows = list(reversed(rows))
            lines = []
            for role, content in rows:
                name = '老大' if role == 'user' else '汐缘'
                c = ' '.join((content or '').split())[:120]
                lines.append(f"{name}: {c}")
            return '\n'.join(lines)
        except Exception as e:
            log(f"[mem read err] {e}")
            return ''

    def _write_message(self, role, content):
        """把抖音私信往来写到 sidecar 文件；AstrBot 的 douyin_bridge 插件会 ingest 进 conversations.db。
        （livingmemory 目录对子进程只读，直接写库会 readonly，所以走 sidecar。）"""
        try:
            rec = {
                'role': role,
                'content': content,
                'sender': '潮汐' if role == 'user' else '汐缘',
                'ts': time.time(),
            }
            with open(DOUYIN_MEMORY_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except Exception as e:
            log(f"[mem write err] {e}")

    # ---- 自主刷指令转发：老大说"可以/别刷/刷N小时" → 更新 auto_browse 的状态文件 ----
    def _handle_auto_cmd(self, text):
        try:
            state_file = os.path.join(HERE, 'auto_state.json')
            s = {}
            if os.path.exists(state_file):
                try:
                    s = json.load(open(state_file, encoding='utf-8'))
                except Exception:
                    s = {}
            auto_py = os.path.join(HERE, 'auto_browse.py')
            low = text.lower().replace(' ', '')
            # 先判"喊停"（任何时候生效，避免"别刷"被误判为批准）
            stop = ('别刷', '不要刷', '不准刷', '别看了', '别逛', '停', '睡觉', '够了', '休息')
            if any(w in low for w in stop):
                s['enabled'] = False
                s['pending'] = False
                s['until'] = 0
                s['until_note'] = ''
                s['unanswered_asks'] = 0
                json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                log('[auto] 老大喊停自主刷')
                return
            # 再判"风控解除"：老大说「解决了/搞定」→ 清 risk_wait，恢复自主刷
            if s.get('risk_wait'):
                # 先挡"还没解决/没搞定"这类否定句，别把"还没有"误判成"解决"
                if any(w in low for w in ('没解决', '还没', '都没有', '没搞定', '未解决', '还没好')):
                    log('[auto] 老大说还没处理好，继续等风控解除')
                else:
                    solved = ('已解决', '解决了', '搞定', '处理好', '弄好', '通过了', '过了', '解除了')
                    if any(w in low for w in solved):
                        s['risk_wait'] = False
                        json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                        log('[auto] 老大确认风控已解决，恢复自主刷')
                        # 同步复位 risk_guard（watchdog 据此恢复探测）
                        try:
                            import risk_guard
                            risk_guard.mark_solved()
                        except Exception as e:  # noqa: BLE001
                            log(f'[auto] risk_guard 复位失败: {e}')
                        return
            # 再判"时长指令"：刷N小时 / 刷N分钟 / 刷到几点 / 刷一会儿（用了 parse_until）
            kill_words = ('我知道', '明白了', '好的呢', '知道了', '哦', '懂了', '哈哈哈', '哈哈')
            # 触发词：'刷' 或 '测试/试/继续/接着/一直' + '到/至' + 时间
            has_trigger = '刷' in low or re.search(r'(?:测试|试|继续|接着|一直|帮我|给我).*(?:到|至).*(?:点|小时|分钟)', low)
            if has_trigger and not any(w in low for w in kill_words):
                try:
                    from auto_browse import parse_until as _pu
                    until, note = _pu(low)
                    if until:
                        s['enabled'] = True
                        s['pending'] = False
                        s['last_browse'] = 0
                        s['next_browse_at'] = 0
                        s['until'] = until
                        s['until_note'] = note
                        s['unanswered_asks'] = 0
                        json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                        log(f'[auto] 老大设定时长自主刷：{note}→截止 {until}')
                        # 若 auto_browse 不在跑，后台拉起它（尽力而为；已在跑会被单实例挡住）
                        try:
                            import subprocess
                            py = os.path.join(HERE, '.venv', 'Scripts', 'python.exe')
                            subprocess.Popen(
                                [py, auto_py],
                                cwd=HERE,
                                stdout=open(os.path.join(HERE, 'auto_browse_run.log'), 'a', encoding='utf-8'),
                                stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
                            )
                            log('[auto] 已后台拉起 auto_browse.py')
                        except Exception as e:
                            log(f'[auto] 拉起 auto_browse 失败（可能已在跑）: {e}')
                        return
                except Exception as e:
                    log(f'[auto] 时长解析失败: {e}')
            # 默认时长：只说"刷"/"刷抖音"没给具体时间 → 默认 2 小时
            if has_trigger and not s.get('pending') and not s.get('enabled'):
                s['enabled'] = True
                s['pending'] = False
                s['last_browse'] = 0
                s['next_browse_at'] = 0
                s['until'] = time.time() + 7200
                s['until_note'] = "刷两小时（默认）"
                s['unanswered_asks'] = 0
                json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                log('[auto] 老大说刷但没给时长，默认两小时')
                try:
                    import subprocess
                    py = os.path.join(HERE, '.venv', 'Scripts', 'python.exe')
                    subprocess.Popen(
                        [py, auto_py],
                        cwd=HERE,
                        stdout=open(os.path.join(HERE, 'auto_browse_run.log'), 'a', encoding='utf-8'),
                        stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
                    )
                    log('[auto] 已后台拉起 auto_browse.py（默认两小时）')
                except Exception as e:
                    log(f'[auto] 拉起 auto_browse 失败（可能已在跑）: {e}')
                return
            # 再判"批准"（只在 pending 时生效，避免误触发）
            if s.get('pending'):
                approve = ('可以', '好', '行', '准', '批准', '开刷', '去刷', '刷吧', 'ok', '好的')
                if any(w in low for w in approve):
                    s['enabled'] = True
                    s['pending'] = False
                    s['last_browse'] = 0  # 批准后立即可以刷一轮
                    s['next_browse_at'] = 0  # 清掉退避计时，马上按 3 分钟节奏开始
                    s['unanswered_asks'] = 0
                    json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                    log('[auto] 老大批准自主刷')
        except Exception as e:
            log(f'[auto cmd err] {e}')

    # ---- 重启指令：老大发"重启"→ 进程自重启，起来后回一句"重启成功" ----
    def _handle_restart(self, text, sender_str):
        low = text.lower().replace(' ', '')
        if ('重启' in text) or ('restart' in low) or ('重新启动' in text):
            if ('成功' in text) or ('回来' in text):
                return  # 避免把"重启成功"这种回复再误触发
            log(f"[restart] 老大 {sender_str} 触发重启")
            try:
                json.dump({'uid': sender_str, 'ts': time.time()},
                          open(RESTART_NOTIFY_FILE, 'w', encoding='utf-8'))
            except Exception as e:
                log(f"[restart flag err] {e}")
            os.execv(HERE_PY, [HERE_PY] + sys.argv)  # 真正重启（固定用 .venv 解释器，避免串到 D:\python）

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

    # ---- 交给 AstrBot 主脑：人格/记忆/情绪/工具/能力清单全部走 QQ 那套 ----
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
            r = _SESSION.post(BRAIN_URL, json=payload, timeout=BRAIN_TIMEOUT)
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

    # ---- DeepSeek 人设回复（带 QQ 发送工具，真发而不是幻觉） ----
    def generate_reply(self, text, sender_label=None, is_master=True, use_pro=False, sender_id=None):
        """返回 (reply_text, image_paths) 或 None。"""
        # 先问主脑：同一个脑子生成的回复
        result = self._brain_reply(text, sender_id=sender_id, sender_label=sender_label,
                                   is_master=is_master, use_pro=use_pro)
        if result:
            return result
        who = f"\n\n[正在和你私信的人是：{sender_label}]" if sender_label else ""
        hist = self._read_recent_qq() if is_master else ''
        hist_block = f"\n\n[最近和老大在QQ上的聊天记录，供你记忆连贯]\n{hist}" if hist else ""
        vids = self._recent_video_lines() if is_master else ''
        vid_block = (
            '\n\n[老大以前发给你的抖音视频/分享（你是真看过的，用来回忆；'
            '如果老大这会儿发来新的视频，以新的那条为准，绝不许拿旧的顶替）]\n' + vids
            if vids
            else ''
        )
        system = self.persona + load_mood_line() + who + hist_block + vid_block
        messages = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': text},
        ]
        # B 方案：平时 flash，遇到表情包/梗才切 doubao pro（理解力强，但不带发QQ工具）
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
            payload['messages'] = messages = [
                {'role': 'system', 'content': system + '\n\n[只输出你要发出去的那一句话。不要写「回应思路」「最终回复」这类标注，不要解释。]'},
                {'role': 'user', 'content': text},
            ]
        else:
            try:
                msg = deepseek_chat(messages, tools=[QQ_TOOL] if is_master else None)
                if msg is None:
                    return None
                for _ in range(3):  # 继续最多 3 轮工具调用，加上首调共 4 轮
                    tool_calls = msg.get('tool_calls') or []
                    if not tool_calls:
                        reply = msg.get('content') or ''
                        return (_clean_reply(reply), [])
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
                        messages.append({'role': 'tool', 'tool_call_id': tc.get('id', ''), 'content': result})
                    msg = deepseek_chat(messages, tools=[QQ_TOOL] if is_master else None)
                return None
            except Exception as e:
                log(f"[deepseek err] {e}")
                return None
        try:
            for _ in range(4):  # 最多 4 轮工具调用，防死循环
                r = _SESSION.post(
                    url,
                    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                    json=payload,
                    timeout=60,
                )
                if r.status_code != 200:
                    # 429 限流 / 余额不足 都会走这里，只记日志、直接放弃这条（熔断器已移除）
                    log(f"[deepseek] HTTP {r.status_code} {r.text[:160]}")
                    return None
                d = r.json()
                msg = d['choices'][0]['message']
                tool_calls = msg.get('tool_calls') or []
                if not tool_calls:
                    reply = msg.get('content') or ''
                    return (_clean_reply(reply), [])  # 单行化 + 去掉「回应思路」；tuple 格式兼容主脑
                # 有工具调用：执行后把结果喂回模型继续生成
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
                    messages.append({'role': 'tool', 'tool_call_id': tc.get('id', ''), 'content': result})
            return None
        except Exception as e:
            log(f"[deepseek err] {e}")
            return None

    # ---- 发私信(文本+表情包) ----
    def _handle_brain_result(self, sender_uid, result, is_master=False):
        """处理 generate_reply 返回的 (text, images) 元组。"""
        if not result:
            return False
        text, images = result
        ok = self.send_reply(sender_uid, text) if text else False
        if images:
            from meme import send_meme_dm
            for img in images:
                try:
                    r = asyncio.run(send_meme_dm(str(img), headless=True))
                    log(f"[brain img -> {sender_uid}] ok={r.get('ok')} {img}")
                except Exception as e:
                    log(f"[brain img err] {e}")
        if ok and is_master:
            self._write_message('assistant', f'[抖音私信回复] {text}')
        return ok

    def send_reply(self, sender_uid, text):
        uid = str(sender_uid)
        # 一律走浏览器真实前端（imapi send_msg 返回 cmd:100 但被静默丢弃，已实测）。
        # 老大按昵称「潮汐」定位；陌生人按 uid 定位（没设昵称时标题就是 uid）。
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

    # ---- 媒体消息（表情包/图片/视频封面）----
    def _norm_kw(self, s):
        """把标题/描述压成可比较的指纹：去空白/大小写/标点，只留汉字字母数字。"""
        if not s:
            return ''
        return ''.join(ch for ch in str(s).lower() if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')

    def _remember_video_for_media(self, sender_str, title, parts):
        """刚看懂一条视频时记住，供随后的同标题图片帧（图文作品被拆成两条帧）认作同一内容。"""
        try:
            if not sender_str:
                return
            joined = ' | '.join(str(p) for p in (parts or []))
            self._recent_videos[sender_str] = {
                'title': title or '',
                'title_norm': self._norm_kw(title),
                'desc': joined,
                'ts': time.time(),
            }
            if len(self._recent_videos) > 100:
                cut = time.time() - 600
                self._recent_videos = {k: v for k, v in self._recent_videos.items() if v['ts'] > cut}
            log(f'[meme-mem] 记录刚看的视频 key={self._norm_kw(title)[:24]}')
        except Exception as e:
            log(f'[meme-mem] 记录视频失败: {e}')

    def _recent_video_for_media(self, sender_str, content_title):
        """按标题匹配同一位发送者最近(120s内)刚看过的视频，命中则返回其描述，否则 None。"""
        try:
            rec = self._recent_videos.get(sender_str)
            if not rec:
                return None
            if time.time() - rec['ts'] > 120:
                return None
            cur = self._norm_kw(content_title) or ''
            if not rec['title_norm'] or not cur:
                return None
            # 标题完全相同，或较长的一侧包含较短的一侧，判为同一内容。
            if rec['title_norm'] == cur or (
                len(rec['title_norm']) >= 8 and len(cur) >= 8
                and (rec['title_norm'] in cur or cur in rec['title_norm'])
            ):
                return rec['desc']
            return None
        except Exception as e:
            log(f'[meme-mem] 匹配视频失败: {e}')
            return None

    def _handle_media(self, sender, sender_str, is_master, who_label, content, kind):
        """把对方发的表情包/图片用视觉模型看懂，再像人一样回一句。

        图/文作品会被抖音拆成"视频帧 + 图片帧"两条推过来，标题一模一样。这里先按标题
        匹配刚看过的视频，命中就复用视频的描述，不再视图为重发的新图、也不回"又是这位哥"。
        """
        # 1) 若与刚看过的同标题视频撞车，复用它的描述（当成同一个人/同一内容）
        content_title = ''
        try:
            content_title = self._deep_get(content, ('content_title', 'content_name', 'title')) or ''
        except Exception:
            pass
        recent_desc = self._recent_video_for_media(sender_str, content_title)
        if recent_desc:
            log(f"[media] {kind} 与刚看过的视频同标题，复用记忆：{recent_desc[:80]}")
            seen = True
            desc = recent_desc
            key = None
        else:
            url = _find_url(content)
            key = sticker_key(content, url)
            log(f"[media] {kind} url={'有' if url else '无'} key={key or '无'}")
            desc = sticker_recall(key)
            seen = bool(desc)
            if seen:
                log(f"[media] {kind} 记忆命中：{desc}")
            else:
                desc = describe_image_url(url) if url else None
        if not desc:
            if is_master:
                self.send_reply(sender, f'你这{kind}我这边加载不出来呀，再发一次？')
            else:
                log(f"[media] {kind} 描述失败，不回")
            return
        if not seen and key:
            sticker_remember(key, desc, kind)
            log(f"[media] {kind} 内容：{desc}")
        text = f'[对方发来了一个{kind}，你看到的是：{desc}]'
        if seen:
            text += '（这个画面你刚才就见过，按同样的理解接话，别又问「这是谁」）'
        if is_master:
            self._write_message('user', f'[抖音{kind}] {desc}')
        result = self.generate_reply(text, sender_label=who_label, is_master=is_master, use_pro=False, sender_id=sender)
        if result:
            self._handle_brain_result(sender, result, is_master=is_master)

    # ---- 视频消息：真的看画面 + 听声音，而不是只看一张封面 ----
    def _handle_share_text(self, text, sender, sender_str, is_master, who_label):
        """老大把视频以「链接文字」发来：抠出 aweme_id 后走和分享卡片一样的视频链路。

        背景：抖音的分享卡片（type=8/27）推送会时不时漏掉，但纯文字几乎必达。
        所以让老大直接把视频链接当文字发过来，是最稳的一条兜底通路。
        """
        url, aid = _extract_douyin_url(text)
        if not url:
            return False
        log(f'[share] 检测到视频链接 {url}')
        if not aid:
            aid = _resolve_aweme_id(url)
        if not aid:
            self.send_reply(sender, '这个链接我打不开呀，把视频链接完整发我一下？')
            return True
        self._handle_video(sender, sender_str, is_master, who_label, {'itemId': aid})
        return True

    def _handle_video(self, sender, sender_str, is_master, who_label, content):
        """视频消息：先标已读，再查标题/作者/时长，再下载视频抽 N 帧拼图看画面、抽音轨转文字听内容。"""
        # 先标已读并提供反馈：告诉老大机器人已经开始处理了
        try:
            from send_text import _KA, send_text_sync

            _KA.mark_read(sender_str)
        except Exception as e:
            log(f'[video] mark_read 跳过: {e}')
        aid = str(content.get('itemId') or content.get('item_id') or '').strip()
        title = str(content.get('content_title') or '').strip()
        author = str(content.get('content_name') or '').strip()
        dur = 0
        play_url = ''
        images = []
        cover = _video_cover_url(content)
        # 分享卡片可能自带图集（图文作品），不等 API 就能用
        _embedded_images = content.get('images')
        if isinstance(_embedded_images, list) and _embedded_images:
            images = _embedded_images
            log(f"[video] content 自带 {len(images)} 张图")
        if aid:
            # 同一视频经常「分享卡片」和「链接文字」连着推两三次，短窗口内只处理一次，
            # 否则会重复下载几十 MB + 重复调视觉模型，还容易互相把对方拖超时。
            now = time.time()
            last = self._video_dedup.get(aid)
            if last and now - last < 180:
                log(f'[video] 去重：{aid} 3 分钟内已处理过，不再重复下载')
                if is_master:
                    self.send_reply(sender, '这个刚看过啦~过几分钟再发一遍我才重新看哦')
                return
            # 去重没有失效，开始处理
            self._video_dedup[aid] = now
            # 防止字典无限增长
            if len(self._video_dedup) > 200:
                self._video_dedup = {k: v for k, v in self._video_dedup.items() if now - v < 1800}
            # 发过渡消息：告诉老大正在处理，跟 get_video_info 并行
            NUDGE_TIPS = [
                '正在认真看这个视频哦~稍等一下下꒰ᐢ⸝⸝•༝•⸝⸝ᐢ꒱',
                '视频收到了！让我看一遍再来跟你聊~',
            ]
            f_ping = threading.Thread(
                target=send_text_sync,
                args=(random.choice(NUDGE_TIPS), sender_str),
                daemon=True,
            )
            f_ping.start()
            try:
                from get_video_info import get_video_info

                info = get_video_info(aid)
                title = title or (info.get('title') or '').strip()
                author = author or (info.get('author') or '').strip()
                dur = int(info.get('duration') or 0)
                # API 的值只做补全，不覆盖 content 自带的
                play_url = play_url or (info.get('play_url') or '')
                images = images or (info.get('images') or [])
                cover = info.get('cover') or cover
            except Exception as e:  # noqa: BLE001
                log(f'[video] 取详情失败 {aid} {e}')

        desc, speech = None, None
        had_video = bool(play_url)
        if play_url:
            desc, speech = self._describe_video(play_url, dur, sender_str, is_master)
        elif images:
            desc = self._describe_images(images)
        # 封面只用于「拿不到视频/图文」的纯转发卡；真有视频却没能看（画面/声音都没出来）时
        # 绝不拿封面冒充"看了"，见下方 had_video 分支。
        if not desc and not had_video and not images and cover:
            desc = describe_image_url(
                cover,
                prompt=(
                    '这是别人分享给你的作品的第一张图（可能是视频封面或图文首图）。按一行回答，不要客套，不超过50字：'
                    '画面：（谁/什么、在做什么、场景，图上的文字原样写出）；'
                    '风格：（真人/AI生成/动画/游戏，选一个）。'
                ),
            )

        parts = []
        if title:
            parts.append(f'标题：{title}')
        if author:
            parts.append(f'作者：{author}')
        if dur:
            parts.append(f'时长：{dur / 1000:.1f} 秒')
        if desc:
            parts.append(f'画面：{desc}')
        if speech:
            parts.append(f'里面的声音/台词：{speech}')
        # 真有视频但画面/声音都没出来：诚实告诉大脑没看成，别拿封面冒充
        if had_video and not desc and not speech:
            parts.append('画面：（视频画面没加载出来，没能看成）')
        if not parts:
            if is_master:
                self.send_reply(sender, '这个视频我这边加载不出来呀，再发一次？')
            return

        log('[video] 看懂：' + ' | '.join(parts))
        # 记住这条刚看懂的视频：同一图文作品常被抖音拆成"视频帧(type=8)+图片帧(type=77)"两条，
        # 让 _handle_media 碰到同标题的图片帧时能认出是同一个内容，不再回"又是这位哥"。
        self._remember_video_for_media(sender_str, title, parts)
        # 需求：看到视频-已读-下载看完-回复。看完内容后以汐缘的口吻，私信回复
        # 发视频的人（不是对视频公开发评论）。称呼由 generate_reply 按 who_label 区分，
        # 不再有写死的"主人"称呼。
        if is_master:
            self._write_message('user', '[抖音视频] ' + ' | '.join(parts))
        text = '[对方给你发来一个抖音视频，你看到/听到的是：\n' + '\n'.join(parts) + '\n]'
        reply = self.generate_reply(text, sender_label=who_label, is_master=is_master, use_pro=False, sender_id=sender)
        if reply:
            self._handle_brain_result(sender, reply, is_master=is_master)

    def _handle_video_fallback(self, sender, sender_str, is_master, who_label, title, cover):
        """视频帧解析不出 itemId 时的兜底：只有标题/封面也要回，不能当她没收到。"""
        parts = []
        if title:
            parts.append(f'标题：{title}')
        desc = None
        if cover:
            desc = describe_image_url(cover, prompt=PROMPT_VIDEO_COVER)
        if desc:
            parts.append(f'画面：{desc}')
        if not parts:
            if is_master:
                self.send_reply(sender, '这个视频我这边只看到个空壳，没解析出内容，跟我说说是什么？')
            return
        log('[video] 兜底看懂：' + ' | '.join(parts))
        self._remember_video_for_media(sender_str, title, parts)
        text = '[对方给你发来一个抖音视频（拿不到播放地址，只看到封面和标题）：\n' + '\n'.join(parts) + '\n]'
        if is_master:
            self._write_message('user', '[抖音视频] ' + ' | '.join(parts))
        reply = self.generate_reply(text, sender_label=who_label, is_master=is_master, use_pro=False, sender_id=sender)
        if reply:
            self._handle_brain_result(sender, reply, is_master=is_master)

    def _deep_get(self, obj, keys, depth=0):
        """在任意嵌套结构里找第一个命中 keys 的非空值（视频卡的 itemId 层级不固定）。"""
        if depth > 6:
            return None
        if isinstance(obj, dict):
            for k in keys:
                v = obj.get(k)
                if isinstance(v, (str, int)) and str(v).strip():
                    return v
            for v in obj.values():
                r = self._deep_get(v, keys, depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = self._deep_get(v, keys, depth + 1)
                if r is not None:
                    return r
        return None

    def _looks_media(self, content):
        """粗判一坨未知结构里有没有图片/视频链接，有就当媒体处理，别静默丢。"""
        s = json.dumps(content, ensure_ascii=False)
        return ('url_list' in s) or ('cover_url' in s) or ('image' in s and 'http' in s)

    def _first_cover(self, obj, depth=0):
        """递归找封面图：优先 cover_url/cover/origin_cover 里的 url_list[0]。"""
        if depth > 6:
            return None
        if isinstance(obj, dict):
            for k in ('cover_url', 'cover', 'origin_cover', 'dynamic_cover'):
                node = obj.get(k)
                if isinstance(node, dict):
                    urls = node.get('url_list') or []
                    if urls:
                        return urls[0]
            for v in obj.values():
                r = self._first_cover(v, depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = self._first_cover(v, depth + 1)
                if r:
                    return r
        return None

    def _rescue_video_card(self, sender, sender_str, is_master, who_label):
        """WS 视频帧为空（clen=0 / 无 itemId，常见于主人重发或重启后 HTTP 基线已过）时，
        主动 HTTP 回源该 peer 会话，找最新一条 awe=800 视频卡，恢复 itemId 继续完整处理。
        取不到就返回 False，让上层走原来的封面兜底/空壳提示。
        注意：WS 空帧常比 HTTP 接口提交快一拍，第一轮 fetch 可能还没拉到最新卡片，
        因此做短重试（数秒内多探几次）覆盖这个时序窗口。"""
        import im_http
        import time
        waited = False
        for attempt in range(4):
            try:
                data = im_http.fetch(self.my_uid, limit=25)
            except Exception as e:
                log(f'[video] HTTP 回源 fetch 异常(第{attempt}轮): {e}')
                if not waited:
                    time.sleep(0.8); waited = True
                continue
            convs = data.get('conversations') or []
            peer_msgs = None
            if attempt == 0:
                # 一次性诊断：打印 fetch 用的 my_uid、返回的 my_uid、以及所有 peer，核对账号视角是否对
                my_ret = data.get('my_uid') or ''
                peers_all = [ (c.get('peer_uid') or '', len(c.get('messages') or []), c.get('type')) for c in convs ]
                log(f'[video] 回源诊断 我用my_uid={self.my_uid!r} fetch返回my_uid={my_ret!r} 目标peer=<{sender}> peers={peers_all}')
            for conv in convs:
                peer = conv.get('peer_uid') or ''
                if peer == str(sender):
                    peer_msgs = conv.get('messages') or []
                    break
            # 诊断：即使没命中也记录这条 peer 里最近几条 incoming 的视频卡，方便定位时序/契约问题
            if peer_msgs:
                cands = []
                for m in reversed(peer_msgs):
                    if m.get('from_me'):
                        continue
                    awe = m.get('awe') or 0
                    if m.get('item_id') and (awe == 800 or (awe and awe != 700)):
                        cands.append(f'{m.get("item_id")}@ts{m.get("ts")}')
                if cands:
                    log(f'[video] HTTP 回源(第{attempt}轮) 见 {len(cands)} 张候选卡: {", ".join(cands[:4])}')
                for m in reversed(peer_msgs):  # 从新到旧
                    if m.get('from_me'):
                        continue
                    awe = m.get('awe') or 0
                    item = m.get('item_id') or ''
                    if item and (awe == 800 or (awe and awe != 700)):
                        log(f'[video] HTTP 回源恢复空帧：peer={peer} itemId={item} title={m.get("title","")!r}')
                        merged = {
                            'itemId': str(item),
                            'content_title': m.get('title') or '',
                        }
                        cover = m.get('cover_url') or ''
                        if cover:
                            merged['cover_url'] = {'url_list': [cover]}
                        self._handle_video(sender, sender_str, is_master, who_label, merged)
                        return True
                if not waited and attempt < 3:
                    log(f'[video] HTTP 回源第{attempt}轮尚未见到卡片，稍等重试…')
                    time.sleep(0.8); waited = True
        log(f'[video] HTTP 回源未找到 peer={sender} 的视频卡（4 轮未命中）')
        return False

    def _handle_video_card(self, sender, sender_str, is_master, who_label, content):
        """type=27 这类视频卡：字段层级不固定，先深挖 itemId，挖不到就走封面兜底。"""
        item_id = self._deep_get(content, ('itemId', 'item_id', 'aweme_id', 'awemeId'))
        title = (self._deep_get(content, ('content_title', 'content_name', 'title')) or '')
        cover = self._first_cover(content)
        if item_id:
            merged = dict(content)
            merged['itemId'] = str(item_id)
            self._handle_video(sender, sender_str, is_master, who_label, merged)
        elif not content:
            # 空帧：先别急着给"空壳"，主动 HTTP 回源恢复这条视频卡。
            log(f'[video] type=27 空内容帧，尝试 HTTP 回源恢复 itemId')
            if self._rescue_video_card(sender, sender_str, is_master, who_label):
                return
            self._handle_video_fallback(sender, sender_str, is_master, who_label, str(title), cover)
        else:
            log(f'[video] type=27 无 itemId，走封面兜底 title={title!r} cover={bool(cover)}')
            self._handle_video_fallback(sender, sender_str, is_master, who_label, str(title), cover)

    def _describe_images(self, images):
        """图文作品：把前 9 张图拼成一张，交给视觉模型按顺序看。"""
        import subprocess
        import tempfile

        ff = os.environ.get('FFMPEG') or 'ffmpeg'
        tmp = tempfile.mkdtemp(prefix='dyimg_', dir=tempfile.gettempdir())
        files = []
        try:
            for i, u in enumerate((images or [])[:9]):
                if not u:
                    continue
                jpg = os.path.join(tmp, f'i{i}.jpg')
                try:
                    r = _SESSION.get(
                        u,
                        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.douyin.com/'},
                        timeout=30,
                    )
                    if r.status_code != 200 or not r.content:
                        continue
                    with open(jpg, 'wb') as f:
                        f.write(r.content)
                    scaled = os.path.join(tmp, f's{i}.jpg')
                    subprocess.run([ff, '-y', '-loglevel', 'error', '-i', jpg, '-vf', 'scale=320:-2', scaled],
                                   timeout=60, check=False)
                    if os.path.exists(scaled) and os.path.getsize(scaled) > 0:
                        files.append(scaled)
                except Exception as e:  # noqa: BLE001
                    log(f'[image] 第{i + 1}张下载失败 {e}')
            if not files:
                return None
            sheet = self._sheet(files, ff, tmp)
            log(f'[image] 图文共 {len(files)} 张，拼图完成')
            return describe_image_url(
                sheet,
                prompt=(
                    f'这是一组图文作品里的 {len(files)} 张图片（按发布顺序，从左到右、从上到下）。'
                    '按一行回答，不超过70字：内容：（讲了什么、画面里有什么，图上的文字原样写出关键部分）；'
                    '风格：（真人/AI生成/动画/游戏/截图，选一个）。'
                ),
            )
        except Exception as e:  # noqa: BLE001
            log(f'[image] 处理失败 {e}')
            return None
        finally:
            try:
                for name in os.listdir(tmp):
                    os.remove(os.path.join(tmp, name))
                os.rmdir(tmp)
            except Exception:  # noqa: BLE001
                pass

    def _sheet(self, frames, ff, tmp):
        """把若干帧图拼成一张大图：每行 3 张。
        先把每张统一缩放+补边到固定格位（W×H），再把不满 3 张的行补黑到 3W 宽，
        否则末行只有 1~2 张时 hstack 出来的行宽 320/640 和上一行 960 不一致，vstack 直接报错。"""
        import subprocess

        if not frames:
            return None
        if len(frames) == 1:
            return frames[0]

        W, H = 320, 240
        slots = 3

        def norm(idx, path):
            """统一缩放 + 居中补边到 W×H。"""
            out = os.path.join(tmp, f'norm{idx}.jpg')
            vf = (f'scale={W}:{H}:force_original_aspect_ratio=decrease,'
                  f'pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black')
            subprocess.run(
                [ff, '-y', '-loglevel', 'error', '-i', path, '-vf', vf, out],
                timeout=60, check=False,
            )
            return out if os.path.exists(out) and os.path.getsize(out) > 0 else None

        normed = []
        for i, p in enumerate(frames):
            np_ = norm(i, p)
            if np_:
                normed.append(np_)
        if len(normed) == 1:
            return normed[0]
        if not normed:
            return None

        rows = [normed[i:i + slots] for i in range(0, len(normed), slots)]
        row_imgs = []
        for ri, row in enumerate(rows):
            out = os.path.join(tmp, f'row{ri}.jpg')
            args = [ff, '-y', '-loglevel', 'error']
            for p in row:
                args += ['-i', p]
            # hstack 后把宽度补齐到 slots*W，保证每行尺寸一致
            if len(row) > 1:
                fc = (''.join(f'[{i}:v]' for i in range(len(row)))
                      + f'hstack=inputs={len(row)},pad={slots * W}:{H}:0:0:color=black')
            else:
                fc = f'[0:v]pad={slots * W}:{H}:0:0:color=black'
            args += ['-filter_complex', fc, out]
            subprocess.run(args, timeout=90, check=False)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                row_imgs.append(out)

        if not row_imgs:
            return normed[0]
        if len(row_imgs) == 1:
            return row_imgs[0]

        sheet = os.path.join(tmp, 'sheet.jpg')
        args = [ff, '-y', '-loglevel', 'error']
        for p in row_imgs:
            args += ['-i', p]
        fc = ''.join(f'[{i}:v]' for i in range(len(row_imgs))) + f'vstack=inputs={len(row_imgs)}'
        args += ['-filter_complex', fc, sheet]
        subprocess.run(args, timeout=90, check=False)
        if os.path.exists(sheet) and os.path.getsize(sheet) > 0:
            return sheet
        return row_imgs[0]

    def _describe_video(self, play_url, dur_ms, peer_uid=None, is_master=False):
        """下载视频 →（流水线并行：边下边抽帧+抽音频）→ 拼图 → VLM+ASR 并行。返回 (画面描述, 台词)。"""
        import subprocess
        import tempfile

        ff = os.environ.get('FFMPEG') or 'ffmpeg'
        tmp = tempfile.mkdtemp(prefix='dyvid_', dir=tempfile.gettempdir())
        mp4 = os.path.join(tmp, 'v.mp4')
        desc = speech = None
        try:
            # === 1. 下载 + 流水线并行：边下边抽帧 + 抽音频 ===
            r = _SESSION.get(
                play_url,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.douyin.com/'},
                timeout=90,
                stream=True,
            )
            got = 0
            dur_s = max(1.0, (dur_ms or 0) / 1000.0)
            # 抽帧：≤30s 抽 3 帧，≤2min 抽 4 帧，更长抽 5 帧
            if dur_s <= 30:
                n_frames = 3
            elif dur_s <= 120:
                n_frames = 4
            else:
                n_frames = 5
            positions = [0.08 + (0.84 * i / (n_frames - 1)) for i in range(n_frames)]

            # 开一个共享的 ThreadPoolExecutor，下载 → 抽帧 + 抽音频
            xpool = ThreadPoolExecutor(max_workers=max(3, 1 + n_frames))

            # 下载线程：把文件写到磁盘
            f_dl = xpool.submit(self._download_to, r, mp4, tmp)

            # 必须先等下载完成再抽帧：mp4 的 moov atom 通常在文件末尾，
            # ffmpeg 边下边 seek 会读到不完整的文件，报 "moov atom not found" /
            # "Error opening input" / "End of file" 导致全部抽帧失败（见 reply_bot_err.log）。
            try:
                got = f_dl.result(timeout=90)
            except Exception as e:
                log(f'[video] 下载失败: {e}')
                xpool.shutdown(wait=False)
                return desc, speech
            log(f'[video] 下载 {got / 1024 / 1024:.1f} MB')

            # 下载完成后再并行抽帧 + 抽音频（此时文件完整，不再和下载赛跑）
            f_frames = {}
            for i, p in enumerate(positions):
                f_frames[i] = xpool.submit(
                    self._extract_one_frame,
                    ff, mp4, os.path.join(tmp, f'f{i}.jpg'),
                    dur_s * p,  # 这里传秒数，_extract_one_frame 负责格式化
                )
            # 音频：也只依赖 mp4，可以和抽帧并行
            audio = os.path.join(tmp, 'a.mp3')
            f_audio = xpool.submit(
                self._extract_audio,
                ff, mp4, audio,
            )

            # 收抽帧结果
            frames = []
            for i in range(n_frames):
                try:
                    jpg = f_frames[i].result(timeout=60)
                    if jpg and os.path.exists(jpg) and os.path.getsize(jpg) > 0:
                        frames.append(jpg)
                except Exception:
                    pass
            log(f'[video] 抽帧 {len(frames)}/{n_frames}（时长 {dur_s:.1f}s）')

            # 收音频结果
            has_audio = False
            try:
                f_audio.result(timeout=60)
                has_audio = os.path.exists(audio) and os.path.getsize(audio) > 0
            except Exception:
                pass
            if has_audio:
                log(f'[video] 音频 {os.path.getsize(audio) / 1024:.1f} KB')

            sheet = self._sheet(frames, ff, tmp)

            # === 2. VLM 认画面 与 ASR 听台词 并行 ===
            f_vlm = None
            f_asr = None
            with ThreadPoolExecutor(max_workers=2) as ex:
                if sheet:
                    f_vlm = ex.submit(
                        describe_image_url,
                        sheet,
                        prompt=(
                            f'这是从一个短视频里按时间顺序抽出来的 {len(frames)} 帧画面'
                            '（从左到右、从上到下按时间排列）。按一行回答，不超过70字：'
                            '画面：（谁/什么、在做什么、场景怎么变化，图上的文字原样写出）；'
                            '风格：（真人/AI生成/动画/游戏，选一个）。'
                        ),
                    )
                if has_audio:
                    f_asr = ex.submit(_stt_bytes, open(audio, 'rb').read(), 'a.mp3', 'audio/mpeg')

                if f_vlm:
                    desc = f_vlm.result(timeout=60)
                if f_asr:
                    speech = f_asr.result(timeout=60)

            # 语音后处理（噪声过滤 + 截断）
            if speech:
                import re as _re
                if len(_re.findall(r'[\u4e00-\u9fffA-Za-z0-9]', speech)) < 4:
                    log(f'[video] STT 结果像噪声，丢弃：{speech[:40]}')
                    speech = None
                elif len(speech) > 200:
                    speech = speech[:200] + '…'
                    log(f'[video] ASR 截断至 200 字')
        except Exception as e:  # noqa: BLE001
            log(f'[video] 处理失败：{e}')
        finally:
            try:
                for name in os.listdir(tmp):
                    os.remove(os.path.join(tmp, name))
                os.rmdir(tmp)
            except Exception:  # noqa: BLE001
                pass
        return desc, speech

    @staticmethod
    def _download_to(resp, path, tmp_dir, cap=80 * 1024 * 1024):
        """下载 resp 流到 path，返回总字节数。"""
        got = 0
        with open(path, 'wb') as f:
            for chunk in resp.iter_content(1 << 16):
                got += len(chunk)
                if got > cap:
                    raise RuntimeError(f'视频超过 {cap >> 20}MB，放弃下载')
                f.write(chunk)
        return got

    @staticmethod
    def _extract_one_frame(ff, mp4, out_jpg, seek_sec):
        """ffmpeg -ss seek_sec -i mp4 -frames:v 1 -vf scale=320:-2 out_jpg。"""
        import subprocess
        subprocess.run(
            [ff, '-y', '-loglevel', 'error', '-ss', f'{seek_sec:.2f}', '-i', mp4,
             '-frames:v', '1', '-vf', 'scale=320:-2', out_jpg],
            timeout=60,
            check=False,
        )
        return out_jpg

    @staticmethod
    def _extract_audio(ff, mp4, out_audio, max_sec=90):
        """ffmpeg -i mp4 -vn -ac 1 -ar 16k -b:a 32k -t max_sec out_audio。"""
        import subprocess
        subprocess.run(
            [ff, '-y', '-loglevel', 'error', '-i', mp4, '-vn',
             '-ac', '1', '-ar', '16000', '-b:a', '32k', '-t', str(max_sec), out_audio],
            timeout=60,
            check=False,
        )
        return out_audio

    # ---- WebSocket 回调 ----
    def on_open(self, ws):
        log("[ws] connected")
        self._notify_restart_done()  # 重启后给老大回"重启成功"
        if not getattr(self, "_warm_done", False):
            self._warm_done = True
            self._warm_sender()

    def _warm_sender(self):
        """提前拉起常驻浏览器，第一条私信不用等 11 秒冷启动。"""
        try:
            if os.environ.get("SEND_TEXT_PREWARM", "1") == "0":
                log("[send_text] 预热已关闭（SEND_TEXT_PREWARM=0）")
                return
            from send_text import warm_sender_async

            warm_sender_async()
            log("[send_text] 已在后台预热常驻浏览器")
        except Exception as e:  # noqa: BLE001
            log(f"[send_text] 预热启动失败: {e}")

    def _allow_stranger(self, uid):
        """非老大私信限流：同一个人 5 秒一条、每小时最多 60 条，防刷屏。

        只在"真的带内容"的消息上调用（文本/表情/图片/视频/分享卡）。
        正在输入(type=1)、已读回执(type=15)、拍一拍(type=50001) 之类不算——
        之前这些系统帧也会吃掉额度，网友连着发几条就全被限流，表现为"陌生人完全不回"。
        """
        now = time.time()
        rec = self.stranger_hist.setdefault(uid, [])
        rec[:] = [t for t in rec if now - t < 3600]
        if len(rec) >= 60:
            return False
        if rec and now - rec[-1] < 5:
            return False
        rec.append(now)
        return True

    def on_message(self, ws, message):
        self.last_frame_ts = time.time()  # 给「长时间零帧就重连」的看门狗用
        try:
            frame = Live_pb2.PushFrame()
            frame.ParseFromString(message)
            if frame.payloadType != 'pb':
                return
            resp = Response_pb2.Response()
            resp.ParseFromString(frame.payload)
            notify = resp.body.new_message_notify.message
            sender = notify.sender
            msg_type = notify.message_type
            # 50001 是输入状态回声，不算真正的内容推送；看门狗只看内容帧
            if msg_type != 50001:
                self._last_content_ts = time.time()
            index = notify.index_in_conversation
            # 先打到达日志，再解析：解析失败也要留痕，否则视频/分享卡片这类
            # 内容为空的帧会被 json.loads 抛异常整个丢掉（用户发视频她看不到，就是这个原因）
            log(f"[frame] sender={sender} type={msg_type} index={index} cmd={resp.cmd} clen={len(notify.content or '')}")
            try:
                content = json.loads(notify.content) if (notify.content or '').strip() else {}
            except Exception as e:
                content = {}
                log(f"[frame-bad-json] {type(e).__name__}: {e} | raw={(notify.content or '')[:300]!r}")

            # 视频/分享卡片实测会以「她自己」的 uid 推下来（type=27 / type=8），
            # 那其实是老大发来的，不能当回显丢掉，否则她永远“看不见视频”。
            self_video = (sender == self.my_uid) and (msg_type in (8, 27))
            if sender == self.my_uid and not self_video:
                # 自己的回显直接跳过；其他没见过的自己帧留个底，方便事后查协议。
                if msg_type not in (7, 50001):
                    log(f"[frame-self-raw] type={msg_type} clen={len(notify.content or '')} raw={(notify.content or '')[:1200]!r}")
                return  # 忽略自己发的
            if self_video:
                # aweType >= 2700 是富文本贴纸/表情回显，不是老大发的视频/图片
                _awe = int((content or {}).get('aweType') or 0)
                if _awe >= 2700:
                    log(f"[frame-self-video] aweType={_awe} 贴纸回显，跳过")
                    return
                # auto_browse 分享卡片给老大后，抖音会以「自己的 uid」推送卡片回显。
                # 如果这条视频 5 分钟内 auto_browse 刚分享过，跳过（不是老大发来的，是自己的回显）。
                item_id = self._deep_get(content, ('itemId', 'item_id')) or ''
                if item_id and self._auto_browse_just_shared(item_id):
                    log(f"[frame-self-video] itemId={item_id} auto_browse 刚分享过，跳过回显")
                    return
                # _deep_get 没挖到 itemId → 不篡改 sender 让这帧假冒老大视频，
                # 直接跳过。真正的 recv video 帧会带 itemId 在 msg_type==8 分支做检查。
                log(f"[frame-self-video] _deep_get 没挖到 itemId，跳过（等 recv video 帧处理）")
                return
            sender_str = str(sender)
            label = MASTER_MAP.get(sender_str)
            is_master = label is not None
            # 谁都会回（和 QQ 侧一致）；只有老大能触发指令、能收分享/表情包、能进记忆
            who_label = label if is_master else f"一位网友（抖音uid {sender_str}，不是你老大）"
            if index in self.processed:
                return
            self.processed.add(index)
            if len(self.processed) > 1000:
                self.processed.clear()
            if (not is_master) and msg_type in (2, 5, 7, 8, 27) and not self._allow_stranger(sender_str):
                log(f"[throttle] 网友 {sender_str} 发得太频繁，这条先不回（type={msg_type}）")
                return

            if msg_type == 7:
                text = content.get('text', '')
                log(f"[recv text] from={sender} master={is_master} index={index} text={text}")
                # WS 直接处理（即时回复），同时登记去重键：HTTP 回源稍后翻到同一条时跳过。
                # 之前只靠 HTTP 一个入口，一旦 HTTP 那边卡住/拿到旧快照，整条链路就哑了。
                self._http_evt.set()
                if text.strip():
                    k = (sender_str, text.strip()[:80])
                    self._ws_text_ts[k] = time.time()
                    if len(self._ws_text_ts) > 300:
                        cut = time.time() - 600
                        self._ws_text_ts = {kk: v for kk, v in self._ws_text_ts.items() if v > cut}
                    self._dispatch_http(
                        sender_str,
                        {'ts': int(time.time() * 1000), 'from_uid': sender_str, 'text': text,
                         'awe': 0, 'item_id': '', 'title': '', 'from_me': False},
                        src='ws',
                    )
                return
            elif msg_type == 5:
                log(f"[recv emoji] from={sender} master={is_master} raw={json.dumps(content, ensure_ascii=False)[:1500]}")
                self._handle_media(sender, sender_str, is_master, who_label, content, '表情包')
            elif msg_type == 27:
                # type=27 是「富媒体卡片」的通用壳：短视频卡（带 itemId）和图片/表情（aweType 27xx + resource_url）
                # 都走它。不分开的话，一张点赞图会被当成视频，回一句「这个视频解析不出来」，很怪。
                log(f"[recv t27] from={sender} master={is_master} raw={json.dumps(content, ensure_ascii=False)[:1500]}")
                _awe = int(content.get('aweType') or 0)
                _has_video_id = content.get('itemId') or content.get('aweme_id')
                # 修正：有视频 ID 的一律走视频，不再被 aweType 误判成图片
                _is_img = (not _has_video_id) and (
                    (2700 <= _awe < 2800)
                    or content.get('resource_url')
                )
                if _is_img:
                    log(f"[recv t27-img] aweType={_awe} → 按图片处理")
                    self._handle_media(sender, sender_str, is_master, who_label, content, '图片')
                elif not _has_video_id and not content.get('content_title'):
                    self.send_reply(sender, '这个视频我这边解析不出来，你跟我说说是什么内容呗')
                else:
                    self._handle_video_card(sender, sender_str, is_master, who_label, content)
            elif msg_type == 17:
                url = ((content.get('resource_url') or {}).get('url_list') or [None])[0]
                log(f"[recv voice] from={sender} master={is_master} index={index} raw={json.dumps(content, ensure_ascii=False)[:700]}")
                text = transcribe_audio_url(url) if url else None
                if not text:
                    self.send_reply(sender, '语音没听清呀，再说一遍？')
                else:
                    log(f"[stt] {text}")
                    if is_master:
                        self._write_message('user', f'[抖音语音] {text}')
                        self._handle_auto_cmd(text)
                        self._handle_restart(text, sender_str)
                    reply = self.generate_reply(text, sender_label=who_label, is_master=is_master, sender_id=sender_str)
                    if reply:
                        self._handle_brain_result(sender, reply, is_master=is_master)
                        # 语音功能已关闭，不再回语音
            elif msg_type == 2:
                log(f"[recv image] from={sender} raw={json.dumps(content, ensure_ascii=False)[:1500]}")
                self._handle_media(sender, sender_str, is_master, who_label, content, '图片')
            elif msg_type == 8:
                log(f"[recv video] from={sender} itemId={content.get('itemId')} raw={json.dumps(content, ensure_ascii=False)[:800]}")
                # auto_browse 刚分享过 → 跳过回显
                _item_id = content.get('itemId') or ''
                if _item_id and self._auto_browse_just_shared(_item_id):
                    log(f"[recv video] itemId={_item_id} auto_browse 刚分享过，跳过回显")
                    return
                # 需求：看到视频-抖音已读-下载看完-私信回复发视频的人（m03343）。
                # 只标已读不回复会让用户觉得"她看不到视频"。所以既标已读，又走完整处理。
                try:
                    from send_text import _KA
                    _KA.mark_read(sender_str)
                except Exception as e:
                    log(f'[video] mark_read 跳过: {e}')
                self._handle_video_card(sender, sender_str, is_master, who_label, content)
            elif msg_type == 50001:
                pass
            else:
                # 兜底：任何没见过的新类型都别静默丢弃，至少把原文落盘，
                # 有图片/视频链接的话顺手当媒体处理一次。
                log(f"[recv unknown] type={msg_type} from={sender} master={is_master} raw={json.dumps(content, ensure_ascii=False)[:1200]}")
                if self._looks_media(content):
                    self._handle_media(sender, sender_str, is_master, who_label, content, '图片')
        except Exception as e:
            log(f"[on_message err] {e}")
            traceback.print_exc()

    def on_error(self, ws, error):
        log(f"[ws error] {error}")

    def on_close(self, ws, code, msg):
        log(f"[ws closed] {code} {msg}")

    def _silence_watchdog(self):
        """长时间收不到任何帧就主动重连。

        实测：连接看着还 Established、心跳也正常，但服务端从某一刻起就不再推消息
        （15:13 之后整整 30 分钟零帧，而会话列表里确实有老大新发的图）。这种情况下
        只能靠重连让服务端重新注册推送会话，光靠协议层心跳发现不了。
        """
        idle_limit = 600       # 10 分钟零帧就重连
        cooldown    = 1800     # 重连后冷却 30 分钟（退避，防风暴）
        _last_wd_reconnect = 0
        while True:
            time.sleep(60)
            try:
                gap = time.time() - getattr(self, '_last_content_ts', time.time())
                since_last = time.time() - _last_wd_reconnect
                if gap >= idle_limit and since_last >= cooldown and self.ws is not None:
                    log(f"[watchdog] {int(gap)}s 没收到内容帧（不含 50001 回声），主动重连一次")
                    self._last_content_ts = time.time()  # 重连后重置时钟，避免反复触发
                    _last_wd_reconnect = time.time()
                    try:
                        self.ws.close()
                    except Exception as e:
                        log(f"[watchdog] close 失败：{e}")
                        _last_wd_reconnect = 0  # 重连失败不回退冷却，下次再试
            except Exception as e:
                log(f"[watchdog] err {e}")

    def _notification_watchdog(self):
        """每 5 分钟扫一次抖音通知页，发现有人回复了汐缘的评论就自动回。
        流程：扫描通知→过滤新回复→LLM 生成回复→post_comment(reply_cid=...)。
        """
        time.sleep(90)  # 先等浏览器预热
        NOTIFY_INTERVAL = 300  # 5 分钟扫一次

        while True:
            try:
                import headed_browser as hb
                replies = hb.run(hb.scan_reply_notifications(), timeout=45)
                if replies is None:
                    replies = []
            except Exception as e:
                log(f"[notify-wd] 扫描通知失败: {e}")
                time.sleep(NOTIFY_INTERVAL)
                continue

            if not replies:
                log("[notify-wd] 扫描完成，无新回复")
                time.sleep(NOTIFY_INTERVAL)
                continue

            log(f"[notify-wd] 发现 {len(replies)} 条新评论回复")

            import requests as _rq

            for r in replies:
                try:
                    nid = r.get("nid", "")
                    replier_name = r.get("replier_name", "网友")
                    reply_text = r.get("reply_text", "")
                    comment_url = r.get("comment_url", "")

                    # 优先用 API 返回的 aweme_id / cid 字段
                    aweme_id = r.get("aweme_id", "")
                    reply_cid = r.get("cid", "")
                    parent_cid = r.get("parent_cid", "")

                    # 兜底：从 comment_url 提取
                    if (not aweme_id or not reply_cid) and comment_url:
                        import re as _re2
                        if not aweme_id:
                            m = _re2.search(r'/video/(\d+)', comment_url)
                            if m:
                                aweme_id = m.group(1)
                        if not reply_cid:
                            m2 = _re2.search(r'(?:comment_id|reply_id|cid)=(\d+)', comment_url)
                            if m2:
                                reply_cid = m2.group(1)

                    if not aweme_id:
                        log(f"[notify-wd] 跳过：无法从 {comment_url} 提取 aweme_id")
                        hb.mark_notification_processed(nid)
                        continue

                    # 用 DeepSeek 生成简短回复
                    prompt = (
                        f"你是汐缘。有人回复了你在抖音的评论。"
                        f"网友「{replier_name}」说：「{reply_text}」"
                        f"请用汐缘的语气回一句话（15字以内，可爱俏皮）。只输出回复内容，不要解释。"
                    )
                    system = self.persona[:400] if hasattr(self, 'persona') else "你是汐缘，一个温柔可爱的AI。"
                    messages = [
                        {'role': 'system', 'content': system},
                        {'role': 'user', 'content': prompt},
                    ]
                    try:
                        resp = _rq.post(
                            DEEPSEEK_URL,
                            headers={'Authorization': f'Bearer {DEEPSEEK_KEY}',
                                     'Content-Type': 'application/json'},
                            json={'model': DEEPSEEK_MODEL, 'messages': messages,
                                  'max_tokens': 320, 'temperature': 0.95},
                            timeout=60,
                        )
                        if resp.status_code == 200:
                            llm_reply = (resp.json().get('choices', [{}])[0]
                                         .get('message', {}).get('content', '')).strip()
                        else:
                            log(f"[notify-wd] DeepSeek HTTP {resp.status_code}: {resp.text[:100]}")
                            hb.mark_notification_processed(nid)
                            continue
                    except Exception as e:
                        log(f"[notify-wd] LLM 调用失败: {e}")
                        hb.mark_notification_processed(nid)
                        continue

                    if not llm_reply or len(llm_reply) < 2:
                        log(f"[notify-wd] LLM 返回空，跳过 nid={nid[:20]}")
                        hb.mark_notification_processed(nid)
                        continue

                    # 发嵌套回复
                    log(f"[notify-wd] 回复 {replier_name}: {llm_reply[:40]}")
                    result = hb.run(
                        hb.reply_to_notification(nid, aweme_id, reply_cid, llm_reply,
                                                  reply_content=reply_text,
                                                  parent_cid=parent_cid,
                                                  replier_name=replier_name),
                        timeout=90,
                    )
                    if result and result.get("ok"):
                        log(f"[notify-wd] ✅ 已回复 {replier_name}")
                    else:
                        err = (result or {}).get("error", "未知")
                        log(f"[notify-wd] ❌ 回复失败: {err}")
                        hb.mark_notification_processed(nid)  # 失败了也标记，避免死循环

                except Exception as e:
                    log(f"[notify-wd] 处理通知异常: {e}")
                    try:
                        import headed_browser as hb2
                        hb2.mark_notification_processed(r.get("nid", ""))
                    except Exception:
                        pass

            time.sleep(NOTIFY_INTERVAL)

    # ---------- 轮询兜底：WS 推送会静默失效，靠网页回源兜底 ----------
    POLL_FILE = os.path.join(HERE, 'poll_state.json')

    def _load_poll_counts(self):
        try:
            return dict(json.load(open(self.POLL_FILE, encoding='utf-8')).get('counts') or {})
        except Exception:
            return {}

    def _save_poll_counts(self, counts):
        try:
            json.dump({'counts': counts, 'ts': int(time.time())},
                      open(self.POLL_FILE, 'w', encoding='utf-8'), ensure_ascii=False)
        except Exception as e:
            log(f'[poll] 存状态失败 {e}')

    def _poll_loop(self):
        """每 40 秒让常驻浏览器回源拉一次最近消息。

        背景：抖音这条 WS 从某一刻起会「静默死掉」——TCP 还 Established、心跳也正常，
        但服务端不再推任何帧（15:13 之后整整 40 分钟零帧，而会话页点出去再点回来就能
        把消息拉出来）。所以推送只能当加速用，真正保底靠这里轮询。

        改进：之前只处理纯文本消息，视频/分享卡片（含链接但可能无文字）被静默丢弃。
        现在同时检测卡片里的链接，发现新卡片立刻走 _handle_share_text 处理。
        """
        time.sleep(75)  # 等浏览器预热完成，别和启动抢资源
        prev = self._load_poll_counts()
        seen_cards = set()  # 已处理的卡片链接，防重复
        seen_cmds = set()   # 已处理的命令文本（直接指令扫描，不依赖计数去重）
        log(f'[poll] 启动，基线 {len(prev)} 条')
        while True:
            try:
                from send_text import fetch_recent_sync

                rows = fetch_recent_sync('潮汐', True, '285391874')
                # 文本：和以前一样做计数去重
                cnt = {}
                # 卡片：按链接去重
                cards_to_handle = []
                for r in rows:
                    if r.get('side') != 'him':
                        continue
                    t = (r.get('text') or '').strip()
                    if t:
                        cnt[t] = cnt.get(t, 0) + 1
                    for link in (r.get('links') or []):
                        if link and link not in seen_cards:
                            seen_cards.add(link)
                            cards_to_handle.insert(0, link)  # 新的在前，回放时先处理旧的

                # 文本
                fresh_text = []
                for t, c in cnt.items():
                    if c > prev.get(t, 0):
                        fresh_text.append(t)
                if fresh_text:
                    log(f'[poll] 回源发现 {len(fresh_text)} 条新文本: {fresh_text[:3]}')
                    for t in reversed(fresh_text):
                        self._handle_polled_text(t)

                # 直接指令扫描：不依赖计数去重，每轮都把看起来像指令的文字走一遍 _handle_auto_cmd
                # （WS 一死，主人发了命令可能永远等不到。这里保底：只要聊天记录里出现了指令关键词就跑一遍。）
                cmd_keywords = ('刷', '别刷', '停', '继续', '好了', '可以', '搞定', '休息', '睡觉', '够了')
                for t in set(cnt.keys()):
                    low = t.lower().replace(' ', '')
                    if any(kw in low for kw in cmd_keywords) and t not in seen_cmds:
                        seen_cmds.add(t)
                        log(f'[poll] 指令扫描命中: {t[:60]}')
                        self._handle_auto_cmd(t)

                # 卡片（视频/分享链接）
                if cards_to_handle:
                    log(f'[poll] 回源发现 {len(cards_to_handle)} 张新卡片')
                    for link in cards_to_handle:
                        sender_str = str(next(iter(MASTER_MAP)))
                        log(f'[poll] 处理卡片链接: {link[:80]}')
                        self._handle_share_text(link, int(sender_str), sender_str, True, '老大')

                prev = cnt
                self._save_poll_counts(cnt)
            except Exception as e:
                log(f'[poll] err {type(e).__name__}: {e}')
            time.sleep(40)

    def _handle_polled_text(self, text):
        """轮询捞到的老大文字消息：走和实时推送一样的回复链路。"""
        try:
            idx = 'poll:' + hashlib.md5(text.encode('utf-8')).hexdigest()[:12]
            if idx in self.processed:
                return
            self.processed.add(idx)
            log(f'[poll] 处理: {text[:60]}')
            self._handle_auto_cmd(text)
            self._handle_restart(text, str(next(iter(MASTER_MAP))))
            if self._handle_share_text(text, next(iter(MASTER_MAP)), str(next(iter(MASTER_MAP))), True, '老大'):
                return
            reply = self.generate_reply(text, sender_label='老大', is_master=True)
            self._write_message('user', f'[抖音私信] {text}')
            if reply:
                self._handle_brain_result(next(iter(MASTER_MAP)), reply, is_master=True)
        except Exception as e:
            log(f'[poll] 处理失败 {type(e).__name__}: {e}')

    def _http_load_state(self):
        try:
            return dict(json.load(open(self.HTTP_STATE, encoding='utf-8')).get('last') or {})
        except Exception:
            return {}

    def _http_save_state(self, last):
        try:
            json.dump({'last': last, 'ts': int(time.time())},
                      open(self.HTTP_STATE, 'w', encoding='utf-8'), ensure_ascii=False)
        except Exception as e:
            log(f'[http] 存状态失败 {e}')

    def _http_poll_loop(self):
        """每 12 秒（或被 WS 帧立刻唤醒）用 Cookie 直连 IM 接口回源一次。

        这是现在**唯一**的文本/卡片处理入口。原因见 im_http.py 顶部注释：
        WS 推送会静默死掉，而且卡片是以自己的 uid 推下来的、帧里也没有时间戳；
        HTTP 接口一次 ~0.5 秒拿到全部会话 + 真实时间戳 + itemId，稳定得多。
        """
        import im_http

        last = self._http_load_state()
        first = not last
        poll_n = 0
        did_startup_scan = False  # 启动后第一轮扫一次已有指令（不管新建基线还是续跑）
        log(f'[http] 启动，{"建立基线" if first else f"续跑（{len(last)} 个会话）"}')
        while True:
            try:
                self._http_evt.wait(12)
                self._http_evt.clear()
                poll_n += 1
                r = im_http.fetch(my_uid=self.my_uid, limit=25)
                if not r.get('ok'):
                    log(f"[http] 回源失败 {r.get('error')}")
                    time.sleep(3)
                    continue
                self.my_uid = r.get('my_uid') or self.my_uid
                dispatched = 0
                for conv in r.get('conversations') or []:
                    cid = conv['conv_id']
                    if str(conv.get('peer_uid') or '') == str(self.my_uid):
                        continue  # 自己的会话（自己发给自己）不参与，避免自我循环
                    msgs = conv.get('messages') or []
                    if not msgs:
                        continue
                    prev = float(last.get(cid) or 0)
                    fresh = [m for m in msgs if (m['ts'] or 0) > prev and not m.get('from_me')]
                    newest = max(m['ts'] or 0 for m in msgs)
                    if first:
                        last[cid] = newest
                        continue
                    for m in fresh:
                        dispatched += 1
                        try:
                            self._dispatch_http(conv['peer_uid'], m)
                        except Exception as e:
                            log(f"[http] 处理失败 {type(e).__name__}: {e}")
                            traceback.print_exc()
                    if newest > prev:
                        last[cid] = newest
                if first:
                    first = False
                    log(f'[http] 基线完成：{len(last)} 个会话，从此刻起只回新消息')
                # 启动后第一轮（不管新建基线还是续跑）：扫一遍主人会话里已有的指令
                if not did_startup_scan:
                    did_startup_scan = True
                    for conv2 in r.get('conversations') or []:
                        if str(conv2.get('peer_uid') or '') == str(self.my_uid):
                            continue
                        if str(conv2.get('peer_uid') or '') not in MASTER_MAP:
                            continue
                        for old_m in (conv2.get('messages') or []):
                            if old_m.get('from_me'):
                                continue
                            txt = (old_m.get('text') or '').strip()
                            if not txt:
                                continue
                            cmd_kw = ('刷', '停', '继续', '休息', '好了', '可以', '搞定', '够了', '睡觉')
                            if any(kw in txt.lower().replace(' ', '') for kw in cmd_kw):
                                log(f'[http] 回溯指令: {txt[:60]}')
                                self._handle_auto_cmd(txt)
                elif poll_n % 10 == 0:
                    # 心跳：证明轮询线程还活着，以及这一轮到底翻到几条新消息
                    log(f'[http] 心跳 第{poll_n}轮 会话{len(r.get("conversations") or [])} 新消息{dispatched}')
                self._http_save_state(last)
            except Exception as e:
                log(f'[http] err {type(e).__name__}: {e}')
                time.sleep(5)

    def _dispatch_http(self, peer_uid, m, src='http'):
        """把一条消息送进回复链路（WS 实时帧和 HTTP 回源共用，靠 src 区分）。"""
        # 自己的消息一律不回：否则「自己发给自己」的会话会无限自我对话
        if str(peer_uid) == str(self.my_uid) or str(m.get('from_uid') or '') == str(self.my_uid):
            log('[http] 跳过自己发的消息')
            return
        sender_str = str(peer_uid)
        label = MASTER_MAP.get(sender_str)
        is_master = label is not None
        who_label = label if is_master else f'一位网友（抖音uid {sender_str}，不是你老大）'
        awe, item = m.get('awe'), (m.get('item_id') or '').strip()
        title = (m.get('title') or '').strip()

        # auto_browse 分享卡片后 HTTP 回源也可能拉到回显：跳过刚分享的
        if item and self._auto_browse_just_shared(item):
            log(f'[http] itemId={item} auto_browse 刚分享过，跳过回显')
            return

        # 800 = 分享作品卡片（视频/图文）；图文作品的 awe 是 0，但同样带 itemId + 封面，
        # 所以判据改成「有 itemId 且不是 700(纯链接文字)」，否则图文会被当成普通文本漏掉。
        if (awe == 800 or (item and awe != 700)) and (item or title):
            log(f'[http] 卡片 item={item} title={title[:40]}')
            content = {'itemId': item, 'content_title': title}
            if m.get('cover_url'):
                content['cover_url'] = {'url_list': [m['cover_url']]}
            if not item:
                self._handle_video_fallback(int(sender_str) if sender_str.isdigit() else sender_str,
                                            sender_str, is_master, who_label, title, m.get('cover_url'))
            else:
                self._handle_video_card(int(sender_str), sender_str, is_master, who_label, content)
            return

        text = (m.get('text') or '').strip()
        if not text:
            return
        if src == 'http':
            # WS 已经把这条回过了（两者文本一致），HTTP 只做兜底，不再重复回一遍
            k = (sender_str, text[:80])
            seen = self._ws_text_ts.get(k)
            if seen and time.time() - seen < 300:
                log(f'[http] 跳过重复（WS 已回）：{text[:40]}')
                return
        log(f'[http] 文本 from={sender_str} master={is_master} src={src} text={text[:60]}')
        # 限流只在 HTTP 回源这条路上做一次；src='ws' 的实时帧在 on_message 里已经算过了，
        # 否则同一条消息被数两次，陌生人永远卡在限流里（之前"陌生人不回"就是这个双计数）。
        if src == 'http' and not is_master and not self._allow_stranger(sender_str):
            log(f'[throttle] 网友 {sender_str} 发得太频繁，这条先不回（http）')
            return
        if is_master:
            self._handle_auto_cmd(text)
            self._handle_restart(text, sender_str)
            if self._handle_share_text(text, int(sender_str), sender_str, is_master, who_label):
                return
        reply = self.generate_reply(text, sender_label=who_label, is_master=is_master, sender_id=sender_str)
        if is_master:
            self._write_message('user', f'[抖音私信] {text}')
        if reply:
            self._handle_brain_result(sender_str, reply, is_master=is_master)
            rtext = reply[0] if reply[0] else ''
            if is_master and (random.random() < MEME_PROB or _wants_meme(text)):
                # 别在加载失败/错误兜底后面再接一张表情，看着像自说自话
                if '加载不出来' not in rtext and '没听清' not in rtext:
                    send_meme_async(f'{text} {rtext}')

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

    def start(self):
        # 心跳——面板靠这个判断在线/离线（比 WMI 可靠得多）
        threading.Thread(target=self._heartbeat_writer, name="dy-heartbeat", daemon=True).start()
        # 风控 watchdog：周期探测，撞上 verify_check 类风控就用抖音私信通知主人手动处理
        try:
            import risk_guard
            threading.Thread(target=risk_guard.run_watchdog, name="dy-risk-guard", daemon=True).start()
        except Exception:
            pass  # watchdog 起不来不阻塞主流程
        threading.Thread(target=self._silence_watchdog, name="dy-silence-wd", daemon=True).start()
        # 常驻有头浏览器：评论/点赞/收藏共用一个窗口，静音不关闭
        try:
            import headed_browser
            threading.Thread(target=headed_browser.start, name="dy-headed-browser", daemon=True).start()
        except Exception:
            pass
        # 评论回复通知 watchdog：定时扫通知页，有人回了汐缘就自动怼回去
        threading.Thread(target=self._notification_watchdog, name="dy-notify-wd", daemon=True).start()
        threading.Thread(target=self._http_poll_loop, name="dy-http-poll", daemon=True).start()
        while True:
            try:
                self.ws = WebSocketApp(
                    url=self.url,
                    header={
                        'Pragma': 'no-cache',
                        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
                        'User-Agent': HeaderBuilder.ua,
                        'Cache-Control': 'no-cache',
                        'Sec-WebSocket-Protocol': 'binary, base64, pbbp2',
                        'Sec-WebSocket-Extensions': 'permessage-deflate; client_max_window_bits',
                    },
                    cookie=self.auth.cookie_str,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    on_open=self.on_open,
                )
                # 心跳：每 20 秒发一次 ping，10 秒内收不到 pong 视为断线并重连
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


def load_auth():
    cookie_str = ''
    for l in open(os.path.join(HERE, '.env'), encoding='utf-8'):
        l = l.strip()
        if l.startswith('DY_COOKIES='):
            cookie_str = l[len('DY_COOKIES='):]
    keys_str = json.load(open(os.path.join(HERE, 'auth_keys.json'), encoding='utf-8'))['keys_str']
    auth = DouyinAuth()
    auth.perepare_auth(cookie_str, '', keys_str)
    # 空字符串占位（实测发私信不需要 web_protect 票据）
    auth.ticket = ''
    auth.ts_sign = ''
    auth.client_cert = ''
    return auth


if __name__ == '__main__':
    if not acquire_single_instance():
        log("[dup] 已有 reply_bot 在运行，本实例退出（避免同一条私信回两遍）")
        sys.exit(0)
    auth = load_auth()
    bot = ReplyBot(auth)
    bot.start()
