"""MoviePilot 应用版前端托管层。"""

from .proxy import BackendProxy
from .server import PROXY_MOUNTS, create_app, serve
from .watchdog import BackendWatchdog

__all__ = ["BackendProxy", "BackendWatchdog", "PROXY_MOUNTS", "create_app", "serve"]
