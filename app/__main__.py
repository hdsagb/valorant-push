"""程序入口。

常用方式：
    python -m app                      启动托盘 + 本地设置面板
    python -m app --headless           只跑后台（无托盘，便于调试）
    python -m app --push-now           立即推送一次后退出
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse  # noqa: E402
import asyncio  # noqa: E402
import logging  # noqa: E402
import time  # noqa: E402
import webbrowser  # noqa: E402

from app import __version__, paths, pipeline  # noqa: E402
from app.instance import InstanceLock, running_url, save_port  # noqa: E402
from app.runtime import Runtime  # noqa: E402

logger = logging.getLogger(__name__)


def say(message: str) -> None:
    if sys.stdout is not None:
        print(message, flush=True)


def fatal(message: str) -> None:
    logger.error(message)
    say(message)
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "无畏契约商店推送", 0x10)
        except Exception:
            pass


async def _push_once() -> int:
    try:
        result = await pipeline.run_push(trigger="cli")
    except pipeline.LoginRequired as exc:
        fatal(f"{exc}\n请先运行程序并在设置面板扫码登录。")
        return 1
    except Exception as exc:
        fatal(f"推送失败：{exc}")
        return 1
    say(result.summary)
    say(f"\n推送完成，渠道：{result.channel}，图片：{result.image}")
    if result.fallback_reason:
        say(result.fallback_reason)
    return 0


def _wait_forever() -> None:
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="valorant-push-personal", description="无畏契约商店推送 · 个人版"
    )
    parser.add_argument("--headless", action="store_true", help="不显示托盘，仅运行后台服务")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开设置页面")
    parser.add_argument("--push-now", action="store_true", help="立即推送一次后退出")
    parser.add_argument("--port", type=int, default=None, help="本地设置面板端口，默认 8787")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="store_true", help="显示版本号")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.version:
        say(f"无畏契约商店推送 个人版 v{__version__}")
        return 0

    paths.setup_logging(args.debug)
    logger.info("启动 v%s，数据目录：%s", __version__, paths.DATA_DIR)

    lock = InstanceLock()
    if not lock.acquire():
        url = running_url()
        message = "程序已经在运行。"
        if url:
            message += f"\n设置面板：{url}"
            if not args.no_browser:
                webbrowser.open(url)
        say(message)
        return 0

    runtime: Runtime | None = None
    try:
        if args.push_now:
            return asyncio.run(_push_once())

        runtime = Runtime(args.port)
        url = runtime.start()
        save_port(url)
        say(f"设置面板：{url}")
        if not args.no_browser:
            webbrowser.open(url)

        if args.headless:
            say("后台服务运行中，按 Ctrl+C 退出。")
            _wait_forever()
            return 0

        try:
            from app.tray import run_tray
        except ImportError as exc:
            logger.error("托盘组件不可用：%s", exc)
            say("未安装托盘依赖（pystray），已转为后台模式运行。")
            _wait_forever()
            return 0

        run_tray(runtime)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.exception("启动失败")
        fatal(f"启动失败：{exc}")
        return 1
    finally:
        if runtime:
            runtime.stop()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
