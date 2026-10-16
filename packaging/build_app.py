#!/usr/bin/env python3
"""组装 MoviePilot.app。

开发模式把本地 CLI 版仓库当运行时，秒级出包，用于验证壳层与托管层；
发布模式从上游 tag 组装自包含运行时、配套前端与签名。
"""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
HOST_BINARY = ROOT / "host" / ".build" / "release" / "MoviePilotHost"
FRONTEND_RELEASE_URL = "https://github.com/jxxghp/MoviePilot-Frontend/releases/download/{tag}/dist.zip"


def run(*cmd: str, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def build_host() -> Path:
    run("swift", "build", "-c", "release", cwd=ROOT / "host")
    if not HOST_BINARY.is_file():
        raise SystemExit(f"宿主可执行文件缺失：{HOST_BINARY}")
    return HOST_BINARY


def write_info_plist(contents: Path, short_version: str, build_version: str, appcast_url: str) -> None:
    template = (PACKAGING / "Info.plist").read_text(encoding="utf-8")
    payload = (
        template.replace("__SHORT_VERSION__", short_version)
        .replace("__BUILD_VERSION__", build_version)
        .replace("__APPCAST_URL__", appcast_url)
        .replace("__SPARKLE_KEY__", "")
    )
    (contents / "Info.plist").write_text(payload, encoding="utf-8")
    plistlib.loads(payload.encode())  # 立刻验证 plist 合法


def assemble(app: Path, short_version: str, build_version: str, appcast_url: str) -> Path:
    binary = build_host()
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    resources = contents / "Resources"
    resources.mkdir()
    shutil.copy2(binary, contents / "MacOS" / "MoviePilotHost")
    shutil.copytree(ROOT / "pythonhost" / "mpweb", resources / "pythonhost" / "mpweb")
    shutil.copy2(PACKAGING / "MoviePilot.entitlements", resources / "MoviePilot.entitlements")
    write_info_plist(contents, short_version, build_version, appcast_url)
    return app


def sign_dev(app: Path) -> None:
    """开发包只做 ad-hoc 签名：够验证路径与权限，不依赖开发者证书。"""
    run(
        "codesign",
        "--force",
        "--sign",
        "-",
        "--options",
        "runtime",
        "--entitlements",
        str(app / "Contents" / "Resources" / "MoviePilot.entitlements"),
        str(app),
    )


def build_dev(dev_root: Path, out: Path, appcast_url: str) -> Path:
    if not (dev_root / "venv" / "bin" / "python").is_file():
        raise SystemExit(f"{dev_root} 不是可用的 CLI 版仓库（缺少 venv）")
    version = read_versions(dev_root / "version.py")
    app = assemble(out / "MoviePilot.app", version["APP_VERSION"], version["APP_VERSION"], appcast_url)
    (app / "Contents" / "Resources" / "dev-runtime.json").write_text(
        json.dumps({"backendRoot": str(dev_root)}, indent=2) + "\n", encoding="utf-8"
    )
    sign_dev(app)
    print(f"开发包已生成：{app}")
    print(f"运行：MOVPILOT_DEV_ROOT={dev_root} open {app}")
    return app


def read_versions(version_file: Path) -> dict[str, str]:
    """从上游 version.py 取版本号，前后端版本必须成对解析。"""
    values: dict[str, str] = {}
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        name, _, raw = line.partition("=")
        values[name.strip()] = raw.strip().strip("'\"")
    missing = {"APP_VERSION", "FRONTEND_VERSION"} - values.keys()
    if missing:
        raise SystemExit(f"版本文件缺少字段：{sorted(missing)}")
    return values


def fetch_frontend(tag: str, target: Path) -> None:
    url = FRONTEND_RELEASE_URL.format(tag=tag)
    with urllib.request.urlopen(url, timeout=120) as response, tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        shutil.copyfileobj(response, Path(tmp.name))
        archive = Path(tmp.name)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if "version.txt" not in names:
            raise SystemExit("前端发布包缺少 version.txt，拒绝安装")
        bundle.extractall(target)
    archive.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="组装 MoviePilot.app")
    parser.add_argument("--dev-root", help="本地 CLI 版仓库路径，走开发模式")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="输出目录")
    parser.add_argument("--appcast-url", default="https://moviepilot.org/appcast/stable.xml")
    parser.add_argument("--tag", help="上游后端 tag，发布模式必填")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    if args.dev_root:
        build_dev(Path(args.dev_root).expanduser().resolve(), args.out, args.appcast_url)
        return 0
    if not args.tag:
        raise SystemExit("发布模式需要 --tag，或使用 --dev-root 走开发模式")
    # 发布模式：运行时与源码来自上游 tag，前端按 version.py 声明的配套版本取包。
    raise SystemExit("发布模式待接入 python-build-standalone 运行时下载（M2 里程碑）")


if __name__ == "__main__":
    sys.exit(main())
