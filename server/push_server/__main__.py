"""服务器版命令行入口。

用法：
  python -m push_server --push-now   # 立即执行一次推送（供 cron/systemd 调用）
  python -m push_server --import-credentials <.vpcred 文件>   # 从个人版导出的凭证包导入账号
  python -m push_server --version    # 打印版本号
  python -m push_server --debug      # 配合 --push-now 开启调试日志
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path

from . import __version__, credpack, paths, pipeline

logger = logging.getLogger(__name__)


def import_command(pack_path: Path) -> int:
    """--import-credentials 入口。退出码：0=导入/更新至少一个；1=文件或口令错误；2=文件有效但无账号被导入。"""
    if not pack_path.is_file():
        print(f"导入失败：文件不存在 {pack_path}", file=sys.stderr)
        return 1
    try:
        raw = pack_path.read_bytes()
    except OSError as exc:
        print(f"导入失败：读取文件出错 {exc}", file=sys.stderr)
        return 1
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            # 非交互（管道/自动化）：直接读 stdin 一行。
            # getpass 在 Windows 管道 stdin 下会走 msvcrt.getwch() 等待控制台而挂起，
            # 不能依赖其自动退化（stdin 仍可能 is sys.__stdin__）。
            password = sys.stdin.readline().rstrip("\r\n")
        else:
            password = getpass.getpass("输入凭证包口令：")
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    try:
        payload = credpack.parse_pack(raw, password)
    except credpack.CredpackError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1

    result = credpack.import_accounts(payload)
    for item in result["skipped"]:
        print(f"跳过 {item['account']}：{item['reason']}")
    print(
        f"导入完成：新增 {result['added']} 个，更新 {result['updated']} 个"
        f"（共 {result['total']} 个，跳过 {len(result['skipped'])} 个）"
    )
    if result["added"] + result["updated"] == 0:
        return 2
    print()
    print("注意：同一账号凭证只在一端启用推送——这些账号已在服务器版启用，请在个人版停止其定时推送。")
    print("提醒：导入完成后请删除本地导出的凭证包文件。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="push_server", description="无畏契约商店推送 · 服务器版"
    )
    parser.add_argument("--push-now", action="store_true", help="立即执行一次推送流程")
    parser.add_argument(
        "--import-credentials",
        metavar="文件",
        help="从个人版导出的凭证包（.vpcred）导入账号；口令交互输入",
    )
    parser.add_argument("--version", action="store_true", help="打印版本号")
    parser.add_argument("--debug", action="store_true", help="开启调试日志")
    args = parser.parse_args(argv)

    if args.version:
        print(f"push_server {__version__}")
        return 0

    paths.setup_logging(debug=args.debug)

    if args.import_credentials:
        return import_command(Path(args.import_credentials))

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
