# -*- coding: utf-8 -*-
"""表情包：从 AstrBot 的 meme_manager 表情库里挑一张图，发到抖音私信。

表情库结构（AstrBot 侧）：
    <packs>/<pack_id>/memes/<分类>/<图片>
图片格式 .jpg/.jpeg/.png/.gif/.webp —— 正好都在抖音网页私信允许的类型里。

CLI:
    python meme.py --query "被夸了 开心"          # 只挑一张，打印路径
    python meme.py --query "无语" --send          # 挑一张并发到抖音私信（老大）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

PACKS_ROOT = Path(__file__).parent.parent / 'astrbot' / 'data' / 'plugin_data' / 'meme_manager' / 'packs'
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# 情绪关键词 → 分类（分类名来自库里的目录名；只分情感，不再有"鲸鱼"来源分类）
CATEGORY_KEYS: dict[str, tuple[str, ...]] = {
    "委屈": ("委屈", "难过", "伤心", "哭", "可怜", "示弱", "被凶"),
    "嫌弃": ("嫌弃", "无语", "吐槽", "哼", "气", "臭", "滚", "别烦"),
    "开心": ("开心", "高兴", "好耶", "得意", "棒", "喜欢", "嘿嘿", "哈哈", "赞"),
    "撒娇": ("撒娇", "抱抱", "贴贴", "想你", "黏", "害羞", "亲亲", "陪"),
    "困": ("困", "睡", "晚安", "熬夜", "累", "困死"),
    "打工": ("打工", "上班", "加班", "debug", "写代码", "工作", "干活"),
    "摸鱼": ("摸鱼", "偷懒", "吃", "喝", "下班", "躺平"),
}

# 没匹配到情绪时的兜底偏好（越靠前越优先）
FALLBACK_ORDER = ("开心", "撒娇", "摸鱼")

# 发送时让 flash 判可爱，最多试几张（全不可爱就回退第一张）
CUTE_TRIES = 3


def categories() -> dict[str, list[Path]]:
    """扫描所有表情包，返回 {分类: [图片路径]}。"""
    out: dict[str, list[Path]] = {}
    if not PACKS_ROOT.is_dir():
        return out
    for pack in PACKS_ROOT.iterdir():
        memes_dir = pack / "memes"
        if not memes_dir.is_dir():
            continue
        for cat in memes_dir.iterdir():
            if not cat.is_dir():
                continue
            files = [f for f in cat.iterdir() if f.suffix.lower() in IMAGE_EXTS]
            if files:
                out.setdefault(cat.name, []).extend(files)
    return out


def _ranked_candidates(query: str | None = None, category: str | None = None) -> list[Path]:
    """按一句话给候选图排序（最高分分类在前，其次兜底），返回候选列表。"""
    cats = categories()
    if not cats:
        return []
    if category and category in cats:
        return list(cats[category])

    q = query or ""
    scores: dict[str, int] = {}
    for cat, keys in CATEGORY_KEYS.items():
        if cat not in cats:
            continue
        hit = sum(1 for k in keys if k in q)
        if hit:
            scores[cat] = hit
    if scores:
        best = max(scores.values())
        cands = [c for c, v in scores.items() if v == best]
        return list(cats[random.choice(cands)])

    for name in FALLBACK_ORDER:
        if name in cats:
            return list(cats[name])
    return list(cats[random.choice(list(cats))])


def _is_cute(path: Path) -> bool:
    """让 flash 判这张图可爱不可爱；判不了（None/异常）就当可爱处理。"""
    try:
        from vlm_emotion import is_cute_image

        r = is_cute_image(path.read_bytes(), path.suffix.lstrip(".").lower())
        return r is not False
    except Exception:
        return True


def pick_meme(query: str | None = None, category: str | None = None, cute: bool = False) -> Path | None:
    """按一句话挑一张图；挑不到返回 None。

    cute=True 时先让 flash 判可爱：不可爱换一张再判，最多试 CUTE_TRIES 张，
    全不可爱就回退到第一张（兜底不空发）。
    """
    cands = _ranked_candidates(query, category)
    if not cands:
        return None
    random.shuffle(cands)
    if not cute:
        return cands[0]
    for p in cands[:CUTE_TRIES]:
        if _is_cute(p):
            return p
    return cands[0]


def describe() -> str:
    """给提示词用的分类清单。"""
    cats = categories()
    return "、".join(f"{c}({len(v)}张)" for c, v in sorted(cats.items()))


async def send_meme_dm(image_path: str | Path, headless: bool = True, conv_name: str | None = None) -> dict:
    """把一张图发到抖音私信（复用 send_voice_dm 的上传链路：同一个 input[type=file]）。"""
    from send_voice import send_voice_dm  # 同一个文件上传链路，不限 mp4

    return await send_voice_dm(str(image_path), headless=headless, conv_name=conv_name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default=None)
    ap.add_argument("--category", default=None)
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.list:
        print(describe())
        return
    picked = pick_meme(args.query, args.category, cute=args.send)
    if picked is None:
        print(json.dumps({"ok": False, "error": "表情库为空"}, ensure_ascii=False))
        return
    if not args.send:
        print(json.dumps({"ok": True, "path": str(picked)}, ensure_ascii=False))
        return
    r = asyncio.run(send_meme_dm(picked, headless=args.headless))
    print(json.dumps({**r, "picked": str(picked)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
