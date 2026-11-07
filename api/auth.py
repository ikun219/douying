# -*- coding: utf-8 -*-
"""抖音登录模块 — 一键登录（QR 扫码 + 风控三阶段自动解除）。

集成 solve_risk_chain 完整链路：
  阶段 1：扫码登录（浏览器弹出二维码）
  阶段 2：评论手机验证码（自动发评论触发，手动在浏览器窗口填验证码）
  阶段 3：搜索滑块验证（打开搜索页，手动拖滑块，自动探测恢复）

导出:
    login()   — 一键登录 + 自动解除风控
    verify()  — 校验当前登录是否有效
    whoami()  — 当前登录账号信息

CLI:
    python -m douyin.api.auth login                # 扫码登录 + 解除风控
    python -m douyin.api.auth login --skip-search   # 跳过搜索阶段
    python -m douyin.api.auth verify               # 验证凭据
    python -m douyin.api.auth whoami               # 显示账号

注意：登录过程中弹出的验证码和滑块需要人工在浏览器窗口里操作，
     脚本会自动探测到操作完成并继续。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # douyin/
STATE = HERE / "storage_state.json"


def _log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------- 核心登录（集成风控三阶段）
async def login(
    headless: bool = False,
    kw: str = "美食",
    conv: str = "潮汐",
    search_timeout: int = 900,
    chat_timeout: int = 600,
    cdp_port: int | None = None,
    skip_search: bool = False,
    skip_chat: bool = False,
) -> dict:
    """一键登录 + 自动解除风控。

    阶段 1：扫码登录（浏览器窗口弹出二维码，抖音 App 扫码）
    阶段 2：评论手机验证码（自动发评论触发，在弹出窗口里填手机验证码）
    阶段 3：搜索滑块验证（打开搜索页，在窗口里拖动滑块，脚本自动探测恢复）

    参数:
        headless: True=无头模式（不推荐，风控更严）
        kw: 搜索关键词（阶段 3 搜索验证用）
        conv: 私信会话名（预留）
        search_timeout: 搜索阶段超时（秒，默认 900）
        chat_timeout: 评论阶段超时（秒，默认 600）
        cdp_port: 连接已有 Edge 端口（如 9222），None 则独立启动
        skip_search: 跳过搜索滑块阶段
        skip_chat: 跳过评论验证码阶段

    返回 {"ok": True, "stage1": True, "stage2": True, "stage3": True}
    或   {"ok": False, "error": "..."}
    """
    try:
        import solve_risk_chain as _src
    except ImportError as e:
        return {"ok": False, "error": f"无法导入 solve_risk_chain: {e}"}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"ok": False, "error": "请先安装 playwright: pip install playwright && playwright install chromium"}

    async with async_playwright() as p:
        browser, ctx, page, is_cdp = await _src._setup_browser(p, cdp_port)

        try:
            # === 阶段 1：扫码登录 ===
            ok1 = await _src.stage1_login(page, ctx, is_cdp)
            if not ok1:
                if not is_cdp:
                    await browser.close()
                return {"ok": False, "stage1": False, "error": "登录失败（超时或未扫码）"}

            # === 阶段 2：评论手机验证码 ===
            ok2 = True
            if not skip_chat:
                ok2 = await _src.stage2_comment_verify(page, kw, chat_timeout)

            # === 阶段 3：搜索滑块验证 ===
            ok3 = True
            if not skip_search:
                ok3 = await _src.stage3_search_verify(page, kw, search_timeout)

            # 补导一次 storage_state（非 CDP 模式已由 stage1 保存，这里兜底）
            if is_cdp:
                try:
                    await ctx.storage_state(path=str(STATE))
                except Exception:
                    pass

        finally:
            if not is_cdp:
                await browser.close()

    return {
        "ok": ok1,
        "stage1": ok1,
        "stage2": ok2,
        "stage3": ok3,
        "state_file": str(STATE),
        "hint": "全部通过！可正常使用 api.content / api.message" if (ok1 and ok2 and ok3) else (
            "部分阶段未通过，可重新运行 login() 或调用 solve_risk_chain.py --skip-login"
        ),
    }


# --------------------------------------------------------------- 验证
def verify(storage_state: str | None = None) -> dict:
    """验证当前登录是否有效（用 storage_state 发 API 请求）。

    Returns {"ok": True, "uid": "123...", "nickname": "..."}
         or {"ok": False, "error": "..."}
    """
    try:
        import requests

        state_path = Path(storage_state or STATE)
        if not state_path.exists():
            return {"ok": False, "error": f"storage_state 不存在: {state_path}"}

        # 读取 cookies
        state = json.loads(state_path.read_text(encoding="utf-8"))
        jar = requests.cookies.RequestsCookieJar()
        for c in state.get("cookies", []):
            jar.set(c["name"], c["value"], domain=c.get("domain", ".douyin.com"), path=c.get("path", "/"))

        s = requests.Session()
        s.cookies = jar
        s.verify = False

        # 用 get_my_uid 验证
        from builder.auth import DouyinAuth
        from builder.header import HeaderBuilder, HeaderType

        auth = DouyinAuth()
        auth.cookie = jar
        headers = HeaderBuilder().build(HeaderType.GET).get()

        r = s.get(
            "https://www.douyin.com/aweme/v1/web/user/profile/self/",
            headers=headers,
            timeout=10,
        )
        if r.status_code == 200 and "user" in r.json():
            user = r.json()["user"]
            return {
                "ok": True,
                "uid": user.get("uid", ""),
                "nickname": user.get("nickname", ""),
                "unique_id": user.get("unique_id", ""),
            }
        # 回退：用简单的 cookie 检查
        try:
            r2 = s.get("https://www.douyin.com/passport/web/account_info/", timeout=10)
            data = r2.json() if r2.status_code == 200 else None
            if isinstance(data, dict) and data.get("user_name"):
                return {
                    "ok": True,
                    "uid": str(data.get("user_id", "?")),
                    "nickname": data.get("user_name", "?"),
                }
        except Exception:
            pass
        return {"ok": False, "error": f"API {r.status_code}: cookie 可能已过期"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def whoami(storage_state: str | None = None) -> dict:
    """同 verify，别名。"""
    return verify(storage_state)


# --------------------------------------------------------------- CLI
def main():
    import argparse

    ap = argparse.ArgumentParser(description="汐缘 抖音登录（含风控解除）")
    ap.add_argument("cmd", nargs="?", default="login",
                    choices=["login", "verify", "whoami"],
                    help="login=登录+风控解除, verify/whoami=检查登录状态")
    ap.add_argument("--kw", default="美食", help="搜索关键词（阶段 3 用）")
    ap.add_argument("--search-timeout", type=int, default=900, help="搜索阶段超时(秒)")
    ap.add_argument("--chat-timeout", type=int, default=600, help="评论阶段超时(秒)")
    ap.add_argument("--cdp-port", type=int, default=None, help="连接已有 Edge CDP 端口（如 9222）")
    ap.add_argument("--skip-search", action="store_true", help="跳过搜索滑块阶段")
    ap.add_argument("--skip-chat", action="store_true", help="跳过评论验证码阶段")
    a = ap.parse_args()

    if a.cmd == "login":
        result = asyncio.run(login(
            kw=a.kw,
            search_timeout=a.search_timeout,
            chat_timeout=a.chat_timeout,
            cdp_port=a.cdp_port,
            skip_search=a.skip_search,
            skip_chat=a.skip_chat,
        ))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif a.cmd in ("verify", "whoami"):
        result = verify()
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()