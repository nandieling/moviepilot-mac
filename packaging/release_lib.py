"""发布组包的公共工具：带缓存与校验的下载、子进程、Mach-O 遍历。"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = REPO_ROOT / ".cache"
API = "https://api.github.com"


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _curl(args: list[str]) -> None:
    """下载一律交给系统 curl：它用钥匙串里的根证书，比某些自装 python 可靠。"""
    subprocess.run(["curl", "--fail", "--location", "--retry", "3", "--retry-delay", "2",
                    *args], check=True)


def download(url: str, dest: Path, sha256: str | None = None, label: str = "") -> Path:
    """下载到缓存目录，已存在且校验通过就复用。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha256 and sha256_file(dest) == sha256:
        log(f"  复用缓存 {label or dest.name}（{dir_size_mb(dest)} MB）")
        return dest
    if not url:
        raise SystemExit(f"{label or dest.name} 需要下载地址，但没拿到")
    log(f"  下载 {label or dest.name} <- {url.split('/')[-1]}")
    part = dest.with_suffix(dest.suffix + ".part")
    _curl(["--progress-bar", "--output", str(part), url])
    part.replace(dest)
    actual = sha256_file(dest)
    if sha256 and actual != sha256:
        dest.unlink(missing_ok=True)
        raise SystemExit(f"校验失败 {label or dest.name}：期望 {sha256}，实际 {actual}")
    return dest


def github_asset(repo: str, tag: str, asset: str) -> tuple[str, str]:
    """取发布产物的下载地址与 sha256（GitHub 直接给了 digest，不用再抓一遍算）。"""
    url = f"{API}/repos/{repo}/releases/tags/{tag}"
    with tempfile.NamedTemporaryFile("rb+", suffix=".json", delete=True) as handle:
        _curl(["--silent", "--show-error", "--header", "Accept: application/vnd.github+json",
               "--output", handle.name, url])
        payload = json.load(handle)
    for item in payload.get("assets", []):
        if item["name"] == asset:
            digest = (item.get("digest") or "").removeprefix("sha256:")
            if not digest:
                raise SystemExit(f"{repo}@{tag} 的 {asset} 没有 digest，请改用 --sha256 指定")
            return item["browser_download_url"], digest
    raise SystemExit(f"{repo}@{tag} 找不到产物 {asset}")


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    shown = " ".join(str(c) for c in cmd)
    log(f"  $ {shown if len(shown) < 200 else shown[:197] + '...'}")
    merged = {**os.environ, **(env or {})}
    subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=merged, check=True)


def require(tool: str, hint: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise SystemExit(f"缺少 {tool}：{hint}")
    return path


def host_cpu() -> str:
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}[platform.machine()]


def dir_size_mb(path: Path) -> int:
    """目录取总大小，单个文件取自身大小。"""
    if path.is_file():
        return path.stat().st_size // 1024 // 1024
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
    return total // 1024 // 1024


MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
                b"\xbe\xba\xfe\xca", b"\xcf\xfa\xdf\xfe", b"\x7fELF")


def iter_macho(root: Path):
    """遍历包内所有需要签名的 Mach-O（.so/.dylib/可执行文件）。"""
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as handle:
                if handle.read(4) in MACHO_MAGICS:
                    yield path
        except OSError:
            continue
