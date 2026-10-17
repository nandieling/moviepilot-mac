"""更新落地：把载荷换进 .app 并重新签名。

上游更新的检查、下载、校验在应用版里都能跑，唯独替换程序目录那步只认 Docker ——
`apply_prepared_update()` 开头就是 is_docker() 守卫，CLI 的 apply-prepared-update 同。
容器里那是 root 更新 worker 的活，应用版由这个模块顶上：载荷既收上游自己下好的
backend.zip / frontend.zip，也收我们按发布包切出来的增分包。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# 站点原生扩展与站点数据包：上游后端包不带这两个文件，换包时要从旧包继承。
SITE_DIR_CANDIDATES = (
    Path("app") / "application" / "site",
    Path("app") / "adapters" / "network",
    Path("app") / "infrastructure",
    Path("app") / "helper",
)
NATIVE_SUFFIXES = frozenset({".so", ".pyd", ".dylib"})
PLUGIN_PACKAGES = Path("app") / "plugins"
BUILD_INFO = "build-info.json"
UPSTREAM_ROOT = "moviepilot-update"


class UpdateError(RuntimeError):
    """载荷不可用、校验不过，或者替换中途失败。"""


@dataclass
class Payload:
    """一次更新要落地的内容：后端源码、前端静态文件、站点运行资源。"""

    backend: Path | None
    frontend: Path | None
    resources: tuple[Path, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        parts = []
        if self.backend:
            parts.append(f"后端 {app_version(self.backend) or '版本未知'}")
        if self.frontend:
            parts.append("前端")
        if self.resources:
            parts.append(f"站点资源 {len(self.resources)} 个文件")
        return "、".join(parts) or "空载荷"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def app_version(backend_root: Path) -> str | None:
    """从后端源码的 version.py 取版本号，取不到返回 None。"""
    try:
        lines = (backend_root / "version.py").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        name, sep, raw = line.partition("=")
        if sep and name.strip() == "APP_VERSION":
            return raw.strip().strip("'\"")
    return None


def site_dir(root: Path) -> Path:
    """定位后端携带的站点资源目录，兼容历史路径。"""
    for candidate in SITE_DIR_CANDIDATES:
        if (root / candidate).is_dir():
            return root / candidate
    return root / SITE_DIR_CANDIDATES[0]


def is_site_runtime(path: Path) -> bool:
    """判断是否属于必须跨版本继承的站点运行资源。"""
    if not path.is_file():
        return False
    name = path.name
    return (name.startswith("user.sites.") and path.suffix == ".bin") or (
        name.startswith("sites.") and path.suffix in NATIVE_SUFFIXES
    )


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """解压前先拒绝绝对路径和越界相对路径，避免载荷写到临时目录之外。"""
    for name in archive.namelist():
        member = Path(name)
        if member.is_absolute() or ".." in member.parts:
            raise UpdateError(f"更新包含不安全的路径：{name}")
    destination.mkdir(parents=True, exist_ok=True)
    archive.extractall(destination)


def _single_root(destination: Path) -> Path:
    """后端包按上游约定只有一个顶层目录，取出来当源码根。"""
    entries = list(destination.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        raise UpdateError("后端更新包源码根目录无效")
    return entries[0]


def _verify(path: Path, expected: str | None, label: str) -> Path:
    if not path.is_file():
        raise UpdateError(f"缺少{label}：{path.name if path.name else path}")
    if expected and sha256_file(path) != expected:
        raise UpdateError(f"{label}校验失败")
    return path


@contextlib.contextmanager
def payload_from_zip(zip_path: Path) -> Iterator[Payload]:
    """我们按发布包切出来的增分包：manifest.json + app/ + web/。"""
    if not zip_path.is_file():
        raise UpdateError(f"更新包不存在：{zip_path}")
    staging = Path(tempfile.mkdtemp(prefix="mpupdate-"))
    try:
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract(archive, staging)
            names = set(archive.namelist())
        manifest: dict[str, Any] = {}
        if (staging / "manifest.json").is_file():
            manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        # zip 只列文件条目，目录要靠前缀判断有没有带这一层。
        backend = staging / "app" if any(name.startswith("app/") for name in names) else None
        frontend = staging / "web" if any(name.startswith("web/") for name in names) else None
        if backend is None and frontend is None:
            raise UpdateError("更新包既没有后端源码也没有前端产物")
        yield Payload(backend=backend, frontend=frontend, provenance=manifest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@contextlib.contextmanager
def payload_from_staged(config_dir: Path) -> Iterator[Payload]:
    """上游自己下好并确认过的载荷：temp/moviepilot-update 里的清单和两个 zip。"""
    root = config_dir / "temp" / UPSTREAM_ROOT
    manifest_path = root / "install.json"
    if not manifest_path.is_file():
        raise UpdateError("没有待安装的更新，先在界面上检查并下载更新")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    targets = [str(value) for value in manifest.get("targets") or []]
    if not targets:
        raise UpdateError("待安装清单缺少更新类型")

    staging = Path(tempfile.mkdtemp(prefix="mpupdate-"))
    try:
        backend: Path | None = None
        frontend: Path | None = None
        if "application" in targets:
            backend_zip = _verify(
                Path(str(manifest.get("backend_archive") or "")),
                manifest.get("backend_sha256"),
                "后端更新包",
            )
            frontend_zip = _verify(
                Path(str(manifest.get("frontend_archive") or "")),
                manifest.get("frontend_sha256"),
                "前端更新包",
            )
            with zipfile.ZipFile(backend_zip) as archive:
                _safe_extract(archive, staging / "backend")
            backend = _single_root(staging / "backend")
            with zipfile.ZipFile(frontend_zip) as archive:
                _safe_extract(archive, staging / "frontend")
            frontend = staging / "frontend" / "dist"
            if not frontend.is_dir():
                raise UpdateError("前端更新包缺少 dist 目录")

        resources: list[Path] = []
        if "resources" in targets:
            for item in manifest.get("resource_files") or []:
                resources.append(
                    _verify(Path(str(item.get("path") or "")), item.get("sha256"), "站点资源文件")
                )
            if not resources:
                raise UpdateError("站点资源更新清单为空")

        yield Payload(
            backend=backend,
            frontend=frontend,
            resources=tuple(resources),
            provenance={
                "backend": {"tag": manifest.get("version"), "source": "上游 Release"},
                "frontend": {"tag": manifest.get("frontend_version")},
            },
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _snapshot_runtime(old_root: Path, snapshot: Path) -> tuple[list[Path], Path | None]:
    """换包前先把必须继承的东西挪出来：站点运行资源，以及还没接软链的插件目录。"""
    kept: list[Path] = []
    source = site_dir(old_root)
    if source.is_dir():
        target = snapshot / "site"
        target.mkdir(parents=True, exist_ok=True)
        for path in source.iterdir():
            if is_site_runtime(path):
                kept.append(shutil.copy2(path, target / path.name))

    packages: Path | None = None
    in_package = old_root / PLUGIN_PACKAGES
    if in_package.is_dir() and not in_package.is_symlink():
        packages = snapshot / "plugins"
        os.rename(in_package, packages)
    return kept, packages


def _install_site_resources(root: Path, files: Iterator[Path] | tuple[Path, ...]) -> list[Path]:
    """把站点运行资源写进新包；载荷自带同名文件时以载荷为准。"""
    destination = site_dir(root)
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for path in files:
        target = destination / path.name
        if target.is_file() and not is_site_runtime(target):
            continue
        shutil.copy2(path, target)
        written.append(target)
    return written


def _seed_package_marker(backend_root: Path, mirror: Path) -> None:
    """保证数据目录那份插件仓库有包标记：市场装的插件全住在这里。"""
    marker = mirror / "__init__.py"
    if marker.exists():
        return
    mirror.mkdir(parents=True, exist_ok=True)
    shipped = backend_root / PLUGIN_PACKAGES / "__init__.py"
    if shipped.is_file():
        shutil.copy2(shipped, marker)
    else:
        marker.write_text("", encoding="utf-8")


def _relocate_plugins(new_root: Path, mirror: Path, backup: Path | None) -> Path:
    """新包里的 app/plugins 换成指向数据目录的软链，出厂标记留在数据目录。"""
    _seed_package_marker(new_root, mirror)
    if backup is not None and backup.is_dir():
        for source in backup.iterdir():
            if source.name == "__init__.py":
                continue
            target = mirror / source.name
            if target.exists():
                continue
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
    in_package = new_root / PLUGIN_PACKAGES
    if in_package.is_symlink() or in_package.exists():
        if in_package.is_dir() and not in_package.is_symlink():
            shutil.rmtree(in_package)
        else:
            in_package.unlink()
    in_package.symlink_to(mirror, target_is_directory=True)
    return mirror


def _swap_directory(destination: Path, source: Path) -> None:
    """先改名腾位置再拷新内容，拷失败时能原样换回去。"""
    previous = destination.with_name(f".{destination.name}.__previous__")
    shutil.rmtree(previous, ignore_errors=True)
    existed = destination.exists() or destination.is_symlink()
    if existed:
        os.rename(destination, previous)
    try:
        shutil.copytree(source, destination, symlinks=True)
    except OSError as error:
        if existed:
            os.rename(previous, destination)
        raise UpdateError(f"替换 {destination.name} 失败：{error}") from error
    if existed:
        shutil.rmtree(previous, ignore_errors=True)


def _identity() -> str:
    return os.environ.get("MOVPILOT_CODESIGN_IDENTITY", "-") or "-"


def _sign_native(paths: list[Path]) -> None:
    """新写入的原生扩展要自己签一遍，否则 hardened runtime 下加载会被拒。"""
    targets = [str(path) for path in paths if path.is_file() and path.suffix in NATIVE_SUFFIXES]
    if not targets:
        return
    subprocess.run(
        ["codesign", "--force", "--sign", _identity(), "--timestamp=none", *targets],
        check=True,
        capture_output=True,
    )


def _reseal(app: Path) -> None:
    entitlements = app / "Contents" / "Resources" / "MoviePilot.entitlements"
    command = ["codesign", "--force", "--sign", _identity()]
    if entitlements.is_file():
        command += ["--options", "runtime", "--entitlements", str(entitlements)]
    command += ["--timestamp=none", str(app)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise UpdateError(f"重新签名失败：{(result.stderr or result.stdout).strip()}")


def _guard_runtime(payload: Payload, info: dict[str, Any]) -> None:
    """依赖清单变了就不是换源码能解决的事，硬换上去只会起不来。"""
    if payload.backend is None:
        return
    lock = payload.backend / "uv.lock"
    if not lock.is_file():
        return
    have = str((info.get("runtime") or {}).get("lock_sha256") or "")
    if have and sha256_file(lock) != have:
        raise UpdateError("这一版依赖清单有变动，需要安装完整更新包")


def apply_payload(
    app: Path,
    payload: Payload,
    *,
    config_dir: Path,
    resign: bool = True,
    ignore_lock_change: bool = False,
) -> dict[str, Any]:
    """把载荷落进 .app，返回一份写进日志的摘要。"""
    resources = app / "Contents" / "Resources"
    backend_root = resources / "app"
    web_root = resources / "web"
    if not backend_root.is_dir():
        raise UpdateError(f"应用包里没有后端源码：{backend_root}")

    try:
        info = json.loads((resources / BUILD_INFO).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        info = {}
    if not ignore_lock_change:
        _guard_runtime(payload, info)

    snapshot = Path(tempfile.mkdtemp(prefix="mpupdate-old-"))
    swapped: list[str] = []
    native: list[Path] = []
    try:
        if payload.backend is not None:
            if not (payload.backend / "version.py").is_file():
                raise UpdateError("后端更新包缺少 version.py")
            kept, plugin_backup = _snapshot_runtime(backend_root, snapshot)
            _swap_directory(backend_root, payload.backend)
            native += _install_site_resources(backend_root, kept)
            swapped.append("后端源码")
        elif payload.resources:
            native += _install_site_resources(backend_root, payload.resources)

        if payload.frontend is not None:
            if not (payload.frontend / "index.html").is_file():
                raise UpdateError("前端更新包缺少 index.html")
            _swap_directory(web_root, payload.frontend)
            swapped.append("前端产物")

        if payload.backend is not None:
            mirror = _relocate_plugins(
                backend_root, config_dir / "plugin_packages", snapshot / "plugins"
            )
        else:
            mirror = config_dir / "plugin_packages"

        if payload.resources and payload.backend is not None:
            native += _install_site_resources(backend_root, payload.resources)

        _sign_native(native)
        info.update(payload.provenance)
        info["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (resources / BUILD_INFO).write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if resign:
            _reseal(app)
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)
    return {"swapped": swapped, "plugins": str(mirror), "to": app_version(backend_root)}


def _log(config_dir: Path, message: str) -> None:
    with contextlib.suppress(OSError):
        log_dir = config_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with (log_dir / "update.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")


def _wait_for_process(pid: int, timeout: float = 180.0) -> None:
    """等应用本体退出：主程序的可执行文件被重新签名，正在跑的进程会被系统杀掉。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.3)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mpweb.updater", description="应用版更新落地")
    # 共用参数挂在子命令上：调用方只会写 `apply-zip --zip ... --app ...` 这一种顺序。
    common = argparse.ArgumentParser(prog="mpweb.updater", add_help=False)
    common.add_argument("--app", type=Path, default=None, help=".app 路径，默认取当前所在包")
    common.add_argument("--config-dir", type=Path, default=None, help="数据目录")
    common.add_argument("--wait-for-pid", type=int, default=None, help="等这个进程消失后再动手")
    common.add_argument("--relaunch", action="store_true", help="装完重新打开应用")
    common.add_argument("--no-resign", action="store_true", help="只换文件不重签，测试用")
    common.add_argument("--ignore-lock-change", action="store_true", help="明知依赖变了也要换")
    source = parser.add_subparsers(dest="mode", required=True)
    staged = source.add_parser("apply-staged", parents=[common], help="装上游下载好的更新")
    staged.set_defaults(kind="staged")
    staged.add_argument("--keep-state", action="store_true", help="保留待安装状态文件")
    archive = source.add_parser("apply-zip", parents=[common], help="装我们出的增分包")
    archive.set_defaults(kind="zip")
    archive.add_argument("--zip", type=Path, required=True)
    args = parser.parse_args(argv)

    app = (args.app or Path(__file__).resolve().parents[3]).expanduser()
    config_dir = (
        args.config_dir or Path(os.environ.get("CONFIG_DIR", ""))
        or Path.home() / "Library/Application Support/MoviePilot"
    ).expanduser()
    if args.wait_for_pid:
        _wait_for_process(args.wait_for_pid)

    outcome = 0
    try:
        if args.kind == "staged":
            with payload_from_staged(config_dir) as payload:
                _apply(args, app, config_dir, payload)
            if not args.keep_state:
                # 状态文件留着会让界面停在「待安装」，清掉让下次检查重新填。
                shutil.rmtree(config_dir / "temp" / UPSTREAM_ROOT, ignore_errors=True)
        else:
            with payload_from_zip(args.zip) as payload:
                _apply(args, app, config_dir, payload)
    except (UpdateError, OSError, json.JSONDecodeError) as error:
        _log(config_dir, f"更新失败：{error}")
        print(f"[updater] 更新失败：{error}", file=sys.stderr, flush=True)
        outcome = 1
    if args.relaunch:
        # 换失败时包还是原来那份，照样把应用叫回来，免得用户对着消失的图标发愣。
        subprocess.run(["/usr/bin/open", str(app)], check=False)
    return outcome


def _apply(args: argparse.Namespace, app: Path, config_dir: Path, payload: Payload) -> dict[str, Any]:
    summary = apply_payload(
        app,
        payload,
        config_dir=config_dir,
        resign=not args.no_resign,
        ignore_lock_change=args.ignore_lock_change,
    )
    _log(config_dir, f"已应用 {payload.describe()} -> {summary.get('to') or '未知版本'}")
    print(f"[updater] 已应用 {payload.describe()}；{summary}", flush=True)
    return summary


if __name__ == "__main__":
    sys.exit(main())
