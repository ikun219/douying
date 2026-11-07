# -*- coding: utf-8 -*-
"""带人工介入的抖音评论：遇到短信/滑块验证不关闭窗口，把窗口顶到最前，等主人手动过。
用法: python hold_comment.py <aweme_id> <评论内容> [--wait 300]
"""
import asyncio, json, io, sys, time, ctypes
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
from playwright.async_api import async_playwright

HAS_VERIFY_JS = r'''() => {
    const body = document.body.innerText || "";
    return body.includes("验证码") || body.includes("安全验证") || body.includes("滑块") || body.includes("拖动下方滑块");
}'''


def focus_window():
    """把标题含抖音的浏览器窗口提到前台"""
    try:
        user32 = ctypes.windll.user32
        found = []

        def cb(hwnd, lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n == 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value
                if ('抖音' in t) or ('douyin' in t.lower()):
                    found.append(hwnd)
            except Exception:
                pass
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        for h in found:
            user32.ShowWindow(h, 9)
            user32.SetForegroundWindow(h)
            user32.BringWindowToTop(h)
        return len(found)
    except Exception:
        return 0


async def has_verify(page):
    try:
        return bool(await page.evaluate(HAS_VERIFY_JS))
    except Exception:
        return False


async def find_box(page, timeout=25):
    sels = ['.comment-input-inner-container [contenteditable]',
            '.comment-input-inner-container',
            '[data-e2e="comment-input"] [contenteditable]',
            '[data-e2e="comment-input"]',
            '.public-DraftEditor-content',
            'div[contenteditable="true"]']

    async def try_find():
        for sel in sels:
            try:
                loc = page.locator(sel)
                n = await loc.count()
            except Exception:
                continue
            for i in range(min(n, 5)):
                c = loc.nth(i)
                try:
                    if await c.is_visible():
                        return c
                except Exception:
                    continue
        return None

    box = await try_find()
    if box is not None:
        return box
    try:
        await page.mouse.wheel(0, 700)
        await asyncio.sleep(2)
    except Exception:
        pass
    box = await try_find()
    if box is not None:
        return box
    for icon in ('[data-e2e="feed-comment-icon"]', '[data-e2e="comment-icon"]',
                 '[data-e2e="video-player-comment"]'):
        try:
            await page.locator(icon).first.click(timeout=3000)
            await asyncio.sleep(2.5)
            box = await try_find()
            if box is not None:
                return box
        except Exception:
            continue
    deadline = time.time() + timeout
    while time.time() < deadline:
        box = await try_find()
        if box is not None:
            return box
        await asyncio.sleep(1.0)
    return None


async def main():
    argv = sys.argv[1:]
    wait_sec = 300
    if '--wait' in argv:
        i = argv.index('--wait')
        wait_sec = int(argv[i + 1]); del argv[i:i + 2]
    aweme_id, text = argv[0], argv[1]

    async with async_playwright() as p:
        b = await p.chromium.launch(channel='msedge', headless=False,
                                    args=['--disable-blink-features=AutomationControlled',
                                          '--start-maximized'])
        ctx = await b.new_context(storage_state='storage_state.json', viewport=None)
        page = await ctx.new_page()
        await page.bring_to_front()
        resp = {}

        async def on_resp(r):
            if 'comment/publish' in r.url:
                try:
                    resp['body'] = await r.text()
                except Exception:
                    pass
        page.on('response', on_resp)

        print('打开视频页…', flush=True)
        await page.goto(f'https://www.douyin.com/video/{aweme_id}', wait_until='domcontentloaded')
        await asyncio.sleep(6)
        await page.bring_to_front()
        print('窗口标题：', await page.title(), flush=True)
        print('窗口数（含抖音）：', focus_window(), flush=True)

        if await has_verify(page):
            print('!! 一进页面就弹验证，窗口已顶到最前，等你处理…', flush=True)
            await wait_verify(page, wait_sec)

        # 滚一下叫出评论区
        try:
            await page.mouse.wheel(0, 700)
            await asyncio.sleep(2)
        except Exception:
            pass
        box = await find_box(page, timeout=25)
        if box is None:
            print('找不到评论框，放弃', flush=True)
            await asyncio.sleep(2)
            await b.close()
            return

        await box.click()
        await asyncio.sleep(0.5)
        await page.keyboard.type(text, delay=60)
        await asyncio.sleep(1)
        print('已输入：' + text, flush=True)

        for attempt in range(3):
            clicked = await page.evaluate('''() => {
                const el = document.querySelector('[data-e2e="comment-submit"],.comment-input-container .submit,.comment-input-submit,button[class*="submit"],div[class*="submit"]');
                if (el && el.offsetParent !== null) { el.click(); return true; }
                return false;
            }''')
            if not clicked:
                await page.keyboard.press('Enter')
            print(f'第 {attempt+1} 次发送，等结果…', flush=True)

            ok = False
            deadline = time.time() + 40
            while time.time() < deadline:
                await asyncio.sleep(1.5)
                if resp.get('body'):
                    try:
                        d = json.loads(resp['body'])
                        cid = (d.get('comment') or {}).get('cid')
                        if cid:
                            print('SUCCESS 评论已发出 cid=' + str(cid), flush=True)
                            ok = True
                            break
                    except Exception:
                        pass
                try:
                    found = await page.evaluate(
                        r'''(t) => [...document.querySelectorAll('.comment-item-info-wrap,.comment-content,.comment-item-content')].some(e => e.textContent.includes(t))''',
                        text[:20])
                    if found:
                        print('SUCCESS DOM 里已经看到自己的评论了', flush=True)
                        ok = True
                        break
                except Exception:
                    pass
                if await has_verify(page):
                    print('!! 弹验证了，窗口已顶到最前，我不点取消，等你过来操作（最多等 ' + str(wait_sec) + ' 秒）…', flush=True)
                    focus_window()
                    passed = await wait_verify(page, wait_sec)
                    if not passed:
                        print('TIMEOUT 等你等到超时，窗口留着不关', flush=True)
                        await asyncio.sleep(3600)
                    print('验证没了，重新点发送', flush=True)
                    break
            if ok:
                break
            if attempt == 2:
                print('FAILED 3 次都没成功', flush=True)
                try:
                    await page.screenshot(path='tmp/hold_comment_fail.png')
                except Exception:
                    pass
                await asyncio.sleep(20)
        await asyncio.sleep(2)
        await b.close()
        print('DONE', flush=True)


async def wait_verify(page, wait_sec):
    t0 = time.time()
    while time.time() - t0 < wait_sec:
        await asyncio.sleep(3)
        if not await has_verify(page):
            print('验证似乎过了（%.0fs）' % (time.time() - t0), flush=True)
            return True
        left = int(wait_sec - (time.time() - t0))
        if left % 30 < 3:
            print('还在等你，剩 %ds' % left, flush=True)
    return False


if __name__ == '__main__':
    asyncio.run(main())
