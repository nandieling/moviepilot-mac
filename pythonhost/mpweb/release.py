"""发布通道：从本仓库的 GitHub Release 取增分包。

上游自带的更新只管它自己的产物，应用版的增分包发在我们自己的 Release 上，
所以「有没有新版」得问我们自己发的那份。这里只做取件：查最新发布、比版本、下载校验，
然后把「该装更新了」连同包路径投递给宿主，真正落地仍由 mpweb.updater 在应用退出后进行。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

API = "https://api.github.com"
GITHUB = "https://github.com"
# 没有发布通道时的兜底：总得给个能下载的地方
UPSTREAM_RELEASES = "https://github.com/jxxghp/MoviePilot/releases"
USER_AGENT = "moviepilot-mac"
BUILD_INFO = "Contents/Resources/build-info.json"
DEFAULT_ASSET_SUFFIX = "-appweb.zip"

# 宿主按这几个退出码给用户回话
AVAILABLE = 0         # 取到新包并已投递
FAILED = 1            # 网络、校验或磁盘出问题
UP_TO_DATE = 10       # 已经是最新
NO_CHANNEL = 20       # 这个包没写发布通道


class ChannelError(RuntimeError):
    """发布通道没配、资产找不到、校验不过这类可解释的失败。"""


def installed_app(default: Path | None = None) -> Path:
    """按包内布局反推 .app 位置；开发态那份得显式传 --app。"""
    if default:
        return Path(default)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.suffix == ".app":
            return parent
    raise ChannelError("不在 .app 包里运行，需要显式给出 --app")


def read_build_info(app: Path) -> dict:
    path = app / BUILD_INFO
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ChannelError(f"读不到 {path}：{exc}") from exc


def channel_of(info: dict) -> tuple[str, str]:
    """组包时写进 build-info 的发布通道：仓库名与资产后缀。"""
    channel = info.get("channel") or {}
    repo = str(channel.get("repo") or "").strip()
    if not repo:
        raise ChannelError("包里没写发布通道（组包时给 --release-repo）")
    return repo, str(channel.get("asset_suffix") or DEFAULT_ASSET_SUFFIX)


def release_page(app: Path | None = None, fallback: str = UPSTREAM_RELEASES) -> str:
    """发布页地址：界面点「更新」时用它。没写通道就退回上游发布页，至少不给空链接。"""
    try:
        repo, _ = channel_of(read_build_info(installed_app(app)))
    except ChannelError:
        return fallback
    return f"{GITHUB}/{repo}/releases/latest"


def version_key(tag: str) -> tuple[int, ...]:
    """v3.1.10 > v3.1.9：只按数字段比，别拿字符串顺序说事。"""
    numbers = tuple(int(n) for n in re.findall(r"\d+", tag or ""))
    return numbers or (-1,)


def env_file_value(path: Path, name: str) -> str:
    """读 app.env 里的一个键。上游配置页写的 GITHUB_TOKEN 就存在这儿。"""
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip("'\"")
    return ""


def github_token(config_dir: Path) -> str | None:
    """匿名访问发布接口每小时只有 60 次，配了 token 就用上。"""
    token = (os.environ.get("GITHUB_TOKEN") or env_file_value(config_dir / "app.env", "GITHUB_TOKEN")).strip()
    # 上游把 GITHUB_TOKEN 的默认值填成 moviepilot，那不是凭证
    return token if token and token.lower() != "moviepilot" else None


def host_request_file(config_dir: Path) -> Path:
    from_env = os.environ.get("MOVPILOT_HOST_REQUEST_FILE", "").strip()
    if from_env:
        return Path(from_env)
    return config_dir / "temp" / "mpweb" / "host-request.json"


def log(config_dir: Path, message: str) -> None:
    with contextlib.suppress(OSError):
        (config_dir / "logs").mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with (config_dir / "logs" / "update.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} 发布通道：{message}\n")


def _curl(args: list[str]) -> bytes:
    """取件交给系统 curl：包内那个解释器不带根证书，urllib 换个机器就验不过。"""
    process = subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                              "--max-time", "120", *args], capture_output=True)
    if process.returncode != 0:
        reason = process.stderr.decode("utf-8", "replace").strip() or f"curl 退出码 {process.returncode}"
        raise ChannelError(f"取件失败：{reason}")
    return process.stdout


def _headers(token: str | None, *, api: bool = False) -> list[str]:
    args = ["--header", f"User-Agent: {USER_AGENT}"]
    if api:
        args += ["--header", "Accept: application/vnd.github+json"]
    if token:
        args += ["--header", f"Authorization: Bearer {token}"]
    return args


def fetch_json(url: str, token: str | None) -> dict:
    return json.loads(_curl([*_headers(token, api=True), url]).decode("utf-8"))


def download(url: str, dest: Path, token: str | None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    _curl(["--output", str(part), *_headers(token), url])
    part.replace(dest)
    return dest


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def pick_asset(release: dict, suffix: str) -> tuple[dict, dict | None]:
    """找增分包本体和它的 sha256 旁文件。"""
    assets = [a for a in release.get("assets", []) if str(a.get("name", "")).endswith(suffix)]
    sidecars = [a for a in release.get("assets", []) if str(a.get("name", "")).endswith(suffix + ".sha256")]
    if not assets:
        raise ChannelError(f"发布产物里没有以 {suffix} 结尾的包")
    sidecar = next((s for s in sidecars if s["name"] == assets[0]["name"] + ".sha256"), None)
    return assets[0], sidecar


def check(app: Path, config_dir: Path, *, repo: str | None = None,
          force: bool = False, fetch: Callable[[str, str | None], dict] = fetch_json,
          pull: Callable[[str, Path, str | None], Path] = download) -> dict:
    """查一次发布通道。返回结论，不抛网络异常：定时任务里失败就该安静下来。"""
    info = read_build_info(app)
    if repo:
        channel_repo, suffix = repo.strip(), DEFAULT_ASSET_SUFFIX
    else:
        channel_repo, suffix = channel_of(info)
    installed = str((info.get("backend") or {}).get("tag") or "")
    token = github_token(config_dir)
    release = fetch(f"{API}/repos/{channel_repo}/releases/latest", token)
    newest = str(release.get("tag_name") or "")
    verdict = {"installed": installed, "release": newest, "repo": channel_repo}
    if not force and version_key(newest) <= version_key(installed):
        return verdict | {"state": "latest"}
    asset, sidecar = pick_asset(release, suffix)
    name = str(asset["name"])
    dest = config_dir / "temp" / "mpweb" / "updates" / name
    expected = ""
    if sidecar:
        sidecar_path = pull(str(sidecar["browser_download_url"]), dest.with_suffix(".sha256"), token)
        expected = sidecar_path.read_text(encoding="utf-8").split()[0]
    pull(str(asset["browser_download_url"]), dest, token)
    actual = sha256_file(dest)
    if expected and actual != expected:
        dest.unlink(missing_ok=True)
        raise ChannelError(f"{name} 校验不过：期望 {expected}，实际 {actual}")
    verdict |= {"state": "available", "zip": str(dest), "size": dest.stat().st_size, "sha256": actual}
    _prune_updates(dest.parent, keep=dest)
    return verdict


def _prune_updates(directory: Path, keep: Path) -> None:
    """只留最新这一份，56 MB 的东西不该在数据目录里堆一串。"""
    for stale in directory.glob("*.zip"):
        if stale != keep:
            stale.unlink(missing_ok=True)


def handoff(config_dir: Path, zip_path: Path) -> Path:
    """写投递文件，宿主的两秒轮询会取走它。"""
    dest = host_request_file(config_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"action": "update", "zip": str(zip_path)}, ensure_ascii=False)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(dest)
    return dest


def main(argv: list[str] | None = None, *, fetch=fetch_json, pull=download) -> int:
    parser = argparse.ArgumentParser(description="查本仓库的 Release 取增分包")
    parser.add_argument("--app", type=Path, help="MoviePilot.app 路径，包内运行时可省略")
    parser.add_argument("--config-dir", type=Path, default=Path(os.environ.get("CONFIG_DIR", ".")))
    parser.add_argument("--repo", help="覆盖发布通道仓库，形如 owner/name")
    parser.add_argument("--force", action="store_true", help="版本不比已装的更新也照取")
    parser.add_argument("--no-handoff", action="store_true", help="只下载，不通知宿主安装")
    args = parser.parse_args(argv)
    try:
        verdict = check(installed_app(args.app), args.config_dir, repo=args.repo, force=args.force,
                        fetch=fetch, pull=pull)
    except ChannelError as exc:
        print(f"发布通道：{exc}", file=sys.stderr)
        return NO_CHANNEL if "发布通道" in str(exc) else FAILED
    except OSError as exc:
        print(f"发布通道：查询失败 {exc}", file=sys.stderr)
        return FAILED
    log(args.config_dir, f"{verdict['state']}：已装 {verdict['installed']}，最新 {verdict['release']}")
    if verdict["state"] != "available":
        print(f"已是最新（{verdict['installed']}）")
        return UP_TO_DATE
    if not args.no_handoff:
        handoff(args.config_dir, Path(verdict["zip"]))
    print(f"取到 {verdict['release']}：{verdict['zip']}（{verdict['size'] // (1 << 20)} MB）")
    return AVAILABLE


if __name__ == "__main__":
    sys.exit(main())
