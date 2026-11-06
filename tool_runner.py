# -*- coding: utf-8 -*-
"""抖音能力统一入口。AstrBot 的 douyin_tools 插件通过 subprocess 调用本脚本。

用法:
  python tool_runner.py <tool_name> '<json_args>'

返回: 一行 JSON（保证 UTF-8）。字段约定 {ok: bool, ...}。

tools:
  douyin_feed           刷推荐流（requests，快）
  douyin_search         搜索视频（requests，快）
  douyin_get_video_info 视频信息（requests，快）
  douyin_share_video    把视频分享给主人
  douyin_post_comment   发评论/回评论（浏览器 DOM，慢，可能触发风控验证码）
  douyin_like           点赞（浏览器 DOM）
  douyin_collect        收藏（浏览器 DOM）
  douyin_screenshot     截屏发 QQ（浏览器 DOM）
  douyin_send_voice     用语音说话（文字→TTS→mp4→发私信）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# ---- 关键环境变量：必须在 import playwright 之前设置 ----
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("TMP", os.path.join(HERE, "tmp"))
os.environ.setdefault("TEMP", os.path.join(HERE, "tmp"))
os.environ.setdefault("TMPDIR", os.path.join(HERE, "tmp"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(HERE, "playwright_browsers"))
os.makedirs(os.path.join(HERE, "tmp"), exist_ok=True)

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "utils"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _compact_videos(items: list[dict], limit: int = 6) -> list[dict]:
    """压缩视频列表，只保留 LLM 需要的关键字段。"""
    out = []
    for it in items[:limit]:
        out.append(
            {
                "aweme_id": it.get("aweme_id"),
                "desc": (it.get("desc") or it.get("title") or "")[:80],
                "author": it.get("author", ""),
                "url": it.get("url", ""),
                "digg_count": it.get("digg_count"),
            }
        )
    return out


def _load_share_targets() -> list[dict]:
    """读 share_targets.json，返回固定分享名单。
    每次分享都发给名单里的所有人；role=master 的老大单独写话，其它人统一一句话。"""
    path = os.path.join(HERE, "share_targets.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("targets") or []
        return [r for r in rows if (r.get("uid") or "").strip()]
    except Exception as e:  # noqa: BLE001
        # 配置损坏时兜底回退：只发老大
        return [{"uid": "3900658741936829", "name": "老大", "role": "master"}]


def _run_sync(tool: str, args: dict) -> dict:
    if tool == "douyin_feed":
        from feed import get_feed  # noqa: E402

        items = get_feed(int(args.get("pull_type", 0)))
        return {"ok": True, "count": len(items), "videos": _compact_videos(items)}
    if tool == "douyin_search":
        items = None
        try:
            import headed_browser  # noqa: E402

            r = headed_browser.run(headed_browser.search(args["keyword"], int(args.get("count", 10))))
            if r is not None:
                items = r
        except Exception:  # noqa: BLE001
            pass
        if items is None:
            from search import search  # noqa: E402

            items = search(args["keyword"], int(args.get("count", 10)))
        if not items:
            # 搜不到结果，疑似撞上 verify_check 风控——上报让风险处理机制通知主人
            try:
                from risk_guard import check_risk  # noqa: E402
                check_risk(force_notify=True)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "count": len(items), "videos": _compact_videos(items)}
    if tool == "douyin_get_video_info":
        from get_video_info import get_video_info  # noqa: E402

        return {"ok": True, "info": get_video_info(str(args["aweme_id"]))}
    if tool == "douyin_share_video":
        # 分享 = 在抖音里把「视频卡片」发给固定的一群人（抖音私信），永远不走 QQ
        from share_card import share_card  # noqa: E402
        from send_text import send_text_to_uid  # noqa: E402

        aweme_id = str(args["aweme_id"])
        # 分享目标和话术配置在 share_targets.json，QQ 里可直接让汐缘增删
        targets = _load_share_targets()

        # 是否只分享给老大（role=master）：带 @ 的评论场景默认只发老大，不补话
        only_master = bool(args.get("only_master", False))
        if only_master:
            targets = [t for t in targets if t.get("role") == "master"]

        # 固定分享名单：role=master 一个，其它都是 role=other
        share_uids = [t["uid"] for t in targets if t.get("uid")]
        names = [t.get("name", "") for t in targets]

        res = asyncio.run(
            share_card(
                aweme_id,
                uids=share_uids or None,
                names=names or None,
                headless=bool(args.get("headless", False)),
            )
        )

        # 分享之后，按角色发「补一句话」私信：
        #  - 老大(role=master) 收 args['remark']（LLM 每次现写，更亲切）
        #  - 其它人收同一条 args['remark_other']（所有 other 一模一样）
        #  - 带 @ 的评论场景（only_master=True）不补任何话
        remarks_sent = []
        if not only_master:
            master_remark = str(args.get("remark") or "").strip()
            other_remark = str(args.get("remark_other") or "").strip()
            if not (master_remark or other_remark):
                master_remark = str(args.get("text") or "").strip()
        else:
            master_remark = ""
            other_remark = ""

        for t in targets:
            uid = (t.get("uid") or "").strip()
            if not uid:
                continue
            role = t.get("role", "other")
            msg = (master_remark if role == "master" else other_remark) or master_remark
            if not msg:
                continue
            try:
                r = send_text_to_uid(msg, uid, headless=bool(args.get("headless", False)))
                remarks_sent.append({"uid": uid, "name": t.get("name"), "role": role,
                                     "ok": bool(r.get("ok")), "detail": {k: r.get(k) for k in ("ok", "error", "sent") if k in r}})
            except Exception as e:  # noqa: BLE001
                remarks_sent.append({"uid": uid, "name": t.get("name"), "role": role,
                                     "ok": False, "error": f"{type(e).__name__}: {e}"})

        share_ok = res.get("ok", False) if res else False
        share_detail = res.get("results") if res and "results" in res else res
        return {
            "ok": share_ok,
            "share": share_detail,
            "remarks": remarks_sent,
            "targets": [{"uid": t.get("uid"), "name": t.get("name"), "role": t.get("role")} for t in targets],
            "note": "已在抖音私信里把视频卡片分享给目标（带@评论场景只发老大且不补话；否则发全体并按角色补了话：老大单独、其它人统一）",
        }
    if tool == "douyin_check_login":
        from search import check_login  # noqa: E402

        r = check_login()
        return {"ok": True, **r}
    raise ValueError(f"unknown sync tool: {tool}")


async def _run_async(tool: str, args: dict) -> dict:
    headless = bool(args.get("headless", False))

    if tool == "douyin_send_voice":
        # 文字 → 语音 mp4 → 发抖音私信
        from voice import build_voice  # noqa: E402
        from send_voice import send_voice_dm  # noqa: E402

        mp4 = build_voice(args["text"])
        r = await send_voice_dm(str(mp4), headless=headless)
        return {"ok": bool(r.get("ok")), "file": str(mp4), "detail": r}

    aweme_id = str(args["aweme_id"])

    if tool == "douyin_post_comment":
        # 常驻有头浏览器优先：会话活着就复用同一个带窗浏览器（更接近真人、降风控）；
        # 评论可能弹验证码，给 720s（12 分钟）让主人手动过验证
        # 未就绪/超时/异常返回 None，回退独立 launch 路径（此时 headless 参数照旧生效）
        image_path = args.get("image")
        reply_cid = args.get("reply_cid") or args.get("cid")
        try:
            import headed_browser  # noqa: E402

            r = headed_browser.run(
                headed_browser.post_comment(aweme_id, args["text"], args.get("reply_to_text"), image_path, reply_cid),
                timeout=720,
            )
            if r is not None:
                return r
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[tool_runner] headed post_comment 失败，回退独立浏览器: {type(e).__name__}: {e}\n")

        from post_comment import post_comment  # noqa: E402

        return await post_comment(
            aweme_id, args["text"], args.get("reply_to_text"), headless=headless, image_path=image_path, reply_cid=reply_cid
        )
    if tool == "douyin_like":
        try:
            import headed_browser  # noqa: E402

            r = headed_browser.run(headed_browser.like(aweme_id))
            if r is not None:
                return r
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[tool_runner] headed like 失败，回退独立浏览器: {type(e).__name__}: {e}\n")

        from video_actions import _do  # noqa: E402

        return await _do(aweme_id, "like", headless=headless)
    if tool == "douyin_collect":
        try:
            import headed_browser  # noqa: E402

            r = headed_browser.run(headed_browser.collect(aweme_id))
            if r is not None:
                return r
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[tool_runner] headed collect 失败，回退独立浏览器: {type(e).__name__}: {e}\n")

        from video_actions import _do  # noqa: E402

        return await _do(aweme_id, "collect", headless=headless)
    if tool == "douyin_screenshot":
        from screenshot_video import screenshot_video  # noqa: E402

        path = await screenshot_video(aweme_id, headless=headless)
        if not args.get("only_save", False):
            from send_to_qq import send_to_qq  # noqa: E402

            send_to_qq(
                text=f"[抖音截图] https://www.douyin.com/video/{aweme_id}",
                image_path=str(path),
            )
        return {"ok": True, "path": str(path), "note": "已经截好图发给主人了"}
    raise ValueError(f"unknown async tool: {tool}")


SYNC_TOOLS = {
    "douyin_feed",
    "douyin_search",
    "douyin_get_video_info",
    "douyin_share_video",
    "douyin_check_login",
}


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "usage: tool_runner.py <tool> '<args>'"}, ensure_ascii=False))
        return
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        if tool in SYNC_TOOLS:
            result = _run_sync(tool, args)
        else:
            result = asyncio.run(_run_async(tool, args))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
