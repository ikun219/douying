# -*- coding: utf-8 -*-
"""记忆模块 — 抖音私信 ↔ QQ 长期记忆互通 + 上下文组装。

提供：
- MemoryStore: 独立的记忆读写类，无 ReplyBot 依赖，可单独测试。
"""

import json
import os
import re
import time


class MemoryStore:
    """跨聊记忆：抖音私信往来 → sidecar 文件 → AstrBot livingmemory 同步。

    用法:
        mem = MemoryStore(douyin_memory_file, livingmemory_db, qq_session, log_fn)
        mem.write_message('user', '你好')
        qq_context = mem.read_recent_qq(12)
        video_context = mem.recent_video_lines(3, 72)
    """

    def __init__(self, memory_file, livingmemory_db, qq_session, log_fn=None):
        self.memory_file = memory_file          # DOUYIN_MEMORY_FILE
        self.livingmemory_db = livingmemory_db  # LIVINGMEMORY_DB (read-only SQLite)
        self.qq_session = qq_session            # QQ_SESSION
        self.log = log_fn or (lambda msg: None) # log()

    # ---- 写入 sidecar ----
    def write_message(self, role, content):
        """把抖音私信往来写到 sidecar 文件；AstrBot 的 douyin_bridge 插件会 ingest 进 conversations.db。

        （livingmemory 目录对子进程只读，直接写库会 readonly，所以走 sidecar。）
        """
        try:
            rec = {
                'role': role,
                'content': content,
                'sender': '潮汐' if role == 'user' else '汐缘',
                'ts': time.time(),
            }
            with open(self.memory_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except Exception as e:
            self.log(f"[mem write err] {e}")

    # ---- 读 QQ 记录 ----
    def read_recent_qq(self, n=12):
        """读最近 n 条 QQ 聊天记录，拼成对话摘录注入 DeepSeek 上下文。"""
        try:
            import sqlite3
            con = sqlite3.connect(f'file:{self.livingmemory_db}?mode=ro', uri=True)
            rows = con.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (self.qq_session, n),
            ).fetchall()
            con.close()
            rows = list(reversed(rows))
            lines = []
            for role, content in rows:
                name = '老大' if role == 'user' else '汐缘'
                c = ' '.join((content or '').split())[:120]
                lines.append(f"{name}: {c}")
            return '\n'.join(lines)
        except Exception as e:
            self.log(f"[mem read err] {e}")
            return ''

    # ---- 最近视频记忆 ----
    def recent_video_lines(self, n=3, hours=72):
        """从 douyin_memory.jsonl 捞出老大最近发来的视频/分享，拼成给 LLM 看的清单。

        没有这段注入时，间隔几小时后她会一口咬定「你没发给我」。
        """
        rows = []
        try:
            with open(self.memory_file, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get('role') != 'user':
                        continue
                    c = (r.get('content') or '').strip()
                    if not c.startswith(('[抖音视频', '[抖音分享', '[抖音截图')):
                        continue
                    if c[:14].find('视频封面') >= 0:  # 封面兜底那条信息少，有正式记录时别重复
                        continue
                    rows.append((float(r.get('ts') or 0), c))
        except Exception as e:
            self.log(f'[video-mem err] {e}')
            return ''
        now = time.time()
        pick = [(t, c) for t, c in rows if 0 < now - t <= hours * 3600][-n:]
        if not pick:
            return ''
        # 去重：同一视频（标题/aweme_id 相同）只保留最新一条
        seen = set()
        dedup = []
        for t, c in reversed(pick):
            m = re.search(r'aweme_id[：:]s*(d+)', c)
            key = m.group(1) if m else c[:60]
            if key not in seen:
                seen.add(key)
                dedup.append((t, c))
        dedup.reverse()
        out = []
        for t, c in dedup:
            mins = (now - t) / 60.0
            ago = f'{mins:.0f} 分钟前' if mins < 90 else f'{mins / 60:.0f} 小时前'
            out.append(f'- {ago}：{c[:240]}')
        return chr(10).join(out)

    # ---- 上下文组装 ----
    def build_context(self, is_master, persona, mood_line, sender_label=''):
        """组装注入 LLM 的完整上下文：人设 + 最近 QQ 聊天 + 视频记忆 + 情绪权重。

        Returns:
            (system_prompt: str, video_context: str)  — 调用方拼到 messages 里。
        """
        system_parts = [persona]
        if mood_line:
            system_parts.append(f'[当前情绪基调]\n{mood_line}')
        system_prompt = '\n'.join(system_parts)

        video_context = ''
        if is_master:
            video_context = self.recent_video_lines(3, 72)
        return system_prompt, video_context