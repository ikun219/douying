# -*- coding: utf-8 -*-
"""ML/LLM 桥接工具 — 从 core/tools.py 拆出的 STT / VLM / URL 解析函数。

纯函数 + 薄封装，依赖 llm_providers 和 _SESSION。
"""
import re
import core.tools


# ═══════════════════════════════════════════
#  视频/图片理解 Prompt
# ═══════════════════════════════════════════

PROMPT_VIDEO_COVER = (
    '这是一个短视频的封面图。按一行回答，不要客套，不超过50字：'
    '画面：（谁/什么、在做什么、场景，图上的文字原样写出）；风格'
    '：（真人/AI生成/动画/游戏，选一个）。'
)


# ═══════════════════════════════════════════
#  URL / 图片 遍历
# ═══════════════════════════════════════════

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


# ═══════════════════════════════════════════
#  VLM 图片/视频理解 (→ llm_providers)
# ═══════════════════════════════════════════

def describe_image_url(url: str, timeout: int = 90, prompt: str = None):
    """使用百炼 qwen3-vl-plus 识别图片。失败返回 None。

    url 也可以直接是本地文件路径（抽帧后的拼图就是本地文件）。
    """
    from llm_providers import vlm_describe_image
    return vlm_describe_image(url, timeout=timeout, prompt=prompt)


# ═══════════════════════════════════════════
#  分享链接解析
# ═══════════════════════════════════════════

def _extract_douyin_url(text: str):
    """从文本里挑出一个抖音分享链接，返回 (url, aweme_id 或 None)。"""
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
    from builder.header import HeaderBuilder
    try:
        r = core.tools._SESSION.get(url, allow_redirects=False, timeout=15,
                                headers={'User-Agent': HeaderBuilder.ua})
        loc = r.headers.get('Location') or ''
        if not loc:
            loc = r.text[:3000] if 'douyin.com' not in r.headers.get('Location', '') else ''

        m = re.search(r'(?:/video/|/note/|modal_id=)(\d+)', loc)
        if m:
            return m.group(1)
        m = re.search(r'(?:/video/|/note/|modal_id=)(\d+)', url)
        if m:
            return m.group(1)
    except Exception as e:
        core.tools._log(f'[share] 解析短链失败 {url} {e}')
    return None


# ═══════════════════════════════════════════
#  STT 语音识别 (→ llm_providers)
# ═══════════════════════════════════════════

def transcribe_audio_url(url: str, timeout: int = 120):
    """下载抖音语音消息 → SenseVoice 转文字。失败返回 None。"""
    try:
        r = core.tools._SESSION.get(url, timeout=timeout)
        if r.status_code != 200 or not r.content:
            core.tools._log(f"[stt] 下载失败 {r.status_code}")
            return None
        return _stt_bytes(r.content, 'voice.mp3', 'audio/mpeg', timeout)
    except Exception as e:
        core.tools._log(f"[stt err] {e}")
        return None


def _stt_bytes(content: bytes, filename: str, mime: str, timeout: int = 120):
    """把一段音频丢给 SenseVoice 转文字。"""
    from llm_providers import stt_transcribe
    return stt_transcribe(content, filename, mime, timeout)