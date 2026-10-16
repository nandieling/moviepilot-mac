#!/usr/bin/env python3
"""生成包内 AppIcon.icns。

母版优先用前端产物里的 pwa-1024x1024.png，随上游版本自动换图；
macOS 的图标惯例是圆角方块，直接塞方图会显得像贴了张 Windows 磁贴，
所以先用 Pillow 做一层 iOS 同款超椭圆遮罩（没装 Pillow 就退回方图）。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SIZES = [(16, False), (32, True), (32, False), (64, True), (128, False), (256, True),
         (256, False), (512, True), (512, False), (1024, True)]
CORNER_RATIO = 0.2237  # iOS/macOS 圆角方块的比例


def _run(cmd: list[str]) -> None:
    subprocess.run([str(c) for c in cmd], check=True, capture_output=True)


def master_from_frontend(archive: Path) -> Path | None:
    """从已缓存的前端 zip 里取母版，优先非 maskable 的那张。"""
    if not archive.is_file():
        return None
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        for candidate in ("dist/pwa-1024x1024.png", "pwa-1024x1024.png",
                          "dist/icon.png", "icon.png"):
            if candidate in names:
                out = Path(tempfile.mkdtemp(prefix="icon-")) / "master.png"
                out.write_bytes(bundle.read(candidate))
                return out
    return None


def round_corners(source: Path, dest: Path) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    image = Image.open(source).convert("RGBA")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, side - 1, side - 1],
                                           radius=int(side * CORNER_RATIO), fill=255)
    image.putalpha(mask)
    image.save(dest)
    return True


def build_icns(master: Path, out: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="iconset-") as staging:
        iconset = Path(staging) / "AppIcon.iconset"
        iconset.mkdir()
        for size, retina in SIZES:
            pixel = size * (2 if retina else 1)
            name = f"icon_{size}x{size}" + ("@2x" if retina else "") + ".png"
            target = iconset / name
            _run(["sips", "-z", pixel, pixel, str(master), "--out", str(target)])
        if out.exists():
            out.unlink()
        _run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-zip", type=Path, help="已缓存的前端 dist.zip，用来取母版")
    parser.add_argument("--source", type=Path, help="直接指定母版 PNG（优先于 --frontend-zip）")
    parser.add_argument("--out", type=Path, required=True, help="输出 AppIcon.icns")
    parser.add_argument("--no-round", action="store_true", help="不做圆角遮罩")
    args = parser.parse_args()

    source = args.source
    if source is None and args.frontend_zip:
        source = master_from_frontend(args.frontend_zip)
    if source is None or not Path(source).is_file():
        raise SystemExit("找不到图标母版：给 --source 或先取前端产物")

    master = Path(source)
    if not args.no_round:
        rounded = master.parent / "master-rounded.png"
        if round_corners(master, rounded):
            master = rounded
            print("  图标已按 macOS 惯例做圆角遮罩")
        else:
            print("  未安装 Pillow，图标保持方图（pip install pillow 可启用圆角）")

    build_icns(master, args.out)
    print(f"  图标：{args.out}（{args.out.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
