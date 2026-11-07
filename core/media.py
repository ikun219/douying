# -*- coding: utf-8 -*-
"""媒体处理模块 — 视频/图片下载、抽帧、拼接、VLM 描述、去重记忆。

从 MessageRouter 拆出，通过 bot 协作者访问 ReplyBot 状态。
"""

import json
import os
import re
import time
import random
import tempfile
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import requests as _requests

from core.ml_util import describe_image_url, PROMPT_VIDEO_COVER, _stt_bytes


# ═══════════════════════════════════════════
#  共享工具函数（router 也会用）
# ═══════════════════════════════════════════

def deep_get(obj, keys, depth=0):
    """递归在 dict/list 中按 keys 查找第一个非空值。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                return v
        for v in obj.values():
            r = deep_get(v, keys, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_get(v, keys, depth + 1)
            if r is not None:
                return r
    return None


def first_cover(obj, depth=0):
    """递归找第一张封面图 URL。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in ('cover_url', 'cover', 'origin_cover', 'dynamic_cover'):
            node = obj.get(k)
            if isinstance(node, dict):
                urls = node.get('url_list') or []
                if urls:
                    return urls[0]
        for v in obj.values():
            r = first_cover(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = first_cover(v, depth + 1)
            if r:
                return r
    return None


def norm_kw(s):
    """文本归一化：小写 + 只保留字母数字和中文。"""
    if not s:
        return ''
    return ''.join(ch for ch in str(s).lower() if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')


# ═══════════════════════════════════════════
#  MediaHandler
# ═══════════════════════════════════════════

class MediaHandler:
    """视频/图片媒体处理：下载 → 抽帧 → VLM/STT → 回复。

    通过 bot 协作者访问 ReplyBot 的全部状态。
    """

    def __init__(self, bot):
        self.bot = bot

    # ── 视频入口 ─────────────────────────────

    def handle_video(self, sender, sender_str, is_master, who_label, content):
        bot = self.bot
        try:
            from send_text import _KA, send_text_sync
            _KA.mark_read(sender_str)
        except Exception as e:
            bot.log(f'[video] mark_read 跳过: {e}')
        aid = str(content.get('itemId') or content.get('item_id') or '').strip()
        title = str(content.get('content_title') or '').strip()
        author = str(content.get('content_name') or '').strip()
        dur = 0
        play_url = ''
        images = []
        cover = None
        try:
            from core.ml_util import _video_cover_url
            cover = _video_cover_url(content)
        except Exception:
            pass
        _embedded_images = content.get('images')
        if isinstance(_embedded_images, list) and _embedded_images:
            images = _embedded_images
            bot.log(f"[video] content 自带 {len(images)} 张图")
        if aid:
            now = time.time()
            last = bot._video_dedup.get(aid)
            if last and now - last < 180:
                bot.log(f'[video] 去重：{aid} 3 分钟内已处理过，不再重复下载')
                if is_master:
                    bot.send_reply(sender, '这个刚看过啦~过几分钟再发一遍我才重新看哦')
                return
            bot._video_dedup[aid] = now
            if len(bot._video_dedup) > 200:
                bot._video_dedup = {k: v for k, v in bot._video_dedup.items() if now - v < 1800}
            NUDGE_TIPS = [
                '正在认真看这个视频哦~稍等一下下꒰ᐢ⸝⸝•༝•⸝⸝ᐢ꒱',
                '视频收到了！让我看一遍再来跟你聊~',
            ]
            f_ping = threading.Thread(
                target=send_text_sync,
                args=(random.choice(NUDGE_TIPS), sender_str),
                daemon=True,
            )
            f_ping.start()
            try:
                from get_video_info import get_video_info
                info = get_video_info(aid)
                title = title or (info.get('title') or '').strip()
                author = author or (info.get('author') or '').strip()
                dur = int(info.get('duration') or 0)
                play_url = play_url or (info.get('play_url') or '')
                images = images or (info.get('images') or [])
                cover = info.get('cover') or cover
            except Exception as e:
                bot.log(f'[video] 取详情失败 {aid} {e}')

        desc, speech = None, None
        had_video = bool(play_url)
        if play_url:
            desc, speech = self.describe_video(play_url, dur, sender_str, is_master)
        elif images:
            desc = self.describe_images(images)
        if not desc and not had_video and not images and cover:
            desc = describe_image_url(
                cover,
                prompt=(
                    '这是别人分享给你的作品的第一张图（可能是视频封面或图文首图）。按一行回答，不要客套，不超过50字：'
                    '画面：（谁/什么、在做什么、场景，图上的文字原样写出）；'
                    '风格：（真人/AI生成/动画/游戏，选一个）。'
                ),
            )

        parts = []
        if title:
            parts.append(f'标题：{title}')
        if author:
            parts.append(f'作者：{author}')
        if dur:
            parts.append(f'时长：{dur / 1000:.1f} 秒')
        if desc:
            parts.append(f'画面：{desc}')
        if speech:
            parts.append(f'里面的声音/台词：{speech}')
        if had_video and not desc and not speech:
            parts.append('画面：（视频画面没加载出来，没能看成）')
        if not parts:
            if is_master:
                bot.send_reply(sender, '这个视频我这边加载不出来呀，再发一次？')
            return

        bot.log('[video] 看懂：' + ' | '.join(parts))
        self.remember_video_for_media(sender_str, title, parts)
        if is_master:
            bot._write_message('user', '[抖音视频] ' + ' | '.join(parts))
        text = '[对方给你发来一个抖音视频，你看到/听到的是：\n' + '\n'.join(parts) + '\n]'
        reply = bot.generate_reply(text, sender_label=who_label, is_master=is_master, use_pro=False, sender_id=sender)
        if reply:
            bot.router.handle_brain_result(sender, reply, is_master=is_master)

    def handle_video_fallback(self, sender, sender_str, is_master, who_label, title, cover):
        bot = self.bot
        parts = []
        if title:
            parts.append(f'标题：{title}')
        desc = None
        if cover:
            desc = describe_image_url(cover, prompt=PROMPT_VIDEO_COVER)
        if desc:
            parts.append(f'画面：{desc}')
        if not parts:
            if is_master:
                bot.send_reply(sender, '这个视频我这边只看到个空壳，没解析出内容，跟我说说是什么？')
            return
        bot.log('[video] 兜底看懂：' + ' | '.join(parts))
        self.remember_video_for_media(sender_str, title, parts)
        text = '[对方给你发来一个抖音视频（拿不到播放地址，只看到封面和标题）：\n' + '\n'.join(parts) + '\n]'
        if is_master:
            bot._write_message('user', '[抖音视频] ' + ' | '.join(parts))
        reply = bot.generate_reply(text, sender_label=who_label, is_master=is_master, use_pro=False, sender_id=sender)
        if reply:
            bot.router.handle_brain_result(sender, reply, is_master=is_master)

    def handle_video_card(self, sender, sender_str, is_master, who_label, content):
        item_id = deep_get(content, ('itemId', 'item_id', 'aweme_id', 'awemeId'))
        title = (deep_get(content, ('content_title', 'content_name', 'title')) or '')
        cover = first_cover(content)
        if item_id:
            merged = dict(content)
            merged['itemId'] = str(item_id)
            self.handle_video(sender, sender_str, is_master, who_label, merged)
        elif not content:
            self.bot.log(f'[video] type=27 空内容帧，尝试 HTTP 回源恢复 itemId')
            if self.rescue_video_card(sender, sender_str, is_master, who_label):
                return
            self.handle_video_fallback(sender, sender_str, is_master, who_label, str(title), cover)
        else:
            self.bot.log(f'[video] type=27 无 itemId，走封面兜底 title={title!r} cover={bool(cover)}')
            self.handle_video_fallback(sender, sender_str, is_master, who_label, str(title), cover)

    def rescue_video_card(self, sender, sender_str, is_master, who_label):
        import im_http
        bot = self.bot
        waited = False
        for attempt in range(4):
            try:
                data = im_http.fetch(bot.my_uid, limit=25)
            except Exception as e:
                bot.log(f'[video] HTTP 回源 fetch 异常(第{attempt}轮): {e}')
                if not waited:
                    time.sleep(0.8); waited = True
                continue
            convs = data.get('conversations') or []
            peer_msgs = None
            if attempt == 0:
                my_ret = data.get('my_uid') or ''
                peers_all = [(c.get('peer_uid') or '', len(c.get('messages') or []), c.get('type')) for c in convs]
                bot.log(f'[video] 回源诊断 我用my_uid={bot.my_uid!r} fetch返回my_uid={my_ret!r} 目标peer=<{sender}> peers={peers_all}')
            for conv in convs:
                peer = conv.get('peer_uid') or ''
                if peer == str(sender):
                    peer_msgs = conv.get('messages') or []
                    break
            if peer_msgs:
                cands = []
                for m in reversed(peer_msgs):
                    if m.get('from_me'):
                        continue
                    awe = m.get('awe') or 0
                    if m.get('item_id') and (awe == 800 or (awe and awe != 700)):
                        cands.append(f'{m.get("item_id")}@ts{m.get("ts")}')
                if cands:
                    bot.log(f'[video] HTTP 回源(第{attempt}轮) 见 {len(cands)} 张候选卡: {", ".join(cands[:4])}')
                for m in reversed(peer_msgs):
                    if m.get('from_me'):
                        continue
                    awe = m.get('awe') or 0
                    item = m.get('item_id') or ''
                    if item and (awe == 800 or (awe and awe != 700)):
                        bot.log(f'[video] HTTP 回源恢复空帧：peer={peer} itemId={item} title={m.get("title","")!r}')
                        merged = {
                            'itemId': str(item),
                            'content_title': m.get('title') or '',
                        }
                        cover = m.get('cover_url') or ''
                        if cover:
                            merged['cover_url'] = {'url_list': [cover]}
                        self.handle_video(sender, sender_str, is_master, who_label, merged)
                        return True
                if not waited and attempt < 3:
                    bot.log(f'[video] HTTP 回源第{attempt}轮尚未见到卡片，稍等重试…')
                    time.sleep(0.8); waited = True
        bot.log(f'[video] HTTP 回源未找到 peer={sender} 的视频卡（4 轮未命中）')
        return False

    # ── 图片/视频理解 ─────────────────────────

    def describe_images(self, images):
        bot = self.bot
        ff = os.environ.get('FFMPEG') or 'ffmpeg'
        tmp = tempfile.mkdtemp(prefix='dyimg_', dir=tempfile.gettempdir())
        files = []
        try:
            for i, u in enumerate((images or [])[:9]):
                if not u:
                    continue
                jpg = os.path.join(tmp, f'i{i}.jpg')
                try:
                    r = _requests.get(
                        u,
                        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.douyin.com/'},
                        timeout=30,
                    )
                    if r.status_code != 200 or not r.content:
                        continue
                    with open(jpg, 'wb') as f:
                        f.write(r.content)
                    scaled = os.path.join(tmp, f's{i}.jpg')
                    subprocess.run([ff, '-y', '-loglevel', 'error', '-i', jpg, '-vf', 'scale=320:-2', scaled],
                                   timeout=60, check=False)
                    if os.path.exists(scaled) and os.path.getsize(scaled) > 0:
                        files.append(scaled)
                except Exception as e:
                    bot.log(f'[image] 第{i + 1}张下载失败 {e}')
            if not files:
                return None
            sheet = self.sheet(files, ff, tmp)
            bot.log(f'[image] 图文共 {len(files)} 张，拼图完成')
            return describe_image_url(
                sheet,
                prompt=(
                    f'这是一组图文作品里的 {len(files)} 张图片（按发布顺序，从左到右、从上到下）。'
                    '按一行回答，不超过70字：内容：（讲了什么、画面里有什么，图上的文字原样写出关键部分）；'
                    '风格：（真人/AI生成/动画/游戏/截图，选一个）。'
                ),
            )
        except Exception as e:
            bot.log(f'[image] 处理失败 {e}')
            return None
        finally:
            try:
                for name in os.listdir(tmp):
                    os.remove(os.path.join(tmp, name))
                os.rmdir(tmp)
            except Exception:
                pass

    def describe_video(self, play_url, dur_ms, peer_uid=None, is_master=False):
        bot = self.bot
        ff = os.environ.get('FFMPEG') or 'ffmpeg'
        tmp = tempfile.mkdtemp(prefix='dyvid_', dir=tempfile.gettempdir())
        mp4 = os.path.join(tmp, 'v.mp4')
        desc = speech = None
        try:
            r = _requests.get(
                play_url,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.douyin.com/'},
                timeout=90,
                stream=True,
            )
            got = 0
            dur_s = max(1.0, (dur_ms or 0) / 1000.0)
            if dur_s <= 30:
                n_frames = 3
            elif dur_s <= 120:
                n_frames = 4
            else:
                n_frames = 5
            positions = [0.08 + (0.84 * i / (n_frames - 1)) for i in range(n_frames)]

            xpool = ThreadPoolExecutor(max_workers=max(3, 1 + n_frames))
            f_dl = xpool.submit(_download_to, r, mp4, tmp)
            try:
                got = f_dl.result(timeout=90)
            except Exception as e:
                bot.log(f'[video] 下载失败: {e}')
                xpool.shutdown(wait=False)
                return desc, speech
            bot.log(f'[video] 下载 {got / 1024 / 1024:.1f} MB')

            f_frames = {}
            for i, p in enumerate(positions):
                f_frames[i] = xpool.submit(
                    _extract_one_frame,
                    ff, mp4, os.path.join(tmp, f'f{i}.jpg'),
                    dur_s * p,
                )
            audio = os.path.join(tmp, 'a.mp3')
            f_audio = xpool.submit(_extract_audio, ff, mp4, audio)

            frames = []
            for i in range(n_frames):
                try:
                    jpg = f_frames[i].result(timeout=60)
                    if jpg and os.path.exists(jpg) and os.path.getsize(jpg) > 0:
                        frames.append(jpg)
                except Exception:
                    pass
            bot.log(f'[video] 抽帧 {len(frames)}/{n_frames}（时长 {dur_s:.1f}s）')

            has_audio = False
            try:
                f_audio.result(timeout=60)
                has_audio = os.path.exists(audio) and os.path.getsize(audio) > 0
            except Exception:
                pass
            if has_audio:
                bot.log(f'[video] 音频 {os.path.getsize(audio) / 1024:.1f} KB')

            sheet = self.sheet(frames, ff, tmp)

            f_vlm = None
            f_asr = None
            with ThreadPoolExecutor(max_workers=2) as ex:
                if sheet:
                    f_vlm = ex.submit(
                        describe_image_url,
                        sheet,
                        prompt=(
                            f'这是从一个短视频里按时间顺序抽出来的 {len(frames)} 帧画面'
                            '（从左到右、从上到下按时间排列）。按一行回答，不超过70字：'
                            '画面：（谁/什么、在做什么、场景怎么变化，图上的文字原样写出）；'
                            '风格：（真人/AI生成/动画/游戏，选一个）。'
                        ),
                    )
                if has_audio:
                    f_asr = ex.submit(_stt_bytes, open(audio, 'rb').read(), 'a.mp3', 'audio/mpeg')

                if f_vlm:
                    desc = f_vlm.result(timeout=60)
                if f_asr:
                    speech = f_asr.result(timeout=60)

            if speech:
                if len(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]', speech)) < 4:
                    bot.log(f'[video] STT 结果像噪声，丢弃：{speech[:40]}')
                    speech = None
                elif len(speech) > 200:
                    speech = speech[:200] + '…'
                    bot.log(f'[video] ASR 截断至 200 字')
        except Exception as e:
            bot.log(f'[video] 处理失败：{e}')
        finally:
            try:
                for name in os.listdir(tmp):
                    os.remove(os.path.join(tmp, name))
                os.rmdir(tmp)
            except Exception:
                pass
        return desc, speech

    # ── 帧/音频工具 ──────────────────────────

    def sheet(self, frames, ff, tmp):
        if not frames:
            return None
        if len(frames) == 1:
            return frames[0]

        W, H = 320, 240
        slots = 3

        def norm(idx, path):
            out = os.path.join(tmp, f'norm{idx}.jpg')
            vf = (f'scale={W}:{H}:force_original_aspect_ratio=decrease,'
                  f'pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black')
            subprocess.run(
                [ff, '-y', '-loglevel', 'error', '-i', path, '-vf', vf, out],
                timeout=60, check=False,
            )
            return out if os.path.exists(out) and os.path.getsize(out) > 0 else None

        normed = []
        for i, p in enumerate(frames):
            np_ = norm(i, p)
            if np_:
                normed.append(np_)
        if len(normed) == 1:
            return normed[0]
        if not normed:
            return None

        rows = [normed[i:i + slots] for i in range(0, len(normed), slots)]
        row_imgs = []
        for ri, row in enumerate(rows):
            out = os.path.join(tmp, f'row{ri}.jpg')
            args = [ff, '-y', '-loglevel', 'error']
            for p in row:
                args += ['-i', p]
            if len(row) > 1:
                fc = (''.join(f'[{i}:v]' for i in range(len(row)))
                      + f'hstack=inputs={len(row)},pad={slots * W}:{H}:0:0:color=black')
            else:
                fc = f'[0:v]pad={slots * W}:{H}:0:0:color=black'
            args += ['-filter_complex', fc, out]
            subprocess.run(args, timeout=90, check=False)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                row_imgs.append(out)

        if not row_imgs:
            return normed[0]
        if len(row_imgs) == 1:
            return row_imgs[0]

        sheet_path = os.path.join(tmp, 'sheet.jpg')
        args = [ff, '-y', '-loglevel', 'error']
        for p in row_imgs:
            args += ['-i', p]
        fc = ''.join(f'[{i}:v]' for i in range(len(row_imgs))) + f'vstack=inputs={len(row_imgs)}'
        args += ['-filter_complex', fc, sheet_path]
        subprocess.run(args, timeout=90, check=False)
        if os.path.exists(sheet_path) and os.path.getsize(sheet_path) > 0:
            return sheet_path
        return row_imgs[0]

    # ── 视频记忆 ─────────────────────────────

    def auto_browse_just_shared(self, aweme_id):
        bot = self.bot
        aid = str(aweme_id)
        cutoff = time.time() - 300
        try:
            with open(bot._shared_watched, encoding='utf-8') as f:
                for line in reversed(list(f)):
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    ts = float(r.get('ts') or 0)
                    if ts < cutoff:
                        break
                    if r.get('action') == 'shared' and str(r.get('aweme_id')) == aid:
                        return True
        except FileNotFoundError:
            pass
        except Exception as e:
            bot.log(f'[shared-check err] {e}')
        return False

    def remember_video_for_media(self, sender_str, title, parts):
        bot = self.bot
        try:
            if not sender_str:
                return
            joined = ' | '.join(str(p) for p in (parts or []))
            bot._recent_videos[sender_str] = {
                'title': title or '',
                'title_norm': norm_kw(title),
                'desc': joined,
                'ts': time.time(),
            }
            if len(bot._recent_videos) > 100:
                cut = time.time() - 600
                bot._recent_videos = {k: v for k, v in bot._recent_videos.items() if v['ts'] > cut}
            bot.log(f'[meme-mem] 记录刚看的视频 key={norm_kw(title)[:24]}')
        except Exception as e:
            bot.log(f'[meme-mem] 记录视频失败: {e}')

    def recent_video_for_media(self, sender_str, content_title):
        bot = self.bot
        try:
            rec = bot._recent_videos.get(sender_str)
            if not rec:
                return None
            if time.time() - rec['ts'] > 120:
                return None
            cur = norm_kw(content_title) or ''
            if not rec['title_norm'] or not cur:
                return None
            if rec['title_norm'] == cur or (
                len(rec['title_norm']) >= 8 and len(cur) >= 8
                and (rec['title_norm'] in cur or cur in rec['title_norm'])
            ):
                return rec['desc']
            return None
        except Exception as e:
            bot.log(f'[meme-mem] 匹配视频失败: {e}')
            return None


# ═══════════════════════════════════════════
#  模块级下载/抽帧工具（纯函数）
# ═══════════════════════════════════════════

def _download_to(resp, path, tmp_dir, cap=80 * 1024 * 1024):
    got = 0
    with open(path, 'wb') as f:
        for chunk in resp.iter_content(1 << 16):
            got += len(chunk)
            if got > cap:
                raise RuntimeError(f'视频超过 {cap >> 20}MB，放弃下载')
            f.write(chunk)
    return got


def _extract_one_frame(ff, mp4, out_jpg, seek_sec):
    subprocess.run(
        [ff, '-y', '-loglevel', 'error', '-ss', f'{seek_sec:.2f}', '-i', mp4,
         '-frames:v', '1', '-vf', 'scale=320:-2', out_jpg],
        timeout=60,
        check=False,
    )
    return out_jpg


def _extract_audio(ff, mp4, out_audio, max_sec=90):
    subprocess.run(
        [ff, '-y', '-loglevel', 'error', '-i', mp4, '-vn',
         '-ac', '1', '-ar', '16000', '-b:a', '32k', '-t', str(max_sec), out_audio],
        timeout=60,
        check=False,
    )
    return out_audio