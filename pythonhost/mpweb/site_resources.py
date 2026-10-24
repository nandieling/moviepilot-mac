"""站点资源（认证与索引）的就地更新。

上游把更新的前两步押在 `can_restart()` 守卫后面，应用版既没有容器里的 supervisor 也没有
CLI 状态文件，界面点「更新」只会顶回一句「当前运行环境不支持升级操作」。可站点资源根本
不需要换整包：照上游 `ResourceHelper` 那同一份清单取件、就地换掉 `app/application/site`
下的文件、再叫宿主重启后端把新资源读进来就行。

取件走两条路：先按 `GITHUB_PROXY` 前缀取原始地址，不通再退回 `api.github.com`。
国内网络常把 `raw.githubusercontent.com` 解析成 0.0.0.0，而 API 域名按 IP 直连是通的，
这样即使什么都不配置也能更新。
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

from .release import (API, ChannelError, _curl, env_file_value, github_token, log,
                      sha256_file)
from .updater import NATIVE_SUFFIXES, _sign_native

REPO = "jxxghp/MoviePilot-Resources"
VERSION_FLAG = "v3"
# 清单声明的目标目录，也是唯一允许写入的位置。
TARGET = "app/application/site"
BUNDLE_APP = "Contents/Resources/app"
MANIFEST_RAW = f"https://raw.githubusercontent.com/{REPO}/main/package.{VERSION_FLAG}.json"
MANIFEST_API = f"{API}/repos/{REPO}/contents/package.{VERSION_FLAG}.json"
LISTING_API = f"{API}/repos/{REPO}/contents/resources.{VERSION_FLAG}"


def needed_files() -> list[str]:
    """当前解释器和平台要用的资源文件名，规则与上游一致。"""
    tag = f"{sys.version_info.major}{sys.version_info.minor}"
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        tag += "t"
    machine = platform.machine().lower()
    machine = {"arm64": "aarch64", "amd64": "x86_64"}.get(machine, machine)
    system = platform.system().lower()
    files = [f"user.sites.{VERSION_FLAG}.bin"]
    if system == "darwin":
        files.append(f"sites.cpython-{tag}-darwin.so")
    elif system == "linux":
        files.append(f"sites.cpython-{tag}-{machine}-linux-gnu.so")
    elif system == "windows":
        files.append(f"sites.cp{tag}-win_amd64.pyd")
    return files


def _headers(token: str | None, *, raw: bool = False) -> list[str]:
    accept = "application/vnd.github.raw" if raw else "application/vnd.github+json"
    args = ["--header", f"Accept: {accept}"]
    if token:
        args += ["--header", f"Authorization: Bearer {token}"]
    return args


def _via_api(url: str, token: str | None) -> bytes:
    """把 raw 地址换成同仓库的 API 原文地址：域名被解析成 0.0.0.0 时靠它兜底。"""
    _, _, rest = url.partition("://raw.githubusercontent.com/")
    repo, _, path = rest.partition("/main/")
    if not (repo and path):
        raise ChannelError(f"认不出这个资源地址：{url}")
    return _curl([*_headers(token, raw=True), f"{API}/repos/{repo}/contents/{path}"], max_time=120)


def _get(url: str, token: str | None, mirror: str) -> bytes:
    """带镜像前缀取原始地址，不通退回 API。"""
    try:
        return _curl([*_headers(token), f"{mirror}{url}"], max_time=120)
    except ChannelError as direct:
        try:
            return _via_api(url, token)
        except ChannelError:
            raise direct


def _get_entry(entry: dict, token: str | None, mirror: str) -> bytes:
    """下载清单里的一个文件；原始地址不通就用它自己的 blob 地址。"""
    url = str(entry.get("download_url") or "")
    blob = str(entry.get("git_url") or "")
    try:
        return _curl([*_headers(token), f"{mirror}{url}"], max_time=600, stall=(1024, 60))
    except ChannelError as direct:
        if not blob:
            raise direct
        try:
            return _curl([*_headers(token, raw=True), blob], max_time=600)
        except ChannelError:
            raise direct


def _listing(config_dir: Path) -> tuple[dict, dict[str, dict]]:
    """读资源清单和文件列表，返回 `(清单, 按文件名索引的文件列表)`。"""
    token = github_token(config_dir)
    mirror = env_file_value(config_dir / "app.env", "GITHUB_PROXY")
    info = json.loads(_get(MANIFEST_RAW, token, mirror))
    files = json.loads(_curl([*_headers(token), LISTING_API], max_time=120))
    if not isinstance(info, dict) or not isinstance(files, list):
        raise ChannelError("站点资源清单格式不对")
    by_name = {str(item.get("name")): item for item in files if isinstance(item, dict)}
    return info, by_name


def _install(target: Path, blob: bytes) -> bool:
    """写一个新资源文件；原生扩展先就地签一遍，否则 hardened runtime 会拒绝加载。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(target.suffix + ".new")
    staged.write_bytes(blob)
    if target.suffix in NATIVE_SUFFIXES:
        try:
            _sign_native([staged])
        except subprocess.CalledProcessError:
            staged.unlink(missing_ok=True)
            return False
    staged.replace(target)
    return True


def update(app: Path, config_dir: Path) -> dict[str, Any]:
    """就地更新站点资源。返回结论而不抛异常：界面只想知道成没成。"""
    try:
        info, listing = _listing(config_dir)
    except (ChannelError, OSError, ValueError) as error:
        log(config_dir, f"站点资源：查不到更新，{error}")
        return {"state": "failed", "error": str(error)}
    resources = info.get("resources") or {}
    root = app / BUNDLE_APP
    token, mirror = github_token(config_dir), env_file_value(config_dir / "app.env", "GITHUB_PROXY")
    todo: list[tuple[Path, bytes, str]] = []
    for name in needed_files():
        declared = resources.get(name)
        if not isinstance(declared, dict):
            return {"state": "failed", "error": f"清单里没有当前平台要的 {name}"}
        if Path(str(declared.get("target") or "")).as_posix() != TARGET:
            return {"state": "failed", "error": f"清单给出的目标目录不安全：{declared.get('target')}"}
        entry = listing.get(name)
        if not isinstance(entry, dict):
            return {"state": "failed", "error": f"资源仓库里找不到 {name}"}
        try:
            blob = _get_entry(entry, token, mirror)
        except (ChannelError, OSError) as error:
            return {"state": "failed", "error": f"{name} 下载失败：{error}"}
        digest = hashlib.sha256(blob).hexdigest()
        target = root / TARGET / name
        if target.is_file() and sha256_file(target) == digest:
            continue
        todo.append((target, blob, str(declared.get("version") or "")))
    if not todo:
        log(config_dir, "站点资源：已是最新")
        return {"state": "latest", "files": []}
    # 要么都换要么都不换：只换上一个文件的话，认证和索引就分到两版上了
    changed: list[str] = []
    backups: dict[Path, bytes | None] = {}
    for target, blob, version in todo:
        backups[target] = target.read_bytes() if target.is_file() else None
        if not _install(target, blob):
            for path, previous in backups.items():
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous)
            log(config_dir, f"站点资源：{target.name} 装不上，已退回原来的文件")
            return {"state": "failed", "error": f"{target.name} 写不进包，或者签名没过"}
        changed.append(target.name)
        log(config_dir, f"站点资源：换掉 {target.name} → {version}（{hashlib.sha256(blob).hexdigest()[:12]}）")
    _reseal_quietly(app, config_dir)
    log(config_dir, f"站点资源：已更新 {len(changed)} 个文件，等后端重启加载")
    return {"state": "updated", "files": changed,
            "package_version": str(info.get("version") or "")}


def _reseal_quietly(app: Path, config_dir: Path) -> None:
    """整包重新签一遍；没签成也不影响加载，只是校验会报。"""
    from .updater import UpdateError, _reseal

    try:
        _reseal(app)
    except (UpdateError, OSError) as error:
        log(config_dir, f"站点资源：整包重新签名没成，{error}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .release import installed_app

    parser = argparse.ArgumentParser(description="就地更新站点资源")
    parser.add_argument("--app", type=Path, help="MoviePilot.app 路径，包内运行时可省略")
    parser.add_argument("--config-dir", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    outcome = update(installed_app(args.app), args.config_dir)
    print(outcome)
    return 0 if outcome["state"] in {"latest", "updated"} else 1


if __name__ == "__main__":
    sys.exit(main())
