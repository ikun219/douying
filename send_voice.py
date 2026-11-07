# -*- coding: utf-8 -*-
"""把本地 mp4（含音轨）发到抖音私信。

抖音网页版私信不支持语音气泡，只接受 .jpg/.png/.gif/.mp4；
所以语音被做成 mp4（静态画面+音轨），对方点开即可听到。

CLI:
    python send_voice.py path/to/voice.mp4 [--headless] [--conv 潮汐]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "storage_state.json"
os.environ.setdefault("TMP", str(HERE / "tmp"))
os.environ.setdefault("TEMP", str(HERE / "tmp"))
os.environ.setdefault("TMPDIR", str(HERE / "tmp"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(HERE / "playwright_browsers"))

from playwright.async_api import async_playwright  # noqa: E402


# ------------------------------------------------------------------ 常驻浏览器
class _VoiceSender:
    """复用浏览器进程：语音/表情包发私信走同一个 keepalive 浏览器。"""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return self._loop
        loop = asyncio.new_event_loop()
        th = threading.Thread(target=loop.run_forever, name="douyin-voice-loop", daemon=True)
        th.start()
        self._loop, self._thread = loop, th
        return loop

    def _submit(self, coro, timeout: float = 120.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        return fut.result(timeout)

    async def _ensure_browser(self):
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = await self._pw.chromium.launch(channel="msedge", headless=True)
        return self._pw, self._browser

    async def _send_async(self, mp4_path: str, conv_name: str | None = None) -> dict:
        mp4 = Path(mp4_path).resolve()
        if not mp4.exists():
            return {"ok": False, "error": f"文件不存在: {mp4}"}
        hits: list[str] = []

        _, browser = await self._ensure_browser()
        ctx = await browser.new_context(storage_state=str(STATE), viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        def on_req(r):
            if r.method in ("POST", "PUT") and any(k in r.url for k in ("tos-", "/message/send", "upload")):
                hits.append(f"{r.method} {r.url[:120]}")

        page.on("request", on_req)
        try:
            await page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded")
            await page.wait_for_selector('[data-e2e="conversation-item"]', timeout=45000)
            await asyncio.sleep(4)

            items = page.locator('[data-e2e="conversation-item"]')
            n = await items.count()
            if n == 0:
                return {"ok": False, "error": "没有会话列表（可能登录失效）"}
            target = items.first
            if conv_name:
                for i in range(n):
                    t = (await items.nth(i).inner_text() or "")
                    if conv_name in t:
                        target = items.nth(i)
                        break
            await target.click(timeout=10000)
            await asyncio.sleep(4)

            box = page.locator("input[type=file].semi-upload-hidden-input")
            if await box.count() == 0:
                return {"ok": False, "error": "找不到文件上传控件（页面结构可能变了）"}
            await box.first.set_input_files(str(mp4))
            await asyncio.sleep(3)

            sent = False
            for sel in ["button:has-text('发送')", ".semi-modal button:has-text('发送')"]:
                try:
                    await page.click(sel, timeout=6000)
                    sent = True
                    break
                except Exception:
                    continue
            if not sent:
                return {"ok": False, "error": "没找到发送按钮"}
            await asyncio.sleep(14)
            return {"ok": True, "file": str(mp4), "requests": hits[-4:]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    def send(self, mp4_path: str, conv_name: str | None = None) -> dict:
        with self._lock:
            return self._submit(self._send_async(mp4_path, conv_name))


_VOICE_SENDER = _VoiceSender()


async def send_voice_dm(mp4_path: str, headless: bool = False, conv_name: str | None = None) -> dict:
    """发语音私信（保留旧签名兼容）。headless 参数已忽略——常驻浏览器始终 headless。"""
    return _VOICE_SENDER.send(mp4_path, conv_name=conv_name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mp4")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--conv", default=None)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    # CLI 仍然直连（不复用常驻浏览器，因为用完就退出了）
    r = asyncio.run(_VOICE_SENDER._send_async(args.mp4, conv_name=args.conv))
    print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()