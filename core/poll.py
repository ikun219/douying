# -*- coding: utf-8 -*-
"""轮询与看门狗模块 — WS 断线兜底 + 静默检测 + 评论通知。

从 MessageRouter 拆出，通过 bot + router 双协作者访问状态。
"""

import json
import os
import re
import time
import hashlib
import traceback

import requests as _requests

from llm_providers import DEEPSEEK_KEY, DEEPSEEK_MODEL, DEEPSEEK_URL


class PollHandler:
    """轮询兜底 + 看门狗：WS 断线时 HTTP 拉消息，静默检测，评论通知回复。

    __init__(bot, router):
        bot    — ReplyBot 实例（所有共享状态）
        router — MessageRouter 实例（dispatch_http / handle_auto_cmd 等）
    """

    def __init__(self, bot, router):
        self.bot = bot
        self.router = router

    # ═══════════════════════════════════════════
    #  轮询文本处理
    # ═══════════════════════════════════════════

    def handle_polled_text(self, text):
        bot = self.bot
        try:
            idx = 'poll:' + hashlib.md5(text.encode('utf-8')).hexdigest()[:12]
            if idx in bot.processed:
                return
            bot.processed.add(idx)
            bot.log(f'[poll] 处理: {text[:60]}')
            self.router.handle_auto_cmd(text)
            from reply_bot import MASTER_MAP
            master_uid = next(iter(MASTER_MAP))
            self.router.handle_restart(text, str(master_uid))
            if self.router.handle_share_text(text, master_uid, str(master_uid), True, '老大'):
                return
            reply = bot.generate_reply(text, sender_label='老大', is_master=True)
            bot._write_message('user', f'[抖音私信] {text}')
            if reply:
                self.router.handle_brain_result(master_uid, reply, is_master=True)
        except Exception as e:
            bot.log(f'[poll] 处理失败 {type(e).__name__}: {e}')

    # ═══════════════════════════════════════════
    #  poll_state.json 兜底轮询
    # ═══════════════════════════════════════════

    def poll_loop(self):
        bot = self.bot
        time.sleep(75)
        HERE = bot._HERE
        POLL_FILE = os.path.join(HERE, 'poll_state.json')

        def _load_poll_counts():
            try:
                return dict(json.load(open(POLL_FILE, encoding='utf-8')).get('counts') or {})
            except Exception:
                return {}

        def _save_poll_counts(counts):
            try:
                json.dump({'counts': counts, 'ts': int(time.time())},
                          open(POLL_FILE, 'w', encoding='utf-8'), ensure_ascii=False)
            except Exception as e:
                bot.log(f'[poll] 存状态失败 {e}')

        prev = _load_poll_counts()
        seen_cards = set()
        seen_cmds = set()
        bot.log(f'[poll] 启动，基线 {len(prev)} 条')
        from reply_bot import MASTER_MAP
        master_uid = next(iter(MASTER_MAP))
        while True:
            try:
                from send_text import fetch_recent_sync
                rows = fetch_recent_sync('潮汐', True, '285391874')
                cnt = {}
                cards_to_handle = []
                for r in rows:
                    if r.get('side') != 'him':
                        continue
                    t = (r.get('text') or '').strip()
                    if t:
                        cnt[t] = cnt.get(t, 0) + 1
                    for link in (r.get('links') or []):
                        if link and link not in seen_cards:
                            seen_cards.add(link)
                            cards_to_handle.insert(0, link)

                fresh_text = []
                for t, c in cnt.items():
                    if c > prev.get(t, 0):
                        fresh_text.append(t)
                if fresh_text:
                    bot.log(f'[poll] 回源发现 {len(fresh_text)} 条新文本: {fresh_text[:3]}')
                    for t in reversed(fresh_text):
                        self.handle_polled_text(t)

                cmd_keywords = ('刷', '别刷', '停', '继续', '好了', '可以', '搞定', '休息', '睡觉', '够了')
                for t in set(cnt.keys()):
                    low = t.lower().replace(' ', '')
                    if any(kw in low for kw in cmd_keywords) and t not in seen_cmds:
                        seen_cmds.add(t)
                        bot.log(f'[poll] 指令扫描命中: {t[:60]}')
                        self.router.handle_auto_cmd(t)

                if cards_to_handle:
                    bot.log(f'[poll] 回源发现 {len(cards_to_handle)} 张新卡片')
                    for link in cards_to_handle:
                        sender_str = str(master_uid)
                        bot.log(f'[poll] 处理卡片链接: {link[:80]}')
                        self.router.handle_share_text(link, int(sender_str), sender_str, True, '老大')

                prev = cnt
                _save_poll_counts(cnt)
            except Exception as e:
                bot.log(f'[poll] err {type(e).__name__}: {e}')
            time.sleep(40)

    # ═══════════════════════════════════════════
    #  HTTP 轮询（im_http 兜底）
    # ═══════════════════════════════════════════

    def http_poll_loop(self):
        import im_http
        bot = self.bot
        HTTP_STATE = bot._HTTP_STATE

        def _http_load_state():
            try:
                return dict(json.load(open(HTTP_STATE, encoding='utf-8')).get('last') or {})
            except Exception:
                return {}

        def _http_save_state(last):
            try:
                json.dump({'last': last, 'ts': int(time.time())},
                          open(HTTP_STATE, 'w', encoding='utf-8'), ensure_ascii=False)
            except Exception as e:
                bot.log(f'[http] 存状态失败 {e}')

        last = _http_load_state()
        first = not last
        poll_n = 0
        did_startup_scan = False
        bot.log(f'[http] 启动，{"建立基线" if first else f"续跑（{len(last)} 个会话）"}')
        from reply_bot import MASTER_MAP
        while True:
            try:
                bot._http_evt.wait(12)
                bot._http_evt.clear()
                poll_n += 1
                r = im_http.fetch(my_uid=bot.my_uid, limit=25)
                if not r.get('ok'):
                    bot.log(f"[http] 回源失败 {r.get('error')}")
                    time.sleep(3)
                    continue
                bot.my_uid = r.get('my_uid') or bot.my_uid
                dispatched = 0
                for conv in r.get('conversations') or []:
                    cid = conv['conv_id']
                    if str(conv.get('peer_uid') or '') == str(bot.my_uid):
                        continue
                    msgs = conv.get('messages') or []
                    if not msgs:
                        continue
                    prev_ts = float(last.get(cid) or 0)
                    fresh = [m for m in msgs if (m['ts'] or 0) > prev_ts and not m.get('from_me')]
                    newest = max(m['ts'] or 0 for m in msgs)
                    if first:
                        last[cid] = newest
                        continue
                    for m in fresh:
                        dispatched += 1
                        try:
                            self.router.dispatch_http(conv['peer_uid'], m)
                        except Exception as e:
                            bot.log(f"[http] 处理失败 {type(e).__name__}: {e}")
                            traceback.print_exc()
                    if newest > prev_ts:
                        last[cid] = newest
                if first:
                    first = False
                    bot.log(f'[http] 基线完成：{len(last)} 个会话，从此刻起只回新消息')
                if not did_startup_scan:
                    did_startup_scan = True
                    for conv2 in r.get('conversations') or []:
                        if str(conv2.get('peer_uid') or '') == str(bot.my_uid):
                            continue
                        if str(conv2.get('peer_uid') or '') not in MASTER_MAP:
                            continue
                        for old_m in (conv2.get('messages') or []):
                            if old_m.get('from_me'):
                                continue
                            txt = (old_m.get('text') or '').strip()
                            if not txt:
                                continue
                            cmd_kw = ('刷', '停', '继续', '休息', '好了', '可以', '搞定', '够了', '睡觉')
                            if any(kw in txt.lower().replace(' ', '') for kw in cmd_kw):
                                bot.log(f'[http] 回溯指令: {txt[:60]}')
                                self.router.handle_auto_cmd(txt)
                elif poll_n % 10 == 0:
                    bot.log(f'[http] 心跳 第{poll_n}轮 会话{len(r.get("conversations") or [])} 新消息{dispatched}')
                _http_save_state(last)
            except Exception as e:
                bot.log(f'[http] err {type(e).__name__}: {e}')
                time.sleep(5)

    # ═══════════════════════════════════════════
    #  看门狗
    # ═══════════════════════════════════════════

    def silence_watchdog(self):
        bot = self.bot
        idle_limit = 600
        cooldown = 1800
        _last_wd_reconnect = 0
        while True:
            time.sleep(60)
            try:
                gap = time.time() - getattr(bot, '_last_content_ts', time.time())
                since_last = time.time() - _last_wd_reconnect
                if gap >= idle_limit and since_last >= cooldown and bot.ws is not None:
                    bot.log(f"[watchdog] {int(gap)}s 没收到内容帧（不含 50001 回声），主动重连一次")
                    bot._last_content_ts = time.time()
                    _last_wd_reconnect = time.time()
                    try:
                        bot.ws.close()
                    except Exception as e:
                        bot.log(f"[watchdog] close 失败：{e}")
                        _last_wd_reconnect = 0
            except Exception as e:
                bot.log(f"[watchdog] err {e}")

    def notification_watchdog(self):
        bot = self.bot
        time.sleep(90)
        NOTIFY_INTERVAL = 300
        while True:
            try:
                import headed_browser as hb
                replies = hb.run(hb.scan_reply_notifications(), timeout=45)
                if replies is None:
                    replies = []
            except Exception as e:
                bot.log(f"[notify-wd] 扫描通知失败: {e}")
                time.sleep(NOTIFY_INTERVAL)
                continue

            if not replies:
                bot.log("[notify-wd] 扫描完成，无新回复")
                time.sleep(NOTIFY_INTERVAL)
                continue

            bot.log(f"[notify-wd] 发现 {len(replies)} 条新评论回复")
            for r in replies:
                try:
                    nid = r.get("nid", "")
                    replier_name = r.get("replier_name", "网友")
                    reply_text = r.get("reply_text", "")
                    comment_url = r.get("comment_url", "")

                    aweme_id = r.get("aweme_id", "")
                    reply_cid = r.get("cid", "")
                    parent_cid = r.get("parent_cid", "")

                    if (not aweme_id or not reply_cid) and comment_url:
                        if not aweme_id:
                            m = re.search(r'/video/(\d+)', comment_url)
                            if m:
                                aweme_id = m.group(1)
                        if not reply_cid:
                            m2 = re.search(r'(?:comment_id|reply_id|cid)=(\d+)', comment_url)
                            if m2:
                                reply_cid = m2.group(1)

                    if not aweme_id:
                        bot.log(f"[notify-wd] 跳过：无法从 {comment_url} 提取 aweme_id")
                        hb.mark_notification_processed(nid)
                        continue

                    prompt = (
                        f"你是汐缘。有人回复了你在抖音的评论。"
                        f"网友「{replier_name}」说：「{reply_text}」"
                        f"请用汐缘的语气回一句话（15字以内，可爱俏皮）。只输出回复内容，不要解释。"
                    )
                    system = bot.persona[:400] if hasattr(bot, 'persona') else "你是汐缘，一个温柔可爱的AI。"
                    messages = [
                        {'role': 'system', 'content': system},
                        {'role': 'user', 'content': prompt},
                    ]
                    try:
                        resp = _requests.post(
                            DEEPSEEK_URL,
                            headers={'Authorization': f'Bearer {DEEPSEEK_KEY}',
                                     'Content-Type': 'application/json'},
                            json={'model': DEEPSEEK_MODEL, 'messages': messages,
                                  'max_tokens': 320, 'temperature': 0.95},
                            timeout=60,
                        )
                        if resp.status_code == 200:
                            llm_reply = (resp.json().get('choices', [{}])[0]
                                          .get('message', {}).get('content', '')).strip()
                        else:
                            bot.log(f"[notify-wd] DeepSeek HTTP {resp.status_code}: {resp.text[:100]}")
                            hb.mark_notification_processed(nid)
                            continue
                    except Exception as e:
                        bot.log(f"[notify-wd] LLM 调用失败: {e}")
                        hb.mark_notification_processed(nid)
                        continue

                    if not llm_reply or len(llm_reply) < 2:
                        bot.log(f"[notify-wd] LLM 返回空，跳过 nid={nid[:20]}")
                        hb.mark_notification_processed(nid)
                        continue

                    bot.log(f"[notify-wd] 回复 {replier_name}: {llm_reply[:40]}")
                    result = hb.run(
                        hb.reply_to_notification(nid, aweme_id, reply_cid, llm_reply,
                                                   reply_content=reply_text,
                                                   parent_cid=parent_cid,
                                                   replier_name=replier_name),
                        timeout=90,
                    )
                    if result and result.get("ok"):
                        bot.log(f"[notify-wd] ✅ 已回复 {replier_name}")
                    else:
                        err = (result or {}).get("error", "未知")
                        bot.log(f"[notify-wd] ❌ 回复失败: {err}")
                        hb.mark_notification_processed(nid)

                except Exception as e:
                    bot.log(f"[notify-wd] 处理通知异常: {e}")
                    try:
                        import headed_browser as hb2
                        hb2.mark_notification_processed(r.get("nid", ""))
                    except Exception:
                        pass

            time.sleep(NOTIFY_INTERVAL)