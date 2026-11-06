"""单实例锁：避免重复双击导致两个定时任务同时推送。"""
from __future__ import annotations

import logging
import sys

from . import paths

logger = logging.getLogger(__name__)

LOCK_FILE = paths.DATA_DIR / "app.lock"
PORT_FILE = paths.DATA_DIR / "port.txt"


class InstanceLock:
    def __init__(self) -> None:
        self._handle = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True
        import msvcrt  # noqa: PLC0415

        paths.ensure_dirs()
        handle = open(LOCK_FILE, "a+")
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if not self._handle:
            return
        import msvcrt  # noqa: PLC0415

        try:
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None


def running_url() -> str:
    try:
        port = PORT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return f"http://127.0.0.1:{port}" if port else ""


def save_port(url: str) -> None:
    port = url.rsplit(":", 1)[-1]
    try:
        PORT_FILE.write_text(port, encoding="utf-8")
    except OSError:
        pass
