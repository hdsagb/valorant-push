"""服务器版命令行入口。

用法：
  python -m push_server [serve]   # 常驻 serve：管理界面 + 定时推送（无参数默认此项）
  python -m push_server --push-now   # 立即执行一次推送（供 cron/systemd 调用）
  python -m push_server --import-credentials <.vpcred 文件>   # 从个人版导出的凭证包导入账号
  python -m push_server --version    # 打印版本号
  python -m push_server --debug      # 配合 --push-now 开启调试日志

serve 绑定可用 --web-host/--web-port 或环境变量 VAL_SERVER_WEB_HOST/VAL_SERVER_WEB_PORT 覆盖。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path

from aiohttp import web as aio_web

from . import __version__, auth, config, credpack, paths, pipeline, scheduler
from . import web as web_module

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


def resolve_web_bind(host: str | None, port: int | None) -> tuple[str, int]:
    """绑定优先级：命令行参数 > 环境变量 > 缺省值。"""
    host = host or os.getenv("VAL_SERVER_WEB_HOST") or "0.0.0.0"
    port = port or int(os.getenv("VAL_SERVER_WEB_PORT") or "8787")
    return host, port


async def _serve_async(host: str, port: int) -> None:
    """常驻 serve：管理界面 + 定时推送同一事件循环；Ctrl+C/进程终止时清理。"""
    # 旧部署迁移种子（M4.5 决策 4）：仅 serve 路径调用，--push-now/--import-credentials 不调
    config.seed_domain_from_env()
    if not auth.is_initialized():
        auth.ensure_setup_code()  # 首次启动生成一次性设置码并打印到日志
    web_app = web_module.build_app()
    runner = aio_web.AppRunner(web_app)
    await runner.setup()
    site = aio_web.TCPSite(runner, host, port)
    await site.start()
    scheduler_task = asyncio.create_task(scheduler.loop())
    web_module.set_scheduler_task(scheduler_task)
    logger.info("管理界面已启动 http://%s:%s（未初始化时从日志获取一次性设置码）", host, port)
    try:
        await asyncio.Event().wait()  # 常驻，直至进程被终止
    finally:
        web_module.set_scheduler_task(None)
        scheduler_task.cancel()
        await runner.cleanup()


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
    parser.add_argument(
        "--web-host",
        default=None,
        help="管理界面绑定地址（默认 VAL_SERVER_WEB_HOST，再缺省 0.0.0.0）",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="管理界面端口（默认 VAL_SERVER_WEB_PORT，再缺省 8787）",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"push_server {__version__}")
        return 0

    paths.setup_logging(debug=args.debug)

    if args.import_credentials:
        return import_command(Path(args.import_credentials))

    if args.push_now:
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

    # 无标志默认 serve：常驻进程同时跑管理界面与定时推送
    host, port = resolve_web_bind(args.web_host, args.web_port)
    try:
        asyncio.run(_serve_async(host, port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
