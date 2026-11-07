# -*- coding: utf-8 -*-
"""把抖音视频以「分享卡片」发给指定好友（浏览器 DOM，无头可跑）。

为什么要卡片：纯文本 + URL 做不出卡片，而且文本链接在抖音里点不开。
卡片（aweType 800 / MessageItemShareAweme）是前端把结构化消息交给服务端生成的。

链路：
  视频页 → 真实鼠标轨迹 hover 分享图标 → 面板展开
        → 找到 uid==MASTER_UID 的那一行 → 点该行「分享」按钮 → 卡片发出

用法：
  python share_card.py <aweme_id>            # 发给老大（默认）
  python share_card.py <aweme_id> --peek     # 只展开面板，打印每一行 + fiber 里的 uid/sec_uid
  python share_card.py <aweme_id> --dry      # 匹配到人了但不点
  python share_card.py <aweme_id> --uid 123  # 换人
  python share_card.py <aweme_id> --name 昵称 # 按昵称匹配（不推荐，昵称会重名）

返回一行 JSON：{ok, sent, to, matched_by, panel}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("TMP", os.path.join(HERE, "tmp"))
os.environ.setdefault("TEMP", os.path.join(HERE, "tmp"))
os.environ.setdefault("TMPDIR", os.path.join(HERE, "tmp"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(HERE, "playwright_browsers"))
os.makedirs(os.path.join(HERE, "tmp"), exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MASTER_UID = "3900658741936829"  # 老大的抖音 uid（IM 侧用的就是这个）

SHARE_ICON_SELECTORS = [
    '[data-e2e="video-share-icon-container"]',
    '[data-e2e="video-player-share"]',
]

# 在页面里扫分享面板的每一行，把「昵称 + 从 React fiber 里读到的 uid/sec_uid + 该行分享按钮坐标」一起吐出来
PEEK_JS = r"""
(function(){
  var sc = document.querySelector('[data-e2e="video-share-container"]');
  if (!sc) return JSON.stringify({err:'no-panel'});
  function idsOf(node){
    var fk = Object.keys(node).find(function(k){ return k.indexOf('__reactFiber$')===0; });
    if (!fk) return [];
    var f = node[fk], d = 0, out = [];
    while (f && d < 14) {
      var p = f.memoizedProps;
      if (p && typeof p === 'object') {
        for (var k in p) {
          var v = p[k];
          if (typeof v === 'string' && /^(MS4wLjABAA|11-)\S{6,}$/.test(v)) out.push(k+'='+v);
          else if (v && typeof v === 'object') {
            if (v.sec_uid) out.push('sec_uid='+v.sec_uid);
            if (v.uid) out.push('uid='+v.uid);
            if (v.user_id) out.push('user_id='+v.user_id);
          }
        }
      }
      f = f.return; d++;
    }
    var uniq = [];
    out.forEach(function(x){ if (uniq.indexOf(x)<0) uniq.push(x); });
    return uniq;
  }
  var rows = [];
  sc.querySelectorAll('div,span,p').forEach(function(e){
    var t = (e.innerText||'').replace(/\s+/g,' ').trim();
    if (!t || t.length > 40 || t === '分享' || t === '取消') return;
    var ids = idsOf(e);
    if (!ids.length) return;
    // 该行里的「分享」按钮
    var btn = null, row = e;
    for (var i=0;i<6 && row && !btn;i++){
      var cands = Array.prototype.slice.call(row.querySelectorAll('div,span,button')).filter(function(b){
        return (b.innerText||'').trim() === '分享';
      });
      if (cands.length){
        for (var j=0;j<cands.length;j++){
          var r = cands[j].getBoundingClientRect();
          if (r.width > 16 && r.height > 12) { btn = {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2), w:Math.round(r.width), h:Math.round(r.height)}; break; }
        }
      }
      row = row.parentElement;
    }
    rows.push({text: t.slice(0,30), ids: ids.slice(0,6), btn: btn});
  });
  var seen = {}, uniq = [];
  rows.forEach(function(o){ var k = o.text; if (!seen[k]) { seen[k]=1; uniq.push(o); } });
  return JSON.stringify({rows: uniq.slice(0, 20), panelText: (sc.innerText||'').replace(/\s+/g,' ').slice(0,400)});
})()
"""


async def _hover_share_icon(page) -> bool:
    """用真实鼠标轨迹 hover 分享图标（它没有 onClick，是 onMouseEnter 弹窗）。"""
    box = None
    for sel in SHARE_ICON_SELECTORS:
        box = await page.evaluate(
            """(sel)=>{
                var e = document.querySelector(sel);
                if (!e) return null;
                var r = e.getBoundingClientRect();
                if (!r.width || !r.height) return null;
                return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), sel: sel};
            }""",
            sel,
        )
        if box:
            break
    if not box:
        return False
    # 先移开，再分步移过去（模拟人的轨迹）
    await page.mouse.move(20, 20)
    await asyncio.sleep(0.25)
    steps = 8
    for i in range(1, steps + 1):
        await page.mouse.move(20 + (box["x"] - 20) * i / steps, 20 + (box["y"] - 20) * i / steps)
        await asyncio.sleep(0.07)
    await page.mouse.move(box["x"], box["y"])
    await asyncio.sleep(2.0)
    return True


def _match_target(rows, uid: str, name: str = ""):
    """在一组分享面板行里找一个匹配 uid(或昵称) 的目标行。
    返回 (row, matched_by)。"""
    if uid:
        for r in rows:
            hit = any(
                (i == f"uid={uid}") or (i.endswith(uid) and ("uid=" in i or "user_id=" in i))
                for i in r.get("ids", [])
            )
            if hit and r.get("btn"):
                return r, "uid"
    if name:
        for r in rows:
            if r.get("text", "").strip() == name and r.get("btn"):
                return r, "nickname"
    return None, ""


async def _click_share_to(page, row) -> dict:
    """点掉某个目标行的「分享」按钮并发卡片，返回 after 快照。"""
    btn = row["btn"]
    await page.mouse.move(btn["x"], btn["y"])
    await asyncio.sleep(0.15)
    await page.mouse.down()
    await asyncio.sleep(0.06)
    await page.mouse.up()
    await asyncio.sleep(2.5)

    after = await page.evaluate(
        """()=>{
            var sc = document.querySelector('[data-e2e="video-share-container"]');
            var body = document.body.innerText || '';
            return JSON.stringify({
                panelOpen: sc ? sc.getBoundingClientRect().height > 10 : false,
                toast: body.indexOf('已发送') >= 0 || body.indexOf('发送成功') >= 0,
            });
        }"""
    )
    return json.loads(after)


async def share_card(aweme_id: str, uid: str = MASTER_UID, name: str = "",
                     uids: list | None = None, names: list | None = None,
                     headless: bool = True, peek: bool = False, dry: bool = False) -> dict:
    """把视频以分享卡片发给一个人或一组人。

    - 单人：传 uid 或 name（老用法，向后兼容）。
    - 多人：传 uids=[uid,...]（可选 names=[昵称,...] 兜底），同一次浏览器会话里逐个发。
    - peek：只展开面板并打印每一行（uid/sec_uid/坐标）。
    """
    from playwright.async_api import async_playwright

    # 归一化目标列表
    targets = []
    if uids or names:
        names = names or []
        for i, u in enumerate(uids or []):
            targets.append({"uid": u, "name": (names[i] if i < len(names) else "")})
    else:
        targets = [{"uid": uid, "name": name}]

    url = f"https://www.douyin.com/video/{aweme_id}"
    async with async_playwright() as p:
        b = await p.chromium.launch(
            channel="msedge", headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await b.new_context(
            storage_state=os.path.join(HERE, "storage_state.json"),
            viewport={"width": 1600, "height": 900},
        )
        page = await ctx.new_page()
        results = []
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(7)

            if peek:
                if not await _hover_share_icon(page):
                    return {"ok": False, "error": "找不到分享图标（页面没加载好 / 未登录）"}
                panel = await page.evaluate(
                    """()=>{
                        var sc = document.querySelector('[data-e2e="video-share-container"]');
                        return sc ? (sc.innerText||'').replace(/\\s+/g,' ').slice(0,400) : '';
                    }"""
                )
                info = json.loads(await page.evaluate(PEEK_JS))
                return {"ok": True, "peek": True, "panel": panel, "rows": info.get("rows", [])}

            for t in targets:
                # 每次都要重新 hover 面板（上一个目标点完面板会收起）
                if not await _hover_share_icon(page):
                    results.append({"uid": t["uid"], "name": t["name"], "ok": False,
                                    "error": "找不到分享图标（页面没加载好 / 未登录）"})
                    break
                panel = await page.evaluate(
                    """()=>{
                        var sc = document.querySelector('[data-e2e="video-share-container"]');
                        return sc ? (sc.innerText||'').replace(/\\s+/g,' ').slice(0,400) : '';
                    }"""
                )
                info = json.loads(await page.evaluate(PEEK_JS))
                rows = info.get("rows") or []

                if dry:
                    row, matched_by = _match_target(rows, t.get("uid", ""), t.get("name", ""))
                    results.append({
                        "ok": bool(row), "dry": True, "uid": t.get("uid"), "name": t.get("name"),
                        "to": row.get("text") if row else None,
                        "matched_by": matched_by, "panel": panel,
                        "error": None if row else "分享面板里没找到该目标（面板只列最近分享过的人）",
                    })
                    continue

                row, matched_by = _match_target(rows, t.get("uid", ""), t.get("name", ""))
                if not row:
                    results.append({
                        "ok": False, "uid": t.get("uid"), "name": t.get("name"),
                        "error": "分享面板里没找到该目标（面板只列最近分享过的人）",
                        "panel": panel,
                        "rows": [{"text": r.get("text"), "ids": r.get("ids")} for r in rows],
                    })
                    continue

                after = await _click_share_to(page, row)
                results.append({
                    "ok": True, "sent": True, "uid": t.get("uid"), "name": t.get("name"),
                    "to": row.get("text"), "matched_by": matched_by, "after": after, "panel": panel,
                })

            ok_all = bool(results) and all(r.get("ok") for r in results)
            return {"ok": ok_all, "results": results}
        finally:
            try:
                await ctx.close()
                await b.close()
            except Exception:
                pass


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(json.dumps({"ok": False, "error": "用法: share_card.py <aweme_id> [--peek|--dry] [--uid 123 [--uid 456 ...]] [--name 昵称]"}, ensure_ascii=False))
        return
    aweme_id = args[0]
    uids = []
    names = []
    for i, a in enumerate(sys.argv):
        if a == "--uid" and i + 1 < len(sys.argv):
            uids.append(sys.argv[i + 1])
        if a == "--name" and i + 1 < len(sys.argv):
            names.append(sys.argv[i + 1])
    res = asyncio.run(
        share_card(
            aweme_id,
            uid=(uids[0] if uids else MASTER_UID),
            name=(names[0] if names else ""),
            uids=(uids if uids else None),
            names=(names if names else None),
            headless="--headed" not in sys.argv,
            peek="--peek" in sys.argv,
            dry="--dry" in sys.argv,
        )
    )
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
