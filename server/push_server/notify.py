"""定时推送最终失败通知（服务器版）。

与 pipeline._notify_expired（凭证失效通知）互补：
- 凭证失效 → pipeline 在推送过程中即时通知（引导重新导入凭证）
- 推送最终失败 → scheduler 重试用尽后由本模块通知（报告当日结果）

按天去重：同一天只发一次，防容器重启后重复通知；发送失败仅记日志，绝不影响调度。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .channels import OutgoingMessage
from .channels import email as email_channel
from .channels import wecom as wecom_channel
from .config import channel_configured
from .models import zone

logger = logging.getLogger(__name__)

SUBJECT_FAILURE = "无畏契约商店推送 · 定时推送失败"
MAX_ERROR_CHARS = 300

# 模块级去重标记（会话仅内存态，与 scheduler 的模块级计数同模式）
_notified_date = ""


async def notify_push_failure(settings: dict[str, Any], error: str, attempts: int) -> None:
    """定时推送最终失败后通知（按天去重）。渠道未配置/发送失败时仅记日志。"""
    global _notified_date
    today = datetime.now(zone()).strftime("%Y-%m-%d")
    if _notified_date == today:
        return
    _notified_date = today  # 发送前写入：渠道故障不引起重复

    text = (
        f"今日定时推送最终失败（已重试 {attempts} 次）。\n"
        f"原因：{error[:MAX_ERROR_CHARS]}\n"
        "可到管理界面查看详情、手动重试，或检查渠道配置。"
    )
    message = OutgoingMessage(subject=SUBJECT_FAILURE, text=text, html="")

    other = "email" if settings["channel"] == "wecom" else "wecom"
    for channel in (settings["channel"], other):
        if not channel_configured(settings, channel):
            continue
        try:
            if channel == "wecom":
                await wecom_channel.send(settings, message)
            else:
                await email_channel.send(settings, message)
            logger.info("推送失败通知已发送（渠道：%s）", channel)
            return
        except Exception as exc:  # 通知失败不影响主流程
            logger.warning("推送失败通知发送未成功（%s）：%s", channel, exc)


def reset_state() -> None:
    """测试用：清空按天去重标记。"""
    global _notified_date
    _notified_date = ""
