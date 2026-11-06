"""系统托盘：桌面常驻入口。"""
from __future__ import annotations

import logging
import os
import threading
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont

from . import autostart, paths
from .runtime import Runtime

logger = logging.getLogger(__name__)


def _icon_image() -> Image.Image:
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, size - 2, size - 2), 14, fill=(255, 70, 85))

    for candidate in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf"):
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, 38)
                draw.text((size / 2, size / 2 - 2), "V", font=font, fill="white", anchor="mm")
                return image
            except OSError:
                continue

    draw.polygon([(18, 16), (28, 16), (32, 40), (36, 16), (46, 16), (38, 50), (26, 50)], fill="white")
    return image


def _notify(icon: pystray.Icon, message: str, title: str = "无畏契约商店推送") -> None:
    try:
        icon.notify(message, title)
    except Exception:
        pass


def run_tray(runtime: Runtime) -> None:
    def open_panel(icon=None, item=None) -> None:
        webbrowser.open(runtime.url)

    def push(icon: pystray.Icon, item=None) -> None:
        _notify(icon, "正在获取商店并推送…")

        def worker() -> None:
            result = runtime.push_now()
            _notify(icon, result["message"], "推送完成" if result["ok"] else "推送失败")

        threading.Thread(target=worker, daemon=True).start()

    def toggle_autostart(icon: pystray.Icon, item=None) -> None:
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except Exception as exc:
            _notify(icon, f"设置开机自启失败：{exc}")

    def open_data_dir(icon=None, item=None) -> None:
        try:
            os.startfile(paths.DATA_DIR)  # type: ignore[attr-defined]
        except Exception:
            pass

    def quit_app(icon: pystray.Icon, item=None) -> None:
        runtime.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开设置面板", open_panel, default=True),
        pystray.MenuItem("立即推送一次", push),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "开机自动启动",
            toggle_autostart,
            checked=lambda item: autostart.is_enabled(),
        ),
        pystray.MenuItem("打开数据目录", open_data_dir),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    icon = pystray.Icon("valorant-shop-push", _icon_image(), "无畏契约商店推送", menu)
    logger.info("托盘已启动，设置面板：%s", runtime.url)
    icon.run()
