#!/usr/bin/env python3
"""从已组装好的 .app 切出前后端增分包。

只换后端源码与前端产物，内嵌运行时原封不动，更新包因此能从整包的体量降到百 MB 上下。
manifest.json 直接沿用包内 build-info.json，落地时靠包里的 uv.lock 摘要判断这次能不能只换源码。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lib as rl

CONTENTS = ("Contents", "Resources")
PAYLOADS = {"app": "app", "web": "web"}
# 应用内发布通道按这个后缀在 Release 里找包，改这里要同步 mpweb.release
ARCHIVE_SUFFIX = "-appweb.zip"


def _iter_files(root: Path, prefix: str) -> Iterator[tuple[Path, str]]:
    """按 zip 内路径遍历文件；软链目录（插件目录）整棵跳过，由落地端重建。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            path = Path(dirpath) / name
            if path.is_symlink():
                rl.log(f"  跳过软链目录：{prefix}/{path.relative_to(root)}")
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            yield path, f"{prefix}/{relative}"


def _entry(info: os.stat_result, arcname: str) -> zipfile.ZipInfo:
    """保留源文件时间与权限位：落地后 .pyc 的失效判断和可执行位都不该变。"""
    parsed = time.localtime(getattr(info, "st_mtime", 0))[:6]
    entry = zipfile.ZipInfo(
        arcname,
        date_time=(parsed[0], parsed[1], parsed[2], parsed[3], parsed[4], parsed[5] // 2 * 2),
    )
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = (info.st_mode & 0xFFFF) << 16
    return entry


def build_delta(app: Path, out: Path) -> Path:
    """切出 MoviePilot-<版本>-<架构>-appweb.zip 和它的 sha256 旁文件。"""
    resources = app.joinpath(*CONTENTS)
    build_info = resources / "build-info.json"
    if not build_info.is_file():
        raise SystemExit(f"{app} 不是发布组装出来的包（缺少 build-info.json）")
    info = json.loads(build_info.read_text(encoding="utf-8"))
    version = str((info.get("backend") or {}).get("tag") or "unknown")
    cpu = str(info.get("cpu") or rl.host_cpu())

    out.mkdir(parents=True, exist_ok=True)
    archive = out / f"MoviePilot-{version}-{cpu}{ARCHIVE_SUFFIX}"
    with zipfile.ZipFile(archive, "w", compresslevel=9) as bundle:
        manifest = dict(info)
        manifest["kind"] = "appweb"
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for name, prefix in PAYLOADS.items():
            source = resources / name
            if not source.is_dir():
                raise SystemExit(f"包里缺少 {name}，无法出增分包")
            rl.log(f"装入 {prefix}/")
            for path, arcname in _iter_files(source, prefix):
                bundle.writestr(_entry(path.stat(), arcname), path.read_bytes())
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    sidecar.write_text(f"{rl.sha256_file(archive)}  {archive.name}\n", encoding="utf-8")
    rl.log(f"增分包：{archive}（{round(archive.stat().st_size / 1024 / 1024)} MB）")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description="从 .app 切前后端增分包")
    parser.add_argument("--app", type=Path, required=True, help="发布组装出来的 MoviePilot.app")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "dist")
    args = parser.parse_args()
    build_delta(args.app.resolve(), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
