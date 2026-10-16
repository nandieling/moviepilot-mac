"""以后宿主可托管的方式启动上游后端。

后端源码不参与打包改造，这里只加一层薄壳：转发停止信号，
并在宿主消失时代为收尾，避免留下占着端口的孤儿进程。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

# 后端自身有优雅退出窗口，这里给足时间再强杀。
TERM_GRACE_SECONDS = 30
PARENT_POLL_SECONDS = 2.0


def _host_pid() -> int:
    try:
        return int(os.environ.get("MOVPILOT_HOST_PID", "0") or 0)
    except ValueError:
        return 0


def main(argv: list[str] | None = None) -> int:
    host_pid = _host_pid()
    child = subprocess.Popen([sys.executable, "-m", "app.main", *(argv or [])])

    def forward(signum, frame):  # noqa: ARG001
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    def watch_parent() -> None:
        if host_pid <= 0:
            return
        while child.poll() is None:
            time.sleep(PARENT_POLL_SECONDS)
            if os.getppid() == 1 or os.getppid() != host_pid:
                print("[mpbackend] 宿主进程已退出，后端跟随退出", flush=True)
                if child.poll() is None:
                    child.send_signal(signal.SIGTERM)
                    try:
                        child.wait(TERM_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        child.kill()
                os._exit(1)

    threading.Thread(target=watch_parent, daemon=True).start()
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())
