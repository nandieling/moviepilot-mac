#!/usr/bin/env python3
"""组装包内 Python 运行时：下载独立解释器、按上游锁文件装依赖、预编译中文检索扩展。

依赖直接装进解释器自己的 site-packages（不建 venv），宿主里的 runtime/bin/python3
就是最终跑的 python，sys.prefix 也就落在包内。装完做一次性导入验收。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lib as rl

# 打包不需要的那些：tkinter 及随附的 tcl/tk、idle、自带测试与自举 pip
PRUNE = ["include", "share"]
PRUNE_GLOBS = ["lib/tcl*", "lib/itcl*", "lib/thread*", "lib/tk*", "lib/libtcl*", "lib/libtk*",
               "lib/python3.*/idlelib", "lib/python3.*/turtledemo", "lib/python3.*/tkinter",
               "lib/python3.*/test", "lib/python3.*/lib2to3", "lib/python3.*/ensurepip"]


def extract_runtime(archive: Path, dest: Path) -> None:
    """独立运行时解压出来是 python/ 一层，内容提到 dest 下。"""
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-") as staging:
        root = Path(staging) / "python"
        with tarfile.open(archive) as tar:
            # 包内自带 python/ 一层，解到 staging 而不是 root
            tar.extractall(staging, filter="data")
        if not (root / "bin" / "python3").exists():
            raise SystemExit(f"{archive.name} 里没有 bin/python3，包结构变了")
        for child in root.iterdir():
            target = dest / child.name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            child.replace(target)


def prune(dest: Path) -> int:
    before = rl.dir_size_mb(dest)
    for rel in PRUNE:
        target = dest / rel
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
    for pattern in PRUNE_GLOBS:
        for target in sorted(dest.glob(pattern)):
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
    return before - rl.dir_size_mb(dest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", required=True, type=Path, help="Resources/runtime")
    parser.add_argument("--src", required=True, type=Path, help="后端源码树（取 uv.lock 与 native/）")
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parent / "runtime-manifest.json")
    parser.add_argument("--cache-dir", type=Path, default=rl.DEFAULT_CACHE)
    parser.add_argument("--skip-deps", action="store_true", help="只解解释器，装依赖留给后面")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    spec = manifest["python"]
    cpu = rl.host_cpu()
    if cpu not in spec["assets"]:
        raise SystemExit(f"清单里没有 {cpu} 的运行时条目")
    asset = spec["assets"][cpu]

    uv = rl.require("uv", "装依赖要用它解析上游锁文件：brew install uv 或 curl -LsSf https://astral.sh/uv/install.sh | sh")
    info = args.dest / "runtime-info.json"
    lock_hash = rl.sha256_file(args.src / "uv.lock")
    if info.is_file() and not args.force:
        recorded = json.loads(info.read_text(encoding="utf-8"))
        if recorded.get("runtime_sha256") == asset["sha256"] and recorded.get("lock_sha256") == lock_hash:
            rl.log(f"  运行时已就绪（Python {recorded['python']}，依赖 {recorded['deps_count']} 个），跳过")
            return 0

    args.dest.parent.mkdir(parents=True, exist_ok=True)
    if args.force and args.dest.exists():
        shutil.rmtree(args.dest)

    archive = rl.download(asset["url"], args.cache_dir / asset["file"], asset["sha256"],
                          label=f"Python {spec['version']}")
    rl.log(f"  解包运行时 -> {args.dest}")
    extract_runtime(archive, args.dest)
    python = args.dest / "bin" / "python3"

    deps_count = 0
    if not args.skip_deps:
        with tempfile.TemporaryDirectory(prefix="reqs-") as tmp:
            reqs = Path(tmp) / "requirements.txt"
            rl.run([uv, "export", "--frozen", "--no-emit-project", "--no-annotate",
                    "--format", "requirements-txt", "-o", reqs], cwd=args.src)
            rl.log("  安装上游锁定依赖（只装本机架构需要的轮子）")
            rl.run([uv, "pip", "install", "--python", python, "--no-progress",
                    "--requirements", reqs])
        # 构建机自身的 python 和包内解释器版本可能不同，按包内实际目录找
        site = next(iter(sorted(args.dest.glob("lib/python3.*/site-packages"))), None)
        if site is None:
            raise SystemExit("装完依赖找不到 site-packages，解释器布局变了")
        deps_count = sum(1 for p in site.glob("*.dist-info"))
        rl.log(f"  已安装 {deps_count} 个包")

        rl.log("  预编译中文检索扩展")
        rl.run([python, args.src / "native" / "fts5_cjk" / "install.py"])

    reclaimed = prune(args.dest)
    info.write_text(json.dumps({
        "python": spec["version"], "channel": spec["channel"], "cpu": cpu,
        "runtime_sha256": asset["sha256"], "lock_sha256": lock_hash,
        "deps_count": deps_count, "pruned_mb": reclaimed,
        "size_mb": rl.dir_size_mb(args.dest),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    rl.log(f"  运行时就绪：Python {spec['version']} / {deps_count} 个依赖 / "
           f"剪掉 {reclaimed} MB / 共 {rl.dir_size_mb(args.dest)} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
