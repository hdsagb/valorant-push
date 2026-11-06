"""开机自启：写入当前用户的 Windows 启动项（不需要管理员权限）。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import paths

logger = logging.getLogger(__name__)

VALUE_NAME = "ValorantPushPersonal"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --no-browser'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else Path(sys.executable)
    script = paths.ROOT / "app" / "__main__.py"
    return f'"{launcher}" "{script}" --no-browser'


def _winreg():
    if sys.platform != "win32":
        raise RuntimeError("开机自启仅支持 Windows")
    import winreg  # noqa: PLC0415

    return winreg


def is_enabled() -> bool:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    winreg = _winreg()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
            logger.info("已开启开机自启：%s", _command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
                logger.info("已关闭开机自启")
            except FileNotFoundError:
                pass
