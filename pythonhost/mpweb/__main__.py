"""命令行入口：python -m mpweb

环境变量沿用上游 service.js 已有的名字，宿主应用与现有脚本无需改动。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .server import serve


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mpweb", description="MoviePilot 前端托管层")
    parser.add_argument("--web-root", type=Path, help="前端 dist 目录，默认取 MOVIEPILOT_WEB_ROOT")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("NGINX_PORT", 3000),
        help="监听端口，默认取 NGINX_PORT 或 3000",
    )
    parser.add_argument(
        "--backend-host",
        default=(os.environ.get("MOVIEPILOT_BACKEND_HOST") or "127.0.0.1").strip(),
        help="后端地址",
    )
    parser.add_argument(
        "--backend-port",
        type=int,
        default=_env_int("PORT", 3001),
        help="后端端口，默认取 PORT 或 3001",
    )
    parser.add_argument(
        "--host-pid",
        type=int,
        default=_env_int("MOVPILOT_HOST_PID", 0),
        help="宿主应用进程号，宿主消失时本进程跟随退出",
    )
    parser.add_argument(
        "--max-failures",
        type=int,
        default=max(_env_int("MOVIEPILOT_FRONTEND_MAX_FAILURES", 4), 1),
        help="允许后端连续健康检查失败的次数",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    web_root = args.web_root or Path(os.environ.get("MOVIEPILOT_WEB_ROOT", "")).expanduser()
    if not str(web_root) or not web_root.is_dir():
        print(f"[mpweb] 前端资源目录不存在：{web_root}", file=sys.stderr)
        return 2
    return asyncio.run(
        serve(
            web_root=web_root,
            backend_url=f"http://{args.backend_host}:{args.backend_port}",
            host=args.host,
            port=args.port,
            health_interval=_env_int("MOVIEPILOT_FRONTEND_HEALTH_INTERVAL_MS", 15000) / 1000,
            health_timeout=_env_int("MOVIEPILOT_FRONTEND_HEALTH_TIMEOUT_MS", 3000) / 1000,
            max_failures=args.max_failures,
            host_pid=args.host_pid,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
