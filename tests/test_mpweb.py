"""mpweb 托管层测试：转发保真、SPA 回退、缓存策略、看门狗。

用 httpx 的 ASGITransport 直接驱动应用，避免测试客户端的桥接线程干扰判定。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pythonhost"))

import httpx

from mpweb import BackendProxy, BackendWatchdog, create_app, serve

SEEN: dict = {}


class FakeBackend(BaseHTTPRequestHandler):
    """按需返回不同形态的响应，覆盖代理需要保真的每种情况。"""

    protocol_version = "HTTP/1.1"

    def _respond(self) -> None:
        SEEN["method"] = self.command
        SEEN["path"] = self.path
        SEEN["headers"] = {k.lower(): v for k, v in self.headers.items()}
        length = int(self.headers.get("content-length") or 0)
        SEEN["body"] = self.rfile.read(length).decode() if length else ""

        if self.path.startswith("/api/v1/cookies"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "a=1; Path=/")
            self.send_header("Set-Cookie", "b=2; Path=/")
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path.startswith("/api/v1/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: one\n\ndata: two\n\n")
            self.close_connection = True
            return

        if self.path.startswith("/api/v1/boom"):
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        payload = json.dumps(
            {"method": self.command, "path": self.path, "body": SEEN["body"]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = _respond

    def log_message(self, *args) -> None:
        pass


class ProxyTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), FakeBackend)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        cls.tmp = TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "index.html").write_text("<!doctype html>app", encoding="utf-8")
        (root / "service-worker.js").write_text("// sw", encoding="utf-8")
        (root / "version.txt").write_text("v9.9.9", encoding="utf-8")
        (root / "assets").mkdir()
        (root / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
        cls.web_root = root

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    async def asyncSetUp(self) -> None:
        self.proxy = BackendProxy(f"http://127.0.0.1:{self.port}")
        self.transport = httpx.ASGITransport(
            app=create_app(web_root=self.web_root, proxy=self.proxy)
        )
        self.client = httpx.AsyncClient(
            transport=self.transport, base_url="http://local.test"
        )
        # 应用版里托管层知道数据目录在哪，重启请求才有人接；测试按同样的形态摆好。
        self.config_dir = self.web_root.parent / f"config-{self._testMethodName}"
        self.request_file = self.config_dir / "temp" / "mpweb" / "host-request.json"
        environment = mock.patch.dict(
            os.environ,
            {
                "CONFIG_DIR": str(self.config_dir),
                "MOVPILOT_HOST_REQUEST_FILE": str(self.request_file),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(lambda: shutil.rmtree(self.config_dir, ignore_errors=True))

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.proxy.close()

    async def test_path_and_query_are_forwarded_verbatim(self) -> None:
        response = await self.client.get(
            "/api/v1/login/initialization", params={"refresh": "1", "q": "中"}
        )
        self.assertEqual(response.status_code, 200)
        forwarded = json.loads(response.text)["path"]
        self.assertTrue(forwarded.startswith("/api/v1/login/initialization?"))
        self.assertIn("refresh=1", forwarded)
        self.assertIn("q=%E4%B8%AD", forwarded)

    async def test_method_and_body_survive_proxy(self) -> None:
        response = await self.client.post("/api/v1/echo", json={"hello": "world"})
        payload = json.loads(response.text)
        self.assertEqual(payload["method"], "POST")
        self.assertEqual(json.loads(payload["body"]), {"hello": "world"})

    async def test_duplicate_set_cookie_headers_preserved(self) -> None:
        response = await self.client.get("/api/v1/cookies")
        cookies = sorted(
            value for name, value in response.headers.multi_items()
            if name.lower() == "set-cookie"
        )
        self.assertEqual(cookies, ["a=1; Path=/", "b=2; Path=/"])

    async def test_streaming_response_passes_through(self) -> None:
        response = await self.client.get("/api/v1/stream")
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        self.assertEqual(response.text, "data: one\n\ndata: two\n\n")

    async def test_upstream_status_is_not_rewritten(self) -> None:
        response = await self.client.get("/api/v1/boom")
        self.assertEqual(response.status_code, 500)

    async def test_proxy_adds_forwarded_headers(self) -> None:
        await self.client.get("/cookiecloud/sync")
        self.assertIn("x-forwarded-for", SEEN["headers"])
        self.assertEqual(SEEN["headers"]["x-forwarded-proto"], "http")

    async def test_api_responses_are_not_cacheable(self) -> None:
        # 后端不写 Cache-Control，代理必须补上，否则 WebView 会把旧的初始化状态当成现状。
        response = await self.client.get("/api/v1/login/initialization")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_upstream_cache_policy_is_not_overridden(self) -> None:
        from mpweb.proxy import apply_cache_policy

        kept = apply_cache_policy([(b"cache-control", b"public, max-age=60")])
        self.assertEqual(kept, [(b"cache-control", b"public, max-age=60")])
        added = apply_cache_policy([(b"content-type", b"application/json")])
        self.assertIn((b"cache-control", b"no-store"), added)

    async def test_service_worker_and_version_are_revalidated(self) -> None:
        for name in ("service-worker.js", "version.txt"):
            with self.subTest(name=name):
                response = await self.client.get(f"/{name}")
                self.assertEqual(response.headers["cache-control"], "no-cache")

    async def test_service_worker_is_replaced_by_the_stub(self) -> None:
        # 上游那份 workbox 会把接口响应冻在 CacheStorage，应用版必须换成不缓存的替身。
        response = await self.client.get("/service-worker.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("skipWaiting", response.text)
        # 上游那份是打包进来的 workbox，带预缓存清单；替身里没有这些。
        self.assertNotIn("precacheAndRoute", response.text)

    async def test_index_html_is_revalidated_every_time(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.headers["cache-control"], "no-cache")

    async def test_hashed_assets_are_immutable(self) -> None:
        response = await self.client.get("/assets/index-abc123.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response.headers["cache-control"])

    async def test_unknown_get_falls_back_to_index(self) -> None:
        response = await self.client.get("/subscription/detail")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "<!doctype html>app")

    async def test_api_prefix_is_never_served_from_disk(self) -> None:
        # 前端目录里也有 index.html，代理前缀必须抢先命中后端
        response = await self.client.get("/api/index.html")
        self.assertEqual(json.loads(response.text)["path"], "/api/index.html")

    async def test_backend_down_yields_502_not_traceback(self) -> None:
        dead = BackendProxy("http://127.0.0.1:1")
        transport = httpx.ASGITransport(
            app=create_app(web_root=self.web_root, proxy=dead)
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://local.test"
        ) as client:
            response = await client.get("/api/v1/anything")
        await dead.close()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["success"])

    async def test_restart_is_handed_to_the_host(self) -> None:
        response = await self.client.get(
            "/api/v1/system/restart", headers={"Cookie": "moviepilot=1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(json.loads(self.request_file.read_text())["action"], "restart")

    async def test_restart_installs_a_staged_update_when_one_is_waiting(self) -> None:
        staged = self.config_dir / "temp" / "moviepilot-update"
        staged.mkdir(parents=True)
        (staged / "install.json").write_text('{"targets": ["application"]}', encoding="utf-8")
        response = await self.client.get(
            "/api/v1/system/restart", headers={"Cookie": "moviepilot=1"}
        )
        self.assertTrue(response.json()["success"])
        self.assertEqual(json.loads(self.request_file.read_text())["action"], "update")

    async def test_restart_without_credentials_still_reaches_the_backend(self) -> None:
        await self.client.get("/api/v1/system/restart")
        self.assertEqual(SEEN["path"], "/api/v1/system/restart")

    async def test_update_button_opens_the_release_page(self) -> None:
        """界面点「更新」：交宿主开浏览器，别让上游那句「环境不支持」留在屏幕上。"""
        response = await self.client.post(
            "/api/v1/system/update/download",
            json={"target": "application"},
            headers={"Cookie": "moviepilot=1"},
        )
        self.assertTrue(response.json()["success"])
        request = json.loads(self.request_file.read_text())
        self.assertEqual(request["action"], "open")
        self.assertTrue(request["url"].startswith("https://github.com/"))

    async def test_update_install_does_not_claim_to_be_installing(self) -> None:
        response = await self.client.post(
            "/api/v1/system/update/install",
            json={"target": "application"},
            headers={"Cookie": "moviepilot=1"},
        )
        body = response.json()
        self.assertFalse(body["success"])
        self.assertIn("更新页面", body["message"])

    async def test_update_without_credentials_still_reaches_the_backend(self) -> None:
        await self.client.post("/api/v1/system/update/download", json={})
        self.assertEqual(SEEN["path"], "/api/v1/system/update/download")


class WatchdogTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_serve_refuses_to_start_without_backend(self) -> None:
        with TemporaryDirectory() as tmp:
            code = await serve(
                web_root=Path(tmp),
                backend_url="http://127.0.0.1:1",
                host="127.0.0.1",
                port=0,
                health_timeout=0.5,
                max_failures=2,
                ready_timeout=1,
            )
        self.assertEqual(code, 1)

    async def test_watchdog_gives_up_after_ready_timeout(self) -> None:
        """后端始终不响应：等到超时才放弃，并把失败次数留在 failures 上。"""
        watchdog = BackendWatchdog(
            base_url="http://127.0.0.1:1", timeout=0.5, ready_timeout=0, probe_interval=0
        )
        self.assertFalse(await watchdog.wait_ready())
        self.assertEqual(watchdog.failures, 1)

    async def test_watchdog_waits_out_slow_backend(self) -> None:
        """后端慢启动：只要预算内就绪就算成功，不能被少数几次失败判死。"""
        class SlowBackend(BackendWatchdog):
            probes = 0

            async def check(self) -> bool:
                type(self).probes += 1
                return type(self).probes >= 4

        watchdog = SlowBackend(base_url="http://127.0.0.1:1", timeout=0.5,
                               ready_timeout=10, probe_interval=0)
        self.assertTrue(await watchdog.wait_ready())
        self.assertEqual(SlowBackend.probes, 4)
        self.assertEqual(watchdog.failures, 0)


if __name__ == "__main__":
    unittest.main()
