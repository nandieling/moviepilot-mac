#!/usr/bin/env python3
"""同步站点运行资源（sites 原生扩展与站点数据包）。

这两个文件不在后端 git 里，是安装阶段从 MoviePilot-Resources 取当前解释器/平台对应的那份，
CLI 版靠 local_setup.py init 完成。应用版在组包时就位，用户机器上不再需要编译器与网络。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lib as rl

RESOURCES_ZIP = "https://github.com/jxxghp/MoviePilot-Resources/archive/refs/heads/main.zip"
MARKER = ".resources.json"

# local_setup 的 ROOT 取自脚本自身位置，因此必须用包内那份脚本来跑，落点才在包内
SNIPPET = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
import local_setup
copied = local_setup.install_resources(None, Path(sys.argv[2]))
print("已同步：" + "、".join(copied))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path, help="包内后端源码目录（Resources/app）")
    parser.add_argument("--python", required=True, type=Path, help="包内解释器，决定取哪个 ABI 的扩展")
    parser.add_argument("--cache-dir", type=Path, default=rl.DEFAULT_CACHE)
    parser.add_argument("--offline-dir", type=Path, help="已解包的 MoviePilot-Resources 仓库，跳过下载")
    args = parser.parse_args()

    staging: Path | None = None
    if args.offline_dir:
        repo = args.offline_dir.expanduser().resolve()
        source = next((p for p in (repo / "resources.v3", repo / "resources" / "resources.v3")
                       if p.is_dir()), None)
        if source is None:
            raise SystemExit(f"{repo} 下没有 resources.v3")
        digest = "offline"
    else:
        archive = rl.download(RESOURCES_ZIP, args.cache_dir / "moviepilot-resources-main.zip",
                              label="站点资源")
        digest = rl.sha256_file(archive)
        staging = Path(tempfile.mkdtemp(prefix="resources-", dir=args.cache_dir))
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging)
        source = next((p for p in staging.glob("*/resources.v3") if p.is_dir()), None)
        if source is None:
            shutil.rmtree(staging, ignore_errors=True)
            raise SystemExit("资源包里没有 resources.v3 目录，上游布局变了")

    with tempfile.TemporaryDirectory(prefix="mp-resources-check-") as config_dir:
        result = subprocess.run([str(args.python), "-c", SNIPPET, str(args.app), str(source)],
                                check=True, text=True, capture_output=True,
                                env={**os.environ, "CONFIG_DIR": config_dir})
        rl.log("  " + result.stdout.strip().splitlines()[-1])

    files = {}
    for name in ("user.sites.v3.bin",):
        target = args.app / "app" / "application" / "site" / name
        if not target.is_file():
            raise SystemExit(f"缺少 {name}，站点资源没装全")
        files[name] = target.stat().st_size
    native = sorted((args.app / "app" / "application" / "site").glob("sites.cpython-*.so"))
    if not native:
        raise SystemExit("缺少 sites 原生扩展，站点资源没装全")
    files[native[0].name] = native[0].stat().st_size

    (args.app / MARKER).write_text(json.dumps({
        "source": RESOURCES_ZIP if digest != "offline" else str(source),
        "zip_sha256": digest, "files": files,
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if staging:
        shutil.rmtree(staging, ignore_errors=True)
    rl.log(f"  站点资源就绪：{'、'.join(f'{k} {v // 1024} KB' for k, v in files.items())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
