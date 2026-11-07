# -*- coding: utf-8 -*-
"""抖音私信发送模块 — 文字/语音/表情包统一接口。

导出:
    send_text(uid, text)   — 发文字私信
    send_voice(uid, mp4)   — 发语音私信
    send_meme(uid, hint)   — 发表情包私信
    send_file(uid, path)   — 发文件（图片/视频）

CLI:
    python -m douyin.api.message text <uid> <消息>
    python -m douyin.api.message voice <uid> <mp4路径>
    python -m douyin.api.message meme <uid> <关键词>
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # douyin/
sys.path.insert(0, str(HERE))


def send_text(uid: str, text: str, conv_name: str | None = None) -> dict:
    """发文字私信（复用 DouyinMessenger keepalive 浏览器）。

    Args:
        uid: 抖音用户 ID
        text: 消息内容
        conv_name: 会话名（可选，模糊匹配）

    Returns {"ok": True} or {"ok": False, "error": "..."}
    """
    try:
        from send_text import _KA
        r = _KA.send(text, conv_name=conv_name, conv_uid=uid)
        return {"ok": r.get("ok", False), "mode": r.get("mode", "keepalive")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def send_voice(uid: str, mp4_path: str, conv_name: str | None = None) -> dict:
    """发语音私信（复用 _VoiceSender keepalive 浏览器）。

    Args:
        uid: 抖音用户 ID（当前版本忽略，自己选会话列表第一项或 conv_name）
        mp4_path: mp4 文件路径
        conv_name: 会话名（可选，模糊匹配）

    Returns {"ok": True, "file": "..."} or {"ok": False, "error": "..."}
    """
    try:
        from send_voice import send_voice_dm
        return asyncio.run(send_voice_dm(mp4_path, headless=True, conv_name=conv_name))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def send_meme(uid: str, hint: str = "", conv_name: str | None = None, cute: bool = True) -> dict:
    """发表情包私信。

    Args:
        uid: 抖音用户 ID（当前版本忽略）
        hint: 表情包关键词（如 "开心"、"无语"）
        conv_name: 会话名（可选，模糊匹配）
        cute: 是否先让 flash 判可爱再发（默认 True）

    Returns {"ok": True, "file": "...", "hint": "..."} or {"ok": False, "error": "..."}
    """
    try:
        from meme import pick_meme, send_meme_dm
        img = pick_meme(hint, cute=cute)
        if img is None:
            return {"ok": False, "error": "表情库为空或无匹配"}
        r = asyncio.run(send_meme_dm(img, headless=True, conv_name=conv_name))
        r["hint"] = hint
        return r
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def send_file(uid: str, path: str, conv_name: str | None = None) -> dict:
    """发文件（图片/视频）私信 — 走同样的上传链路。

    send_voice_dm 的上传 input[type=file] 接受 jpg/png/gif/mp4/webp。
    """
    return send_voice(uid, path, conv_name=conv_name)


def _env_info() -> dict:
    """快速诊断 — 当前环境是否就绪。"""
    info = {}
    try:
        from send_text import _KA
        info["send_text"] = "ready" if _KA._loop is not None else "warm pending"
    except Exception as e:
        info["send_text"] = f"error: {e}"
    try:
        from send_voice import send_voice_dm
        info["send_voice"] = "ready"
    except Exception as e:
        info["send_voice"] = f"error: {e}"
    try:
        from meme import categories
        n = sum(len(v) for v in categories().values())
        info["meme"] = f"ready ({n} images)"
    except Exception as e:
        info["meme"] = f"error: {e}"
    state = HERE / "storage_state.json"
    info["storage_state"] = "exists" if state.exists() else "missing"
    return info


# --------------------------------------------------------------- CLI
def main():
    if len(sys.argv) < 3:
        print("用法:", file=sys.stderr)
        print("  python -m douyin.api.message text <uid> <消息>", file=sys.stderr)
        print("  python -m douyin.api.message voice <uid> <mp4路径>", file=sys.stderr)
        print("  python -m douyin.api.message meme <uid> <关键词>", file=sys.stderr)
        print("  python -m douyin.api.message env", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "env":
        import json
        print(json.dumps(_env_info(), ensure_ascii=False, indent=2))
        return

    uid = sys.argv[2]
    rest = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""

    import json
    if cmd == "text":
        r = send_text(uid, rest)
    elif cmd == "voice":
        r = send_voice(uid, rest)
    elif cmd == "meme":
        r = send_meme(uid, rest)
    elif cmd == "file":
        r = send_file(uid, rest)
    else:
        print(f"未知命令: {cmd}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()