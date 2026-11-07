# -*- coding: utf-8 -*-
"""消息路由模块 — 从 ReplyBot 拆出的消息分类/派发/处理逻辑。

提供：
- MessageRouter: 接收 ReplyBot 实例作为协作者，处理所有 DM 消息的路由与回复。
- 媒体处理委托给 core.media.MediaHandler，轮询/看门狗委托给 core.poll.PollHandler。
"""

import json
import os
import re
import sys
import time
import random
import asyncio
import traceback
import subprocess

from static import Live_pb2, Response_pb2
from core.tools import sticker_key, sticker_recall, sticker_remember
from core.ml_util import (
    _find_url, describe_image_url, transcribe_audio_url,
    _extract_douyin_url, _resolve_aweme_id,
)
from core.text_util import _wants_meme, MEME_PROB, send_meme_async
from core.media import MediaHandler, deep_get
from core.poll import PollHandler


# ═══════════════════════════════════════════
#  模块级工具
# ═══════════════════════════════════════════

def _looks_media(content):
    """检查 content 是否像媒体类型（含 url_list / cover_url / image）。"""
    s = json.dumps(content, ensure_ascii=False)
    return ('url_list' in s) or ('cover_url' in s) or ('image' in s and 'http' in s)


# ═══════════════════════════════════════════
#  MessageRouter
# ═══════════════════════════════════════════

class MessageRouter:
    """消息路由：WS 帧/HTTP 回源 → 分类 → 处理 → 回复。

    通过 bot 协作者访问 ReplyBot 的全部状态，媒体处理委托给 self.media，
    轮询/看门狗委托给 self.poll。
    """

    def __init__(self, bot):
        self.bot = bot
        self.media = MediaHandler(bot)
        self.poll = PollHandler(bot, self)

    # ═══════════════════════════════════════════
    #  WS 消息入口
    # ═══════════════════════════════════════════

    def on_message(self, ws, message):
        bot = self.bot
        bot.last_frame_ts = time.time()
        try:
            frame = Live_pb2.PushFrame()
            frame.ParseFromString(message)
            if frame.payloadType != 'pb':
                return
            resp = Response_pb2.Response()
            resp.ParseFromString(frame.payload)
            notify = resp.body.new_message_notify.message
            sender = notify.sender
            msg_type = notify.message_type
            if msg_type != 50001:
                bot._last_content_ts = time.time()
            index = notify.index_in_conversation
            bot.log(
                f"[frame] sender={sender} type={msg_type} index={index} "
                f"cmd={resp.cmd} clen={len(notify.content or '')}"
            )
            try:
                content = json.loads(notify.content) if (notify.content or '').strip() else {}
            except Exception as e:
                content = {}
                bot.log(f"[frame-bad-json] {type(e).__name__}: {e} | raw={(notify.content or '')[:300]!r}")

            # 视频/分享卡片以自己 uid 推下来 → 是老大发的，不能当回显丢
            self_video = (sender == bot.my_uid) and (msg_type in (8, 27))
            if sender == bot.my_uid and not self_video:
                if msg_type not in (7, 50001):
                    bot.log(f"[frame-self-raw] type={msg_type} clen={len(notify.content or '')} raw={(notify.content or '')[:1200]!r}")
                return
            if self_video:
                _awe = int((content or {}).get('aweType') or 0)
                if _awe >= 2700:
                    bot.log(f"[frame-self-video] aweType={_awe} 贴纸回显，跳过")
                    return
                item_id = deep_get(content, ('itemId', 'item_id')) or ''
                if item_id and self.media.auto_browse_just_shared(item_id):
                    bot.log(f"[frame-self-video] itemId={item_id} auto_browse 刚分享过，跳过回显")
                    return
                bot.log(f"[frame-self-video] _deep_get 没挖到 itemId，跳过（等 recv video 帧处理）")
                return

            sender_str = str(sender)
            from reply_bot import MASTER_MAP
            label = MASTER_MAP.get(sender_str)
            is_master = label is not None
            who_label = label if is_master else f"一位网友（抖音uid {sender_str}，不是你老大）"

            if index in bot.processed:
                return
            bot.processed.add(index)
            if len(bot.processed) > 1000:
                bot.processed.clear()

            if (not is_master) and msg_type in (2, 5, 7, 8, 27) and not bot._allow_stranger(sender_str):
                bot.log(f"[throttle] 网友 {sender_str} 发得太频繁，这条先不回（type={msg_type}）")
                return

            if msg_type == 7:
                text = content.get('text', '')
                bot.log(f"[recv text] from={sender} master={is_master} index={index} text={text}")
                bot._http_evt.set()
                if text.strip():
                    k = (sender_str, text.strip()[:80])
                    bot._ws_text_ts[k] = time.time()
                    if len(bot._ws_text_ts) > 300:
                        cut = time.time() - 600
                        bot._ws_text_ts = {kk: v for kk, v in bot._ws_text_ts.items() if v > cut}
                    self.dispatch_http(
                        sender_str,
                        {'ts': int(time.time() * 1000), 'from_uid': sender_str, 'text': text,
                         'awe': 0, 'item_id': '', 'title': '', 'from_me': False},
                        src='ws',
                    )
                return
            elif msg_type == 5:
                bot.log(f"[recv emoji] from={sender} master={is_master} raw={json.dumps(content, ensure_ascii=False)[:1500]}")
                self.handle_media(sender, sender_str, is_master, who_label, content, '表情包')
            elif msg_type == 27:
                bot.log(f"[recv t27] from={sender} master={is_master} raw={json.dumps(content, ensure_ascii=False)[:1500]}")
                _awe = int(content.get('aweType') or 0)
                _has_video_id = content.get('itemId') or content.get('aweme_id')
                _is_img = (not _has_video_id) and (
                    (2700 <= _awe < 2800) or content.get('resource_url')
                )
                if _is_img:
                    bot.log(f"[recv t27-img] aweType={_awe} → 按图片处理")
                    self.handle_media(sender, sender_str, is_master, who_label, content, '图片')
                elif not _has_video_id and not content.get('content_title'):
                    bot.send_reply(sender, '这个视频我这边解析不出来，你跟我说说是什么内容呗')
                else:
                    self.media.handle_video_card(sender, sender_str, is_master, who_label, content)
            elif msg_type == 17:
                url = ((content.get('resource_url') or {}).get('url_list') or [None])[0]
                bot.log(f"[recv voice] from={sender} master={is_master} index={index} raw={json.dumps(content, ensure_ascii=False)[:700]}")
                text = transcribe_audio_url(url) if url else None
                if not text:
                    bot.send_reply(sender, '语音没听清呀，再说一遍？')
                else:
                    bot.log(f"[stt] {text}")
                    if is_master:
                        bot._write_message('user', f'[抖音语音] {text}')
                        self.handle_auto_cmd(text)
                        self.handle_restart(text, sender_str)
                    reply = bot.generate_reply(text, sender_label=who_label, is_master=is_master, sender_id=sender_str)
                    if reply:
                        self.handle_brain_result(sender, reply, is_master=is_master)
            elif msg_type == 2:
                bot.log(f"[recv image] from={sender} raw={json.dumps(content, ensure_ascii=False)[:1500]}")
                self.handle_media(sender, sender_str, is_master, who_label, content, '图片')
            elif msg_type == 8:
                bot.log(f"[recv video] from={sender} itemId={content.get('itemId')} raw={json.dumps(content, ensure_ascii=False)[:800]}")
                _item_id = content.get('itemId') or ''
                if _item_id and self.media.auto_browse_just_shared(_item_id):
                    bot.log(f"[recv video] itemId={_item_id} auto_browse 刚分享过，跳过回显")
                    return
                try:
                    from send_text import _KA
                    _KA.mark_read(sender_str)
                except Exception as e:
                    bot.log(f'[video] mark_read 跳过: {e}')
                self.media.handle_video_card(sender, sender_str, is_master, who_label, content)
            elif msg_type == 50001:
                pass
            else:
                bot.log(f"[recv unknown] type={msg_type} from={sender} master={is_master} raw={json.dumps(content, ensure_ascii=False)[:1200]}")
                if _looks_media(content):
                    self.handle_media(sender, sender_str, is_master, who_label, content, '图片')
        except Exception as e:
            bot.log(f"[on_message err] {e}")
            traceback.print_exc()

    # ═══════════════════════════════════════════
    #  HTTP 回源派发
    # ═══════════════════════════════════════════

    def dispatch_http(self, peer_uid, m, src='http'):
        bot = self.bot
        if str(peer_uid) == str(bot.my_uid) or str(m.get('from_uid') or '') == str(bot.my_uid):
            bot.log('[http] 跳过自己发的消息')
            return
        sender_str = str(peer_uid)
        from reply_bot import MASTER_MAP
        label = MASTER_MAP.get(sender_str)
        is_master = label is not None
        who_label = label if is_master else f"一位网友（抖音uid {sender_str}，不是你老大）"
        awe, item = m.get('awe'), (m.get('item_id') or '').strip()
        title = (m.get('title') or '').strip()

        if item and self.media.auto_browse_just_shared(item):
            bot.log(f'[http] itemId={item} auto_browse 刚分享过，跳过回显')
            return

        if (awe == 800 or (item and awe != 700)) and (item or title):
            bot.log(f'[http] 卡片 item={item} title={title[:40]}')
            content = {'itemId': item, 'content_title': title}
            if m.get('cover_url'):
                content['cover_url'] = {'url_list': [m['cover_url']]}
            if not item:
                self.media.handle_video_fallback(
                    int(sender_str) if sender_str.isdigit() else sender_str,
                    sender_str, is_master, who_label, title, m.get('cover_url'))
            else:
                self.media.handle_video_card(int(sender_str), sender_str, is_master, who_label, content)
            return

        text = (m.get('text') or '').strip()
        if not text:
            return
        if src == 'http':
            k = (sender_str, text[:80])
            seen = bot._ws_text_ts.get(k)
            if seen and time.time() - seen < 300:
                bot.log(f'[http] 跳过重复（WS 已回）：{text[:40]}')
                return
        bot.log(f'[http] 文本 from={sender_str} master={is_master} src={src} text={text[:60]}')
        if src == 'http' and not is_master and not bot._allow_stranger(sender_str):
            bot.log(f'[throttle] 网友 {sender_str} 发得太频繁，这条先不回（http）')
            return
        if is_master:
            self.handle_auto_cmd(text)
            self.handle_restart(text, sender_str)
            if self.handle_share_text(text, int(sender_str), sender_str, is_master, who_label):
                return
        reply = bot.generate_reply(text, sender_label=who_label, is_master=is_master, sender_id=sender_str)
        if is_master:
            bot._write_message('user', f'[抖音私信] {text}')
        if reply:
            self.handle_brain_result(sender_str, reply, is_master=is_master)
            rtext = reply[0] if reply[0] else ''
            if is_master and (random.random() < MEME_PROB or _wants_meme(text)):
                if '加载不出来' not in rtext and '没听清' not in rtext:
                    send_meme_async(f'{text} {rtext}')

    # ═══════════════════════════════════════════
    #  指令处理
    # ═══════════════════════════════════════════

    def handle_auto_cmd(self, text):
        bot = self.bot
        try:
            HERE = bot._HERE
            state_file = os.path.join(HERE, 'auto_state.json')
            s = {}
            if os.path.exists(state_file):
                try:
                    s = json.load(open(state_file, encoding='utf-8'))
                except Exception:
                    s = {}
            auto_py = os.path.join(HERE, 'auto_browse.py')
            low = text.lower().replace(' ', '')
            stop = ('别刷', '不要刷', '不准刷', '别看了', '别逛', '停', '睡觉', '够了', '休息')
            if any(w in low for w in stop):
                s['enabled'] = False
                s['pending'] = False
                s['until'] = 0
                s['until_note'] = ''
                s['unanswered_asks'] = 0
                json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                bot.log('[auto] 老大喊停自主刷')
                return
            if s.get('risk_wait'):
                if any(w in low for w in ('没解决', '还没', '都没有', '没搞定', '未解决', '还没好')):
                    bot.log('[auto] 老大说还没处理好，继续等风控解除')
                else:
                    solved = ('已解决', '解决了', '搞定', '处理好', '弄好', '通过了', '过了', '解除了')
                    if any(w in low for w in solved):
                        s['risk_wait'] = False
                        json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                        bot.log('[auto] 老大确认风控已解决，恢复自主刷')
                        try:
                            import risk_guard
                            risk_guard.mark_solved()
                        except Exception as e:
                            bot.log(f'[auto] risk_guard 复位失败: {e}')
                        return
            kill_words = ('我知道', '明白了', '好的呢', '知道了', '哦', '懂了', '哈哈哈', '哈哈')
            has_trigger = '刷' in low or re.search(r'(?:测试|试|继续|接着|一直|帮我|给我).*(?:到|至).*(?:点|小时|分钟)', low)
            if has_trigger and not any(w in low for w in kill_words):
                try:
                    from auto_browse import parse_until as _pu
                    until, note = _pu(low)
                    if until:
                        s['enabled'] = True
                        s['pending'] = False
                        s['last_browse'] = 0
                        s['next_browse_at'] = 0
                        s['until'] = until
                        s['until_note'] = note
                        s['unanswered_asks'] = 0
                        json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                        bot.log(f'[auto] 老大设定时长自主刷：{note}→截止 {until}')
                        try:
                            py = os.path.join(HERE, '.venv', 'Scripts', 'python.exe')
                            subprocess.Popen(
                                [py, auto_py],
                                cwd=HERE,
                                stdout=open(os.path.join(HERE, 'auto_browse_run.log'), 'a', encoding='utf-8'),
                                stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
                            )
                            bot.log('[auto] 已后台拉起 auto_browse.py')
                        except Exception as e:
                            bot.log(f'[auto] 拉起 auto_browse 失败（可能已在跑）: {e}')
                        return
                except Exception as e:
                    bot.log(f'[auto] 时长解析失败: {e}')
            if has_trigger and not s.get('pending') and not s.get('enabled'):
                s['enabled'] = True
                s['pending'] = False
                s['last_browse'] = 0
                s['next_browse_at'] = 0
                s['until'] = time.time() + 7200
                s['until_note'] = "刷两小时（默认）"
                s['unanswered_asks'] = 0
                json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                bot.log('[auto] 老大说刷但没给时长，默认两小时')
                try:
                    py = os.path.join(HERE, '.venv', 'Scripts', 'python.exe')
                    subprocess.Popen(
                        [py, auto_py],
                        cwd=HERE,
                        stdout=open(os.path.join(HERE, 'auto_browse_run.log'), 'a', encoding='utf-8'),
                        stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
                    )
                    bot.log('[auto] 已后台拉起 auto_browse.py（默认两小时）')
                except Exception as e:
                    bot.log(f'[auto] 拉起 auto_browse 失败（可能已在跑）: {e}')
                return
            if s.get('pending'):
                approve = ('可以', '好', '行', '准', '批准', '开刷', '去刷', '刷吧', 'ok', '好的')
                if any(w in low for w in approve):
                    s['enabled'] = True
                    s['pending'] = False
                    s['last_browse'] = 0
                    s['next_browse_at'] = 0
                    s['unanswered_asks'] = 0
                    json.dump(s, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False)
                    bot.log('[auto] 老大批准自主刷')
        except Exception as e:
            bot.log(f'[auto cmd err] {e}')

    def handle_restart(self, text, sender_str):
        bot = self.bot
        low = text.lower().replace(' ', '')
        if ('重启' in text) or ('restart' in low) or ('重新启动' in text):
            if ('成功' in text) or ('回来' in text):
                return
            bot.log(f"[restart] 老大 {sender_str} 触发重启")
            try:
                json.dump({'uid': sender_str, 'ts': time.time()},
                          open(bot._RESTART_NOTIFY_FILE, 'w', encoding='utf-8'))
            except Exception as e:
                bot.log(f"[restart flag err] {e}")
            os.execv(bot._HERE_PY, [bot._HERE_PY] + sys.argv)

    # ═══════════════════════════════════════════
    #  回复处理
    # ═══════════════════════════════════════════

    def handle_brain_result(self, sender_uid, result, is_master=False):
        bot = self.bot
        if not result:
            return False
        text, images = result
        ok = bot.send_reply(sender_uid, text) if text else False
        if images:
            from meme import send_meme_dm
            for img in images:
                try:
                    r = asyncio.run(send_meme_dm(str(img), headless=True))
                    bot.log(f"[brain img -> {sender_uid}] ok={r.get('ok')} {img}")
                except Exception as e:
                    bot.log(f"[brain img err] {e}")
        if ok and is_master:
            bot._write_message('assistant', f'[抖音私信回复] {text}')
        return ok

    # ═══════════════════════════════════════════
    #  媒体消息处理
    # ═══════════════════════════════════════════

    def handle_media(self, sender, sender_str, is_master, who_label, content, kind):
        bot = self.bot
        content_title = ''
        try:
            content_title = deep_get(content, ('content_title', 'content_name', 'title')) or ''
        except Exception:
            pass
        recent_desc = self.media.recent_video_for_media(sender_str, content_title)
        if recent_desc:
            bot.log(f"[media] {kind} 与刚看过的视频同标题，复用记忆：{recent_desc[:80]}")
            seen = True
            desc = recent_desc
            key = None
        else:
            url = _find_url(content)
            key = sticker_key(content, url)
            bot.log(f"[media] {kind} url={'有' if url else '无'} key={key or '无'}")
            desc = sticker_recall(key)
            seen = bool(desc)
            if seen:
                bot.log(f"[media] {kind} 记忆命中：{desc}")
            else:
                desc = describe_image_url(url) if url else None
        if not desc:
            if is_master:
                bot.send_reply(sender, f'你这{kind}我这边加载不出来呀，再发一次？')
            else:
                bot.log(f"[media] {kind} 描述失败，不回")
            return
        if not seen and key:
            sticker_remember(key, desc, kind)
            bot.log(f"[media] {kind} 内容：{desc}")
        text = f'[对方发来了一个{kind}，你看到的是：{desc}]'
        if seen:
            text += '（这个画面你刚才就见过，按同样的理解接话，别又问「这是谁」）'
        if is_master:
            bot._write_message('user', f'[抖音{kind}] {desc}')
        result = bot.generate_reply(text, sender_label=who_label, is_master=is_master, use_pro=False, sender_id=sender)
        if result:
            self.handle_brain_result(sender, result, is_master=is_master)

    # ═══════════════════════════════════════════
    #  分享链接处理
    # ═══════════════════════════════════════════

    def handle_share_text(self, text, sender, sender_str, is_master, who_label):
        bot = self.bot
        url, aid = _extract_douyin_url(text)
        if not url:
            return False
        bot.log(f'[share] 检测到视频链接 {url}')
        if not aid:
            aid = _resolve_aweme_id(url)
        if not aid:
            bot.send_reply(sender, '这个链接我打不开呀，把视频链接完整发我一下？')
            return True
        self.media.handle_video(sender, sender_str, is_master, who_label, {'itemId': aid})
        return True