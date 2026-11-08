"""服务器版命令行入口。

用法：
  python -m push_server --push-now   # 立即执行一次推送（供 cron/systemd 调用）
  python -m push_server --version    # 打印版本号
  python -m push_server --debug      # 配合 --push-now 开启调试日志
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__, paths, pipeline

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="push_server", description="无畏契约商店推送 · 服务器版"
    )
    parser.add_argument("--push-now", action="store_true", help="立即执行一次推送流程")
    parser.add_argument("--version", action="store_true", help="打印版本号")
    parser.add_argument("--debug", action="store_true", help="开启调试日志")
    args = parser.parse_args(argv)

    if args.version:
        print(f"push_server {__version__}")
        return 0

    paths.setup_logging(debug=args.debug)

    if not args.push_now:
        parser.print_help()
        return 0

    try:
        result = asyncio.run(pipeline.run_push(trigger="cli"))
    except pipeline.LoginRequired as exc:
        # 无账号 / 全部凭证失效：属预期状态，退出 0 让调度器不误报
        logger.info("跳过推送：%s", exc)
        return 0
    except Exception:
        logger.exception("推送流程异常退出")
        return 1

    logger.info("推送结果：%s（渠道：%s）", result.message, result.channel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
