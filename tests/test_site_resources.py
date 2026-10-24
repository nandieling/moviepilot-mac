"""站点资源就地更新：清单校验、下载兜底、签名不过要整体退回。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pythonhost"))

from mpweb import site_resources
from mpweb.release import ChannelError

RAW = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/"


def needed():
    return site_resources.needed_files()


def bundle(root: Path) -> Path:
    """够用的假包：站点资源目录里先摆上旧文件。"""
    app = root / "MoviePilot.app"
    site = app / site_resources.BUNDLE_APP / site_resources.TARGET
    site.mkdir(parents=True)
    for name in needed():
        (site / name).write_bytes(b"old")
    return app


def site_dir(app: Path) -> Path:
    return app / site_resources.BUNDLE_APP / site_resources.TARGET


def manifest(target: str | None = None) -> dict:
    return {"version": "28",
            "resources": {name: {"type": "indexer" if name.endswith(".bin") else "auth",
                                 "platform": "MacOS",
                                 "target": target or site_resources.TARGET,
                                 "version": "3.0.24"} for name in needed()}}


def listing() -> list[dict]:
    return [{"name": name,
             "download_url": f"{RAW}resources.v3/{name}",
             "git_url": f"https://api.github.com/repos/{site_resources.REPO}/git/blobs/sha-{name}",
             "size": 3} for name in needed()]


def fake_curl(body: bytes, *, blob: bool = False, fail: tuple[str, ...] = (),
              package: dict | None = None):
    """假 curl：按地址片段给响应；`blob` 是只给 API 地址备货。"""
    table = {"package.v3.json": package or manifest(), "contents/resources.v3": listing()}
    for name in needed():
        table[f"sha-{name}" if blob else f"resources.v3/{name}"] = body
    calls: list[str] = []

    def curl(args, **kwargs):
        url = args[-1]
        calls.append(url)
        if any(bad in url for bad in fail):
            raise ChannelError(f"连不上 {url}")
        for key, payload in table.items():
            if key in url:
                return payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        raise ChannelError(f"没准备这个地址：{url}")

    return curl, calls


class UpdateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.app = bundle(self.root)
        self.addCleanup(self._tmp.cleanup)
        patches = [mock.patch.object(site_resources, "_reseal_quietly")]
        self.seal = patches[0].start()
        self.addCleanup(mock.patch.stopall)

    def run_update(self, curl) -> dict:
        with mock.patch.object(site_resources, "_curl", curl):
            return site_resources.update(self.app, self.config)

    def test_换掉内容变了的文件(self):
        curl, calls = fake_curl(b"new")
        outcome = self.run_update(curl)
        self.assertEqual(outcome["state"], "updated")
        self.assertEqual(sorted(outcome["files"]), sorted(needed()))
        for name in needed():
            self.assertEqual((site_dir(self.app) / name).read_bytes(), b"new")
        self.assertEqual(len(calls), 2 + len(needed()))
        self.assertTrue(self.seal.called, "换过内嵌二进制要重新签整包")

    def test_内容一样就不折腾(self):
        curl, _ = fake_curl(b"old")
        outcome = self.run_update(curl)
        self.assertEqual(outcome["state"], "latest")
        self.assertFalse(self.seal.called)

    def test_raw域名不通就退回API(self):
        curl, calls = fake_curl(b"new", blob=True, fail=("raw.githubusercontent.com",))
        outcome = self.run_update(curl)
        self.assertEqual(outcome["state"], "updated")
        # 原始地址那一下还是会先试，但清单和文件都得能从 API 拿回来
        self.assertTrue(any("api.github.com/repos/jxxghp/MoviePilot-Resources/contents/package" in url
                            for url in calls))
        self.assertTrue(any("/git/blobs/sha-" in url for url in calls))

    def test_清单目标目录不安全就什么都不动(self):
        curl, _ = fake_curl(b"new", package=manifest(target="../../etc"))
        outcome = self.run_update(curl)
        self.assertEqual(outcome["state"], "failed")
        self.assertIn("目标目录", outcome["error"])
        for name in needed():
            self.assertEqual((site_dir(self.app) / name).read_bytes(), b"old")

    def test_签名不过整体退回(self):
        """只换一个文件的话认证和索引就分到两版上了，得一起退回去。"""
        curl, _ = fake_curl(b"new")
        with mock.patch.object(site_resources, "_sign_native",
                               side_effect=subprocess.CalledProcessError(1, "codesign")):
            outcome = self.run_update(curl)
        self.assertEqual(outcome["state"], "failed")
        for name in needed():
            self.assertEqual((site_dir(self.app) / name).read_bytes(), b"old")
        self.assertFalse(self.seal.called)

    def test_取不到清单只报失败(self):
        def curl(args, **kwargs):
            raise ChannelError("连不上")

        outcome = self.run_update(curl)
        self.assertEqual(outcome["state"], "failed")


if __name__ == "__main__":
    unittest.main()
