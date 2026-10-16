#!/usr/bin/env python3
"""应用级冒烟测试：确认界面能出、接口能通、缓存策略正确。

只用标准库，CI 在装依赖之前也能跑。本地代理会被显式绕过。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def normalize(headers) -> dict[str, str]:
    """HTTP 首部不区分大小写，uvicorn 发的是小写名。"""
    return {key.lower(): value for key, value in dict(headers).items()}


def fetch(url: str, method: str = "GET", body: bytes | None = None, timeout: float = 15.0):
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json, text/html")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return response.status, normalize(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, normalize(error.headers), error.read()


def wait_ready(url: str, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            status, _, _ = fetch(url, timeout=3)
            if status < 500:
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


class Checks:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  ' + detail}")
        if not ok:
            self.failed.append(name)


def main() -> int:
    parser = argparse.ArgumentParser(description="MoviePilot 应用冒烟测试")
    parser.add_argument("--url", default="http://127.0.0.1:3000", help="前端托管层地址")
    parser.add_argument("--wait", type=int, default=0, help="等待服务就绪的秒数")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    if args.wait and not wait_ready(base, args.wait):
        print(f"FAIL  服务在 {args.wait} 秒内未就绪：{base}")
        return 1

    checks = Checks()

    status, headers, body = fetch(f"{base}/")
    checks.check("入口页返回 200", status == 200, f"实际 {status}")
    checks.check("入口页是 HTML", "text/html" in headers.get("content-type", ""), headers.get("content-type", ""))
    checks.check("入口页每次回源校验", headers.get("cache-control") == "no-cache", str(headers.get("cache-control")))

    asset = re.search(r"assets/(index-[A-Za-z0-9_-]+\.js)", body.decode("utf-8", "replace"))
    checks.check("入口页引用了哈希产物", asset is not None)
    if asset:
        status, headers, _ = fetch(f"{base}/assets/{asset.group(1)}")
        checks.check("哈希产物返回 200", status == 200, f"实际 {status}")
        checks.check("哈希产物走不可变缓存", "immutable" in headers.get("cache-control", ""), str(headers.get("cache-control")))

    status, _, body = fetch(f"{base}/subscription")
    checks.check("前端路由回退到入口页", status == 200 and b"<" in body, f"实际 {status}")

    status, _, body = fetch(f"{base}/api/v1/login/initialization")
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    checks.check("接口经代理返回 JSON", status == 200 and "success" in payload, f"实际 {status} {body[:80]!r}")

    status, _, _ = fetch(f"{base}/api/v1/system/global")
    checks.check("代理保留后端状态码", status < 500, f"实际 {status}")

    status, _, _ = fetch(f"{base}/api/v1/login/login", method="POST", body=b'{}')
    checks.check("POST 经代理可达后端", status < 500, f"实际 {status}")

    status, _, _ = fetch(f"{base}/assets/__missing__.js")
    checks.check("缺失产物回退入口页而非 500", status == 200, f"实际 {status}")

    print("失败项：" + ", ".join(checks.failed) if checks.failed else "全部通过")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
