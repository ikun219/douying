# -*- coding: utf-8 -*-
"""抖音发评论/回评论（DOM 自动化，让 App 自己签名）。用法:
  python post_comment.py <aweme_id> <评论内容>                    # 发评论
  python post_comment.py <aweme_id> <回复内容> --reply <目标评论文字>  # 回评论（按文字匹配定位）
  python post_comment.py <aweme_id> <评论内容> --reply-cid <cid>    # 回评论（按 cid 精确定位，含嵌套回复）
  python post_comment.py <aweme_id> <评论内容> --image <图片路径>    # 发带图评论
"""
import asyncio, json, io, sys, os
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from playwright.async_api import async_playwright

# 点击目标评论的"回复"按钮（按文字匹配）。返回是否成功定位到评论。
# 实测（probe_comment_node）：完整评论文本并不在 .comment-item-info-wrap 里
# （info-wrap 的 textContent 被截断成"昵称..."），而在独立的 DIV.FduGc_lz（评论文本节点）。
# 所以必须遍历 [data-e2e="comment-item"]，用评论容器的整体 textContent 做 includes 匹配，
# 再点该 item 内的"回复"按钮（probe_item_match 证明此法能唯一命中目标评论 idx0）。
CLICK_REPLY_JS = r'''
async (matchText) => {
    const norm = s => (s || '').replace(/\s+/g, '');
    // 主策略：遍历评论项（data-e2e=comment-item），用整体文本子串匹配
    const items = [...document.querySelectorAll('[data-e2e="comment-item"]')];
    for (const it of items) {
        if (matchText && !norm(it.textContent).includes(norm(matchText))) continue;
        // 在 item 内找可见的"回复"按钮
        const btn = it.querySelector('div.tFq3uJx3') ||
                    [...it.querySelectorAll('span,div')].find(e => e.textContent.trim() === '回复' && e.offsetWidth > 0);
        if (btn) { btn.click(); return true; }
    }
    // 兜底策略：老结构 .comment-item-info-wrap（含完整文本的情况）
    const infos = [...document.querySelectorAll('.comment-item-info-wrap')];
    for (const info of infos) {
        if (matchText && !info.textContent.includes(matchText)) continue;
        const root = info.closest('[class*="comment"]') || info.parentElement;
        const stats = root && (root.querySelector('.comment-item-stats-container') ||
                               info.parentElement.querySelector('.comment-item-stats-container'));
        if (!stats) continue;
        const btn = stats.querySelector('div.tFq3uJx3') ||
                    [...stats.querySelectorAll('span,div')].find(e => e.textContent.trim() === '回复' && e.offsetWidth > 0);
        if (btn) { btn.click(); return true; }
    }
    return false;
}
'''

# 点击目标评论的"回复"按钮（按 cid 精确匹配，支持嵌套回复）。
# cid 存储位置：comment-item 的 data-cid 属性 OR comment-item-info-wrap 内嵌的 data 属性。
CLICK_REPLY_BY_CID_JS = r'''
async (cid) => {
    // 策略1: 找 data-cid 属性
    let el = document.querySelector(`[data-cid="${cid}"]`);
    if (!el) {
        // 策略2: 遍历所有评论块，从内部数据绑定中找 cid
        const blocks = [...document.querySelectorAll('[class*="comment-item"]')];
        for (const block of blocks) {
            const dataset = block.dataset || {};
            if (dataset.cid === cid || dataset.commentId === cid) {
                el = block; break;
            }
        }
    }
    if (!el) {
        // 策略3: 搜包含 reply 按钮的容器，检查内嵌的 Vue data
        const allReplyBtns = [...document.querySelectorAll('.tFq3uJx3')];
        for (const btn of allReplyBtns) {
            const root = btn.closest('[class*="comment"]');
            if (!root) continue;
            // 检查 __vue_app__ / __vue__ 等内嵌属性
            const walk = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            let node;
            while (node = walk.nextNode()) {
                for (const key of Object.keys(node)) {
                    if (key.startsWith('__vue') && node[key] && node[key].props) {
                        if (node[key].props.cid === cid || node[key].props.commentId === cid) {
                            el = root; break;
                        }
                    }
                }
                if (el) break;
            }
            if (el) break;
        }
    }
    if (!el) return false;
    // 找到 el 后点回复按钮
    const stats = el.querySelector('.comment-item-stats-container') ||
                  el.querySelector('[class*="stats"]') ||
                  el.parentElement?.querySelector('.comment-item-stats-container');
    if (!stats && el.closest) {
        const parent = el.closest('[class*="comment-item"]');
        if (parent) el = parent;
    }
    const btn = (el.querySelector('div.tFq3uJx3') ||
                 [...(el.querySelectorAll('span,div') || [])].find(e =>
                     e.textContent.trim() === '回复' && e.offsetWidth > 0));
    if (btn) { btn.click(); return true; }
    return false;
}
'''

async def _find_comment_box(page, timeout=30):
    """找评论输入框。抖音的评论面板不一定一开始就在，得先滚到评论区再找。"""
    selectors = [
        '.comment-input-inner-container [contenteditable]',
        '.comment-input-inner-container',
        '[data-e2e="comment-input"] [contenteditable]',
        '[data-e2e="comment-input"]',
        '.public-DraftEditor-content',
        'div[contenteditable="true"]',
    ]

    async def try_find():
        for sel in selectors:
            try:
                loc = page.locator(sel)
                n = await loc.count()
            except Exception:
                continue
            for i in range(min(n, 5)):
                cand = loc.nth(i)
                try:
                    if await cand.is_visible():
                        return sel, cand
                except Exception:
                    continue
        return None, None

    sel, box = await try_find()
    if box is not None:
        return sel, box

    # 滚到评论区，并尝试点一下评论图标把面板叫出来
    try:
        await page.mouse.wheel(0, 700)
        await asyncio.sleep(2)
    except Exception:
        pass
    sel, box = await try_find()
    if box is not None:
        return sel, box

    for icon in ('[data-e2e="feed-comment-icon"]', '[data-e2e="comment-icon"]',
                 '[data-e2e="video-player-comment"]'):
        try:
            await page.locator(icon).first.click(timeout=3000)
            await asyncio.sleep(2.5)
            sel, box = await try_find()
            if box is not None:
                return sel, box
        except Exception:
            continue

    # 最后再轮询等一会儿
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        sel, box = await try_find()
        if box is not None:
            return sel, box
        await asyncio.sleep(1.0)
    return None, None


async def _upload_comment_image(page, image_path, timeout=30):
    """在评论框里上传一张图片。返回 (ok, error_msg)。"""
    if not image_path or not os.path.isfile(image_path):
        return False, f"图片不存在: {image_path}"
    ext = Path(image_path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return False, f"不支持的图片格式: {ext}"

    # 策略 A：找评论区域附近的 input[type=file]，直接 set_input_files
    file_input_js = r"""() => {
        const commentArea = document.querySelector('.comment-input-container') ||
            document.querySelector('[class*="comment-input"]') ||
            document.querySelector('.comment-main-input') ||
            document.body;
        const inputs = commentArea.querySelectorAll('input[type="file"]');
        return inputs.length;
    }"""
    n_inputs = await page.evaluate(file_input_js)
    if n_inputs > 0:
        try:
            # 找离评论框最近的 file input
            box = page.locator('.comment-input-container input[type="file"], '
                               '[class*="comment-input"] input[type="file"], '
                               '.comment-main-input input[type="file"]').first
            await box.set_input_files(image_path, timeout=10000)
            await asyncio.sleep(3)
            return True, None
        except Exception as e:
            pass  # 策略 A 失败，尝试策略 B

    # 策略 B：点击图片按钮 → 弹出文件选择器
    img_btn_selectors = [
        '[data-e2e="comment-image"]',
        '.comment-input-container [class*="image"]',
        '.comment-input-container [class*="photo"]',
        '.comment-input-container [class*="pic"]',
        '.comment-input-container svg',
        '[class*="comment-input"] [class*="image"]',
        '[class*="comment-input"] [class*="photo"]',
        '[class*="comment"] svg[class*="image"]',
        # 通用：找 input[type=file] 的兄弟/父级里的可见按钮
    ]
    for sel in img_btn_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(image_path, timeout=10000)
                await asyncio.sleep(3)
                return True, None
        except Exception:
            continue

    # 策略 C：JS 直接找到评论区的 file input 并触发 click
    try:
        triggered = await page.evaluate(r"""() => {
            const c = document.querySelector('.comment-input-container') ||
                document.querySelector('[class*="comment-input"]') ||
                document.body;
            const inp = c.querySelector('input[type="file"]');
            if (inp) { inp.click(); return true; }
            // 扩大搜索范围
            for (const el of document.querySelectorAll('input[type="file"]')) {
                if (el.closest('[class*="comment"]')) { el.click(); return true; }
            }
            return false;
        }""")
        if triggered:
            async with page.expect_file_chooser(timeout=5000) as fc_info:
                pass  # click 已在上面的 JS 里触发
            file_chooser = await fc_info.value
            await file_chooser.set_files(image_path, timeout=10000)
            await asyncio.sleep(3)
            return True, None
    except Exception:
        pass

    return False, "找不到评论图片上传入口（input[type=file] 或图片按钮）"


async def post_comment(aweme_id, text, reply_to_text=None, headless=False, image_path=None, reply_cid=None):
    """发评论/回复。
    Args:
        reply_to_text: 按文字匹配目标评论（一级回复）。
        reply_cid: 按 cid 精确定位目标评论（支持嵌套回复，优先级高于 reply_to_text）。
    """
    # 抖音 @ 用户名映射（聊天的昵称 → 抖音实际用户名）
    text = text.replace('@潮汐', '@汐')
    async with async_playwright() as p:
        b = await p.chromium.launch(channel='msedge', headless=headless, args=['--disable-blink-features=AutomationControlled'])
        ctx = await b.new_context(storage_state='storage_state.json')
        page = await ctx.new_page()
        resp = {}
        async def on_resp(r):
            if 'comment/publish' in r.url:
                try: resp['body'] = await r.text()
                except: pass
        page.on('response', on_resp)
        # 收集控制台错误，方便排查
        page_errors = []
        page.on('pageerror', lambda e: page_errors.append(str(e)))
        await page.goto(f'https://www.douyin.com/video/{aweme_id}', wait_until='domcontentloaded')
        await asyncio.sleep(6)
        # 检测并关闭验证弹窗（SMS / 滑块 / 风控）
        for attempt in range(4):
            # 检查页面是否有验证相关文字
            has_modal = await page.evaluate('''() => {
                const body = document.body.innerText || "";
                return body.includes("验证码") || body.includes("安全验证") || body.includes("滑块验证");
            }''')
            if not has_modal:
                break
            # 用 Playwright locator 点"取消"（exact=False 子串匹配更宽松）
            clicked = False
            for label in ['取消', '关闭']:
                try:
                    btns = page.get_by_text(label, exact=False)
                    cnt = await btns.count()
                    for i in range(min(cnt, 3)):
                        try:
                            b = btns.nth(i)
                            if await b.is_visible():
                                await b.click()
                                clicked = True
                                break
                        except Exception:
                            continue
                except Exception:
                    continue
                if clicked:
                    break
            if not clicked:
                # 用 JS 兜底：遍历所有可见元素找文字匹配
                await page.evaluate('''() => {
                    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                    let n;
                    while (n = walk.nextNode()) {
                        if (n.offsetParent === null) continue;
                        const t = (n.textContent || "").trim();
                        if ((t === "取消" || t === "关闭") && n.children.length <= 1) {
                            n.click(); return;
                        }
                    }
                    // fallback: any element whose ONLY text is "取消"
                    const all = [...document.querySelectorAll("*")];
                    for (const el of all) {
                        if (el.offsetParent === null) continue;
                        if ((el.textContent || "").trim() === "取消" && el.children.length <= 1) {
                            el.click(); return;
                        }
                    }
                }''')
            await asyncio.sleep(2)
        # 4 轮都没关掉验证弹窗（短信验证码/滑块）→ 命中风控，别硬刚，直接上报
        if has_modal:
            await b.close()
            return {'ok': False, 'risk': True, 'risk_type': 'sms_verify',
                    'error': '页面弹出短信/滑块验证码且连续关闭失败（风控）'}
        await asyncio.sleep(3)
        # 若是回评论：先点目标评论的"回复"按钮（支持文字匹配 + cid 精确定位）
        if reply_cid or reply_to_text:
            # 如果是 nested reply，先多滚几下展开嵌套回复区
            if reply_cid:
                for _ in range(3):
                    await page.mouse.wheel(0, 500)
                    await asyncio.sleep(0.8)
                # 尝试点"展开回复"按钮（如果有）
                try:
                    await page.evaluate('''() => {
                        const btns = [...document.querySelectorAll('span,div')].filter(e =>
                            (e.textContent || '').includes('展开') && e.offsetWidth > 0);
                        for (const b of btns) { b.click(); }
                    }''')
                    await asyncio.sleep(1.5)
                except Exception:
                    pass
            if reply_cid:
                clicked = await page.evaluate(CLICK_REPLY_BY_CID_JS, reply_cid)
                if not clicked:
                    return {'ok': False, 'error': f'未找到评论 cid={reply_cid}（可能被折叠或不在当前页）'}
            elif reply_to_text:
                clicked = await page.evaluate(CLICK_REPLY_JS, reply_to_text)
                if not clicked:
                    return {'ok': False, 'error': '未找到匹配评论: ' + reply_to_text[:30]}
            await asyncio.sleep(2)
        # 输入框此时才出现（点回复 或 点输入区 后）
        sel, box = await _find_comment_box(page)
        if box is None:
            await b.close()
            return {'ok': False, 'error': '找不到评论输入框（页面结构变了 / 未登录 / 评论区没加载）'}
        # 先点容器，再用 JS 确保焦点落在真正的 contenteditable 上
        await box.click()
        await asyncio.sleep(0.5)
        await page.evaluate('''() => {
            const input = document.querySelector(
                '.comment-input-inner-container [contenteditable="true"],'
                + '[data-e2e="comment-input"] [contenteditable="true"],'
                + '.public-DraftEditor-content,'
                + 'div[contenteditable="true"]'
            );
            if (input) { input.focus(); input.click(); }
        }''')
        await asyncio.sleep(0.3)
        # ---- 逐字输入，处理 @ 提及 ----
        import re as _re
        mention_pattern = _re.compile(r'(@\S+)')
        parts = mention_pattern.split(text)  # ['前缀', '@name', '后缀', ...]
        for part in parts:
            if part.startswith('@'):
                # @ 提及：打 @ + 名字 → 等下拉 → ↓ + Enter 选中
                name = part[1:]
                await page.keyboard.press('@')
                await asyncio.sleep(0.6)
                await page.keyboard.type(name, delay=60)
                await asyncio.sleep(1.0)  # 等下拉过滤
                # 键盘选中：↓ 到第一条 → Enter 确认
                await page.keyboard.press('ArrowDown')
                await asyncio.sleep(0.3)
                await page.keyboard.press('Enter')
                await asyncio.sleep(0.5)
                # 补空格
                await page.keyboard.press('Space')
                await asyncio.sleep(0.2)
            else:
                # 非 @ 部分，去掉前导空格（@ 后面已经补了空格）
                stripped = part.lstrip()
                if stripped:
                    await page.keyboard.type(stripped, delay=50)
        await asyncio.sleep(1)
        # 优先点发送按钮（比 Enter 更可靠），找不到才按 Enter
        btn_clicked = await page.evaluate('''() => {
            const sel = document.querySelector(
                '[data-e2e="comment-submit"],'
                + '.comment-input-container .submit,'
                + '.comment-input-submit,'
                + 'button[class*="submit"],'
                + 'div[class*="submit"]'
            );
            if (sel && sel.offsetParent !== null) { sel.click(); return true; }
            return false;
        }''')
        if not btn_clicked:
            await page.keyboard.press('Enter')

        # ---- 确认评论已发出（DOM + API 双路径，最长等 12 分钟）----
        import time as _t
        deadline = _t.time() + 120          # 前 2 分钟等 API/DOM 确认
        retry_enter = 0
        risk_hit = False
        verify_wait_start = None            # 验证码开始等待的时间
        VERIFY_WAIT_TIMEOUT = 600           # 验证码等待上限：10 分钟
        while _t.time() < deadline + VERIFY_WAIT_TIMEOUT:
            await asyncio.sleep(1.5)

            # ① 验证码检查（优先级最高 —— 弹了就不能继续）
            try:
                check_modal = await page.evaluate('''() => {
                    const body = document.body.innerText || "";
                    return body.includes("验证码") || body.includes("安全验证") || body.includes("滑块验证");
                }''')
            except Exception:
                check_modal = False
            if check_modal:
                if verify_wait_start is None:
                    verify_wait_start = _t.time()
                    print(f"[comment] ⚠ 验证码弹出！请在浏览器窗口里手动过验证（脚本会在这等，最长 {VERIFY_WAIT_TIMEOUT}s）")
                if _t.time() - verify_wait_start > VERIFY_WAIT_TIMEOUT:
                    risk_hit = True
                    break
                # 还在等 → 继续等
                continue
            # 验证码刚消失 → 等 3 秒让页面稳定
            if verify_wait_start is not None:
                await asyncio.sleep(3)
                verify_wait_start = None
                print("[comment] 验证码已消失，检查评论状态…")

            # ① API 先回来了 → 直接成功
            if resp.get('body'):
                try:
                    d = json.loads(resp['body'])
                    cid = (d.get('comment') or {}).get('cid')
                    if cid:
                        await b.close()
                        return {'ok': True, 'cid': cid, 'text': (d.get('comment') or {}).get('text'),
                                'confirmed_by': 'api'}
                except Exception:
                    pass

            # ② DOM 里出现了自己的评论 → 成功（比 API 更可靠）
            try:
                found = await page.evaluate(
                    r'''(t) => {
                        const items = [...document.querySelectorAll('.comment-item-info-wrap,.comment-content,.comment-item-content')];
                        return items.some(el => el.textContent.includes(t));
                    }''', text[:20])
                if found:
                    await b.close()
                    # 如果 API 也回来了就用 cid，否则只报 DOM 确认
                    cid = None
                    if resp.get('body'):
                        try:
                            d2 = json.loads(resp['body'])
                            cid = (d2.get('comment') or {}).get('cid')
                        except Exception:
                            pass
                    return {'ok': True, 'cid': cid, 'confirmed_by': 'dom'}
            except Exception:
                pass

            # ③ 输入框里还有字 → Enter 没生效或被拦截，再按一次（最多 2 次）
            try:
                leftover = await box.inner_text()
            except Exception:
                leftover = ''
            if leftover and leftover.strip() and retry_enter < 2:
                retry_enter += 1
                await page.keyboard.press('Enter')
                continue

            # 截图保存现场，方便排查
        import tempfile, os as _os
        try:
            tmp_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'tmp')
            _os.makedirs(tmp_dir, exist_ok=True)
            ss_path = _os.path.join(tmp_dir, f'comment_diag_{int(_t.time())}.png')
            await page.screenshot(path=ss_path, full_page=False)
        except Exception:
            ss_path = None
        await b.close()

        # 发送后弹了验证码（短信验证码/滑块）→ 命中风控，上报（调用方会通知主人+暂停自主刷）
        if risk_hit:
            return {'ok': False, 'risk': True, 'risk_type': 'sms_verify',
                    'error': '评论发送后触发短信/滑块验证码（风控）',
                    'screenshot': ss_path}

    # 30 秒都没确认 → 失败
    body = resp.get('body', '')
    if not body:
        err_detail = '30 秒后仍未确认评论已发送（API 无响应且 DOM 里没找到）'
        if page_errors:
            err_detail += '｜page_errors: ' + '; '.join(page_errors[-5:])
        return {'ok': False,
                'error': err_detail,
                'selector': sel,
                'screenshot': ss_path}
    try:
        d = json.loads(body)
        cid = (d.get('comment') or {}).get('cid')
        return {'ok': bool(cid), 'cid': cid, 'text': (d.get('comment') or {}).get('text'),
                'raw': body[:200], 'confirmed_by': 'api_late'}
    except Exception:
        return {'ok': False, 'raw': body[:200]}

async def main():
    argv = sys.argv[1:]
    reply_to_text = None
    if '--reply' in argv:
        i = argv.index('--reply')
        reply_to_text = argv[i+1]
        del argv[i:i+2]
    if len(argv) < 2:
        print('用法: post_comment.py <aweme_id> <内容> [--reply <目标评论文字>]')
        return
    aweme_id, text = argv[0], argv[1]
    r = await post_comment(aweme_id, text, reply_to_text)
    print(json.dumps(r, ensure_ascii=False, indent=1))

if __name__ == '__main__':
    asyncio.run(main())
