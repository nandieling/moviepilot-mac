"""后端健康看门狗。

上游 service.js 的语义是「前端本地代理不允许独自活着」：后端失联就退出，
由宿主应用决定重启还是报错。应用版沿用这条规则，避免出现只剩静态页面的假死界面。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx


@dataclass
class BackendWatchdog:
    base_url: str
    health_path: str = "/health/ready"
    interval: float = 15.0
    timeout: float = 3.0
    max_failures: int = 4
    on_fatal: Callable[[str], Awaitable[None]] | None = None
    _failures: int = field(default=0, init=False)

    async def check(self) -> bool:
        """探测一次后端健康状态。非 2xx 与网络错误都算失败。"""
        try:
            # 与转发客户端一致：健康探测只走本机，不继承系统代理设置。
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as probe:
                response = await probe.get(self.base_url + self.health_path)
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def wait_ready(self, attempts: int | None = None) -> bool:
        """启动前等待后端就绪，失败次数用尽返回 False。"""
        attempts = attempts or self.max_failures
        for attempt in range(1, attempts + 1):
            if await self.check():
                self._failures = 0
                return True
            self._failures = attempt
            if attempt < attempts:
                await asyncio.sleep(1.0)
        return False

    async def run(self) -> None:
        """周期巡检，连续失败达到阈值时通知宿主。"""
        while True:
            await asyncio.sleep(self.interval)
            if await self.check():
                self._failures = 0
                continue
            self._failures += 1
            if self._failures >= self.max_failures:
                reason = (
                    f"后端连续 {self._failures} 次健康检查失败"
                    f"（{time.strftime('%H:%M:%S')}）"
                )
                if self.on_fatal is not None:
                    await self.on_fatal(reason)
                return

    @property
    def failures(self) -> int:
        return self._failures


async def guard_parent_death(host_pid: int, interval: float = 2.0) -> None:
    """宿主被强杀时子进程会被 launchd 收养，这里立刻跟着退出。

    否则旧进程会一直占着端口，下一次启动只会看到一个不再服务的假死界面。
    """
    if host_pid <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        if os.getppid() == 1 or os.getppid() != host_pid:
            print("[mpweb] 宿主进程已退出，前端托管层跟随退出", flush=True)
            os._exit(1)
