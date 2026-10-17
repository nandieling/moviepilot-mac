"""更新落地测试：换包、继承站点资源、重建插件软链、依赖变化时拒绝。"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pythonhost"))

from mpweb.updater import UpdateError, app_version, apply_payload, payload_from_staged, payload_from_zip

LOCK_A = "依赖清单 A\n"
SITE_BIN = b"sites-index"
SITE_SO = b"native-extension"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def installed_bundle(root: Path, *, version: str = "v1.0.0") -> Path:
    """搭一个够用的假包：后端源码、前端产物、站点资源和 build-info。"""
    app = root / "MoviePilot.app"
    resources = app / "Contents" / "Resources"
    backend = resources / "app"
    site = backend / "app" / "application" / "site"
    site.mkdir(parents=True)
    (backend / "version.py").write_text(f"APP_VERSION = '{version}'\n", encoding="utf-8")
    (backend / "uv.lock").write_text(LOCK_A, encoding="utf-8")
    (site / "user.sites.v3.bin").write_bytes(SITE_BIN)
    (site / "sites.cpython-314-darwin.so").write_bytes(SITE_SO)
    plugins = backend / "app" / "plugins"
    plugins.mkdir()
    (plugins / "__init__.py").write_text("出厂标记\n", encoding="utf-8")
    web = resources / "web"
    web.mkdir()
    (web / "index.html").write_text("旧前端", encoding="utf-8")
    (resources / "build-info.json").write_text(
        json.dumps({"runtime": {"lock_sha256": sha256(LOCK_A.encode())}}), encoding="utf-8"
    )
    return app


def new_backend(root: Path, *, version: str = "v2.0.0", lock: str = LOCK_A) -> Path:
    """上游后端包解出来的样子：不带站点运行资源。"""
    backend = root / "新后端"
    (backend / "app" / "application" / "site").mkdir(parents=True)
    (backend / "app" / "plugins").mkdir(parents=True)
    (backend / "app" / "plugins" / "__init__.py").write_text("新版标记\n", encoding="utf-8")
    (backend / "version.py").write_text(f"APP_VERSION = '{version}'\n", encoding="utf-8")
    (backend / "uv.lock").write_text(lock, encoding="utf-8")
    return backend


def new_web(root: Path) -> Path:
    web = root / "新前端"
    web.mkdir()
    (web / "index.html").write_text("新前端", encoding="utf-8")
    return web


class ZipPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.app = installed_bundle(self.base)
        self.config = self.base / "config"
        self.mirror = self.config / "plugin_packages"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _zip(self, backend: Path, web: Path) -> Path:
        archive = self.base / "delta.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("manifest.json", json.dumps({"backend": {"tag": "v2.0.0"}}))
            for source, prefix in ((backend, "app"), (web, "web")):
                for path in source.rglob("*"):
                    if path.is_file():
                        bundle.write(path, f"{prefix}/{path.relative_to(source).as_posix()}")
        return archive

    def test_backend_and_frontend_are_swapped_together(self) -> None:
        archive = self._zip(new_backend(self.base), new_web(self.base))
        with payload_from_zip(archive) as payload:
            summary = apply_payload(self.app, payload, config_dir=self.config, resign=False)
        resources = self.app / "Contents" / "Resources"
        self.assertEqual(app_version(resources / "app"), "v2.0.0")
        self.assertEqual((resources / "web" / "index.html").read_text(), "新前端")
        self.assertEqual(summary["swapped"], ["后端源码", "前端产物"])
        info = json.loads((resources / "build-info.json").read_text())
        self.assertEqual(info["backend"]["tag"], "v2.0.0")
        self.assertIn("updated_at", info)

    def test_site_runtime_resources_survive_the_swap(self) -> None:
        archive = self._zip(new_backend(self.base), new_web(self.base))
        with payload_from_zip(archive) as payload:
            apply_payload(self.app, payload, config_dir=self.config, resign=False)
        site = self.app / "Contents" / "Resources" / "app" / "app" / "application" / "site"
        self.assertEqual((site / "user.sites.v3.bin").read_bytes(), SITE_BIN)
        self.assertEqual((site / "sites.cpython-314-darwin.so").read_bytes(), SITE_SO)

    def test_plugin_dir_is_relinked_into_the_data_dir(self) -> None:
        archive = self._zip(new_backend(self.base), new_web(self.base))
        with payload_from_zip(archive) as payload:
            apply_payload(self.app, payload, config_dir=self.config, resign=False)
        in_package = self.app / "Contents" / "Resources" / "app" / "app" / "plugins"
        self.assertTrue(in_package.is_symlink())
        self.assertEqual(in_package.resolve(), self.mirror.resolve())
        self.assertTrue((self.mirror / "__init__.py").is_file())

    def test_plugins_left_in_the_bundle_are_carried_into_the_mirror(self) -> None:
        stale = self.app / "Contents" / "Resources" / "app" / "app" / "plugins" / "OldPlugin.py"
        stale.write_text("旧插件", encoding="utf-8")
        archive = self._zip(new_backend(self.base), new_web(self.base))
        with payload_from_zip(archive) as payload:
            apply_payload(self.app, payload, config_dir=self.config, resign=False)
        self.assertEqual((self.mirror / "OldPlugin.py").read_text(), "旧插件")

    def test_changed_dependency_lock_refuses_the_swap(self) -> None:
        archive = self._zip(new_backend(self.base, lock="依赖清单 B\n"), new_web(self.base))
        with payload_from_zip(archive) as payload:
            with self.assertRaises(UpdateError):
                apply_payload(self.app, payload, config_dir=self.config, resign=False)
        resources = self.app / "Contents" / "Resources"
        self.assertEqual(app_version(resources / "app"), "v1.0.0")
        self.assertEqual((resources / "web" / "index.html").read_text(), "旧前端")

    def test_lock_change_can_be_overridden(self) -> None:
        archive = self._zip(new_backend(self.base, lock="依赖清单 B\n"), new_web(self.base))
        with payload_from_zip(archive) as payload:
            apply_payload(
                self.app,
                payload,
                config_dir=self.config,
                resign=False,
                ignore_lock_change=True,
            )
        self.assertEqual(app_version(self.app / "Contents" / "Resources" / "app"), "v2.0.0")


class StagedPayloadTest(unittest.TestCase):
    """上游自己下载并确认过的载荷：install.json + backend.zip + frontend.zip。"""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.app = installed_bundle(self.base)
        self.config = self.base / "config"
        self.staged = self.config / "temp" / "moviepilot-update"
        self.staged.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _stage(self, *, backend_sha: str | None = None) -> None:
        backend = new_backend(self.base)
        web = new_web(self.base)
        backend_zip = self.staged / "backend.zip"
        with zipfile.ZipFile(backend_zip, "w") as archive:
            for path in backend.rglob("*"):
                if path.is_file():
                    archive.write(path, f"MoviePilot/{path.relative_to(backend).as_posix()}")
        frontend_zip = self.staged / "frontend.zip"
        with zipfile.ZipFile(frontend_zip, "w") as archive:
            for path in web.rglob("*"):
                if path.is_file():
                    archive.write(path, f"dist/{path.relative_to(web).as_posix()}")
        (self.staged / "install.json").write_text(
            json.dumps(
                {
                    "targets": ["application"],
                    "version": "v2.0.0",
                    "frontend_version": "v2.0.0",
                    "backend_archive": str(backend_zip),
                    "frontend_archive": str(frontend_zip),
                    "backend_sha256": backend_sha or sha256(backend_zip.read_bytes()),
                    "frontend_sha256": sha256(frontend_zip.read_bytes()),
                }
            ),
            encoding="utf-8",
        )

    def test_staged_application_payload_is_applied(self) -> None:
        self._stage()
        with payload_from_staged(self.config) as payload:
            summary = apply_payload(self.app, payload, config_dir=self.config, resign=False)
        resources = self.app / "Contents" / "Resources"
        self.assertEqual(app_version(resources / "app"), "v2.0.0")
        self.assertEqual((resources / "web" / "index.html").read_text(), "新前端")
        self.assertTrue((resources / "app" / "app" / "plugins").is_symlink())
        self.assertEqual(summary["to"], "v2.0.0")

    def test_tampered_archive_is_rejected(self) -> None:
        self._stage(backend_sha=sha256(b"not-that-archive"))
        with self.assertRaises(UpdateError):
            with payload_from_staged(self.config):
                pass

    def test_missing_manifest_is_reported(self) -> None:
        with self.assertRaises(UpdateError):
            with payload_from_staged(self.config):
                pass


if __name__ == "__main__":
    unittest.main()
