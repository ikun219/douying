# -*- coding: utf-8 -*-
"""抖音搜索（真实浏览器路径，规避 verify_check 风控）。

结论（实测）：requests 直连 search 端点无论如何加 a_bogus/头的都永久被
Argus verify_check 拦截；而用 Playwright + msedge + storage_state 打开
搜索页、在页面内 fetch 同一 /single/ 接口，拿到真实浏览器指纹即可正常返回结果。

为保稳：优先走浏览器改出的结果；浏览器启动失败/exceptions 时回退 requests。

环境坑：必须在 playwrigth 之前设 TMP/TEMP/TMPDIR（指向本目录 tmp），否则
EPERM mkdtemp（win bash 下 AppData Temp 不可写）。

用法：
  python search.py <关键词> [数量=15]
  python search.py 猫咪 10
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

_here_dir = Path(__file__).resolve().parent
HERE = str(_here_dir)
os.makedirs(os.path.join(HERE, "tmp"), exist_ok=True)
os.environ.setdefault("TMP", os.path.join(HERE, "tmp"))
os.environ.setdefault("TEMP", os.path.join(HERE, "tmp"))
os.environ.setdefault("TMPDIR", os.path.join(HERE, "tmp"))
os.environ.setdefault("PYTHONUTF8", "1")
_tmpdir = os.path.join(HERE, "tmp")
if tempfile.tempdir is None:
    tempfile.tempdir = _tmpdir
os.environ["TMP"] = _tmpdir
os.environ["TEMP"] = _tmpdir
os.environ["TMPDIR"] = _tmpdir

import requests  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

STATE = _here_dir / "storage_state.json"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
)
_SEARCH_FETCH_JS_TMPL = r"""async () => {
  try {
    const r = await fetch('/aweme/v1/web/general/search/single/?'
      + new URLSearchParams({
        search_channel:'aweme_general', enable_history:'1',
        keyword: KW, search_source:'tab_search', query_correct_type:'1',
        is_filter_search:'0', offset:'0', count:'12',
        need_filter_settings:'1', list_type:'multi',
        device_platform:'webapp', aid:'6383', channel:'channel_pc_web',
        version_code:'190600', version_name:'19.6.0', pc_client_type:'1',
      }), {headers:{'Accept':'application/json, text/plain, */*'}});
    const t = await r.text();
    return t;
  } catch(e){ return JSON.stringify({__err:String(e), __head:''}); }
}"""


def _load_cookies() -> tuple[str, dict]:
    d = json.loads(Path(STATE).read_text(encoding="utf-8"))
    cookies = d.get("cookies", [])
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    cookie_dict = {c["name"]: c["value"] for c in cookies}
    return cookie_str, cookie_dict


def _build_headers(cookie_str: str, cookie_dict: dict, keyword: str) -> dict:
    uifid = cookie_dict.get("UIFID") or cookie_dict.get("UIFID_TEMP", "")
    referer_url = (
        f"https://www.douyin.com/search/{keyword}"
        f"?aid=f594bbd9-a0e2-4651-9319-ebe3cb6298c1&type=general"
    )
    return {
        "User-Agent": DEFAULT_UA,
        "Cookie": cookie_str,
        "Host": "www.douyin.com",
        "Origin": "https://www.douyin.com/",
        "Referer": urllib.parse.quote(referer_url, safe=":/"),
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "x-tt-argus": "1",
        "uifid": uifid,
    }


def _search_params(keyword: str, count: int) -> dict:
    """搜索接口的完整参数（check_login 与 search 共用）。"""
    return {
        "search_channel": "aweme_general",
        "enable_history": "1",
        "keyword": keyword,
        "search_source": "tab_search",
        "query_correct_type": "1",
        "is_filter_search": "0",
        "from_group_id": "7378810571505847586",
        "offset": "0",
        "count": str(count),
        "need_filter_settings": "1",
        "list_type": "multi",
        "search_id": "",
        # common params
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "version_code": "190600",
        "version_name": "19.6.0",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "cookie_enabled": "true",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "125.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "8",
        "device_memory": "8",
        "engine_version": "109.0",
        "platform": "PC",
        "screen_width": "1920",
        "screen_height": "1080",
        "effective_type": "4g",
        "round_trip_time": "50",
        "webid": "7701340071136069120",
    }


def search(keyword: str, count: int = 15) -> list[dict]:
    """搜索关键词，返回 [{aweme_id, desc, author, cover, url}]。

    优先走真实浏览器（规避 verify_check），失败回退 requests。
    """
    raw = _browser_search_raw(keyword, count)
    if raw is None:
        # 浏览器路径不可用，回退旧 requests 直连（可能仍被风控，但尽力）
        return _search_requests(keyword, count)
    return _parse_search_json(raw, keyword)


def _parse_search_json(raw: str, keyword: str) -> list[dict]:
    try:
        frag = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(frag, dict) and frag.get("__err"):
        return []
    if isinstance(frag, dict):
        data = frag.get("data") or []
    else:
        # 旧 requests 路径直接吐 JSON 对象
        data = frag.get("data") or []
    out = []
    for it in data:
        aw = it.get("aweme_info") or {}
        if not aw.get("aweme_id"):
            continue
        cover = ""
        for c in (aw.get("video") or {}).get("cover", {}).get("url_list", []) or []:
            cover = c
            break
        out.append(
            {
                "aweme_id": aw.get("aweme_id"),
                "desc": aw.get("desc", ""),
                "author": (aw.get("author") or {}).get("nickname", ""),
                "cover": cover,
                "url": f"https://www.douyin.com/video/{aw.get('aweme_id')}",
            }
        )
    return out


_BROWSER_TIMEOUT = 60  # 浏览器路径整体预算（秒），防 launch/goto 无限挂起


def _browser_search_raw(keyword: str, count: int) -> str | None:
    """用 Playwright+msedge 打开搜索页并在页面内 fetch，返回原始响应文本（JSON 字符串）。

    verify_check/异常/超时 → 返回 None（由调用方回退 requests）。
    wait_for 取消会抛 CancelledError 进 _browser_search_once 的 async-with，关闭浏览器，
    不会留下孤儿 msedge 进程。
    """

    async def _run_with_budget() -> str | None:
        return await asyncio.wait_for(_browser_search_once(keyword, count), timeout=_BROWSER_TIMEOUT)

    try:
        return asyncio.run(_run_with_budget())
    except asyncio.TimeoutError:
        _log(f"[search] 浏览器路径超时{_BROWSER_TIMEOUT}s，回退 requests")
        return None
    except Exception as e:  # noqa: BLE001
        _log(f"[search] 浏览器路径异常，回退 requests: {type(e).__name__}: {e}")
        return None


async def _browser_search_once(keyword: str, count: int) -> str | None:
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                channel="msedge",
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:  # noqa: BLE001
            _log(f"[search] 启动浏览器失败: {type(e).__name__}: {e}")
            return None
        try:
            ctx = await browser.new_context(
                storage_state=str(STATE),
                user_agent=DEFAULT_UA,
                viewport={"width": 1366, "height": 900},
                locale="zh-CN",
            )
            ctx.set_default_timeout(20000)
            page = await ctx.new_page()
            url = f"https://www.douyin.com/search/{keyword}"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            except Exception:
                pass
            await page.wait_for_timeout(3500)
            js = _SEARCH_FETCH_JS_TMPL.replace("KW", json.dumps(keyword))
            js = js.replace('count:\'12\'', f"count:{json.dumps(str(max(1, int(count))))}")
            out = await page.evaluate(js)
            return str(out) if out else None
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass


def _search_requests(keyword: str, count: int) -> list[dict]:
    """requests 直连回退路径（check_login 与旧 search 共用）。"""
    cookie_str, cookie_dict = _load_cookies()
    params = _search_params(keyword, count)
    headers = _build_headers(cookie_str, cookie_dict, keyword)
    url = "https://www.douyin.com/aweme/v1/web/general/search/single/"
    try:
        r = requests.get(url, params=params, headers=headers, timeout=30)
        data = r.json()
    except Exception:  # noqa: BLE001
        return []
    return _parse_search_json(data, keyword)


def _log(msg: str) -> None:
    try:
        with open(os.path.join(HERE, "search_run.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def check_login() -> dict:
    """探测抖音登录态与搜索可用性。

    返回 {login: bool, reason: str, detail: str}。
    优先走真实浏览器（与 search 同路径）——只有浏览器能真正代表"搜索是否可用"。
    浏览器不可用时回退 requests 探测（旧信号判定，保守）。
    """
    raw = _browser_search_raw("美食", 3)
    if raw is not None:
        low = raw.lower()
        if "verify_check" in low:
            return {"login": False, "reason": "风控/登录失效(verify_check)", "detail": raw[:200]}
        try:
            frag = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"login": False, "reason": "浏览器响应非JSON", "detail": raw[:200]}
        data = (frag or {}).get("data") or [] if isinstance(frag, dict) else []
        if data:
            return {"login": True, "reason": "ok", "detail": f"浏览器搜到 {len(data)} 条"}
        return {"login": False, "reason": "搜索返回空(疑似登录失效/风控)", "detail": raw[:200]}
    # 浏览器不可用 → 回退旧 requests 探测
    cookie_str, cookie_dict = _load_cookies()
    keyword = "美食"
    params = _search_params(keyword, 3)
    headers = _build_headers(cookie_str, cookie_dict, keyword)
    url = "https://www.douyin.com/aweme/v1/web/general/search/single/"
    try:
        r = requests.get(url, params=params, headers=headers, timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"login": False, "reason": f"网络异常: {type(e).__name__}: {e}", "detail": ""}
    body = r.text
    low = body.lower()
    if "verify_check" in body:
        return {"login": False, "reason": "风控/登录失效(verify_check)", "detail": body[:200]}
    if (
        "blocked by argussecurityplugin" in low
        or "uifid not found" in low
        or "signature not found" in low
    ):
        return {"login": False, "reason": "网关拦截(Argus/uifid)", "detail": body[:200]}
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return {"login": False, "reason": "响应非JSON(疑似被拦截)", "detail": body[:200]}
    if data.get("data"):
        return {"login": True, "reason": "ok", "detail": f"搜到 {len(data['data'])} 条"}
    return {"login": False, "reason": "搜索返回空(疑似登录失效/风控)", "detail": body[:200]}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print("用法: search.py <关键词> [数量]")
        return
    keyword = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    items = search(keyword, count)
    print(f"搜到 {len(items)} 条：")
    for it in items:
        print(f"  {it['aweme_id']}  @{it['author']}  {it['desc'][:50]}")


if __name__ == "__main__":
    main()
