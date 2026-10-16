"""反向代理：把前端同源请求流式转发到后端。

行为对齐上游 public/service.js 里的 express-http-proxy，另外补齐两点：
响应头逐字透传（重复 Set-Cookie 不丢），以及请求体流式转发（插件包上传不进内存）。
"""

from __future__ import annotations

from typing import AsyncIterator, List, Tuple

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

# 逐跳首部不得跨代理转发，见 RFC 9110 §7.6.1。
# content-length 与 transfer-encoding 一并剥掉：请求体由 httpx 重新定帧，
# 避免同时出现 Content-Length 与 chunked 这种非法报文。
_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"content-length",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)

# 小请求体直接缓冲，让 httpx 能带上准确的 Content-Length；
# 大请求体（插件包、图片上传）保持流式转发，不进内存。
BUFFERED_BODY_LIMIT = 1024 * 1024
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class BackendUnavailable(RuntimeError):
    """后端不可达，交给看门狗计数决定是否退出。"""


def _upstream_target(scope: dict) -> str:
    """还原被转发请求的完整路径与查询串。

    Starlette 新版本里 Mount 只改 root_path、保留完整 path；旧版本会把 path 剥成剩余部分。
    两种语义都兼容，避免把 /api 拼两次。
    """
    root_path = scope.get("root_path", "") or ""
    path = scope.get("path", "") or ""
    full = path if root_path and path.startswith(root_path) else root_path + path
    query = scope.get("query_string", b"")
    if query:
        full += "?" + query.decode("latin-1")
    return full or "/"


class BackendProxy:
    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 10.0,
        write_timeout: float = 60.0,
    ) -> None:
        # 保存无尾斜杠的后端地址：转发时拼成完整 URL，
        # 避免 httpx 的 base_url 合并把绝对路径变成 //api/... 双斜杠。
        self._base = base_url.rstrip("/")
        # read 超时不限：智能体对话与运行日志都是长时间挂着的流式响应。
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout, read=None, write=write_timeout, pool=10.0
            ),
            follow_redirects=False,
            # 后端在回环地址上，不能被系统代理接管，否则本地一跳会被代理拦下。
            trust_env=False,
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        )
        self.last_error: str | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def close(self) -> None:
        await self._client.aclose()

    async def handle(self, request: Request) -> Response:
        """转发单个请求，路径按 ASGI root_path 约定还原。"""
        path = _upstream_target(request.scope)

        body: bytes | object
        if request.method in _BODYLESS_METHODS:
            body = None
        else:
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) <= BUFFERED_BODY_LIMIT:
                body = await request.body()
            else:
                body = request.stream()

        headers: List[Tuple[str, str]] = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in request.headers.raw
            if name.lower() not in _HOP_BY_HOP
        ]
        client = request.client
        if client is not None:
            headers.append(("X-Forwarded-For", client.host))
            headers.append(("X-Forwarded-Proto", request.url.scheme))
            headers.append(("X-Forwarded-Host", request.headers.get("host", "")))

        upstream_request = self._client.build_request(
            method=request.method,
            url=self._base + path,
            headers=headers,
            content=body,
        )
        try:
            upstream = await self._client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            self.last_error = repr(exc)
            return self._bad_gateway(exc)

        response = StreamingResponse(
            self._iter_chunks(upstream),
            status_code=upstream.status_code,
        )
        # 直接写 raw_headers，保留上游同名多值首部与 Content-Length。
        response.raw_headers = [
            (name, value)
            for name, value in upstream.headers.raw
            if name.lower() not in _HOP_BY_HOP
        ]
        response.background = BackgroundTask(self._close_upstream, upstream)
        return response

    @staticmethod
    async def _iter_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
        async for chunk in response.aiter_raw():
            yield chunk

    @staticmethod
    async def _close_upstream(response: httpx.Response) -> None:
        await response.aclose()

    def _bad_gateway(self, exc: Exception) -> Response:
        return JSONResponse(
            {
                "success": False,
                "message": "后端服务不可用",
                "data": None,
                "detail": repr(exc),
            },
            status_code=502,
        )
