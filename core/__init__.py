# -*- coding: utf-8 -*-
"""汐缘 抖音机器人 — 核心模块。

模块结构（每个模块独立可测，无循环依赖）：

- core/tools.py      : 纯工具/智能体函数（log / 表情包记忆 / 媒体描述 / 人格加载）
- core/memory.py     : 跨聊记忆（抖音 ↔ QQ 长期记忆 + 上下文组装）
- core/router.py     : 消息路由（MessageRouter 协作者 — WS 派发 / HTTP 回源 / 指令处理）
- core/media.py      : 媒体处理（视频/图片下载、抽帧、VLM/STT 描述、去重记忆）
- core/poll.py       : 轮询兜底 + 看门狗（WS 断线 HTTP 兜底、静默检测、评论通知）
"""

from core.memory import MemoryStore
from core.router import MessageRouter

__all__ = ['MemoryStore', 'MessageRouter']