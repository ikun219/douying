# -*- coding: utf-8 -*-
"""抖音视频操作（点赞/收藏/分享）：DOM 自动化，让 App 自己签名。
用法:
  python video_actions.py like <aweme_id>      # 点赞
  python video_actions.py collect <aweme_id>   # 收藏
  python video_actions.py share <aweme_id>     # 打开分享面板并抓分享链接
"""
import asyncio, json, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from playwright.async_api import async_playwright

E2E = {
    'like': 'video-player-digg',
    'collect': 'video-player-collect',
    'share': 'video-player-share',
}

async def _do(aweme_id, action, headless=False):
    """在视频页执行一个操作。返回 {ok, state/url, ...}"""
    async with async_playwright() as p:
        b = await p.chromium.launch(channel='msedge', headless=headless, args=['--disable-blink-features=AutomationControlled'])
        ctx = await b.new_context(storage_state='storage_state.json', viewport={'width': 900, 'height': 600})
        page = await ctx.new_page()
        reqs = []
        async def on_req(r):
            if ('digg' in r.url or 'collect' in r.url or 'commit' in r.url or 'share' in r.url):
                reqs.append({'url': r.url[:120], 'post': r.post_data})
        page.on('request', on_req)
        await page.goto(f'https://www.douyin.com/video/{aweme_id}', wait_until='domcontentloaded')
        await asyncio.sleep(9)
        # 悬停视频让操作栏出现（可选）
        await page.mouse.move(700, 400)
        await asyncio.sleep(1)
        sel = E2E[action]
        r = await page.evaluate('''(sel) => {
            const el = document.querySelector(`[data-e2e="${sel}"]`);
            if (!el) return {ok: false, err: '按钮未找到'};
            el.click();
            return {ok: true, state: el.getAttribute('data-e2e-state')};
        }''', sel)
        await asyncio.sleep(4)
        # 分享面板：抓取分享链接
        share_url = None
        if action == 'share':
            share_url = await page.evaluate('''() => {
                // 找分享面板里的链接或"复制链接"按钮
                const a = document.querySelector('[class*="share"] a[href*="douyin"], input[value*="douyin"], [class*="share-link"]');
                return a ? (a.href || a.value || a.textContent || '').slice(0,200) : null;
            }''')
        await b.close()
    return {
        'ok': r.get('ok', False),
        'action': action,
        'state': r.get('state'),
        'share_url': share_url,
        'reqs': reqs,
    }

async def main():
    if len(sys.argv) < 3:
        print('用法: video_actions.py <like|collect|share> <aweme_id>')
        return
    action, aweme_id = sys.argv[1], sys.argv[2]
    r = await _do(aweme_id, action)
    print(json.dumps(r, ensure_ascii=False, indent=1))

if __name__ == '__main__':
    asyncio.run(main())
