# -*- coding: utf-8 -*-
"""把旧 whale-chan/memes/鲸鱼/ 的表情包按三模型识情投票重归类。

每个文件：flash + 豆包 + 千问 各识别一次情绪 → 取多数（≥2 票一致）那个情感，
移动到 whale-chan/memes/<情感>/；三者全不同（无多数）→ 移到 meme_inbox/鲸鱼_未定/
（不删，留给人看）。迁移后 whale-chan 只按情感分类。

用法：
    python migrate_whale.py            # 全量迁移（137 张，约 20 分钟）
    python migrate_whale.py --limit 5  # 先试 5 张
    python migrate_whale.py --dry-run  # 只打印结果，不移动文件
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from vlm_emotion import classify_file

PACKS = Path(__file__).parent.parent / 'astrbot' / 'data' / 'plugin_data' / 'meme_manager' / 'packs'
SRC = PACKS / "whale-chan" / "memes" / "鲸鱼"
PACK = PACKS / "whale-chan" / "memes"
DISCARD = Path(__file__).resolve().parent / "meme_inbox" / "鲸鱼_未定"
EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_lock = threading.Lock()


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张（0=全部）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=4, help="并发处理图片数")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    files = sorted(f for f in SRC.iterdir() if f.is_file() and f.suffix.lower() in EXTS)
    if args.limit:
        files = files[: args.limit]
    print(f"[migrate] 待迁移 {len(files)} 张（来源 {SRC}），并发 {args.workers}", flush=True)

    moved: dict[str, int] = {}
    discarded: list[str] = []
    errors: list[str] = []
    t0 = time.time()
    total = len(files)

    def work(f: Path):
        emo, votes = classify_file(f)
        if emo:
            if not args.dry_run:
                dst_dir = PACK / emo
                dst_dir.mkdir(parents=True, exist_ok=True)
                f.rename(_unique(dst_dir / f.name))
            return ("moved", f.name, emo, votes)
        discarded_local = f.name
        if not args.dry_run:
            DISCARD.mkdir(parents=True, exist_ok=True)
            f.rename(_unique(DISCARD / f.name))
        return ("discarded", discarded_local, None, votes)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(work, f): f for f in files}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                kind, name, emo, votes = fut.result()
            except Exception as e:
                with _lock:
                    errors.append(str(e))
                    print(f"[{n}/{total}] ERR {e}", flush=True)
                continue
            with _lock:
                if kind == "moved":
                    moved[emo] = moved.get(emo, 0) + 1
                    print(f"[{n}/{total}] {name} → {emo} {votes}", flush=True)
                else:
                    discarded.append(name)
                    print(f"[{n}/{total}] {name} 舍弃 {votes}", flush=True)

    print(json.dumps({
        "ok": True, "dry_run": args.dry_run, "total": len(files),
        "moved": moved, "discarded": len(discarded), "discarded_files": discarded,
        "errors": errors, "elapsed_s": round(time.time() - t0, 1),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
