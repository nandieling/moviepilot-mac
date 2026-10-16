"""应用版的前端托管层：静态资源 + 后端反代 + 健康看门狗。

替代上游发行包里的 public/service.js，让前端只依赖随应用打包的 Python 运行时，
顺带去掉 159MB 的 Node 运行时与 72 个 npm 依赖。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from .proxy import BackendProxy
from .watchdog import BackendWatchdog, guard_parent_death

# 后端同源入口，与 public/service.js 的代理前缀保持一致。
PROXY_MOUNTS: tuple[str, ...] = ("/api", "/cookiecloud")


class SpaStaticFiles(StaticFiles):
    """命中静态资源就按哈希分级缓存，未命中的 GET 回退 index.html。

    index.html 强制每次回源校验：后端换版后如果还拿旧入口，
    前端就会把内置版本号和新的 FRONTEND_VERSION 比成不一致。
    """

    async def get_response(self, path: str, scope) -> Response:
        method = scope.get("method", "GET")
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or method not in ("GET", "HEAD"):
                raise
            return await super().get_response("index.html", scope)

    def file_response(self, fullpath, scope, *args, **kwargs) -> Response:
        response = super().file_response(fullpath, scope, *args, **kwargs)
        response.headers["Cache-Control"] = self._cache_control(str(fullpath))
        return response

    @staticmethod
    def _cache_control(fullpath: str) -> str:
        normalized = str(fullpath).replace("\\", "/")
        if normalized.endswith("index.html"):
            return "no-cache"
        if "/assets/" in normalized:
            # Vite 产物自带内容哈希，可以放心长期不可变缓存。
            return "public, max-age=31536000, immutable"
        return "public, max-age=300"


class ProxyEndpoint:
    """把挂载点下的任意子路径原样转给后端，包括 SSE 流式响应。"""

    def __init__(self, proxy: BackendProxy) -> None:
        self._proxy = proxy

    async def __call__(self, scope, receive, send) -> None:
        request = Request(scope, receive)
        response = await self._proxy.handle(request)
        await response(scope, receive, send)


def create_app(*, web_root: Path, proxy: BackendProxy) -> Starlette:
    routes = [Mount(path, app=ProxyEndpoint(proxy)) for path in PROXY_MOUNTS]
    routes.append(Mount("/", app=SpaStaticFiles(directory=web_root)))
    return Starlette(routes=routes)


async def serve(
    *,
    web_root: Path,
    backend_url: str,
    host: str,
    port: int,
    health_path: str = "/health/ready",
    health_interval: float = 15.0,
    health_timeout: float = 3.0,
    max_failures: int = 4,
    ready_timeout: float = 120.0,
    host_pid: int | None = None,
) -> int:
    """启动托管层。返回进程退出码：0 正常退出，1 后端失联或启动失败。"""
    proxy = BackendProxy(backend_url)
    exit_code = 0

    async def on_fatal(reason: str) -> None:
        nonlocal exit_code
        exit_code = 1
        print(f"[mpweb] {reason}，退出前端托管层", flush=True)
        server.should_exit = True

    watchdog = BackendWatchdog(
        base_url=backend_url,
        health_path=health_path,
        interval=health_interval,
        timeout=health_timeout,
        max_failures=max_failures,
        on_fatal=on_fatal,
    )
    # 前端本地代理不单独存活：后端起不来就直接失败，交给宿主应用处理。
    started = time.monotonic()
    if not await watchdog.wait_ready(ready_timeout):
        await proxy.close()
        print(f"[mpweb] 后端在 {time.monotonic() - started:.0f}s 内未就绪"
              f"（探测 {backend_url + health_path}），跳过启动前端托管层", flush=True)
        return 1

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(web_root=web_root, proxy=proxy),
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    tasks = [asyncio.create_task(watchdog.run())]
    if host_pid:
        tasks.append(asyncio.create_task(guard_parent_death(host_pid)))
    try:
        await server.serve()
    finally:
        for task in tasks:
            task.cancel()
        await proxy.close()
    return exit_code
