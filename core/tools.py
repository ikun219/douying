# -*- coding: utf-8 -*-
"""内核工具模块 — 状态、日志、QQ 桥接、ARK 配置、表情包记忆、人设/情绪读取。

模块化拆分：
  ml_util.py  : STT / VLM / URL 解析 / 图片遍历
  text_util.py: 回复清洗 / 表情包检测 / 语音/表情包发送

用法:
    from core import tools
    tools.init(session=_SESSION, log_fn=log, root_dir=HERE,
              outbox_file=OUTBOX_FILE, mood_file=MOOD_FILE,
              persona_file=PERSONA_FILE, log_file=LOG_FILE)
"""

import json
import os
import time
import ctypes
import asyncio
import hashlib
import threading

# ---- 模块级状态（由 init() 注入） ----
_SESSION = None
_log = print
_HERE = ''

# 路径
OUTBOX_FILE = ''
MOOD_FILE = ''
PERSONA_FILE = ''
LOG_FILE = ''
STICKER_MEM_FILE = ''
ARK_CFG_PATH = ''

# 缓存
_ARK_CACHE = {}
_STICKER_MEM = None
_mood_cache = {'mtime': None, 'line': None}
_MUTEX_HANDLE = None


def init(session, log_fn, root_dir, outbox_file='', mood_file='',
         persona_file='', log_file=''):
    """注入共享状态 — reply_bot 启动时调用一次。"""
    global _SESSION, _log, _HERE
    global OUTBOX_FILE, MOOD_FILE, PERSONA_FILE, LOG_FILE, STICKER_MEM_FILE, ARK_CFG_PATH
    _SESSION = session
    _log = log_fn
    _HERE = root_dir
    OUTBOX_FILE = outbox_file
    MOOD_FILE = mood_file
    PERSONA_FILE = persona_file
    LOG_FILE = log_file
    STICKER_MEM_FILE = os.path.join(root_dir, 'sticker_memory.json')
    ARK_CFG_PATH = os.path.join(root_dir, '..', 'astrbot', 'data', 'cmd_config.json')


# ═══════════════════════════════════════════
#  单实例保护
# ═══════════════════════════════════════════

def acquire_single_instance() -> bool:
    """用 Windows 命名互斥体保证单实例；已有一个在跑就返回 False。
    重试几次：旧进程刚被杀时互斥体可能还没释放（TerminateProcess 异步清理）。"""
    global _MUTEX_HANDLE
    for attempt in range(5):
        _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\xiyuan_reply_bot_singleton")
        if ctypes.windll.kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            return True
        ctypes.windll.kernel32.CloseHandle(_MUTEX_HANDLE)
        _MUTEX_HANDLE = None
        time.sleep(1)
    print("[dup] 已有 reply_bot 在运行，本实例退出（避免重复启动）", flush=True)
    return False


# ═══════════════════════════════════════════
#  日志 / QQ 桥接
# ═══════════════════════════════════════════

def log(*a):
    line = ' '.join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S ') + line + '\n')
    except Exception:
        pass


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


# ═══════════════════════════════════════════
#  豆包（火山方舟）配置
# ═══════════════════════════════════════════

def _ark_conf() -> dict:
    """从 AstrBot 配置里读豆包（火山方舟）的 key 和模型名。"""
    if _ARK_CACHE:
        return _ARK_CACHE
    try:
        cfg = json.load(open(ARK_CFG_PATH, encoding='utf-8-sig'))
        key = ''
        for s in cfg.get('provider_sources') or []:
            if s.get('id') == 'ark':
                k = s.get('key')
                if isinstance(k, (list, tuple)):
                    k = k[0] if k else ''
                if isinstance(k, str) and k.strip().startswith('['):
                    try:
                        k = json.loads(k)[0]
                    except Exception:
                        k = k.strip("[]'\" ")
                key = str(k or '').strip()
                break
        model = ''
        for p in cfg.get('provider') or []:
            pid = str(p.get('id', ''))
            m = str(p.get('model') or '')
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


# ═══════════════════════════════════════════
#  表情包/梗 记忆
# ═══════════════════════════════════════════

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


# ═══════════════════════════════════════════
#  persona / mood 读取
# ═══════════════════════════════════════════

def load_persona():
    return open(PERSONA_FILE, encoding='utf-8').read()


def load_mood_line():
    """按 mtime 缓存 mood.json，省掉每条消息的磁盘读。"""
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