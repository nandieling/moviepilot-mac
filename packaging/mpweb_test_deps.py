#!/usr/bin/env python3
"""从上游锁文件里挑出托管层单测要用的包。

pythonhost 只碰 httpx、starlette、uvicorn 三个模块，跑单测没必要照锁文件装 201 个包；
但版本要跟锁文件一致，否则测的不是随包发出去那份行为。间接依赖交给解析器。

用法：python3 packaging/mpweb_test_deps.py <上游 uv.lock>
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

# 托管层自己的第三方依赖：proxy/watchdog 用 httpx，starlette 撑起 ASGI 面，server 拉 uvicorn
MPWEB_DEPS = ("httpx", "starlette", "uvicorn")


def pinned(lock: Path, names: tuple[str, ...] = MPWEB_DEPS) -> list[str]:
    """按锁文件给出的版本产出 requirements 行；锁里没有就直说，别悄悄放宽。"""
    with lock.open("rb") as handle:
        data = tomllib.load(handle)
    found = {str(pkg.get("name")): str(pkg.get("version"))
             for pkg in data.get("package", []) if pkg.get("name") in names}
    missing = [name for name in names if name not in found]
    if missing:
        raise SystemExit(f"{lock} 里找不到 {', '.join(missing)}，上游锁文件结构变了？")
    return [f"{name}=={found[name]}" for name in names]


def main() -> int:
    parser = argparse.ArgumentParser(description="产出托管层单测的依赖清单")
    parser.add_argument("lock", type=Path, help="上游后端的 uv.lock")
    args = parser.parse_args()
    for line in pinned(args.lock.expanduser().resolve()):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
