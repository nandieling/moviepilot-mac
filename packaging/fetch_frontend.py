#!/usr/bin/env python3
"""按下游 tag 取前端 dist.zip，校验后解压到目标目录。

前端产物随 tag 变，版本由后端 version.py 的 FRONTEND_VERSION 决定；
校验值向 GitHub 取（产物自带 digest），并把结果记在解压目录里，重复构建不再联网。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lib as rl


def _dist_prefix(names: list[str], asset: str) -> str:
    """上游包有时把产物放在 dist/ 一层里，统一拉平到根。"""
    if "index.html" in names:
        return ""
    tops = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(tops) == 1 and f"{tops.pop()}/index.html" in names:
        return next(p for p in (name.split("/", 1)[0] for name in names) if f"{p}/index.html" in names)
    raise SystemExit(f"{asset} 里找不到 index.html，不像是前端 dist 包")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="前端 tag，例如 v3.1.6")
    parser.add_argument("--dest", required=True, type=Path, help="解压目标目录")
    parser.add_argument("--repo", default="jxxghp/MoviePilot-Frontend")
    parser.add_argument("--asset", default="dist.zip")
    parser.add_argument("--sha256", help="跳过 API 查询，直接用这个校验值")
    parser.add_argument("--cache-dir", type=Path, default=rl.DEFAULT_CACHE)
    parser.add_argument("--force", action="store_true", help="即使 provenance 相同也重新解压")
    args = parser.parse_args()

    if args.sha256:
        digest, url = args.sha256, None
    else:
        url, digest = rl.github_asset(args.repo, args.tag, args.asset)

    marker = args.dest / ".provenance.json"
    if marker.is_file() and not args.force:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        if recorded.get("sha256") == digest:
            rl.log(f"  前端已是 {args.tag}（{recorded['files']} 个文件），跳过")
            return 0
    # 换版本时先清掉上一版残留，否则新旧产物混在一起又会触发版本不一致
    if args.dest.is_dir():
        shutil.rmtree(args.dest)

    archive = rl.download(url or "", args.cache_dir / f"{args.asset}", digest, label=f"前端 {args.tag}")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        prefix = _dist_prefix(names, args.asset)
        args.dest.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="frontend-", dir=args.dest.parent))
        try:
            bundle.extractall(staging)
            for child in (staging / prefix).iterdir():
                target = args.dest / child.name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                child.replace(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # 前后端版本不成对就是「缓存版本不一致」弹窗的根源，宁可在这里失败
    declared = args.dest / "version.txt"
    if declared.is_file():
        actual = declared.read_text(encoding="utf-8").strip()
        if actual != args.tag:
            raise SystemExit(f"前端产物版本 {actual} 与要求的 {args.tag} 不一致，拒绝装入")

    files = [p for p in args.dest.rglob("*") if p.is_file() and p.name != ".provenance.json"]
    marker.write_text(json.dumps({
        "repo": args.repo, "tag": args.tag, "asset": args.asset, "sha256": digest,
        "files": len(files), "mb": rl.dir_size_mb(args.dest),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    rl.log(f"  前端 {args.tag}：{len(files)} 个文件 / {rl.dir_size_mb(args.dest)} MB，校验 {digest[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
