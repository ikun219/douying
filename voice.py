# -*- coding: utf-8 -*-
"""汐缘语音：文本 → TTS(mp3) → 带音轨的短视频(mp4)，用于抖音私信发送。

抖音网页版私信只接受 .jpg/.png/.gif/.mp4，没有语音气泡；
所以把语音做成 mp4（静态画面 + 音轨），对方点开就能听到声音。

用法：
    python voice.py "要说的话"                    # 只生成 mp4
    python voice.py "要说的话" --send             # 生成并发送到抖音私信
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parent
TTS_CFG = Path(__file__).parent.parent / 'astrbot' / 'data' / 'config' / 'astrbot_plugin_seed_tts_config.json'
TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
FFMPEG = Path(os.environ.get("FFMPEG", shutil.which("ffmpeg") or ""))
WORK = HERE / "voice_work"

sys.stdout.reconfigure(encoding="utf-8")


def tts_to_mp3(text: str, out_mp3: Path) -> Path:
    """火山 seed-icl-2.0 语音合成，返回 mp3 路径。"""
    cfg = json.loads(TTS_CFG.read_text(encoding="utf-8-sig"))
    api_key = cfg["api_key"]
    speaker = cfg["speaker"]
    resource = cfg.get("resource_id", "seed-icl-2.0")
    audio_params = {
        "format": "mp3",
        "sample_rate": int(cfg.get("sample_rate", 24000)),
        "speech_rate": int(cfg.get("speech_rate", 0)),
    }
    payload = {
        "user": {"uid": "xiyuan_douyin"},
        "req_params": {"text": text, "speaker": speaker, "audio_params": audio_params},
    }
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    req = urllib.request.Request(TTS_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    buf = b""
    with urllib.request.urlopen(req, timeout=120) as resp:
        for line in resp:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            data = obj.get("data")
            if data:
                buf += base64.b64decode(data)
    if not buf:
        raise RuntimeError("TTS 返回空音频")
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    out_mp3.write_bytes(buf)
    return out_mp3


def make_voice_mp4(mp3: Path, out_mp4: Path, color: str = "0x3a3f4b") -> Path:
    """mp3 → mp4（正方形静态底色 + 音轨）。"""
    if not FFMPEG.exists():
        raise RuntimeError(f"ffmpeg 不存在: {FFMPEG}")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={color}:s=480x480:r=2",
        "-i", str(mp3),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart",
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True)
    return out_mp4


def build_voice(text: str, name: str | None = None) -> Path:
    """文本 → mp4（含音轨），返回 mp4 路径。"""
    WORK.mkdir(parents=True, exist_ok=True)
    tag = name or uuid.uuid4().hex[:8]
    mp3 = WORK / f"{tag}.mp3"
    mp4 = WORK / f"{tag}.mp4"
    tts_to_mp3(text, mp3)
    make_voice_mp4(mp3, mp4)
    return mp4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--send", action="store_true", help="生成后直接发到抖音私信")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    mp4 = build_voice(args.text)
    print(f"OK {mp4} ({mp4.stat().st_size} bytes)")
    if args.send:
        import asyncio
        from send_voice import send_voice_dm
        r = asyncio.run(send_voice_dm(str(mp4), headless=args.headless))
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
