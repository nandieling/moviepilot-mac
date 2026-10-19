"""发布通道测试：版本比对、下载校验、投递给宿主的握手。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pythonhost"))

from mpweb import release
from mpweb.release import AVAILABLE, NO_CHANNEL, UP_TO_DATE, ChannelError, check, main

PAYLOAD = b"delta-payload"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def bundle(root: Path, *, tag: str = "v3.1.6", channel: dict | None = None) -> Path:
    """够用的假包：只要 build-info 里有版本和通道就够这一层用。"""
    app = root / "MoviePilot.app"
    info = {
        "backend": {"tag": tag, "commit": "0" * 40},
        "channel": {"repo": "owner/moviepilot-mac"} if channel is None else channel,
    }
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    (resources / "build-info.json").write_text(json.dumps(info), encoding="utf-8")
    return app


def release_payload(tag: str, *, sidecar: bool = True) -> dict:
    assets = [{"name": f"MoviePilot-{tag}-arm64-appweb.zip",
               "browser_download_url": f"https://example.test/{tag}.zip"}]
    if sidecar:
        assets.append({"name": f"MoviePilot-{tag}-arm64-appweb.zip.sha256",
                       "browser_download_url": f"https://example.test/{tag}.zip.sha256"})
    return {"tag_name": tag, "assets": assets}


def fake_pull(body: bytes = PAYLOAD):
    """假下载器：记下取过哪些地址，按后缀决定给校验文本还是包体。"""
    seen: list[str] = []

    def pull(url: str, dest: Path, token: str | None) -> Path:
        seen.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith(".sha256"):
            dest.write_text(f"{DIGEST}  MoviePilot-x.zip\n", encoding="utf-8")
        else:
            dest.write_bytes(body)
        return dest

    return pull, seen


class VersionTest(unittest.TestCase):
    def test_按数字段比版本(self):
        self.assertGreater(release.version_key("v3.1.10"), release.version_key("v3.1.9"))
        self.assertGreater(release.version_key("v3.2"), release.version_key("v3.1.7"))
        self.assertEqual(release.version_key(""), (-1,))


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.addCleanup(self._tmp.cleanup)
        os.environ.pop("MOVPILOT_HOST_REQUEST_FILE", None)
        os.environ.pop("GITHUB_TOKEN", None)

    def test_同版本不下载(self):
        pull, seen = fake_pull()
        verdict = check(bundle(self.root), self.config,
                        fetch=lambda url, token: release_payload("v3.1.6"), pull=pull)
        self.assertEqual(verdict["state"], "latest")
        self.assertEqual(seen, [])

    def test_没传仓库时用包里的通道(self):
        """命令行不给 --repo 是应用内唯一的用法，这里问的是哪个仓库必须对上。"""
        asked: list[str] = []

        def fetch(url: str, token) -> dict:
            asked.append(url)
            return release_payload("v3.1.7")

        pull, _ = fake_pull()
        verdict = check(bundle(self.root), self.config, fetch=fetch, pull=pull)
        self.assertEqual(asked, ["https://api.github.com/repos/owner/moviepilot-mac/releases/latest"])
        self.assertEqual(verdict["repo"], "owner/moviepilot-mac")

    def test_有新版就下载并校验(self):
        pull, seen = fake_pull()
        verdict = check(bundle(self.root), self.config,
                        fetch=lambda url, token: release_payload("v3.1.7"), pull=pull)
        self.assertEqual(verdict["state"], "available")
        self.assertEqual(len(seen), 2, "先取校验文件再取包")
        self.assertEqual(Path(verdict["zip"]).read_bytes(), PAYLOAD)
        self.assertEqual(verdict["sha256"], DIGEST)

    def test_校验不过就删掉包(self):
        pull, _ = fake_pull(body=b"tampered")
        with self.assertRaises(ChannelError):
            check(bundle(self.root), self.config,
                  fetch=lambda url, token: release_payload("v3.1.7"), pull=pull)
        self.assertEqual(list((self.config / "temp/mpweb/updates").glob("*.zip")), [])

    def test_没有增分包就报错(self):
        with self.assertRaises(ChannelError):
            check(bundle(self.root), self.config,
                  fetch=lambda url, token: {"tag_name": "v3.1.7", "assets": []}, pull=fake_pull()[0])

    def test_没配通道退出码单独给(self):
        app = bundle(self.root, channel={})
        self.assertEqual(main(["--app", str(app), "--config-dir", str(self.config)]), NO_CHANNEL)

    def test_命令行会投递握手(self):
        app = bundle(self.root)
        code = main(["--app", str(app), "--config-dir", str(self.config)],
                    fetch=lambda url, token: release_payload("v3.1.7"), pull=fake_pull()[0])
        self.assertEqual(code, AVAILABLE)
        request = json.loads(release.host_request_file(self.config).read_text(encoding="utf-8"))
        self.assertEqual(request["action"], "update")
        self.assertTrue(Path(request["zip"]).is_file())

    def test_只要包不投递(self):
        app = bundle(self.root)
        code = main(["--app", str(app), "--config-dir", str(self.config), "--no-handoff"],
                    fetch=lambda url, token: release_payload("v3.1.7"), pull=fake_pull()[0])
        self.assertEqual(code, AVAILABLE)
        self.assertFalse(release.host_request_file(self.config).exists())

    def test_命令行报已是最新(self):
        app = bundle(self.root)
        code = main(["--app", str(app), "--config-dir", str(self.config)],
                    fetch=lambda url, token: release_payload("v3.1.6"), pull=fake_pull()[0])
        self.assertEqual(code, UP_TO_DATE)
        self.assertFalse(release.host_request_file(self.config).exists())

    def test_取件走系统curl(self):
        # 用 file:// 跑一遍真实实现：至少证明拼装出来的 curl 命令能取回东西
        source = self.root / "asset.bin"
        source.write_bytes(PAYLOAD)
        got = release.download(source.as_uri(), self.config / "out.bin", None)
        self.assertEqual(got.read_bytes(), PAYLOAD)
        meta = self.root / "release.json"
        meta.write_text(json.dumps({"tag_name": "v9.9.9"}), encoding="utf-8")
        self.assertEqual(release.fetch_json(meta.as_uri(), None)["tag_name"], "v9.9.9")

    def test_旧包会被清走(self):
        updates = self.config / "temp/mpweb/updates"
        updates.mkdir(parents=True)
        (updates / "MoviePilot-v3.1.5-arm64-appweb.zip").write_bytes(b"old")
        pull, _ = fake_pull()
        check(bundle(self.root), self.config,
              fetch=lambda url, token: release_payload("v3.1.7"), pull=pull)
        self.assertEqual([p.name for p in updates.glob("*.zip")], ["MoviePilot-v3.1.7-arm64-appweb.zip"])

    def test_token_从配置文件认(self):
        (self.config / "app.env").write_text("GITHUB_TOKEN=ghabc\n", encoding="utf-8")
        self.assertEqual(release.github_token(self.config), "ghabc")
        (self.config / "app.env").write_text("GITHUB_TOKEN=moviepilot\n", encoding="utf-8")
        self.assertIsNone(release.github_token(self.config))
        os.environ["GITHUB_TOKEN"] = "from-env"
        self.assertEqual(release.github_token(self.config), "from-env")


if __name__ == "__main__":
    unittest.main()
