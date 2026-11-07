# -*- coding: utf-8 -*-
"""汐缘 抖音风控一键解除（扫码登录 → 评论手机验证码 → 搜索滑块）

当回复机器人被风控拦住时，运行本脚本：
1. 扫码登录（login_qr 流程，浏览器有头）
2. 登录后自动跳到视频页发评论，触发手机验证码，填完即可
3. 评论验证通过后自动跳到搜索页，请手动过滑块（在窗口里拖滑块）
4. 全部解除后自动退出

每个阶段都在同一浏览器窗口里完成，不用切换窗口。

用法：
    .venv/Scripts/python.exe solve_risk_chain.py [--kw 美食] [--conv 潮汐]
"""

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

# ---- 控制台 UTF-8 ----
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---- 路径与环境 ----
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

os.makedirs(str(HERE / "tmp"), exist_ok=True)
# 强制覆盖 TMP/TEMP/TMPDIR，否则 Playwright 在 AppData\Local\Temp 里 mkdtemp 会 EPERM
os.environ["TMP"] = str(HERE / "tmp")
os.environ["TEMP"] = str(HERE / "tmp")
os.environ["TMPDIR"] = str(HERE / "tmp")
os.environ["PYTHONUTF8"] = "1"

from playwright.async_api import async_playwright

# ---- 常量 ----
STATE = HERE / "storage_state.json"
ENV_FILE = HERE / ".env"
KEYS_FILE = HERE / "auth_keys.json"
WEBPROTECT_FILE = HERE / "auth_webprotect.json"
QR_FILE = HERE / "qr_login.png"

LOGIN_URL = "https://www.douyin.com/login_page?service=https%3A%2F%2Fwww.douyin.com"
TRIGGER_AWEME = "7696406922979229887"
CHAT_URL = "https://www.douyin.com/chat"

BANNER = r"""
╔══════════════════════════════════════════════════════════════╗
║           汐缘 抖音风控一键解除                              ║
║                                                              ║
║  阶段 1：扫码登录（浏览器窗口弹出二维码）                      ║
║  阶段 2：评论页发评论触发手机验证码                            ║
║  阶段 3：搜索页过滑块验证                                     ║
║                                                              ║
║  所有操作在同一浏览器窗口完成，最后自动退出。                  ║
╚══════════════════════════════════════════════════════════════╝
"""

SEARCH_FETCH_JS = r"""async () => {
  try {
    const r = await fetch('/aweme/v1/web/general/search/single/?'
      + new URLSearchParams({
        search_channel:'aweme_general', enable_history:'1',
        keyword: KW, search_source:'tab_search', query_correct_type:'1',
        is_filter_search:'0', offset:'0', count:'12',
        need_filter_settings:'1', list_type:'multi',
        device_platform:'webapp', aid:'6383', channel:'channel_pc_web',
        version_code:'190600', version_name:'19.6.0', pc_client_type:'1',
      }), {headers:{'Accept':'application/json, text/plain, */*'}});
    const t = await r.text();
    return t;
  } catch(e){ return JSON.stringify({__err:String(e)}); }
}"""


def _log(*a):
    print(*a, flush=True)


# ============================================================
# 阶段 1：扫码登录
# ============================================================
async def stage1_login(page, ctx, is_cdp: bool = False) -> bool:
    """扫码登录并保存所有 cookie / keys / storage_state。返回是否成功。"""
    _log("\n" + "=" * 60)
    _log("阶段 1/3：扫码登录")
    _log("=" * 60)

    # CDP 模式：先检查是否已经在原生 Edge 里登录了
    if is_cdp:
        _log("[1a] CDP 模式：检查是否已登录 ...")
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
        url = page.url
        if "sessionid" in cookies and "login" not in url:
            _log("[OK] 原生 Edge 已登录，跳过扫码。正在保存凭据...")
            # 提取 keys + web_protect
            await page.goto(f"https://www.douyin.com/video/{TRIGGER_AWEME}", wait_until="domcontentloaded")
            await asyncio.sleep(6)
            keys_str = await page.evaluate(
                'localStorage["security-sdk/s_sdk_crypt_sdk"] || null'
            )
            web_protect_str = await page.evaluate(
                'localStorage["security-sdk/s_sdk_sign_data_key/web_protect"] || null'
            )
            cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            await ctx.storage_state(path=str(STATE))
            with open(str(ENV_FILE), "w", encoding="utf-8") as f:
                f.write(f"DY_COOKIES={cookie_str}\n")
            with open(str(KEYS_FILE), "w", encoding="utf-8") as f:
                json.dump({"keys_str": keys_str}, f, ensure_ascii=False, indent=2)
            with open(str(WEBPROTECT_FILE), "w", encoding="utf-8") as f:
                json.dump({"web_protect": web_protect_str}, f, ensure_ascii=False, indent=2)
            with open(str(HERE / "auth_cookies.json"), "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            _log(f"[OK] 凭据已导出: {STATE.name} / {ENV_FILE.name} / {KEYS_FILE.name}")
            return True
        _log("[1a] 未登录，继续扫码流程...")

    # 1b. 登录页
    _log("[1b] 打开登录页 ...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    # 1c. 等二维码出来
    qr = None
    for _ in range(25):
        qr = await page.evaluate(
            """() => {
                const imgs = [...document.querySelectorAll('img')];
                const q = imgs.find(i => (i.src || '').startsWith('data:image/png;base64,'));
                return q ? q.src : null;
            }"""
        )
        if qr:
            break
        await asyncio.sleep(1)

    if not qr:
        _log("[FAIL] 二维码没出来，url=", page.url)
        return False

    open(str(QR_FILE), "wb").write(base64.b64decode(qr.split(",", 1)[1]))
    _log(f"[OK] 二维码已保存到 {QR_FILE}，请扫码！")

    # 1d. 轮询登录成功（最多 5 分钟，定期刷新二维码）
    deadline = time.time() + 300
    last_refresh = time.time()
    logged_in = False

    while time.time() < deadline:
        cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
        url = page.url
        if "sessionid" in cookies and "login_page" not in url:
            logged_in = True
            break
        if time.time() - last_refresh > 20:
            qr = await page.evaluate(
                """() => {
                    const imgs = [...document.querySelectorAll('img')];
                    const q = imgs.find(i => (i.src || '').startsWith('data:image/png;base64,'));
                    return q ? q.src : null;
                }"""
            )
            if qr:
                open(str(QR_FILE), "wb").write(base64.b64decode(qr.split(",", 1)[1]))
                last_refresh = time.time()
                _log("[QR] 已刷新")
            else:
                # 二维码没了但登录还没完成 → 可能已经登上了，再检查一次
                cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
                url = page.url
                if "sessionid" in cookies and "login_page" not in url:
                    logged_in = True
                    break
        await asyncio.sleep(3)

    if not logged_in:
        _log("[FAIL] 登录超时")
        return False

    _log("[OK] 扫码登录成功！")

    # 1e. 回主页等 SDK 初始化
    _log("[1e] 回主页等 SDK 初始化 ...")
    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
    await asyncio.sleep(6)

    # 1f. 访问视频页触发 web_protect
    _log("[1f] 触发 web_protect ...")
    await page.goto(f"https://www.douyin.com/video/{TRIGGER_AWEME}", wait_until="domcontentloaded")
    await asyncio.sleep(8)

    # 1g. 提取 keys + web_protect + cookies
    _log("[1g] 提取凭据 ...")
    keys_str = None
    web_protect_str = None
    for _ in range(10):
        keys_str = await page.evaluate(
            'localStorage["security-sdk/s_sdk_crypt_sdk"] || null'
        )
        web_protect_str = await page.evaluate(
            'localStorage["security-sdk/s_sdk_sign_data_key/web_protect"] || null'
        )
        if web_protect_str:
            break
        await asyncio.sleep(2)

    cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # 保存 storage_state.json
    await ctx.storage_state(path=str(STATE))

    # 写 .env
    with open(str(ENV_FILE), "w", encoding="utf-8") as f:
        f.write(f"DY_COOKIES={cookie_str}\n")
    with open(str(KEYS_FILE), "w", encoding="utf-8") as f:
        json.dump({"keys_str": keys_str}, f, ensure_ascii=False, indent=2)
    with open(str(WEBPROTECT_FILE), "w", encoding="utf-8") as f:
        json.dump({"web_protect": web_protect_str}, f, ensure_ascii=False, indent=2)
    with open(str(HERE / "auth_cookies.json"), "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)

    _log(f"[OK] 凭据已保存: {STATE.name} / {ENV_FILE.name} / {KEYS_FILE.name}")
    return True


# ============================================================
# 阶段 2：评论手机验证码
# ============================================================
async def stage2_comment_verify(page, kw: str, timeout: int) -> bool:
    """从首页推荐流拿视频发评论，触发手机验证码。不依赖搜索接口（最严风控时搜索直接返回空/繁忙）。"""
    _log("\n" + "=" * 60)
    _log("阶段 2/3：评论手机验证码")
    _log("=" * 60)

    from post_comment import post_comment as _post_comment

    aweme_id = None
    title = ""

    # 走首页推荐流拿视频 id：导航到 douyin.com，滚几屏加载推荐视频，从 DOM 抓卡片
    _log("[2a] 从首页推荐流抓视频...")
    try:
        await page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=35000)
        await asyncio.sleep(5)
        # 滚几屏让推荐视频加载
        for i in range(3):
            await page.mouse.wheel(0, 1200)
            await asyncio.sleep(2)
        ids = await page.evaluate("""() => {
            const ids = [];
            document.querySelectorAll('a[href*="/video/"]').forEach(a => {
                const m = a.href.match(/\\/video\\/(\\d+)/);
                if (m && m[1]) ids.push(m[1]);
            });
            // 也扫 data-e2e 属性（有的版本用这个）
            document.querySelectorAll('[data-e2e*="video"]').forEach(el => {
                for (const k of ['data-id','data-aweme-id','data-item-id']) {
                    const v = el.getAttribute(k);
                    if (v && /^\\d+$/.test(v)) ids.push(v);
                }
            });
            return [...new Set(ids)];
        }""")
        _log(f"[2a] 首页抓到 {len(ids)} 个视频 id")
        if ids:
            import random as _random
            aweme_id = _random.choice(ids)
            title = f"(首页推荐 #{aweme_id[:6]}...)"
    except Exception as e:
        _log(f"[2a] 首页抓视频失败: {type(e).__name__}: {e}")

    if not aweme_id:
        # 兜底：首页抓不到就用固定视频
        aweme_id = TRIGGER_AWEME
        title = "(兜底视频)"

    test_comment = "ᗜ˰ᗜ"

    _log(f"[2a] 选中视频 {aweme_id} ({title}) 发评论: {test_comment}")
    _log("     如果弹出手机验证码，请在手机上查看验证码并填入浏览器窗口。")
    _log("     脚本会自动探测评论是否发送成功。")

    # 确保当前浏览器的登录态已写入 storage_state.json（post_comment 会读它）
    await page.context.storage_state(path=str(STATE))

    result = await _post_comment(aweme_id, test_comment, headless=False)
    _log(f"[2b] post_comment 返回: {json.dumps(result, ensure_ascii=False)[:300]}")

    # 关键：post_comment 可能 API 提前返回 cid 就报 ok=True，
    # 但验证码可能还没弹出或弹在另一帧。这里补一个验证码探测循环。
    _captcha_seen = False
    _captcha_deadline = time.time() + max(timeout, 600)  # 至少等 10 分钟
    _poll_interval = 3  # 每 3 秒检查一次
    _log("[2b] 等待验证码弹出（如果没弹说明评论验证码已经不需要清了）...")
    while time.time() < _captcha_deadline:
        try:
            body_text = await page.evaluate("() => document.body.innerText || ''")
        except Exception:
            await asyncio.sleep(_poll_interval)
            continue
        has_captcha = any(kw in body_text for kw in ("验证码", "安全验证", "滑块验证"))
        if has_captcha:
            if not _captcha_seen:
                _captcha_seen = True
                _log("[2b] ⚠ 检测到验证码！请在浏览器窗口中手动完成（拖动滑块/输入短信验证码）。")
                _log("     脚本会持续探测，完成后自动继续...")
            await asyncio.sleep(_poll_interval)
            continue
        # 没有验证码
        if _captcha_seen:
            _log("[2b] 验证码已消失（可能是你手动完成了），继续检查 5 秒确认...")
            await asyncio.sleep(5)
            # 二次确认
            try:
                body2 = await page.evaluate("() => document.body.innerText || ''")
            except Exception:
                body2 = ""
            if not any(kw in body2 for kw in ("验证码", "安全验证", "滑块验证")):
                _log("[OK] 评论验证码已清除！")
                return True
            else:
                _log("[2b] 验证码还在，继续等...")
                _captcha_seen = True  # 保持状态
                continue
        # 没弹过验证码：等一段时间看会不会弹
        if not _captcha_seen and time.time() > _captcha_deadline - 30:
            # 快超时了还没弹，说明评论验证码不需要清
            _log("[OK] 评论验证码未弹出，说明当前无需清除（或已通过）。")
            return True
        await asyncio.sleep(_poll_interval)

    if _captcha_seen:
        _log("[FAIL] 评论验证码超时：验证码弹出后未在时限内完成。")
        return False
    else:
        _log("[OK] 等待超时但评论验证码始终未弹出，视为无需清除。")
        return True


# ============================================================
# 阶段 3：搜索过滑块
# ============================================================
async def stage3_search_verify(page, kw: str, timeout: int) -> bool:
    """打开搜索页，等用户手动过滑块，探测到搜索恢复后返回 True。"""
    _log("\n" + "=" * 60)
    _log("阶段 3/3：搜索验证")
    _log("=" * 60)

    search_url = f"https://www.douyin.com/search/{kw}?type=general"
    _log(f"[3a] 打开搜索页: {search_url}")
    _log("     如果弹出滑块验证，请在浏览器窗口里拖动滑块完成验证。")
    _log("     脚本会自动探测搜索是否恢复。")

    await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
    await asyncio.sleep(5)

    js = SEARCH_FETCH_JS.replace("KW", json.dumps(kw))

    def _parse(raw: str):
        try:
            obj = json.loads(raw)
        except Exception:
            return {"verify": True, "count": 0, "first": ""}
        low = raw.lower()
        if isinstance(obj, dict) and obj.get("__err"):
            return {"verify": True, "count": 0, "first": str(obj.get("__err", ""))[:40]}
        data = obj.get("data") if isinstance(obj, dict) else []
        cnt = len(data) if isinstance(data, list) else 0
        first = ""
        if cnt > 0 and isinstance(data[0], dict):
            aw = data[0].get("aweme_info") or {}
            first = (aw.get("desc") or "")[:40]
        return {"verify": "verify_check" in low, "count": cnt, "first": first}

    deadline = time.time() + timeout
    cleared = False
    n = 0

    while time.time() < deadline:
        n += 1
        remain = int(deadline - time.time())
        try:
            raw = await page.evaluate(js)
            r = _parse(str(raw))
            _log(f"  [{n}] verify={r['verify']} count={r['count']} first={r['first']!r} 剩余{remain}s")
            if (not r["verify"]) and r["count"] > 0:
                cleared = True
                break
            if not cleared:
                _log(f"    仍被风控拦住，请在窗口里完成滑块验证")
        except Exception as e:
            _log(f"  [{n}] 探测异常: {e} 剩余{remain}s")

        await asyncio.sleep(20)

    if cleared:
        _log("[OK] 搜索风控已解除！")
        return True
    else:
        _log("[FAIL] 搜索风控超时未解除")
        return False


# ============================================================
# 主流程
# ============================================================

async def _setup_browser(p, cdp_port: int | None):
    """返回 (browser, ctx, page, is_cdp)。
    
    优先通过 CDP 连到用户原生 Edge（零 webdriver 检测）；
    连不上则 launch 独立 Edge 并注入反检测脚本。
    """
    if cdp_port:
        cdp_url = f"http://127.0.0.1:{cdp_port}"
        _log(f"[browser] 尝试连接原生 Edge CDP: {cdp_url}")
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            _log("[browser] ✓ 已连接到原生 Edge（零 webdriver 检测）")
            return browser, ctx, page, True
        except Exception as e:
            _log(f"[browser] CDP 连接失败: {e}，退回独立启动")
    # fallback
    browser = await p.chromium.launch(
        channel="msedge",
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx = await browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="zh-CN",
    )
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });
        window.chrome = window.chrome || {};
        window.chrome.runtime = window.chrome.runtime || {};
    """)
    page = await ctx.new_page()
    _log("[browser] ✓ 独立启动 Edge（已注入反检测）")
    return browser, ctx, page, False


async def main(kw: str, conv: str, search_timeout: int, chat_timeout: int, cdp_port: int | None = None):
    print(BANNER, flush=True)
    _log(f"搜索超时: {search_timeout}s | 评论超时: {chat_timeout}s")
    if cdp_port:
        _log(f"模式: 连接原生 Edge (端口 {cdp_port})")
    _log("")

    async with async_playwright() as p:
        browser, ctx, page, is_cdp = await _setup_browser(p, cdp_port)

        try:
            # === 阶段 1：扫码登录 ===
            ok1 = await stage1_login(page, ctx, is_cdp)
            if not ok1:
                _log("\n[ABORT] 登录失败，退出。")
                if not is_cdp:
                    await browser.close()
                return 1

            # === 阶段 2：评论手机验证码 ===
            ok2 = await stage2_comment_verify(page, kw, chat_timeout)
            if not ok2:
                _log("\n[WARN] 评论验证未解除，但继续进入搜索验证...")
                # 不中断，因为可能是间歇性风控

            # === 阶段 3：搜索过滑块 ===
            ok3 = await stage3_search_verify(page, kw, search_timeout)
            if not ok3:
                _log("\n[WARN] 搜索风控未解除。可能需要再手动操作或重跑本脚本。")

            # 导出 storage_state 给 reply_bot 用
            if is_cdp:
                try:
                    await ctx.storage_state(path=str(STATE))
                    _log("[save] storage_state.json 已从原生 Edge 导出")
                except Exception as e:
                    _log(f"[save] 导出 storage_state 失败: {e}")

        finally:
            if not is_cdp:
                await browser.close()

    _log("\n" + "=" * 60)
    if ok1 and ok2 and ok3:
        _log("全部三个阶段已完成！可以继续干活了。")
    elif ok1:
        _log(f"登录成功，评论{'OK' if ok2 else '未解除'}，搜索{'OK' if ok3 else '未解除'}。")
        if not ok2 or not ok3:
            _log("未完成的部分可以重新跑本脚本（跳过登录，直接评论+搜索）。")
    _log("=" * 60)

    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="汐缘 抖音风控一键解除")
    ap.add_argument("--kw", default="美食", help="搜索关键词（用于搜索阶段）")
    ap.add_argument("--conv", default="潮汐", help="私信会话名（预留，阶段2已改为评论手机验证）")
    ap.add_argument("--search-timeout", type=int, default=900, help="搜索阶段总超时(秒)")
    ap.add_argument("--chat-timeout", type=int, default=600, help="评论阶段总超时(秒)")
    ap.add_argument("--cdp-port", type=int, default=None, help="连接原生 Edge 的 CDP 端口（如 9222），不传则独立启动")
    ap.add_argument("--skip-login", action="store_true", help="跳过登录，直接从评论开始（已有有效 storage_state 时用）")
    ap.add_argument("--skip-search", action="store_true", help="跳过搜索阶段")
    ap.add_argument("--skip-chat", action="store_true", help="跳过评论阶段")
    a = ap.parse_args()

    async def _run_skip():
        async with async_playwright() as p:
            browser, ctx, page, is_cdp = await _setup_browser(p, a.cdp_port)
            if not is_cdp and STATE.exists():
                # 非 CDP 模式且 storage_state 存在 → 加载已保存的登录态
                await ctx.close()  # 关掉无状态的 context，重建一个带 storage 的
                ctx = await browser.new_context(
                    storage_state=str(STATE),
                    viewport={"width": 1366, "height": 900},
                    locale="zh-CN",
                )
                page = await ctx.new_page()
            ok2, ok3 = True, True
            try:
                if not a.skip_chat:
                    ok2 = await stage2_comment_verify(page, a.kw, a.chat_timeout)
                if not a.skip_search:
                    ok3 = await stage3_search_verify(page, a.kw, a.search_timeout)
            finally:
                if not is_cdp:
                    await browser.close()
        return 0 if (ok2 and ok3) else 1

    if a.skip_login:
        sys.exit(asyncio.run(_run_skip()))
    else:
        sys.exit(asyncio.run(main(a.kw, a.conv, a.search_timeout, a.chat_timeout, a.cdp_port)))