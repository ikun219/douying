# -*- coding: utf-8 -*-
"""常驻有头抖音浏览器：评论/点赞/收藏复用同一个有头窗口，静音、不关闭。
tool_runner 优先走这里，失败/未就绪时自动回退独立 launch 路径。
"""
import asyncio, ctypes, json, sys, time, threading, os, subprocess
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).parent
STATE = HERE / "storage_state.json"

# ---------- 单例状态 ----------
CDP_PORT = 9223  # send_text 等模块通过此端口共享同一浏览器进程，省内存

_loop = None
_browser = None
_context = None
_page = None
_ready = False
_lock = threading.Lock()
_launched = threading.Event()

# ---------- 事件循环线程 ----------
def _find_and_minimize():
    """找到所有抖音/Edge 窗口并最小化（不依赖进程 pid，因为 Edge 窗口属于 msedge.exe 非 python.exe）。"""
    user32 = ctypes.windll.user32
    SW_MINIMIZE = 6
    minimized = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(hwnd, _lparam):
        nonlocal minimized
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        if title.value and ("抖音" in title.value or "douyin" in title.value.lower()):
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            minimized += 1
            sys.stderr.write(f"[headed_browser] 已最小化窗口: {title.value}\n")
        return True

    user32.EnumWindows(enum_cb, 0)
    # 兜底：按类名/标题搜
    if minimized == 0:
        h = user32.FindWindowW(None, "抖音")
        if h:
            user32.ShowWindow(h, SW_MINIMIZE)
            sys.stderr.write("[headed_browser] 已最小化窗口（兜底 FindWindow）\n")
            minimized += 1
    if minimized == 0:
        sys.stderr.write("[headed_browser] _find_and_minimize: 未找到可最小化窗口\n")


def _ensure_loop():
    global _loop
    if _loop is not None:
        return
    _loop = asyncio.new_event_loop()
    t = threading.Thread(target=_loop.run_forever, daemon=True, name="headed-browser-loop")
    t.start()


def start():
    """在 reply_bot 启动时调用一次：后台拉起有头抖音窗口。"""
    _ensure_loop()
    asyncio.run_coroutine_threadsafe(_launch(), _loop)


def _cleanup_zombie_edges():
    """杀掉所有 msedge.exe 僵尸进程（每次启动前清理，确保窗口干净）。"""
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", "msedge.exe"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


async def _launch():
    global _browser, _context, _page, _ready
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        sys.stderr.write(f"[headed_browser] playwright 导入失败: {e}\n")
        _launched.set()
        return

    _cleanup_zombie_edges()
    time.sleep(2)  # 等系统释放互斥体/文件锁

    try:
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            channel="msedge",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--mute-audio",
                "--window-size=600,450",
                "--window-position=900,100",
                f"--remote-debugging-port={CDP_PORT}",
            ],
        )
        _context = await _browser.new_context(
            storage_state=str(STATE),
            viewport={"width": 600, "height": 450},
        )
        # 抹掉 navigator.webdriver，抖音靠这一行就能认出 Playwright
        await _context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            // 补刀：chrome.runtime 也给上，普通 Edge 有它
            window.chrome = window.chrome || {};
            window.chrome.runtime = window.chrome.runtime || {};
            // 强制静音：--mute-audio 在某些 Edge 版本不生效，JS 层面兜底
            (function muteAll() {
                document.querySelectorAll('video,audio').forEach(el => { el.muted = true; el.volume = 0; });
                const observer = new MutationObserver(() => {
                    document.querySelectorAll('video,audio').forEach(el => { el.muted = true; el.volume = 0; });
                });
                observer.observe(document.documentElement, { childList: true, subtree: true });
            })();
        """)
        _page = await _context.new_page()
        await _page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=35000)
        await asyncio.sleep(4)
        _ready = True
        sys.stderr.write("[headed_browser] 常驻有头浏览器已就绪\n")
        _patch_notification_cache()  # 加载已处理通知缓存
        _find_and_minimize()
    except Exception as e:
        sys.stderr.write(f"[headed_browser] 启动失败: {type(e).__name__}: {e}\n")
    finally:
        _launched.set()


def _ensure_ready(timeout=60):
    if _ready:
        return True
    if not _launched.wait(timeout):
        return False
    return _ready


def run(coro, timeout=60):
    """在常驻浏览器的事件循环中执行协程；返回结果或 None（未就绪/异常）。
    执行完毕后自动最小化窗口。"""
    if not _ensure_ready(5):
        sys.stderr.write("[headed_browser] 浏览器未就绪，跳过\n")
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, _loop)
        return fut.result(timeout=timeout)
    except Exception as e:
        sys.stderr.write(f"[headed_browser] run 异常: {type(e).__name__}: {e}\n")
        return None
    finally:
        # 调试期间：不自动最小化，方便盯着窗口看
        # _find_and_minimize()
        pass


# ---------- 评论 ----------
async def post_comment(aweme_id: str, text: str, reply_to_text: str = None, image_path: str = None, reply_cid: str = None):
    """在常驻窗口的视频页发评论；遇验证码返回 None 让调用方回退。
    Args:
        reply_to_text: 按文字匹配目标评论（一级回复）。
        reply_cid: 按 cid 精确定位目标评论（支持嵌套回复，优先级高于 reply_to_text）。
    """
    global _page
    if _page is None:
        return None

    page = _page
    # 跳转视频页
    try:
        await page.goto(
            f"https://www.douyin.com/video/{aweme_id}",
            wait_until="domcontentloaded",
            timeout=35000,
        )
    except Exception:
        return None
    await asyncio.sleep(6)

    # 评论区可能没展开，滚一下激活
    try:
        await page.mouse.wheel(0, 700)
        await asyncio.sleep(2)
    except Exception:
        pass

    # 若是回复评论：先点目标评论的"回复"按钮（支持文字匹配 + cid 精确定位，含嵌套回复）
    if reply_cid or reply_to_text:
        from post_comment import CLICK_REPLY_JS, CLICK_REPLY_BY_CID_JS
        if reply_cid:
            # nested reply：多滚几下展开嵌套回复区 + 尝试点"展开回复"
            for _ in range(4):
                await page.mouse.wheel(0, 500)
                await asyncio.sleep(0.8)
            try:
                await page.evaluate('''() => {
                    const btns = [...document.querySelectorAll('span,div')].filter(e =>
                        (e.textContent || '').includes('展开') && e.offsetWidth > 0);
                    for (const b of btns) { b.click(); }
                }''')
                await asyncio.sleep(1.5)
            except Exception:
                pass
            clicked = await page.evaluate(CLICK_REPLY_BY_CID_JS, reply_cid)
            if not clicked:
                sys.stderr.write(f"[headed_browser] 未找到评论 cid={reply_cid}\n")
                return None
        elif reply_to_text:
            clicked = await page.evaluate(CLICK_REPLY_JS, reply_to_text)
            if not clicked:
                sys.stderr.write(f"[headed_browser] 未找到匹配评论: {reply_to_text[:30]}\n")
                return None
        await asyncio.sleep(2)

    # 找评论框
    box = await _find_comment_box(page)
    if box is None:
        sys.stderr.write("[headed_browser] 找不到评论框\n")
        return None
    try:
        await box.click()
        await asyncio.sleep(0.5)
    except Exception:
        pass

    # 发图（如果有）
    if image_path and os.path.isfile(image_path):
        from post_comment import _upload_comment_image as _up
        ok, err = await _up(page, image_path)
        if not ok:
            sys.stderr.write(f"[headed_browser] 发图失败: {err}\n")
            return {"ok": False, "error": f"评论发图失败: {err}"}

    # 输入
    await page.keyboard.type(text, delay=50)
    await asyncio.sleep(1)

    # 抓 comment/publish 响应
    resp = {}
    async def on_resp(r):
        if "comment/publish" in r.url:
            try:
                resp["body"] = await r.text()
            except Exception:
                pass
    page.on("response", on_resp)

    # 发送
    try:
        submit = page.locator('[data-e2e="comment-submit"]')
        if await submit.count() > 0:
            await submit.first.click()
        else:
            await page.keyboard.press("Enter")
    except Exception:
        await page.keyboard.press("Enter")

    # 等确认（先给验证码一点时间弹出来，再检查；总共最多 12 分钟）
    confirm_deadline = time.time() + 120          # 头 2 分钟：等 API/DOM 确认
    verify_wait_start = None                      # 验证码开始等待的时间
    VERIFY_WAIT_TIMEOUT = 600                     # 验证码等待上限：10 分钟

    while time.time() < confirm_deadline + VERIFY_WAIT_TIMEOUT:
        await asyncio.sleep(1.5)

        # --- 验证码检查（优先级最高）---
        if await _has_verify(page):
            if verify_wait_start is None:
                verify_wait_start = time.time()
                sys.stderr.write(
                    f"[headed_browser] ⚠ 评论后弹验证码！请在浏览器窗口里手动过验证…\n"
                )
                # 同步写日志到 auto_browse 也看得到
                log_line = (
                    f"[headed_browser] 验证码弹出！请手动在桌面浏览器窗口里完成验证，"
                    f"脚本会在这等（最长 {VERIFY_WAIT_TIMEOUT}s）"
                )
                try:
                    with open(str(HERE / "auto_browse_run.log"), "a", encoding="utf-8") as lf:
                        lf.write(f"[{time.strftime('%H:%M:%S')}] {log_line}\n")
                except Exception:
                    pass
            # 等满超时 → 放弃
            if time.time() - verify_wait_start > VERIFY_WAIT_TIMEOUT:
                sys.stderr.write("[headed_browser] 验证码超时未手动解除\n")
                return {"ok": False, "risk": True, "risk_type": "sms_verify_timeout",
                        "error": "验证码弹出后 10 分钟未手动完成"}
            # 还在等 → 继续等（不做 API/DOM 检查，因为验证码拦着）
            continue

        # 验证码刚消失 → 等 3 秒让页面稳定，再确认
        if verify_wait_start is not None:
            await asyncio.sleep(3)
            verify_wait_start = None
            sys.stderr.write("[headed_browser] 验证码已消失，确认评论状态…\n")

        # --- API 确认 ---
        body = resp.get("body", "")
        if body:
            try:
                d = json.loads(body)
                cid = (d.get("comment") or {}).get("cid")
                if cid:
                    return {"ok": True, "cid": cid, "confirmed_by": "api"}
            except Exception:
                pass

        # --- DOM 确认 ---
        try:
            found = await page.evaluate(
                r"""(t) => [...document.querySelectorAll(
                    '.comment-item-info-wrap,.comment-content,.comment-item-content'
                )].some(e => e.textContent && e.textContent.includes(t))""",
                text[:20],
            )
            if found:
                return {"ok": True, "confirmed_by": "dom"}
        except Exception:
            pass

        # 超时（且没有验证码）
        if time.time() >= confirm_deadline and verify_wait_start is None:
            break

    # 完全没有确认
    sys.stderr.write("[headed_browser] 评论确认超时\n")
    return {"ok": False, "error": "评论确认超时（API 无响应且 DOM 里没找到）"}


async def _find_comment_box(page):
    sels = [
        ".comment-input-inner-container [contenteditable]",
        ".comment-input-inner-container",
        '[data-e2e="comment-input"] [contenteditable]',
        '[data-e2e="comment-input"]',
        ".public-DraftEditor-content",
        'div[contenteditable="true"]',
    ]
    for sel in sels:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            for i in range(min(n, 5)):
                c = loc.nth(i)
                if await c.is_visible():
                    return c
        except Exception:
            continue
    return None


async def _has_verify(page):
    try:
        return await page.evaluate(
            """() => {
                const b = document.body.innerText || "";
                return b.includes("验证码") || b.includes("安全验证") || b.includes("滑块验证");
            }"""
        )
    except Exception:
        # 页面关了/崩了 → 视为没有验证码（上层会处理 None）
        return False


# ---------- 点赞 / 收藏 ----------
async def like(aweme_id: str):
    return await _video_action(aweme_id, "video-player-digg", "like")


async def collect(aweme_id: str):
    return await _video_action(aweme_id, "video-player-collect", "collect")


async def _video_action(aweme_id: str, e2e: str, action: str):
    """在常驻窗口的视频页点操作按钮。"""
    global _page
    if _page is None:
        return None

    page = _page
    try:
        await page.goto(
            f"https://www.douyin.com/video/{aweme_id}",
            wait_until="domcontentloaded",
            timeout=35000,
        )
    except Exception:
        return None
    await asyncio.sleep(6)

    # 悬停让操作栏出现
    try:
        await page.mouse.move(700, 400)
        await asyncio.sleep(1)
    except Exception:
        pass

    try:
        r = await page.evaluate(
            """(sel) => {
                const el = document.querySelector(`[data-e2e="${sel}"]`);
                if (!el) return {ok: false, err: '按钮未找到'};
                el.click();
                return {ok: true, state: el.getAttribute('data-e2e-state')};
            }""",
            e2e,
        )
        await asyncio.sleep(3)
        return {"ok": r.get("ok", False), "action": action, "state": r.get("state")}
    except Exception as e:
        sys.stderr.write(f"[headed_browser] {action} 异常: {type(e).__name__}: {e}\n")
        return None


# ---------- 搜索 ----------
async def search(keyword: str, count: int = 12):
    """在常驻窗口的搜索页做页内 fetch（规避 verify_check），返回 [{aweme_id,desc,author,cover,url}]。"""
    global _page
    if _page is None:
        return None

    from search import _SEARCH_FETCH_JS_TMPL, _parse_search_json  # noqa: E402

    page = _page
    try:
        await page.goto(
            f"https://www.douyin.com/search/{keyword}",
            wait_until="domcontentloaded",
            timeout=35000,
        )
    except Exception:
        return None
    await asyncio.sleep(4)

    js = _SEARCH_FETCH_JS_TMPL.replace("KW", json.dumps(keyword))
    js = js.replace("count:'12'", f"count:{json.dumps(str(max(1, int(count))))}")
    try:
        raw = await page.evaluate(js)
    except Exception:
        return None
    if raw is None:
        return None
    return _parse_search_json(str(raw), keyword)


# ---------- 探测：评论区可用性（用于 risk_guard 替代搜索探测） ----------
PROBE_AWEME = "7543565577814871334"  # 三角洲行动 游戏日常，兜底视频


async def _pick_random_video(page):
    """从抖音首页推荐流抓几个 video id，随机选一个，兜底 PROBE_AWEME。"""
    import random as _random

    try:
        await page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(5)
        # 滚两下让推荐流加载更多
        for _ in range(3):
            await page.mouse.wheel(0, 1200)
            await asyncio.sleep(2)

        ids = await page.evaluate("""() => {
            const seen = new Set();
            const links = document.querySelectorAll('a[href*="/video/"]');
            for (const a of links) {
                const m = a.href.match(/video\\/(\\d+)/);
                if (m && m[1]) seen.add(m[1]);
                if (seen.size >= 20) break;
            }
            return [...seen];
        }""")
        if ids and len(ids) > 1:
            pick = _random.choice(ids)
            sys.stderr.write(f"[headed_browser] 探测随机视频 {pick}（共 {len(ids)} 个候选）\n")
            return pick
    except Exception as e:
        sys.stderr.write(f"[headed_browser] 随机视频抓取失败，兜底: {e}\n")
    return PROBE_AWEME


async def probe_comment():
    """风控探测：在常驻 context 中新建临时页，**真的发一条测试评论**看是否
    触发验证码。不影响主页面 auto_browse 操作。

    逻辑：随机选推荐流视频 → 输入测试评论 → 提交 → 等 API cid / DOM 确认 →
    评论成功 = 无风控；弹出验证码 = 风控。

    返回 {"login": bool, "reason": str, "detail": str} 或 None（未就绪）。
    """
    global _context
    if _context is None:
        return None

    page = None
    try:
        page = await _context.new_page()

        # 随机选视频
        aweme_id = await _pick_random_video(page)
        await page.goto(
            f"https://www.douyin.com/video/{aweme_id}",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(4)

        # 滚动激活评论区
        try:
            await page.mouse.wheel(0, 700)
            await asyncio.sleep(2)
        except Exception:
            pass

        # 找评论框
        box = await _find_comment_box(page)
        if box is None:
            return {"login": False,
                    "reason": "找不到评论框(疑似风控)",
                    "detail": ""}
        try:
            await box.click()
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # 输入测试评论
        await page.keyboard.type("ᗜ˰ᗜ", delay=50)
        await asyncio.sleep(1)

        # 抓 comment/publish 响应
        resp = {}

        async def on_resp(r):
            if "comment/publish" in r.url:
                try:
                    resp["body"] = await r.text()
                except Exception:
                    pass

        page.on("response", on_resp)

        # 发送
        try:
            submit = page.locator('[data-e2e="comment-submit"]')
            if await submit.count() > 0:
                await submit.first.click()
            else:
                await page.keyboard.press("Enter")
        except Exception:
            await page.keyboard.press("Enter")

        # 等确认（最多 15 秒）
        deadline = time.time() + 15
        while time.time() < deadline:
            await asyncio.sleep(1.5)

            # API cid → 评论成功，无风控
            body = resp.get("body", "")
            if body:
                try:
                    d = json.loads(body)
                    if (d.get("comment") or {}).get("cid"):
                        return {"login": True, "reason": "ok"}
                except Exception:
                    pass

            # DOM 确认 → 评论成功
            try:
                found = await page.evaluate(
                    """(t) => [...document.querySelectorAll(
                        '.comment-item-info-wrap,.comment-content,.comment-item-content'
                    )].some(e => e.textContent && e.textContent.includes(t))""",
                    "ᗜ˰ᗜ",
                )
                if found:
                    return {"login": True, "reason": "ok"}
            except Exception:
                pass

            # 弹验证码 → 风控
            if await _has_verify(page):
                return {"login": False,
                        "reason": "评论触发验证码(风控)",
                        "detail": ""}

        # 超时：评论未确认
        return {"login": False,
                "reason": "评论未确认(疑似风控-超时)",
                "detail": ""}
    except Exception as e:
        return {"login": False,
                "reason": f"评论探测异常: {type(e).__name__}: {e}",
                "detail": str(e)}
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


# ---------- 评论回复通知扫描 ----------
_processed_notifications: set = set()  # 已处理的 notification_id 集合
_NOTIFICATION_CACHE_FILE = HERE / "notification_cache.json"


def _load_notification_cache() -> set:
    try:
        if _NOTIFICATION_CACHE_FILE.is_file():
            data = json.loads(_NOTIFICATION_CACHE_FILE.read_text(encoding="utf-8"))
            return set(data.get("ids", []))
    except Exception:
        pass
    return set()


def _save_notification_cache():
    try:
        data = {"ids": list(_processed_notifications), "updated": time.time()}
        _NOTIFICATION_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _patch_notification_cache():
    """启动时加载缓存。"""
    global _processed_notifications
    _processed_notifications = _load_notification_cache()


async def scan_reply_notifications() -> list:
    """扫描抖音通知页的评论回复通知（有人回复了汐缘的评论）。
    返回未处理的新回复列表。
    每个回复: {
        "nid": str,          # 通知唯一 ID（用于去重）
        "replier_name": str, # 回复者的抖音昵称
        "reply_text": str,   # 回复内容（可能被截断）
        "aweme_id": str,     # 视频 ID
        "comment_url": str,  # 评论直链（可从 URL 提取 cid）
        "timestamp": str,    # 通知时间文本
    }
    """
    global _page, _processed_notifications
    if _page is None:
        return []

    page = _page
    results = []

    try:
        # 方案：通过 douyin API 拉通知（/aweme/v1/web/notice/ 需要 a_bogus 签名，
        # 但走浏览器 page.evaluate() 里的 fetch 可复用 cookie + referer 绕过签名限制）。
        # 先确保在 douyin.com 域下，然后 fetch 评论通知 API。
        if not page.url.startswith("https://www.douyin.com"):
            await page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

        sys.stderr.write(f"[headed_browser] 当前页 URL={page.url[:80]}\n")

        # 通过浏览器内 fetch 请求通知 API（notice_group=2 评论）
        raw_json = await page.evaluate('''async () => {
            try {
                const resp = await fetch(
                    "/aweme/v1/web/notice/?device_platform=webapp&aid=6383&channel=channel_pc_web"
                    + "&is_new_notice=1&is_mark_read=1&notice_group=2&count=20"
                    + "&min_time=0&max_time=0&update_version_code=170400&pc_client_type=1"
                    + "&version_code=170400&version_name=17.4.0&cookie_enabled=true"
                    + "&screen_width=1707&screen_height=960&browser_language=zh-CN"
                    + "&browser_platform=Win32&browser_name=Edge&browser_version=125.0.0.0"
                    + "&browser_online=true&engine_name=Blink&engine_version=125.0.0.0"
                    + "&os_name=Windows&os_version=10",
                    { headers: { "Referer": "https://www.douyin.com/?recommend=1" } }
                );
                return await resp.text();
            } catch(e) {
                return JSON.stringify({_error: e.message || String(e)});
            }
        }''')
        
        try:
            data = json.loads(raw_json)
        except Exception:
            sys.stderr.write(f"[headed_browser] 通知API返回非JSON: {raw_json[:200]}\n")
            return results

        if "_error" in data:
            sys.stderr.write(f"[headed_browser] 通知API fetch失败: {data['_error']}\n")
            return results

        # API 返回结构: { "notice_list_v2": [...] }（旧版是 "notice_list"）
        notice_list = data.get("notice_list_v2", data.get("notice_list", []))
        sys.stderr.write(f"[headed_browser] 通知API返回 {len(notice_list)} 条通知（notice_group=2 评论）\n")

        items = []
        for notice in notice_list:
            ntype = notice.get("type", "")
            # type=31 评论通知（含回复你评论/评论了你视频）
            comment_data = notice.get("comment", {}) or {}
            inner_comment = comment_data.get("comment", {}) or {}

            nick = inner_comment.get("user", {}).get("nickname", "")
            text = inner_comment.get("text", "")
            cid = str(inner_comment.get("cid", ""))
            aweme_id = str(comment_data.get("aweme_id", "") or inner_comment.get("aweme_id", ""))

            # 提取父评论 cid（用户回复的是哪条评论——我们的评论）
            parent_cid = str((comment_data.get("reply_comment") or {}).get("cid", ""))

            # 如果 inner_comment 没 text，fallback 到 reply_comment
            if not text:
                rc = comment_data.get("reply_comment", {}) or {}
                if rc:
                    text = rc.get("text", "")
                    if not nick:
                        nick = rc.get("user", {}).get("nickname", "")
                    if not cid:
                        cid = str(rc.get("cid", ""))
                    if not aweme_id:
                        aweme_id = str(rc.get("aweme_id", ""))
                    if not parent_cid:
                        parent_cid = str(rc.get("cid", ""))

            # 只抓有实际文字内容的回复通知
            if not text:
                continue

            # aweme_id 兜底：顶层 notice 也可能直接带 aweme_id
            if not aweme_id:
                aweme_id = str(notice.get("aweme_id", ""))
            nid = str(notice.get("id", ""))
            nid64 = nid or (nick + text)[:40]
            items.append({
                "nid": nid64,
                "replier_name": nick[:40] if nick else "网友",
                "reply_text": text[:200] if text else "(通知内容为空)",
                "aweme_id": aweme_id,
                "cid": cid,
                "parent_cid": parent_cid,
                "comment_url": "",
                "timestamp": notice.get("create_time", ""),
            })

        sys.stderr.write(f"[headed_browser] 过滤后 {len(items)} 条评论回复\n")

        # 诊断：如果 API 有返回但过滤后为空，dump 第一条原始数据帮助 debug
        if len(items) == 0 and len(notice_list) > 0:
            first = notice_list[0]
            sys.stderr.write(f"[headed_browser] 过滤零命中！第一条通知 keys={list(first.keys())[:15]} type={first.get('type')} content={str(first.get('content',''))[:60]} title={str(first.get('title',''))[:60]} from_user_keys={list(first.get('from_user',{}).keys())[:10] if first.get('from_user') else 'None'}\n")
            # dump full first notice (truncated)
            first_str = json.dumps(first, ensure_ascii=False, default=str)
            sys.stderr.write(f"[headed_browser] 第一条通知完整(截2000字): {first_str[:2000]}\n")

        if items:
            first_item = items[0]
            sys.stderr.write(
                f"[headed_browser] 通知页抓到 {len(items)} 条评论回复通知，"
                f"第一条: aweme_id={first_item.get('aweme_id','')[:20]} "
                f"cid={first_item.get('cid','')[:20]} "
                f"nick={first_item.get('replier_name','')[:10]} "
                f"text={first_item.get('reply_text','')[:30]}\n"
            )
        else:
            sys.stderr.write("[headed_browser] 通知页未抓到评论回复（DOM 结构可能变了，body 已 dump 到上方）\n")

        # 过滤已处理
        new_items = [it for it in items if it["nid"] not in _processed_notifications]
        if new_items:
            sys.stderr.write(
                f"[headed_browser] 其中 {len(new_items)} 条是新的\n"
            )
            results = new_items
    except Exception as e:
        sys.stderr.write(f"[headed_browser] 扫描通知失败: {type(e).__name__}: {e}\n")

    return results


def mark_notification_processed(nid: str):
    """标记某条通知已处理。"""
    _processed_notifications.add(nid)
    _save_notification_cache()


# 从页面 DOM 提取每个"回复"按钮所属的评论容器信息（作者+文本+按钮坐标）
# 注意：抖音会把评论文本截断（innerText 只见 作者/…/前几字/日期/回复），
# 所以唯一可靠的定位字段是「作者名」(replier_name) —— 它是评论块内文本的首行。
COMMENT_BTN_DUMP_JS = r'''() => {
  const out = [];
  const seen = new Set();
  // 先收集所有「回复」按钮可点击元素
  const btns = [];
  for (const el of document.querySelectorAll('span, button')) {
    if (el.innerText && el.innerText.trim() === '回复' && (el.offsetWidth||0) > 5 && (el.offsetHeight||0) > 5) {
      if (seen.has(el)) continue; seen.add(el);
      btns.push(el);
    }
  }
  for (const el of btns) {
    // 最近的一个 data-e2e="comment-item" 祖先 = 该按钮所属的评论块
    let item = el.parentElement;
    for (let lv=0; lv<8 && item; lv++) {
      if (item.getAttribute && item.getAttribute('data-e2e') === 'comment-item') break;
      item = item.parentElement;
    }
    const block = (item ? item.innerText : '') || '';
    const lines = block.split('\n').map(x=>x.trim()).filter(Boolean);
    const author = (lines[0] || '').slice(0, 40);
    const r = el.getBoundingClientRect();
    // 父级评论块（用于决定该评论是顶层还是嵌套回复）
    let wrap = el.parentElement;
    for (let lv=0; lv<6 && wrap; lv++) {
      if (wrap.getAttribute && (wrap.getAttribute('data-e2e') === 'comment-item' || wrap.className.toString().includes('comment-item-info-wrap'))) break;
      wrap = wrap.parentElement;
    }
    out.push({
      x: Math.round(r.x + r.width/2),
      y: Math.round(r.y + r.height/2),
      author: author,
      block: block.replace(/\n/g, ' | ').slice(0, 160),
    });
  }
  return out;
}'''


async def reply_to_notification(nid: str, aweme_id: str, reply_cid: str, text: str,
                                       reply_content: str = "", parent_cid: str = "",
                                       replier_name: str = "") -> dict:
    """针对通知里的评论发嵌套回复。委托给 post_comment.post_comment() —— 独立的有头浏览器 + storage_state。

    为什么必须走独立浏览器而非本模块的 CDP 常驻单例：实测（probe_comment_dom）常驻窗的视频页
    DOM 里没有任何评论输入框/composer（无 data-e2e=comment-input、无 contenteditable，只有顶部搜索框），
    无论点不点"回复"都不会弹出输入框，合成打字全部落空进 body。而 post_comment 用自己 launch 的
    全新有头浏览器（storage_state.json 登录态）能正常渲染输出框并成功发送，实测顶层评论与
    --reply-cid 嵌套回复均 confirmed_by="api" 成功。
    """
    # 懒加载 post_comment（其内部自身维护独立浏览器）
    from post_comment import post_comment as _post_comment

    # 定位目标评论：优先用评论文字（reply_content，走 CLICK_REPLY_JS 子串匹配）。
    # 实测目标评论的 cid 完全不在渲染 DOM（无 data-cid / 不在 __reactProps /
    # 不在 fiber memoizedProps），CLICK_REPLY_BY_CID_JS 必然找不到目标，
    # 必须改按回复目标的评论文字匹配（用 CLICK_REPLY_JS，取较短片段更稳）。
    try:
        target_text = (reply_content or "").strip()
        if target_text:
            # CLICK_REPLY_JS 用 includes 子串匹配，取前 20 字作为匹配片段
            test_frag = target_text[:20]
            result = await _post_comment(
                aweme_id, text, reply_to_text=test_frag, headless=False, reply_cid=None
            )
        else:
            # 无评论文字可匹配时兜底走 cid（虽已知大概率找不到目标，但保留原行为）
            result = await _post_comment(
                aweme_id, text, reply_to_text=None, headless=False, reply_cid=reply_cid or None
            )
    except Exception as e:
        mark_notification_processed(nid)
        sys.stderr.write(f"[headed_browser] [{nid[:8]}] post_comment 调用异常: {e}\n")
        return {"ok": False, "error": f"post_comment 调用异常: {e}"}

    if result.get("ok"):
        sys.stderr.write(
            f"[headed_browser] [{nid[:8]}] 回复成功 cid={result.get('cid')} "
            f"confirmed_by={result.get('confirmed_by')}\n"
        )
        mark_notification_processed(nid)
        return {"ok": True, "cid": result.get("cid"), "confirmed_by": result.get("confirmed_by")}

    mark_notification_processed(nid)
    err = result.get("error") or "post_comment 返回失败"
    sys.stderr.write(f"[headed_browser] [{nid[:8]}] 回复失败: {err}\n")
    return {"ok": False, "error": err}


# ---------- CLI ----------
if __name__ == "__main__":
    print("usage: python headed_browser.py  (no direct CLI; started by reply_bot)")