# -*- coding: utf-8 -*-
"""
llm_providers.py — 汐缘 项目级统一 LLM 调用接口。

集中管理所有云端 LLM 服务的密钥、端点与调用封装，供 reply_bot / auto_browse /
watch_video 等模块共用，消除散落各文件的硬编码重复。

注意：本模块禁止 import reply_bot / auto_browse / watch_video（防止循环依赖）。
自带独立 requests.Session，连接复用与本模块生命周期一致。
"""
import base64
import json
import os
import time

import requests
import urllib3

# 与全项目 verify=False 的用法一致，静默 InsecureRequestWarning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "llm_providers.log")

_SESSION = requests.Session()

# ---------------- DeepSeek（聊天主路由） ----------------
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# ---------------- SiliconFlow（语音转写 + VLM 模型） ----------------
SILICONFLOW_KEY = os.environ.get("SILICONFLOW_KEY", "")
STT_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
STT_MODEL = "FunAudioLLM/SenseVoiceSmall"
SF_CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
SF_FLASH_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"
SF_QWEN_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
# ---------------- 阿里云百炼 DashScope（OpenAI 兼容模式，图片理解，唯一路由） ----------------
DASHSCOPE_KEY = os.environ.get("DASHSCOPE_KEY", "")
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_VL_MODEL = "qwen3-vl-plus"

# ---- 模型配置覆盖 ----
LLM_CONFIG_FILE = os.path.join(HERE, "llm_config.json")


def _load_model_overrides():
    """从 llm_config.json 读取模型覆盖；不存在则返回空 dict。"""
    try:
        if os.path.exists(LLM_CONFIG_FILE):
            with open(LLM_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def get_model_config():
    """返回当前生效的模型配置（含默认值）。"""
    ov = _load_model_overrides()
    return {
        "chat_model": ov.get("chat_model", DEEPSEEK_MODEL),
        "vlm_model": ov.get("vlm_model", DASHSCOPE_VL_MODEL),
        "stt_model": ov.get("stt_model", STT_MODEL),
        "emotion_flash_model": ov.get("emotion_flash_model", SF_FLASH_MODEL),
        "emotion_qwen_model": ov.get("emotion_qwen_model", SF_QWEN_MODEL),
    }


def save_model_config(updates: dict):
    """合并更新并写入 llm_config.json。返回完整配置。"""
    import json as _json
    current = _load_model_overrides()
    current.update(updates)
    with open(LLM_CONFIG_FILE, "w", encoding="utf-8") as f:
        _json.dump(current, f, indent=2, ensure_ascii=False)
    log(f"[config] 模型配置已更新：{updates}")
    return current


def _model(name, default):
    """读当前覆盖值，缺省回退常量。"""
    ov = _load_model_overrides()
    return ov.get(name, default)


def log(msg):
    """统一日志：打印到 stdout 并追加到 llm_providers.log。"""
    try:
        print(msg, flush=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def deepseek_chat(messages, *, max_tokens=320, temperature=0.95, timeout=60, tools=None):
    """调用 DeepSeek chat/completions。

    成功返回 choices[0]['message'] dict（含 content / tool_calls），失败返回 None。
    tools 非空时才附加到请求体（function calling）。
    """
    payload = {
        "model": _model("chat_model", DEEPSEEK_MODEL),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
    try:
        r = _SESSION.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if r.status_code != 200:
            log(f"[deepseek] HTTP {r.status_code} {r.text[:160]}")
            return None
        d = r.json()
        return d["choices"][0]["message"]
    except Exception as e:
        log(f"[deepseek err] {e}")
        return None


def stt_transcribe(content, filename, mime, timeout=120):
    """语音转文字（SiliconFlow SenseVoiceSmall）。

    content 为音频原始字节，filename/mime 为上传文件信息。
    成功返回识别文本（strip 后），失败返回 None。
    """
    try:
        r = _SESSION.post(
            STT_URL,
            headers={"Authorization": f"Bearer {SILICONFLOW_KEY}"},
            files={"file": (filename, content, mime)},
            data={"model": _model("stt_model", STT_MODEL)},
            timeout=timeout,
        )
        if r.status_code != 200:
            log(f"[stt] {r.status_code} {r.text[:120]}")
            return None
        return (r.json() or {}).get("text", "").strip() or None
    except Exception as e:
        log(f"[stt err] {e}")
        return None


def vlm_describe_image(url, timeout=90, prompt=None):
    """下载图片 → 千问 qwen3-vl-plus（百炼）。失败返回 None。

    url 也可以直接是本地文件路径（抽帧后的拼图就是本地文件）。
    """
    try:
        if os.path.exists(url):
            with open(url, "rb") as f:
                raw = f.read()
            mt = "image/jpeg"
        else:
            r = _SESSION.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"},
            )
            if r.status_code != 200 or not r.content:
                log(f"[vlm] 下载失败 {r.status_code}")
                return None
            raw = r.content
            mt = r.headers.get("content-type", "image/jpeg").split(";")[0]

        b64 = base64.b64encode(raw).decode()
        data_uri = f"data:{mt};base64,{b64}"
        prompt = prompt or (
            "这是一张聊天表情包。按一行回答，不要客套、不要解释，总字数不超过50字：梗："
            "（先判断它是不是网络梗/经典模板，如 doge、熊猫头、无语、敲打、破防、猫猫头；"
            "是就写梗名，不是就写「无」）；画面：（画的是谁、在做什么动作、图上文字原样写出来）；"
            "情绪：（发图的人此刻在表达什么，一两个词）。"
        )

        def _call(base, key, model, attempts=2):
            payload = {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                "max_tokens": 120,
                "temperature": 0.2,
            }
            last = None
            for i in range(attempts):
                try:
                    resp = _SESSION.post(
                        base.rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json=payload,
                        timeout=timeout,
                    )
                    if resp.status_code != 200:
                        log(f"[vlm] {model} {resp.status_code} {resp.text[:150]}")
                        return None
                    d = resp.json()
                    txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
                    txt = " ".join(str(txt or "").split())
                    if not txt:
                        return None
                    return txt
                except Exception as e:
                    last = e
                    if i < attempts - 1:
                        log(f"[vlm] {model} 第{i + 1}次失败 {e}，重试")
                        time.sleep(1)
            if last:
                log(f"[vlm] {model} 失败 {last}")
            return None

        txt = _call(DASHSCOPE_BASE, DASHSCOPE_KEY, _model("vlm_model", DASHSCOPE_VL_MODEL))
        if txt:
            log(f"[vlm] 千问(vl-plus) -> {txt}")
        return txt
    except Exception as e:
        log(f"[vlm err] {e}")
        return None


def _vlm_call_single(key, model, data_uri, prompt, timeout=90):
    """对内：单模型 VLM 调用，返回原始文本或 None。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 40,
        "temperature": 0.0,
    }
    try:
        r = _SESSION.post(
            SF_CHAT_URL if key == SILICONFLOW_KEY else DASHSCOPE_BASE.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        if r.status_code != 200:
            log(f"[emotion] {model} HTTP {r.status_code} {r.text[:120]}")
            return None
        return (r.json() or {}).get("choices", [{}])[0].get("message", {}).get("content", "").strip() or None
    except Exception as e:
        log(f"[emotion] {model} 异常 {e}")
        return None


def _normalize_emotion(text, choices):
    """把 VLM 输出归一到 choices 之一；失败返回 None。"""
    if not text or text.startswith("ERR"):
        return None
    t = text.strip().replace("。", "").replace("，", ",").strip(".,!!?!'\" ")
    t = t.split(" ")[0].split("\n")[0].split(",")[0].strip()
    for c in choices:
        if t == c or c in t:
            return c
    return None


def vlm_emotion_classify(data_uri, prompt, choices, timeout=90):
    """三模型并行识情投票（flash + 千问百炼 + 千问SF）。

    返回 (winning_choice | None, {model_tag: raw_text | None})。
    多数（≥2 票一致）→ 该选项；否则 None。
    """
    from concurrent.futures import ThreadPoolExecutor

    jobs = {
        "flash": (SILICONFLOW_KEY, _model("emotion_flash_model", SF_FLASH_MODEL)),
        "dashscope": (DASHSCOPE_KEY, _model("vlm_model", DASHSCOPE_VL_MODEL)),
        "qwen": (SILICONFLOW_KEY, _model("emotion_qwen_model", SF_QWEN_MODEL)),
    }
    votes = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {
            tag: ex.submit(_vlm_call_single, key, model, data_uri, prompt, timeout)
            for tag, (key, model) in jobs.items()
        }
        for tag, fut in futs.items():
            try:
                votes[tag] = _normalize_emotion(fut.result(), choices)
            except Exception as e:
                log(f"[emotion] {tag} 异常 {e}")
                votes[tag] = None

    cnt = {}
    for tag, emo in votes.items():
        if emo:
            cnt[emo] = cnt.get(emo, 0) + 1
    if not cnt:
        return None, votes
    best = max(cnt, key=lambda e: (cnt[e], -next(i for i, x in enumerate(choices) if x == e)))
    return (best if cnt[best] >= 2 else None), votes


def vlm_emotion_is_cute(data_uri, prompt, timeout=90):
    """flash 二分类判断可爱。返回 True（可爱）/ False（不可爱）/ None（失败）。"""
    r = _vlm_call_single(SILICONFLOW_KEY, SF_FLASH_MODEL, data_uri, prompt, timeout=timeout)
    t = _normalize_emotion(r, ("可爱", "不可爱"))
    if t == "可爱":
        return True
    if t == "不可爱":
        return False
    return None