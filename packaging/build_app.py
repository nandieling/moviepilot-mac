#!/usr/bin/env python3
"""组装 MoviePilot.app。

开发模式把本地 CLI 版仓库当运行时，秒级出包，用于验证壳层与托管层；
发布模式从上游 tag 组装：源码用 git archive 取干净树（不含 public），
前端按 version.py 声明的配套 tag 取 dist.zip，运行时交给 build_runtime.py。
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lib as rl

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
HOST_BINARY = ROOT / "host" / ".build" / "release" / "MoviePilotHost"
# 后端不读这些目录：前端另有来源，其余是仓库开发件
SOURCE_DROP = ["public", "frontend-dist", ".github", "docs", "tests", ".devcontainer"]


def run(*cmd, cwd: Path | None = None) -> None:
    rl.run([str(c) for c in cmd], cwd=cwd)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def build_host() -> Path:
    run("swift", "build", "-c", "release", cwd=ROOT / "host")
    if not HOST_BINARY.is_file():
        raise SystemExit(f"宿主可执行文件缺失：{HOST_BINARY}")
    return HOST_BINARY


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


def assemble(out: Path, short_version: str, build_version: str, appcast_url: str) -> Path:
    binary = build_host()
    app = out / "MoviePilot.app"
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


def sign(app: Path, identity: str) -> None:
    """先签包内每个 Mach-O，再签整包；发布证书换掉 identity 就是同一套流程。"""
    entitlements = app / "Contents" / "Resources" / "MoviePilot.entitlements"
    signed = 0
    for path in rl.iter_macho(app / "Contents" / "Resources"):
        subprocess.run(["codesign", "--force", "--sign", identity, "--timestamp=none", str(path)],
                       check=True, capture_output=True)
        signed += 1
    rl.log(f"  已签名 {signed} 个内嵌二进制")
    run("codesign", "--force", "--sign", identity, "--options", "runtime",
        "--entitlements", entitlements, app)


def make_dmg(app: Path, out: Path) -> Path:
    version = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    staging = out / "dmg-staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    shutil.copytree(app, staging / app.name, symlinks=True)
    os.symlink("/Applications", staging / "Applications")
    dmg = out / f"MoviePilot-{version['CFBundleShortVersionString']}-{rl.host_cpu()}.dmg"
    run("hdiutil", "create", "-volname", "MoviePilot", "-srcfolder", staging, "-ov",
        "-format", "UDZO", "-imagekey", "zlib-level=6", dmg)
    shutil.rmtree(staging, ignore_errors=True)
    rl.log(f"  镜像：{dmg}（{rl.dir_size_mb(dmg)} MB）")
    return dmg


def build_dev(dev_root: Path, out: Path, appcast_url: str) -> Path:
    if not (dev_root / "venv" / "bin" / "python").is_file():
        raise SystemExit(f"{dev_root} 不是可用的 CLI 版仓库（缺少 venv）")
    version = read_versions(dev_root / "version.py")
    app = assemble(out, version["APP_VERSION"], version["APP_VERSION"], appcast_url)
    (app / "Contents" / "Resources" / "dev-runtime.json").write_text(
        json.dumps({"backendRoot": str(dev_root)}, indent=2) + "\n", encoding="utf-8")
    sign(app, "-")
    rl.log(f"开发包已生成：{app}")
    return app


def source_at(repo: Path, tag: str, dest: Path) -> str:
    """用 git archive 取该 tag 的干净源码树，不受本地工作区改动影响。"""
    commit = git(repo, "rev-parse", "--verify", f"{tag}^{{commit}}")
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as handle:
        archive = Path(handle.name)
    try:
        run("git", "-C", repo, "archive", "--format=tar", "-o", archive, commit)
        dest.mkdir(parents=True, exist_ok=True)
        run("tar", "-xf", archive, "-C", dest)
    finally:
        archive.unlink(missing_ok=True)
    for name in SOURCE_DROP:
        target = dest / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    return commit


def build_release(args, out: Path) -> Path:
    repo = Path(args.backend_repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"{repo} 不是 git 仓库，无法按 tag 取源码")
    with tempfile.TemporaryDirectory(prefix="backend-src-") as tmp:
        src = Path(tmp) / "backend"
        commit = source_at(repo, args.tag, src)
        version = read_versions(src / "version.py")
        frontend_tag = args.frontend_tag or version["FRONTEND_VERSION"]
        rl.log(f"后端 {version['APP_VERSION']}（{commit[:10]}）/ 前端 {frontend_tag}")

        app = assemble(out, version["APP_VERSION"], args.build_version or version["APP_VERSION"],
                       args.appcast_url)
        resources = app / "Contents" / "Resources"

        rl.log("装入后端源码")
        shutil.copytree(src, resources / "app")
        rl.log("取前端产物")
        run(sys.executable, PACKAGING / "fetch_frontend.py", "--tag", frontend_tag,
            "--dest", resources / "web", "--cache-dir", args.cache_dir)
        rl.log("组装运行时")
        run(sys.executable, PACKAGING / "build_runtime.py", "--dest", resources / "runtime",
            "--src", src, "--cache-dir", args.cache_dir)
        rl.log("同步站点运行资源")
        run(sys.executable, PACKAGING / "fetch_resources.py", "--app", resources / "app",
            "--python", resources / "runtime" / "bin" / "python3", "--cache-dir", args.cache_dir)

        # 用包内解释器真跑一遍后端导入链：缺包、缺库、缺资源都会在这里炸，不用等用户打开窗口
        rl.log("导入后端入口验收")
        with tempfile.TemporaryDirectory(prefix="mp-acceptance-") as config_dir:
            subprocess.run([str(resources / "runtime" / "bin" / "python3"), "-c", "import app.main"],
                           cwd=str(resources / "app"), check=True,
                           env={**os.environ, "CONFIG_DIR": config_dir,
                                "PYTHONPATH": str(resources / "pythonhost")})

        runtime_info = json.loads((resources / "runtime" / "runtime-info.json").read_text(encoding="utf-8"))
        (resources / "build-info.json").write_text(json.dumps({
            "backend": {"tag": args.tag, "commit": commit},
            "frontend": json.loads((resources / "web" / ".provenance.json").read_text(encoding="utf-8")),
            "runtime": runtime_info,
            "resources": json.loads((resources / "app" / ".resources.json").read_text(encoding="utf-8")),
            "cpu": rl.host_cpu(),
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        rl.log("预编译字节码")
        run(resources / "runtime" / "bin" / "python3", "-m", "compileall", "-q",
            resources / "app" / "app", resources / "app" / "database",
            resources / "pythonhost")

        rl.log("签名")
        sign(app, args.identity)
        rl.log(f"发布包：{app}（{rl.dir_size_mb(app)} MB）")
        if args.dmg:
            make_dmg(app, out)
        return app


def main() -> int:
    parser = argparse.ArgumentParser(description="组装 MoviePilot.app")
    parser.add_argument("--dev-root", help="本地 CLI 版仓库路径，走开发模式")
    parser.add_argument("--tag", help="上游后端 tag，发布模式必填")
    parser.add_argument("--backend-repo", default=str(Path.home() / "MoviePilot"),
                        help="取源码用的后端 git 仓库")
    parser.add_argument("--frontend-tag", help="覆盖前端 tag，默认按 version.py")
    parser.add_argument("--build-version", help="CFBundleVersion，默认与后端版本一致")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="输出目录")
    parser.add_argument("--cache-dir", type=Path, default=rl.DEFAULT_CACHE)
    parser.add_argument("--appcast-url", default="https://moviepilot.org/appcast/stable.xml")
    parser.add_argument("--identity", default="-", help="codesign 身份，默认 ad-hoc")
    parser.add_argument("--dmg", action="store_true", help="顺手产出可拖拽安装的 dmg")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    if args.dev_root:
        build_dev(Path(args.dev_root).expanduser().resolve(), args.out, args.appcast_url)
        return 0
    if not args.tag:
        raise SystemExit("发布模式需要 --tag，或使用 --dev-root 走开发模式")
    build_release(args, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
