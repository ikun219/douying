"""风控检测 → 通知主人（用户策略 m00591）。

用户策略（m02930）：不做自动换 IP / 代理 / 重登等自动补救。检测到风控
（verify_check / Argus 拦截 / 评论区验证码 / 评论框不可用）时，通过抖音
私信通知主人（潮汐）让主人手动处理，而不是静默失败。

探测方式：走 headed_browser 打开一个视频，**真的发一条测试评论**看是否
触发验证码。每 30 分钟探测一次（不再用搜索接口——搜索最敏感，反复搜会维
持 IP 高温反而更难解除）。

设计：
- `check_risk()` 在每次搜索/素材来源失败时可调用：它跑一次轻量探测，
  确认真的是风控就把这次标记为风控事件，并（去重后）通知主人一句。
- 内置一个后台 watchdog（`run_watchdog`）：周期探测评论区可用性，即使
  没有任何外部调用，也能主动发现风控并通知主人。
- 去重：同一「风控状态」只通知一次，恢复正常后复位。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))

# 主人（潮汐）昵称，走 send_text_sync(conv_name='潮汐')
MASTER_CONV = "潮汐"

# 同一连续风控期内「已通知」标记；只通知一次，恢复正常后复位
# proc/launch_ts：验证窗口子进程句柄与最近一次拉起时间（用于超时后重拉）
_state = {
    "active": False,        # 当前是否处于风控状态
    "notified": False,      # 本轮风控是否已通知过主人
    "last_check": 0.0,
    "proc": None,           # 验证窗口子进程 (subprocess.Popen | None)
    "launch_ts": 0.0,       # 最近一次拉起窗口的时间戳
    "lock": threading.Lock(),
}

# 窗口进程已退出后重新拉起的冷却时间
WINDOW_RELAUNCH_COOLDOWN = 60  # 秒

AUTO_STATE_FILE = os.path.join(HERE, "auto_state.json")


_last_skip_log = 0.0  # 节流器：只在每30分钟说一次


def _auto_browse_active() -> bool:
    """auto_browse 是否正在跑（enabled + 有心跳 + until 未到期）。"""
    try:
        import json as _json
        if not os.path.isfile(AUTO_STATE_FILE):
            return False
        s = _json.load(open(AUTO_STATE_FILE, "r", encoding="utf-8"))
        if not s.get("enabled"):
            return False
        # until>0 表示有截止时间，已到期就视为不活跃
        until = s.get("until", 0)
        now = time.time()
        if until > 0 and now >= until:
            return False
        # 心跳必须是最近 120 秒内的
        hb = s.get("last_heartbeat", 0)
        if now - hb > 120:
            return False
        return True
    except Exception:
        return False


# 强制「多久没探测了就当需要复查」：watchdog 周期
WATCH_INTERVAL = 1800  # 秒（30 分钟）


def _is_risk(reason: str, body: str = "") -> bool:
    """判断探测结果是否为「需要主人处理」的风控。

    兼容两种探测来源：
      - search.check_login（搜索探测）：verify_check / Argus / uifid / 响应非JSON / 返回空
      - headed_browser.probe_comment（评论探测）：验证码 / 评论区入口不可用

    网络异常不算风控；探测异常不以该途径判风控。
    """
    r = (reason or "").lower()
    low_body = (body or "").lower()
    combined = r + " " + low_body
    if "verify_check" in combined:
        return True
    if "argus" in r or "uifid" in r or "signature not found" in r:
        return True
    if "响应非json" in r:
        return True
    if "返回空" in r or "疑似登录" in r:
        return True
    # 评论区探测的风控信号
    if "验证码" in combined:      # 页面弹出验证码 / 评论触发验证码
        return True
    if "找不到评论框" in r:       # 视频页找不到评论输入框
        return True
    if "评论未确认" in r:         # 评论发送后超时未确认
        return True
    return False


def _probe() -> dict:
    """跑一次真实探测：优先用 headed_browser 检测评论区可用性（不触发搜索接口）。
    搜索接口最敏感，反复搜会维持 IP 高温，反而更难解除；
    改为打开一个视频检查评论区是否可用、是否弹验证码，同样能判定风控状态。

    headed_browser 未就绪时跳过探测（返回 ok，等下次 watchdog 再试），
    避免回退到搜索加重风控。
    """
    try:
        from headed_browser import probe_comment, run  # noqa: E402
        res = run(probe_comment(), timeout=25)
        if res is not None:
            return res
        # headed_browser 还没就绪，不硬探
        return {"login": True, "reason": "ok (headed_browser 未就绪，跳过探测)"}
    except Exception as e:  # noqa: BLE001
        return {"login": False, "reason": f"探测异常: {type(e).__name__}: {e}", "detail": str(e)}


def _notify_master(reason: str) -> bool:
    """通过抖音私信给主人（潮汐）发一条风控通知。失败不抛，只打日志。"""
    try:
        from send_text import send_text_sync
        msg = (
            "老大，我被风控拦住了 ᗜεᗜ\n"
            f"原因：{reason}\n"
            "你双击桌面 talk 里的 solve_risk_chain.cmd，会自动弹浏览器，\n"
            "挨个扫码→拖滑块→过验证码就行，搞完我就能继续干活了~"
        )
        r = send_text_sync(msg, conv_name=MASTER_CONV)
        ok = bool(r.get("ok"))
        log(f"[risk_guard] 通知主人: ok={ok} {str(r)[:120]}")
        return ok
    except Exception as e:  # noqa: BLE001
        log(f"[risk_guard] 通知主人生成失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


def check_risk(force_notify: bool = False) -> dict:
    """对外统一入口：探测一次风控，若命中且未通知过则通知主人。

    force_notify: 调用方（meme_collect/tool_runner/auto_browse）已确定
        这次搜索失败是风控时，传 True，跳过重复探测直接上报。

    返回 {"risk": bool, "notified": bool, "reason": str}。
    """
    with _state["lock"]:
        if force_notify and not _state["active"]:
            # 调用方已经撞到风控了，直接进入风控态并通知
            _state["active"] = True
            _state["notified"] = False
        res = _probe() if not force_notify else {"login": False, "reason": "调用方上报风控", "detail": ""}
        reason = res.get("reason", "")
        body = res.get("detail", "") or ""
        is_risk = _is_risk(reason, body) if not force_notify else True
        notified = False
        if is_risk:
            _state["active"] = True
            if not _state["notified"]:
                log(f"[risk_guard] 命中风控: {reason}")
                notified = _notify_master(reason or "风控")
                _state["notified"] = True
            _state["last_check"] = time.time()
        else:
            # 恢复正常 → 复位，下次风控会再通知
            _state["active"] = False
            _state["notified"] = False
        return {"risk": is_risk, "notified": notified, "reason": reason}


def run_watchdog(interval: float = WATCH_INTERVAL, stop: threading.Event | None = None):
    """后台 watchdog：周期探测，主动发现风控并通知主人。

    用法：
        import threading, risk_guard
        stop = threading.Event()
        threading.Thread(target=risk_guard.run_watchdog, args=(60, stop), daemon=True).start()
        # 进程退出前 stop.set()
    """
    log(f"[risk_guard] watchdog 启动，每 {interval}s 探测一次")
    # 启动先探一次，前几秒等浏览器/网络稳定，所以先睡 15 秒
    time.sleep(15)
    while not (stop and stop.is_set()):
        try:
            # 风控态中不再探测：持续探测会维持 IP 高温，反而更难解除。
            # 只等主人回复「解决了/搞定」→ reply_bot 调 mark_solved() 复位后再恢复探测。
            with _state["lock"]:
                in_risk = _state["active"]
            if not in_risk:
                # auto_browse 没在跑就不探：没刷视频就没有风控风险，省 IP 费。
                if _auto_browse_active():
                    check_risk()
                else:
                    global _last_skip_log
                    now = time.time()
                    if now - _last_skip_log > 1800:
                        log("[risk_guard] auto_browse 未活跃，跳过探测")
                        _last_skip_log = now
        except Exception as e:  # noqa: BLE001
            log(f"[risk_guard] watchdog 异常: {type(e).__name__}: {e}")
            traceback.print_exc()
        time.sleep(interval)


def mark_solved() -> None:
    """主人确认风控已解决：复位风控状态，watchdog 恢复周期探测。

    由 reply_bot 收到「解决了/搞定」类消息时调用；只复位 active/notified，
    不再探测（信号来源是主人的确认，不是探测结果）。
    """
    with _state["lock"]:
        _state["active"] = False
        _state["notified"] = False
    log("[risk_guard] 主人确认已解决，复位风控状态（watchdog 恢复探测）")


def _log_path() -> str:
    return os.path.join(HERE, "risk_guard.log")


def log(msg: str) -> None:
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="风控检测 watchdog")
    ap.add_argument("--interval", type=float, default=WATCH_INTERVAL, help="探测间隔(秒)")
    ap.add_argument("--once", action="store_true", help="只探测一次并退出（用于手动测试）")
    args = ap.parse_args()
    if args.once:
        print(check_risk())
    else:
        stop = threading.Event()
        run_watchdog(args.interval, stop)