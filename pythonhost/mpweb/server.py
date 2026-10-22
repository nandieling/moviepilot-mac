"""应用版的前端托管层：静态资源 + 后端反代 + 健康看门狗。

替代上游发行包里的 public/service.js，让前端只依赖随应用打包的 Python 运行时，
顺带去掉 159MB 的 Node 运行时与 72 个 npm 依赖。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .proxy import BackendProxy
from .release import release_page
from .watchdog import BackendWatchdog, guard_parent_death

# 后端同源入口，与 public/service.js 的代理前缀保持一致。
PROXY_MOUNTS: tuple[str, ...] = ("/api", "/cookiecloud")

# 必须每次回源的文件名。
REVALIDATED = frozenset({"service-worker.js", "version.txt"})

# 替身 Service Worker 的脚本源。
SERVICE_WORKER_STUB = Path(__file__).with_name("sw-stub.js")
SERVICE_WORKER_PATH = "/service-worker.js"

# 界面里的「重启系统」：上游的受管重启只认 Docker 与 CLI 状态文件，应用版由宿主执行。
RESTART_PATH = "/api/v1/system/restart"
# 界面里的「更新」按钮：上游这两步开头都是 can_restart() 守卫，应用版既没有 Docker
# 的 supervisor 也没有 CLI 状态文件，只会被顶回一句「当前运行环境不支持升级操作」。
UPDATE_PATHS = ("/api/v1/system/update/download", "/api/v1/system/update/install")
REQUEST_FILE_ENV = "MOVPILOT_HOST_REQUEST_FILE"


def _config_dir() -> Path | None:
    configured = (os.environ.get("CONFIG_DIR") or "").strip()
    return Path(configured).expanduser() if configured else None


def host_request_file() -> Path | None:
    """宿主取动作请求的文件路径；连数据目录都不知道就说明这层不在应用里跑。"""
    configured = (os.environ.get(REQUEST_FILE_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    config_dir = _config_dir()
    if config_dir is not None:
        return config_dir / "temp" / "mpweb" / "host-request.json"
    return None


def post_host_request(payload: dict) -> bool:
    """把一份动作请求交给宿主；连数据目录都没有就说明这层不在应用里跑。"""
    target = host_request_file()
    if target is None:
        return False
    body = dict(payload, requested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=".request-", delete=False
    )
    try:
        handle.write(json.dumps(body, ensure_ascii=False))
        handle.flush()
    finally:
        handle.close()
    os.replace(handle.name, target)
    return True


def request_host_restart() -> str | None:
    """把重启请求交给宿主并交代它做什么；载荷已下载待安装时顺带让它落地更新。"""
    config_dir = _config_dir()
    staged = (
        config_dir / "temp" / "moviepilot-update" / "install.json"
        if config_dir is not None
        else None
    )
    action = "update" if staged is not None and staged.is_file() else "restart"
    return action if post_host_request({"action": action}) else None


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
        name = normalized.rsplit("/", 1)[-1]
        # 入口、SW 脚本、版本号文件任何一样被缓存，前端就会拿旧状态当现状：
        # 前者会跳过首次设置页，后两者会让版本比对误报「缓存版本不一致」。
        if normalized.endswith("index.html") or name in REVALIDATED or name.endswith(".webmanifest"):
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
        try:
            response = await self._proxy.handle(request)
        except ClientDisconnect:
            # 页面跳转或用户中断时 WebView 会掐掉连接，读请求体失败不算故障，
            # 回不了响应也没人等，整段堆栈打进日志只会盖掉真正要紧的行。
            return
        await response(scope, receive, send)


def service_worker_response() -> Response | None:
    """用替身顶掉前端自带的 workbox：它的 CacheStorage 会把接口响应冻在上一次运行里。

    脚本内容变了浏览器就会换 SW，替身在 activate 里清空历史缓存，
    所以旧版本留下的过期状态会在升级后被自动带走。设 `MOVPILOT_SERVICE_WORKER=upstream` 可退回原样。
    """
    if os.environ.get("MOVPILOT_SERVICE_WORKER", "stub").lower() == "upstream":
        return None
    if not SERVICE_WORKER_STUB.is_file():
        return None
    return Response(
        SERVICE_WORKER_STUB.read_bytes(),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


async def _service_worker_request(request: Request) -> Response:
    response = service_worker_response()
    if response is None:
        raise StarletteHTTPException(status_code=404)
    return response


CREDENTIAL_HEADERS = ("cookie", "authorization", "x-api-key")


def restart_endpoint(proxy: BackendProxy):
    """接管界面的重启按钮：后端自己既不会重启，也不会替换程序目录。

    没有登录凭证的请求原样转给后端，让它按常规回 401，免得本地任意页面都能戳重启。
    """

    async def handler(request: Request) -> Response:
        if host_request_file() is None or not any(
            request.headers.get(name) for name in CREDENTIAL_HEADERS
        ):
            return await proxy.handle(request)
        action = request_host_restart()
        message = "更新已排入本次重启" if action == "update" else "正在重启服务"
        return JSONResponse({"success": True, "message": message, "data": None})

    return handler


def update_endpoint(proxy: BackendProxy):
    """接管界面里的「更新」按钮，改成交给宿主用默认浏览器打开发布页。

    上游的下载与安装两步开头都有 can_restart() 守卫，应用版过不了，点了只会得到一句
    「当前运行环境不支持升级操作」；换整包这件事本来就得由安装包来做，所以这里把用户
    领到发布页去。增分包那套自助升级仍走菜单栏的「检查更新」，不受影响。

    下载那步回成功：前端对 success=false 会先弹服务端消息再补一句「下载失败」，
    叠两条没意义。安装那步照实回失败，免得界面停在「正在安装」。
    """

    async def handler(request: Request) -> Response:
        if host_request_file() is None or not any(
            request.headers.get(name) for name in CREDENTIAL_HEADERS
        ):
            return await proxy.handle(request)
        if not post_host_request({"action": "open", "url": release_page()}):
            return await proxy.handle(request)
        installed = request.url.path.endswith("/download")
        message = ("更新页面已在浏览器打开，下载安装包后覆盖即可"
                   if installed else "应用版请用完整安装包升级，更新页面已在浏览器打开")
        return JSONResponse(
            {"success": installed, "message": message, "data": None},
            status_code=200,
        )

    return handler


def create_app(*, web_root: Path, proxy: BackendProxy) -> Starlette:
    routes: list[Mount | Route] = [
        Route(RESTART_PATH, endpoint=restart_endpoint(proxy), methods=["GET"]),
        Route(UPDATE_PATHS[0], endpoint=update_endpoint(proxy), methods=["POST"]),
        Route(UPDATE_PATHS[1], endpoint=update_endpoint(proxy), methods=["POST"]),
    ]
    routes += [Mount(path, app=ProxyEndpoint(proxy)) for path in PROXY_MOUNTS]
    if service_worker_response() is not None:
        # 路由排在静态挂载之前：这个路径由托管层接管，不再回落到前端自带的那份。
        routes.append(Route(SERVICE_WORKER_PATH, endpoint=_service_worker_request))
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
