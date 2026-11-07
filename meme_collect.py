# -*- coding: utf-8 -*-
"""刷抖音时顺手把「鲸鱼娘 / 哈基米猫猫」这类表情包收进 AstrBot 的表情库。

表情库（meme_manager）是按目录扫图的：
    <packs>/<pack_id>/memes/<分类>/<图片>
所以只要把图落到对应分类目录里，插件自己就能取到（它自带 sha256 判重）。

取图优先级：
    1. 图文作品（images 列表）→ 全下
    2. 视频的 dynamic_cover（动图 webp）
    3. 视频的 cover（静态图）

CLI:
    python meme_collect.py --scan "哈基米 表情包"            # 搜一轮，收匹配的
    python meme_collect.py --scan "鲸鱼娘 表情包" --max 5
    python meme_collect.py --aweme 7519350375590612251       # 收单条
    python meme_collect.py --frames 7519350375590612251      # 视频抽帧（表情包合集）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
import io
from pathlib import Path

import requests
from PIL import Image, ImageChops

from vlm_emotion import classify_image

HERE = Path(__file__).resolve().parent
STATE = HERE / "storage_state.json"
PACKS = HERE.parent.parent / 'astrbot' / 'data' / 'plugin_data' / 'meme_manager' / 'packs'
inbox = HERE / "meme_inbox"   # 沙箱写不进 packs 时先落这里，由 douyin_bridge 插件搬进去
DONE_FILE = HERE / "meme_collected.json"
FFMPEG = Path(os.environ.get("FFMPEG", shutil.which("ffmpeg") or ""))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 命中这些主题词时优先收（偏向喜欢猫猫 / 鲸鱼娘）；未命中也不拒收（收集放开）
WHALE_KEYS = ("鲸鱼娘", "鲸鱼", "大肥鱼", "deepseek娘")
CAT_KEYS = ("哈基米", "猫猫", "猫咪", "耄耋", "小猫", "橘猫", "猫")
MEME_HINT = ("表情包", "表情", "梗图", "沙雕图", "头像", "自取", "随便拿")

# 情绪分类（入库时不再按文案关键词猜，改由三模型识图投票决定，见 _classify_emotion）
EMOTIONS_COPY = ("开心", "委屈", "嫌弃", "撒娇", "困", "打工", "摸鱼", "无语")


def log(*a):
    print(*a, flush=True)


def _load_cookies() -> tuple[str, dict]:
    d = json.loads(STATE.read_text(encoding="utf-8"))
    ck = {c["name"]: c["value"] for c in d["cookies"]}
    return "; ".join(f"{k}={v}" for k, v in ck.items()), ck


def _headers(ck_str: str, ck: dict) -> dict:
    return {
        "User-Agent": UA,
        "Cookie": ck_str,
        "Host": "www.douyin.com",
        "Referer": "https://www.douyin.com/",
        "x-tt-argus": "1",
        "uifid": ck.get("UIFID") or ck.get("UIFID_TEMP", ""),
    }


def fetch_detail(aweme_id: str, ck_str: str, ck: dict) -> dict:
    params = {
        "aweme_id": aweme_id,
        "uifid": ck.get("UIFID") or ck.get("UIFID_TEMP", ""),
        "verifyFp": ck.get("s_v_web_id", ""),
        "fp": ck.get("s_v_web_id", ""),
    }
    r = requests.get(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/",
        params=params,
        headers=_headers(ck_str, ck),
        timeout=30,
    )
    return (r.json() or {}).get("aweme_detail") or {}


def _existing_hashes() -> set[str]:
    hs = set()
    if not PACKS.is_dir():
        return hs
    for p in PACKS.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            try:
                hs.add(hashlib.sha256(p.read_bytes()).hexdigest())
            except OSError:
                pass
    return hs


def _pack_of(desc: str) -> str:
    """按文案把作品分到 pack（仅物理分区，偏向喜欢猫猫/鲸鱼娘）。

    收集已放开：不命中主题词也收，只是默认归类到 whale-chan。
    真正的情绪分类不在这里，由 _classify_emotion 的三模型识图投票决定。
    """
    t = (desc or "").lower()
    if any(k.lower() in t for k in WHALE_KEYS):
        return "whale-chan"
    if any(k.lower() in t for k in CAT_KEYS):
        return "cat-hajimi"
    return "whale-chan"


def _classify_emotion(data: bytes, ext: str) -> tuple[str, dict] | None:
    """三模型识情投票：flash + 豆包 + 千问，取最多的那个情感收录。

    三者结果不一样（无 ≥2 票一致）→ 舍弃（None），不入库。
    """
    emo, votes = classify_image(data, ext)
    if not emo:
        return None
    return emo, votes


def _ext_from(data: bytes, url: str) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)", url or "", re.I)
    return "." + (m.group(1).lower() if m else "jpg")


def _ring_bg(im, step: int = 2) -> tuple:
    """取边框环像素的 24 阶梯桶众数作背景色（抗渐变/JPEG 噪声）。"""
    from collections import Counter
    w, h = im.size
    px = im.load()
    ring = []
    for x in range(0, w, step):
        ring.append(px[x, 0]); ring.append(px[x, h - 1])
    for y in range(1, h - 1, step):
        ring.append(px[0, y]); ring.append(px[w - 1, y])
    buckets = Counter()
    for p in ring:
        buckets[(p[0] // 24 * 24, p[1] // 24 * 24, p[2] // 24 * 24)] += 1
    bg0, _ = buckets.most_common(1)[0]
    return tuple(bg0[i] + 12 for i in range(3))


def _dark_crop_box(im, bg: tuple, margin: int = 6, area_floor: float = 0.9):
    """暗底图（深蓝/黑背景）：内容 = 离 bg 曼哈顿距离 >100 且非近白；
    主体 = 最大列 run（col ≥0.12*max），y = 主体 x 区间内 row ≥0.06*max 的上下界。
    返回 (l, u, r, b) 或 None（找不到主体 / 裁太少不值得）。"""
    try:
        w, h = im.size
        px = im.load()

        def content(p):
            d = abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2])
            return d > 100 and (765 - (p[0] + p[1] + p[2])) > 100

        cc = [0] * w
        for x in range(w):
            for y in range(h):
                if content(px[x, y]):
                    cc[x] += 1
        maxc = max(cc)
        if maxc <= 0:
            return None
        runs, cur = [], None
        thr = 0.12 * maxc
        for i, v in enumerate(cc):
            if v >= thr:
                if cur is None:
                    cur = [i, i, v]
                else:
                    cur[1] = i; cur[2] += v
            elif cur is not None:
                runs.append(tuple(cur)); cur = None
        if cur is not None:
            runs.append(tuple(cur))
        if not runs:
            return None
        best = max(runs, key=lambda t: t[2])  # 面积最大者为主体
        xl, xr = best[0], best[1]
        rc = [0] * h
        for y in range(h):
            for x in range(xl, xr + 1):
                if content(px[x, y]):
                    rc[y] += 1
        maxr = max(rc)
        if maxr <= 0:
            return None
        rowthr = 0.06 * maxr
        # y 界取行 profile 面积最大的连续 run —— 剔除顶部渐变带/白卡文字带等薄条
        yruns, cur = [], None
        for y, v in enumerate(rc):
            if v >= rowthr:
                if cur is None:
                    cur = [y, y, v]
                else:
                    cur[1] = y; cur[2] += v
            elif cur is not None:
                yruns.append(tuple(cur)); cur = None
        if cur is not None:
            yruns.append(tuple(cur))
        if not yruns:
            return None
        ybest = max(yruns, key=lambda t: t[2])
        yt, yb = ybest[0], ybest[1]
        l, u = max(0, xl - margin), max(0, yt - margin)
        r, b = min(w, xr + 1 + margin), min(h, yb + 1 + margin)
        if (r - l) * (b - u) >= w * h * area_floor:  # 裁太少不值得动
            return None
        return (l, u, r, b)
    except Exception:
        return None


def _auto_crop(data: bytes, ext: str) -> bytes:
    """裁掉图片四周无效留白：暗底（深蓝/黑背景）用主体 run 裁剪，亮底用白底比较。动画 GIF 跳过。"""
    try:
        # 动图不处理（抽帧可能多帧，且抖音表情包动图罕见留白问题）
        if ext == '.gif' and data[:3] == b'GIF':
            im = Image.open(io.BytesIO(data))
            if getattr(im, 'is_animated', im.n_frames > 1):
                return data
        im = Image.open(io.BytesIO(data))
        keep_alpha = im.mode == 'RGBA'
        im_rgb = im.convert('RGB')
        bg = _ring_bg(im_rgb)
        # 暗底：深蓝/黑背景 + 主体只占局部（如抖音封面卡）
        box = _dark_crop_box(im_rgb, bg, margin=6) if sum(bg) < 300 else None
        if box is None:
            # 亮底：白底比较（原逻辑）
            w, h = im_rgb.size
            bg_im = Image.new('RGB', im_rgb.size, (255, 255, 255))
            diff = ImageChops.difference(im_rgb, bg_im).convert('L')
            diff = diff.point(lambda x: 255 if x > 12 else 0)
            bbox = diff.getbbox()
            if bbox is None:
                return data  # 全白图，不动
            l, u, r, b = bbox
            l, u = max(0, l - 3), max(0, u - 3)
            r, b = min(w, r + 3), min(h, b + 3)
            box = (l, u, r, b)
        img = im if keep_alpha else im_rgb
        cropped = img.crop(box)
        buf = io.BytesIO()
        save_fmt = 'PNG' if ext == '.png' else 'JPEG'
        cropped.save(buf, format=save_fmt, optimize=True)
        return buf.getvalue()
    except Exception:
        return data


def _save(pack_id: str, category: str, data: bytes, name: str, seen: set[str], aweme_id: str, url: str) -> bool:
    if not data or len(data) < 3_000:  # 太小基本是占位图
        return False
    ext = _ext_from(data, url)
    data = _auto_crop(data, ext)        # 裁掉白底/透明底无效留白
    h = hashlib.sha256(data).hexdigest()
    if h in seen:
        return False
    out_dir = PACKS / pack_id / "memes" / category
    path = out_dir / f"dy_{aweme_id}_{name}{ext}"
    if path.exists():
        return False
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        log(f"  + {pack_id}/{category}/{path.name} ({len(data)//1024} KB)")
    except (PermissionError, OSError):
        # 沙箱里的解释器写不了 AstrBot 目录 → 落到 inbox，由 douyin_bridge 插件搬进 packs
        inbox.mkdir(parents=True, exist_ok=True)
        drop = inbox / f"{pack_id}__{category}__dy_{aweme_id}_{name}{ext}"
        drop.write_bytes(data)
        log(f"  > inbox/{drop.name} ({len(data)//1024} KB) 待插件入库")
    seen.add(h)
    return True


def _download(url: str) -> bytes:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=40)
        return r.content if r.status_code == 200 else b""
    except Exception:
        return b""


def collect_aweme(aweme_id: str, seen: set[str] | None = None) -> dict:
    ck_str, ck = _load_cookies()
    aw = fetch_detail(aweme_id, ck_str, ck)
    if not aw:
        # 偶发风控会把 aweme_detail 置 null，隔一秒再试一次
        time.sleep(1.2)
        aw = fetch_detail(aweme_id, ck_str, ck)
    if not aw:
        return {"aweme_id": aweme_id, "ok": False, "error": "取不到作品详情（可能已删/风控）"}
    desc = aw.get("desc", "") or ""
    pack_id = _pack_of(desc)
    seen = seen if seen is not None else _existing_hashes()
    added = 0
    emotions: list[str] = []

    imgs = aw.get("images")
    if isinstance(imgs, list) and imgs:
        for i, im in enumerate(imgs):
            for u in (im.get("url_list") or [])[:1]:
                data = _download(u)
                if not data:
                    continue
                r = _classify_emotion(data, _ext_from(data, u))
                if not r:
                    log(f"  x 三模型不一致/无效，舍弃 {aweme_id}/{i}")
                    continue
                emo, votes = r
                emotions.append(emo)
                if _save(pack_id, emo, data, str(i), seen, aweme_id, u):
                    added += 1
    else:
        v = aw.get("video") or {}
        for key in ("dynamic_cover", "cover_original_scale", "cover", "origin_cover"):
            urls = ((v.get(key) or {}).get("url_list") or [])
            if not urls:
                continue
            data = _download(urls[0])
            if not data:
                continue
            r = _classify_emotion(data, _ext_from(data, urls[0]))
            if not r:
                log(f"  x 三模型不一致/无效，舍弃 {aweme_id}/{key}")
                continue
            emo, votes = r
            emotions.append(emo)
            if _save(pack_id, emo, data, key, seen, aweme_id, urls[0]):
                added += 1
            break
    return {"aweme_id": aweme_id, "ok": True, "added": added, "pack": pack_id,
            "category": emotions[0] if emotions else "", "desc": desc[:40]}


def collect_frames(aweme_id: str, max_frames: int = 12) -> dict:
    """视频抽帧：表情包合集类视频，用 ffmpeg 场景切换抽图。"""
    ck_str, ck = _load_cookies()
    aw = fetch_detail(aweme_id, ck_str, ck)
    if not aw:
        return {"aweme_id": aweme_id, "ok": False, "error": "取不到详情"}
    desc = aw.get("desc", "") or ""
    pack_id = _pack_of(desc)
    v = aw.get("video") or {}
    urls = ((v.get("play_addr") or {}).get("url_list") or []) or ((v.get("download_addr") or {}).get("url_list") or [])
    if not urls:
        return {"aweme_id": aweme_id, "ok": False, "error": "没有播放地址"}
    seen = _existing_hashes()
    with tempfile.TemporaryDirectory() as td:
        mp4 = Path(td) / "v.mp4"
        mp4.write_bytes(_download(urls[0]))
        if mp4.stat().st_size < 50_000:
            return {"aweme_id": aweme_id, "ok": False, "error": "视频下载失败/过小"}
        out = Path(td) / "f_%03d.jpg"
        subprocess.run(
            [str(FFMPEG), "-y", "-i", str(mp4), "-vf",
             f"select='gt(scene,0.35)',scale=600:-2", "-vsync", "vfr", "-frames:v", str(max_frames), str(out)],
            capture_output=True,
        )
        added = 0
        emotions: list[str] = []
        for f in sorted(Path(td).glob("f_*.jpg")):
            data = f.read_bytes()
            r = _classify_emotion(data, ".jpg")
            if not r:
                log(f"  x 三模型不一致/无效，舍弃 {f.name}")
                continue
            emo, votes = r
            emotions.append(emo)
            if _save(pack_id, emo, data, f.stem, seen, aweme_id, str(f)):
                added += 1
    return {"aweme_id": aweme_id, "ok": True, "added": added, "pack": pack_id,
            "category": emotions[0] if emotions else "", "desc": desc[:40]}


def _done() -> dict:
    if DONE_FILE.exists():
        try:
            return json.loads(DONE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_done(d: dict) -> None:
    DONE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _search_params(keyword: str, count: int) -> dict:
    """搜索参数（与 search.py 一致；搜索不需要 a_bogus）。"""
    return {
        "search_channel": "aweme_general", "enable_history": "1", "keyword": keyword,
        "search_source": "tab_search", "query_correct_type": "1", "is_filter_search": "0",
        "from_group_id": "7378810571505847586", "offset": "0", "count": str(count),
        "need_filter_settings": "1", "list_type": "multi", "search_id": "",
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "version_code": "190600", "version_name": "19.6.0", "update_version_code": "170400",
        "pc_client_type": "1", "cookie_enabled": "true", "browser_language": "zh-CN",
        "browser_platform": "Win32", "browser_name": "Chrome", "browser_version": "125.0.0.0",
        "browser_online": "true", "engine_name": "Blink", "os_name": "Windows",
        "os_version": "10", "cpu_core_num": "8", "device_memory": "8",
        "engine_version": "109.0", "platform": "PC", "screen_width": "1920",
        "screen_height": "1080", "effective_type": "4g", "round_trip_time": "50",
        "webid": "7701340071136069120",
    }


def _search(keyword: str, count: int = 15) -> list[dict]:
    import urllib.parse

    ck_str, ck = _load_cookies()
    uifid = ck.get("UIFID") or ck.get("UIFID_TEMP", "")
    referer = f"https://www.douyin.com/search/{keyword}?aid=f594bbd9-a0e2-4651-9319-ebe3cb6298c1&type=general"
    headers = {
        "User-Agent": UA, "Cookie": ck_str, "Host": "www.douyin.com",
        "Origin": "https://www.douyin.com/",
        "Referer": urllib.parse.quote(referer, safe=":/"),
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "x-tt-argus": "1", "uifid": uifid,
    }
    r = requests.get(
        "https://www.douyin.com/aweme/v1/web/general/search/single/",
        params=_search_params(keyword, count), headers=headers, timeout=30,
    )
    out = []
    for it in (r.json() or {}).get("data") or []:
        aw = it.get("aweme_info") or {}
        if aw.get("aweme_id"):
            desc = aw.get("desc", "") or ""
            out.append({"aweme_id": str(aw["aweme_id"]), "desc": desc,
                        "author": (aw.get("author") or {}).get("nickname", ""),
                        "urls": _item_urls(aw)})
    return out


def _item_urls(aw: dict) -> list[str]:
    """从 aweme_info 里取图（图文取全部 images，视频取封面）。"""
    urls = []
    for im in (aw.get("images") or []):
        for u in (im.get("url_list") or [])[:1]:
            urls.append(u)
    if urls:
        return urls
    v = aw.get("video") or {}
    for k in ("dynamic_cover", "cover_original_scale", "cover", "origin_cover"):
        u = ((v.get(k) or {}).get("url_list") or [])
        if u:
            return [u[0]]
    return []


def save_from_urls(desc: str, urls: list[str], aweme_id: str, seen: set[str] | None = None) -> dict:
    """直接用已知图片 URL 入库（search / feed 结果里就带 URL，省掉详情请求，少碰风控）。"""
    pack_id = _pack_of(desc)
    seen = seen if seen is not None else _existing_hashes()
    added = 0
    emotions: list[str] = []
    for i, u in enumerate(urls[:12]):
        data = _download(u)
        if not data:
            continue
        r = _classify_emotion(data, _ext_from(data, u))
        if not r:
            log(f"  x 三模型不一致/无效，舍弃 {aweme_id}/{i}")
            continue
        emo, votes = r
        emotions.append(emo)
        if _save(pack_id, emo, data, str(i), seen, aweme_id, u):
            added += 1
    return {"aweme_id": aweme_id, "ok": True, "added": added,
            "pack": pack_id, "category": emotions[0] if emotions else "", "desc": desc[:60]}


def _pref_rank(desc: str) -> int:
    """收集偏好：命中猫猫/鲸鱼娘主题词的排前面（0 优先，1 普通）。"""
    t = (desc or "").lower()
    if any(k.lower() in t for k in WHALE_KEYS) or any(k.lower() in t for k in CAT_KEYS):
        return 0
    return 1


def scan(keyword: str, limit: int = 6) -> dict:
    hits = _search(keyword, count=15)
    # 偏向喜欢猫猫 / 鲸鱼娘：命中的先收，其余照收不误
    hits.sort(key=lambda v: _pref_rank(v.get("desc") or ""))
    log(f"[scan] {keyword} → {len(hits)} 条")
    done = _done()
    seen = _existing_hashes()
    total = 0
    results = []
    for v in hits:
        if total >= limit:
            break
        aid = v.get("aweme_id")
        if not aid or aid in done:
            continue
        desc = v.get("desc") or ""
        urls = v.get("urls") or []
        r = save_from_urls(desc, urls, aid, seen) if urls else collect_aweme(aid, seen)
        results.append(r)
        if r.get("ok") and r.get("added"):
            total += r["added"]
            done[aid] = {"at": time.strftime("%Y-%m-%d %H:%M"), "added": r["added"]}
    _save_done(done)
    return {"ok": True, "keyword": keyword, "added": total, "results": results}



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", default=None)
    ap.add_argument("--aweme", default=None)
    ap.add_argument("--frames", default=None)
    ap.add_argument("--save-url", nargs="+", default=None)
    ap.add_argument("--desc", default="")
    ap.add_argument("--max", type=int, default=6)
    ap.add_argument("--max-frames", type=int, default=12)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.save_url:
        print(json.dumps(
            save_from_urls(args.desc or "", args.save_url, args.aweme or "manual"),
            ensure_ascii=False))
    elif args.scan:
        print(json.dumps(scan(args.scan, args.max), ensure_ascii=False))
    elif args.aweme:
        print(json.dumps(collect_aweme(args.aweme), ensure_ascii=False))
    elif args.frames:
        print(json.dumps(collect_frames(args.frames, args.max_frames), ensure_ascii=False))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
