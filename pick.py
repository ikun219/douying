# -*- coding: utf-8 -*-
"""看片分层筛选（硬过滤 → 互动比值打分 → 交给 LLM 做语义判断）。

借用 NEWJO123/dsh-douyin-scroll 的判断层设计，改成适合汐缘的版本：
  第0层 硬过滤：没文案 / 时长<12s / 文案短且评论少 —— 直接丢，省 token
  第1层 打分：互动绝对值 + **比值**（收藏/赞、转/赞、评/赞），播放量不参与
  第2层 语义：只把「证据包」交给 LLM，并要求它引用具体证据
  第3层 不确定就不推

用法：
  python pick.py            # 拉一轮 feed，打印排序结果（不调用 LLM）
  python pick.py --dump     # 额外打印被硬过滤丢掉的条数与原因
"""

from __future__ import annotations

import json
import time

# ---- 阈值（可调） ----
MIN_DURATION_MS = 12_000     # 少于 12s 多半是模板/广告
MIN_DESC_LEN = 12            # 文案太短…
MIN_COMMENTS = 3             # …且评论太少 → 两条同时成立才丢
FRESH_HOURS = 72             # 72 小时内加权

SCORE_W = {"digg": 1, "comment": 2, "collect": 3, "share": 2}


def _num(v):
    try:
        return int(v or 0)
    except Exception:
        return 0


def enrich(v: dict, now: float | None = None) -> dict:
    """给一条视频补上比值 / 时长秒 / 年龄小时 / 打分。"""
    now = now or time.time()
    d = _num(v.get("digg_count"))
    c = _num(v.get("comment_count"))
    col = _num(v.get("collect_count"))
    sh = _num(v.get("share_count"))
    dur_ms = _num(v.get("duration_ms"))
    ct = _num(v.get("create_time"))
    age_h = round((now - ct) / 3600.0, 1) if ct else 999.0

    score = (
        d * SCORE_W["digg"]
        + c * SCORE_W["comment"]
        + col * SCORE_W["collect"]
        + sh * SCORE_W["share"]
    )
    ratio_cd = (c / d) if d else 0.0     # 评论/点赞：越高越"有话说"，过高多是争议
    ratio_col = (col / d) if d else 0.0  # 收藏/点赞：有保存价值
    ratio_sh = (sh / d) if d else 0.0    # 转发/点赞：想转给别人

    why = []
    if 0.02 <= ratio_cd <= 0.25:
        score = int(score * 1.25)
        why.append("评论比健康")
    if ratio_col >= 0.15:
        score = int(score * 1.15)
        why.append("收藏率高(值得存)")
    if ratio_sh >= 0.10:
        score = int(score * 1.10)
        why.append("转发率高(想给人看)")
    if age_h <= FRESH_HOURS:
        score = int(score * 1.15)
        why.append("新鲜")

    out = dict(v)
    out.update(
        {
            "digg": d,
            "comment": c,
            "collect": col,
            "share": sh,
            "duration_s": round(dur_ms / 1000.0, 1) if dur_ms else None,
            "age_h": age_h,
            "ratio_comment": round(ratio_cd, 4),
            "ratio_collect": round(ratio_col, 4),
            "ratio_share": round(ratio_sh, 4),
            "score": int(score),
            "why": why,
        }
    )
    return out


def hard_filter(videos: list[dict]) -> tuple[list[dict], dict]:
    """第0层：丢掉明显不值得看的，返回 (留下的, 丢弃统计)。"""
    dropped = {"no_desc": 0, "too_short": 0, "short_and_quiet": 0}
    kept = []
    for v in videos:
        desc = (v.get("desc") or "").strip()
        if not desc:
            dropped["no_desc"] += 1
            continue
        dur = _num(v.get("duration_ms"))
        if dur and dur < MIN_DURATION_MS:
            dropped["too_short"] += 1
            continue
        c = _num(v.get("comment_count"))
        # 两条同时成立才丢：文案短 且 评论少（单条不满足只降权，不丢）
        if len(desc) < MIN_DESC_LEN and c < MIN_COMMENTS:
            dropped["short_and_quiet"] += 1
            continue
        kept.append(v)
    return kept, dropped


def rank(videos: list[dict], limit: int = 6, dump: bool = False) -> list[dict]:
    """硬过滤 + 打分排序，返回前 limit 条（带 score/why 等证据字段）。"""
    kept, dropped = hard_filter(videos)
    now = time.time()
    rows = [enrich(v, now) for v in kept]
    rows.sort(key=lambda r: -r["score"])
    if dump:
        print(f"[pick] 硬过滤丢弃 {json.dumps(dropped, ensure_ascii=False)}"
              f"（阈值 文案≥{MIN_DESC_LEN} 字 且 评论≥{MIN_COMMENTS}）")
    return rows[: max(1, limit)]


def evidence(v: dict, i: int) -> dict:
    """给 LLM 的压缩证据包（只放判断需要的字段，不放封面/URL 这种烧 token 的）。"""
    return {
        "i": i,
        "aweme_id": v.get("aweme_id"),
        "desc": (v.get("desc") or "")[:120],
        "author": v.get("author", ""),
        "duration_s": v.get("duration_s"),
        "age_h": v.get("age_h"),
        "digg": v.get("digg"),
        "comment": v.get("comment"),
        "collect": v.get("collect"),
        "share": v.get("share"),
        "comment_per_digg": v.get("ratio_comment"),
        "collect_per_digg": v.get("ratio_collect"),
        "share_per_digg": v.get("ratio_share"),
        "signals": v.get("why"),
    }


def main() -> None:
    import feed

    try:
        videos = feed.get_feed_tab()
    except Exception as e:
        print(f"[pick] tab/feed 失败({e})，退回 module/feed", file=__import__("sys").stderr)
        videos = feed.get_feed()
    print(f"feed {len(videos)} 条")
    top = rank(videos, limit=6, dump="--dump" in __import__("sys").argv)
    for i, v in enumerate(top, 1):
        print(
            f"{i}. [score {v['score']}] {v['desc'][:44]}\n"
            f"   @{v['author']} · {v['duration_s']}s · {v['age_h']}h前 · "
            f"赞{v['digg']} 评{v['comment']} 藏{v['collect']} 转{v['share']} · "
            f"评/赞{v['ratio_comment']} 藏/赞{v['ratio_collect']} · {','.join(v['why']) or '-'}"
        )


if __name__ == "__main__":
    main()
