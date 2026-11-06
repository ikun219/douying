# -*- coding: utf-8 -*-
"""用浏览器（真实前端）给抖音好友发一条纯文字私信。

为什么不用 imapi send_msg：接口返回 cmd:100 但消息会被静默丢弃（票据/风控），
而浏览器路径（跟 share_card.py 同一条路）实测可靠。

CLI:
    python send_text.py "你好" [--conv 潮汐] [--headed]
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

INPUT_CANDIDATES = [
    '[data-e2e="message-input"] [contenteditable="true"]',
    '[contenteditable="true"]',
    "textarea",
    ".semi-input-textarea",
]

SEND_BTN_CANDIDATES = [
    '[data-e2e="message-send"]',
    'button:has-text("发送")',
    '[class*="send"]:has-text("发送")',
]


async def send_text_dm(text: str, headless: bool = True, conv_name: str | None = None) -> dict:
    hits: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=headless)
        ctx = await browser.new_context(storage_state=str(STATE), viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        async def on_resp(r):
            if "/message/send" in r.url:
                try:
                    body = await r.text()
                    hits.append(f"{r.status} {body[:300]}")
                except Exception as e:
                    hits.append(f"{r.status} <{type(e).__name__}>")

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))
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
            await asyncio.sleep(3)

            box = None
            used = ""
            for sel in INPUT_CANDIDATES:
                loc = page.locator(sel).last
                if await loc.count() > 0:
                    try:
                        await loc.click(timeout=5000)
                        box, used = loc, sel
                        break
                    except Exception:
                        continue
            if box is None:
                return {"ok": False, "error": "找不到输入框", "tried": INPUT_CANDIDATES}

            await asyncio.sleep(0.5)
            await page.keyboard.insert_text(text)
            await asyncio.sleep(0.8)

            sent_click = False
            for sel in SEND_BTN_CANDIDATES:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    try:
                        await loc.last.click(timeout=4000)
                        sent_click = True
                        used += f" | btn={sel}"
                        break
                    except Exception:
                        continue
            if not sent_click:
                await page.keyboard.press("Enter")
                used += " | btn=Enter"

            await asyncio.sleep(4)
            # 校验：输入框是否已清空 = 发出去了
            left = ""
            try:
                left = await box.inner_text()
            except Exception:
                pass
            return {
                "ok": True,
                "input": used,
                "leftover": (left or "")[:40],
                "requests": hits[-3:],
            }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "requests": hits[-3:]}
        finally:
            await browser.close()


class _SendError(Exception):
    """kind=setup 表示还没开始打字就失败（可安全重试）；kind=type 表示可能已经发出（不许重试）。"""

    def __init__(self, msg: str, kind: str = "setup") -> None:
        super().__init__(msg)
        self.kind = kind


class _KeepAlive:
    """常驻浏览器：打开一次后一直不关，之后每次只点会话 + 打字 + 回车（1~2 秒）。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._boot = threading.Lock()
        self._send_lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._conv: str | None = None

    # ---------- 事件循环跑在专属线程里 ----------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._boot:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            th = threading.Thread(target=loop.run_forever, name="douyin-send-loop", daemon=True)
            th.start()
            self._loop, self._thread = loop, th
            print(f"[send_text] 常驻线程已启动 tid={th.ident}", flush=True)
            return loop

    def _submit(self, coro, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        return fut.result(timeout)

    # ---------- 浏览器/页面 ----------
    async def _open(self):
        if self._pw is None:
            self._pw = await async_playwright().start()
            print("[send_text] playwright 已启动", flush=True)
        if self._browser is None or not self._browser.is_connected():
            self._browser = await self._pw.chromium.launch(
                channel="msedge",
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._use_cdp = False
            self._ctx = None
            self._page = None
            print("[send_text] 浏览器已启动", flush=True)
        if self._ctx is None:
            self._ctx = await self._browser.new_context(
                storage_state=str(STATE), viewport={"width": 1440, "height": 900}
            )
            self._ctx.set_default_timeout(20000)
        page = self._page
        if page is None or page.is_closed():
            page = await self._ctx.new_page()

            async def on_resp(r):
                if "/message/send" in r.url:
                    try:
                        print(f"[send_text] resp {r.status} {(await r.text())[:200]}", flush=True)
                    except Exception:
                        pass

            page.on("response", lambda r: asyncio.create_task(on_resp(r)))
            await page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded")
            await page.wait_for_selector('[data-e2e="conversation-item"]', timeout=45000)
            await asyncio.sleep(3)
            self._conv = None
            self._page = page
            print("[send_text] 会话页已就绪（常驻）", flush=True)
        return page

    async def _reset(self):
        for name in ("_page", "_ctx"):
            obj = getattr(self, name)
            setattr(self, name, None)
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        self._conv = None
        try:
            if self._browser is not None and self._browser.is_connected():
                await self._browser.close()
        except Exception:
            pass
        self._browser = None

    # ---------- 拉取最近消息（绕开失效的推送） ----------
    # 实测结论：WS 推送会静默失效（连接还在、心跳也正常，但服务端不再推新消息）。
    # 而「点开别的会话、再点回来」能强制向服务端拉一次历史，漏掉的消息就会刷出来。
    # 消息在 DOM 里是**新的在前**，用 x 坐标判方向：x>700 是自己发的。
    FETCH_JS = r"""
    () => Array.from(document.querySelectorAll('[data-e2e="msg-item-content"]')).slice(0, 12).map(el => {
      const r = el.getBoundingClientRect();
      const imgs = Array.from(el.querySelectorAll('img'))
        .map(i => i.getAttribute('src') || '')
        .filter(s => s.startsWith('http'));
      // 抓卡片里的分享/视频链接，回源兜底时靠这些发现视频卡片
      const links = Array.from(el.querySelectorAll('a[href]'))
        .map(a => a.getAttribute('href') || '')
        .filter(h => h.includes('/share/') || h.includes('/video/') || h.includes('v.douyin.com'));
      return {
        text: (el.innerText || '').replace(/\s+/g, ' ').trim(),
        x: Math.round(r.x), w: Math.round(r.width), imgs, links
      };
    })
    """

    async def _fetch(self, conv_name: str = "潮汐", resync: bool = True, away: str | None = None):
        page = await self._open()
        if resync:
            # 先点另一条会话再点回来，迫使页面重新向服务端拉一次历史
            try:
                items = page.locator('[data-e2e="conversation-item"]')
                n = await items.count()
                for i in range(min(n, 12)):
                    t = (await items.nth(i).inner_text()) or ""
                    if away and away in t:
                        await items.nth(i).click(timeout=8000)
                        await asyncio.sleep(3)
                        break
            except Exception:
                pass
            self._conv = None  # 逼它重新点回目标会话
        if self._conv != conv_name:
            await self._goto_conv(page, conv_name=conv_name)
            self._conv = conv_name
        await asyncio.sleep(1.0)
        rows = await page.evaluate(self.FETCH_JS)
        out = []
        for r in rows or []:
            side = "me" if r.get("x", 0) > 700 else "him"
            out.append({"side": side, "text": r.get("text", ""), "imgs": r.get("imgs") or [], "links": r.get("links") or []})
        return out

    def fetch_recent(self, conv_name: str = "潮汐", resync: bool = True, away: str | None = None,
                     timeout: float = 120.0):
        with self._send_lock:
            return self._submit(self._fetch(conv_name, resync, away), timeout)

    # ---------- 发送 ----------
    async def _goto_conv(self, page, conv_name: str | None = None, conv_uid: str | None = None):
        """点开某个会话（优先按 conversation id 里的 uid 匹配，其次按标题文字）。"""
        key = conv_uid or conv_name
        items = page.locator('[data-e2e="conversation-item"]')
        n = await items.count()
        if n == 0:
            raise _SendError("没有会话列表（登录可能失效）", "setup")
        target = items.first
        hit = False
        # 优先按会话的 conversation id 匹配：id 形如 "0:1:{uid}:{my_uid}"，
        # 里面一定含对方 uid，且不受"对方是否设了昵称"影响（标题可能是抖音号/昵称，不可靠）。
        try:
            ids = await page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll('[data-e2e="conversation-item"]').forEach(node => {
                        let id = null;
                        const fk = Object.keys(node).find(k => k.indexOf('__reactFiber$') === 0);
                        if (fk) {
                            let f = node[fk], d = 0;
                            while (f && d < 20) {
                                const p = f.memoizedProps;
                                if (p && typeof p === 'object') {
                                    for (const k of ['conversation','conv','item','conversationInfo','chat','session']) {
                                        const c = p[k];
                                        if (c && typeof c === 'object' && c.id) { id = c.id; break; }
                                    }
                                }
                                if (id) break;
                                f = f.return; d++;
                            }
                        }
                        out.push(id || '');
                    });
                    return out;
                }"""
            )
        except Exception:
            ids = []
        for i, cid in enumerate(ids[:40]):
            if conv_uid and cid and conv_uid in str(cid):
                target, hit = items.nth(i), True
                break
        if not hit:
            # 兜底：按标题 inner_text 匹配（昵称或数字标题）
            for i in range(min(n, 40)):
                try:
                    t = await items.nth(i).inner_text()
                except Exception:
                    continue
                if (conv_uid and conv_uid in (t or "")) or (conv_name and conv_name in (t or "")):
                    target, hit = items.nth(i), True
                    break
        try:
            await target.click(timeout=10000)
        except Exception as e:
            raise _SendError(f"点会话失败: {e}", "setup")
        await asyncio.sleep(2)
        self._conv = key if hit else None
        if not hit:
            print(f"[send_text] 警告：没找到会话「{key}」，点了第一个", flush=True)
        return hit

    async def _send(self, text: str, conv_name: str | None = None, conv_uid: str | None = None):
        page = await self._open()

        key = conv_uid or conv_name
        if key and self._conv != key:
            await self._goto_conv(page, conv_name=conv_name, conv_uid=conv_uid)

        box = None
        for sel in INPUT_CANDIDATES:
            loc = page.locator(sel).last
            try:
                if await loc.count() > 0:
                    await loc.click(timeout=5000)
                    box = loc
                    break
            except Exception:
                continue
        if box is None:
            raise _SendError("找不到输入框", "setup")

        await asyncio.sleep(0.3)
        await page.keyboard.insert_text(text)
        await asyncio.sleep(0.5)

        async def leftover() -> str:
            try:
                return ((await box.inner_text()) or "").replace("\u200b", "").strip()
            except Exception:
                return ""

        await page.keyboard.press("Enter")
        await asyncio.sleep(1.2)
        left = await leftover()
        if left:
            for sel in SEND_BTN_CANDIDATES:
                loc = page.locator(sel)
                try:
                    if await loc.count() > 0:
                        await loc.last.click(timeout=4000)
                        await asyncio.sleep(1.0)
                        break
                except Exception:
                    continue
            left = await leftover()
        if left:
            raise _SendError(f"没发出去（输入框还剩 {left[:30]}）", "type")
        return {"ok": True, "mode": "keepalive", "conv": conv_name}

    def send(self, text: str, conv_name: str | None = None, conv_uid: str | None = None, timeout: float = 90.0) -> dict:
        with self._send_lock:
            try:
                return self._submit(self._send(text, conv_name, conv_uid), timeout)
            except _SendError as e:
                if e.kind != "setup":
                    return {"ok": False, "error": str(e), "mode": "keepalive"}
                first = str(e)
            except Exception as e:
                first = f"{type(e).__name__}: {e}"
            # 只有「还没打字就失败」才允许重建重试，避免重复发送
            print(f"[send_text] setup 失败（{first}），重建浏览器后重试一次", flush=True)
            try:
                self._submit(self._reset(), 40)
            except Exception:
                pass
            try:
                return self._submit(self._send(text, conv_name, conv_uid), timeout)
            except Exception as e:
                return {"ok": False, "error": f"{first} / retry {type(e).__name__}: {e}", "mode": "keepalive"}

    def warm(self, timeout: float = 90.0) -> bool:
        """提前把常驻浏览器拉起来（进程启动时调一次，第一条消息就不用等 11 秒）。"""
        try:
            self._submit(self._open(), timeout)
            return True
        except Exception as e:
            print(f"[send_text] 预热失败: {type(e).__name__}: {e}", flush=True)
            return False

    async def _mark_read(self, conv_name: str | None = None):
        """复用常驻浏览器标已读：打开 chat 页面 → 点会话 → 等回执（5~7 秒）。"""
        page = await self._open()
        # 刷新 chat 页面确保消息列表是最新的
        await page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded")
        try:
            await page.wait_for_selector('[data-e2e="conversation-item"]', timeout=30000)
        except Exception:
            return {"ok": False, "error": "没有会话列表（可能登录失效）"}
        await asyncio.sleep(2)
        items = page.locator('[data-e2e="conversation-item"]')
        n = await items.count()
        if n == 0:
            return {"ok": False, "error": "没有会话列表（可能登录失效）"}
        target = items.first
        if conv_name:
            for i in range(n):
                t = (await items.nth(i).inner_text()) or ""
                if conv_name in t:
                    target = items.nth(i)
                    break
        try:
            await target.click(timeout=5000)
        except Exception as e:
            return {"ok": False, "error": f"点击会话失败: {e}"}
        await asyncio.sleep(3)  # 给前端时间发送已读回执
        return {"ok": True}

    def mark_read(self, conv_name: str | None = None, timeout: float = 30.0):
        """同步包装：复用常驻浏览器标已读（5~7 秒），不额外开浏览器。"""
        return self._submit(self._mark_read(conv_name), timeout)

    def close(self, timeout: float = 20.0) -> None:
        try:
            self._submit(self._reset(), timeout)
        except Exception:
            pass
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)


_KA = _KeepAlive()
_SEND_LOCK = threading.Lock()


def fetch_recent_sync(conv_name: str = "潮汐", resync: bool = True, away: str | None = None,
                      timeout: float = 120.0) -> list:
    """同步版：拉会话页最近 N 条消息（新的在前）。用于轮询兜底收消息。"""
    try:
        return _KA.fetch_recent(conv_name, resync, away, timeout)
    except Exception as e:  # noqa: BLE001
        print(f"[send_text] fetch_recent 失败: {type(e).__name__}: {e}", flush=True)
        return []


def warm_sender_async(timeout: float = 90.0) -> None:
    """后台线程里预热，不阻塞调用方。"""
    def _run():
        ok = _KA.warm(timeout)
        print(f"[send_text] 预热{'完成' if ok else '失败'}", flush=True)

    threading.Thread(target=_run, name="douyin-send-warm", daemon=True).start()


def send_text_sync(
    text: str,
    conv_name: str | None = None,
    headless: bool = True,
    keepalive: bool = True,
) -> dict:
    """同步版：默认走常驻浏览器（1~2 秒）；keepalive=False 时退回一次性开浏览器。"""
    if keepalive:
        return _KA.send(text, conv_name)
    with _SEND_LOCK:
        return asyncio.run(send_text_dm(text, headless=headless, conv_name=conv_name))


def send_text_to_uid(text: str, uid: str, headless: bool = True, keepalive: bool = True) -> dict:
    """按对方 uid 定位会话后发送（给陌生人用：没昵称时标题就是 uid）。"""
    if keepalive:
        return _KA.send(text, conv_uid=uid)
    # 一次性浏览器路径也走 uid 匹配
    with _SEND_LOCK:
        return asyncio.run(send_text_dm(text, headless=headless, conv_name=uid))



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--conv", default="潮汐")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    r = asyncio.run(send_text_dm(args.text, headless=not args.headed, conv_name=args.conv))
    print(json.dumps(r, ensure_ascii=False))


async def mark_read_async(conv_name: str | None = None) -> dict:
    """只打开会话页面并点击会话，触发抖音前端自动发送已读回执（不发消息）。"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        ctx = await browser.new_context(
            storage_state=str(STATE), viewport={"width": 1440, "height": 900}
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded")
            await page.wait_for_selector('[data-e2e="conversation-item"]', timeout=30000)
            await asyncio.sleep(2)
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
            await target.click(timeout=5000)
            await asyncio.sleep(3)  # 给前端时间发送已读回执
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            await browser.close()


if __name__ == "__main__":
    main()
