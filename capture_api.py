# -*- coding: utf-8 -*-
"""Capture current douyin web API requests (search + feed + mix) from a live headed-Edge browser."""
import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from playwright.sync_api import sync_playwright

COOKIES = json.load(open('auth_cookies.json', encoding='utf-8'))
CAPTURED = []

def cookie_list():
    out = []
    for k, v in COOKIES.items():
        out.append({'name': k, 'value': v, 'domain': '.douyin.com', 'path': '/'})
    return out

with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=False)
    ctx = browser.new_context()
    ctx.add_cookies(cookie_list())

    page = ctx.new_page()

    def on_response(resp):
        url = resp.url
        if any(x in url for x in ['/aweme/', '/search/', 'listcollection', 'module/feed']):
            try:
                ct = resp.headers.get('content-type', '')
                body = resp.text()
                CAPTURED.append({
                    'method': resp.request.method,
                    'url': url,
                    'post_data': resp.request.post_data,
                    'content_type': ct,
                    'body_preview': body[:300],
                    'body_len': len(body),
                })
            except Exception as e:
                CAPTURED.append({'method': resp.request.method, 'url': url, 'err': str(e)})

    page.on('response', on_response)

    # 1) feed page (精选)
    page.goto('https://www.douyin.com/', wait_until='domcontentloaded', timeout=30000)
    page.wait_for_timeout(8000)

    # 2) search page
    page.goto('https://www.douyin.com/search/%E7%8C%AB?type=general', wait_until='domcontentloaded', timeout=30000)
    page.wait_for_timeout(6000)

    browser.close()

with open('capture_api.json', 'w', encoding='utf-8') as f:
    json.dump(CAPTURED, f, ensure_ascii=False, indent=1)

print(f"captured {len(CAPTURED)} requests")
for c in CAPTURED:
    print('=' * 80)
    print(c.get('method'), c.get('url', '')[:200])
    if c.get('post_data'):
        print('POST_BODY:', c.get('post_data', '')[:200])
    print('CT:', c.get('content_type'), 'LEN:', c.get('body_len'))
    print('BODY:', c.get('body_preview', c.get('err', ''))[:200])
