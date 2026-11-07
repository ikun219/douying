# -*- coding: utf-8 -*-
"""文本工具 — 从 core/tools.py 拆出的回复清洗 / 表情包检测 / 发送函数。
"""

import os
import threading
import asyncio


# ═══════════════════════════════════════════
#  回复清洗
# ═══════════════════════════════════════════

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


# ═══════════════════════════════════════════
#  表情包检测
# ═══════════════════════════════════════════

MEME_PROB = float(os.environ.get('DOUYIN_MEME_PROB', '0.1'))
MEME_REQUEST_KEYS = (
    '表情包', '发表情', '来张图', '发张图', '发个图', '发图',
    '要表情', '来张表情', '张表情包', '丢个表情', '表情',
)


def _wants_meme(text: str) -> bool:
    """用户文本里是否明确要求表情包/图片。"""
    return any(k in text for k in MEME_REQUEST_KEYS)


# ═══════════════════════════════════════════
#  语音 / 表情包 发送
# ═══════════════════════════════════════════

def send_voice_message(text: str) -> bool:
    """文字 → TTS mp3 → 语音 mp4 → 发抖音私信（阻塞，约 20-40 秒，放线程里跑）。"""
    try:
        from voice import build_voice
        from send_voice import send_voice_dm
        mp4 = build_voice(text)
        r = asyncio.run(send_voice_dm(str(mp4), headless=True))
        ok = bool(r.get('ok'))
        from core.tools import log
        log(f"[voice -> 老大] ok={ok} {text[:40]} {r.get('error', '')}")
        return ok
    except Exception as e:
        from core.tools import log
        log(f"[voice err] {e}")
        return False


def send_voice_async(text: str):
    threading.Thread(target=send_voice_message, args=(text,), daemon=True).start()


def send_meme_message(hint: str) -> bool:
    """按情绪挑一张表情包 → 发抖音私信（阻塞约 20 秒，放线程里跑）。"""
    try:
        from meme import pick_meme, send_meme_dm
        img = pick_meme(hint, cute=True)
        if not img:
            from core.tools import log
            log('[meme] 表情库为空')
            return False
        r = asyncio.run(send_meme_dm(str(img), headless=True))
        ok = bool(r.get('ok'))
        from core.tools import log
        log(f"[meme -> 老大] ok={ok} {img.name} {r.get('error', '')}")
        return ok
    except Exception as e:
        from core.tools import log
        log(f"[meme err] {e}")
        return False


def send_meme_async(hint: str):
    threading.Thread(target=send_meme_message, args=(hint,), daemon=True).start()