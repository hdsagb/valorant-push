"""推送流水线：取商店 → 渲染图片 → 通过渠道发送。"""
from __future__ import annotations

import html as html_lib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import cn_client, config, paths, render
from .channels import ChannelError, OutgoingMessage
from .channels import email as email_channel
from .channels import wecom as wecom_channel
from .models import ShopSection

logger = logging.getLogger(__name__)

CHANNEL_NAMES = {"email": "邮件", "wecom": "企业微信"}


class LoginRequired(RuntimeError):
    """还没有登录，或登录已失效。"""


@dataclass
class PushResult:
    ok: bool
    channel: str
    image: Path | None
    summary: str
    message: str = ""
    fallback_reason: str = ""


def build_summary(sections: list[ShopSection], now: datetime | None = None) -> str:
    now = now or datetime.now()
    lines = [f"无畏契约 今日商店 · {now.strftime('%Y-%m-%d')}", ""]
    for section in sections:
        title = section.title
        if section.end_text:
            title += f"（截止 {section.end_text}）"
        lines.append(f"【{title}】")
        for index, item in enumerate(section.items, start=1):
            lines.append(f"{index}. {item.name} · {item.price_vp} VP")
        lines.append("")
    lines.append("价格单位：点券（VP）· 个人自用，仅供参考")
    return "\n".join(lines).strip()


def build_html(sections: list[ShopSection], has_image: bool) -> str:
    blocks: list[str] = []
    for section in sections:
        rows = []
        for index, item in enumerate(section.items, start=1):
            name = html_lib.escape(item.name)
            quality = html_lib.escape(item.quality)
            rows.append(
                "<tr>"
                f'<td style="padding:6px 12px;color:#8a9bb0;">{index}</td>'
                f'<td style="padding:6px 12px;color:#f2f5f8;">{name}'
                + (f'<span style="color:#8a9bb0;font-size:12px;"> · {quality}</span>' if quality else "")
                + "</td>"
                f'<td style="padding:6px 12px;color:#ffe278;text-align:right;">{item.price_vp} VP</td>'
                "</tr>"
            )
        end = f'<span style="color:#8a9bb0;font-size:12px;">截止 {section.end_text}</span>' if section.end_text else ""
        blocks.append(
            '<div style="margin:18px 0;">'
            f'<div style="font-size:16px;font-weight:600;color:#fff;margin-bottom:8px;">'
            f"{html_lib.escape(section.title)} {end}</div>"
            '<table style="border-collapse:collapse;font-size:14px;width:100%;">'
            + "".join(rows)
            + "</table></div>"
        )

    image_html = (
        '<img src="cid:shopimage" alt="商店图片" '
        'style="width:100%;max-width:736px;border-radius:12px;display:block;margin:12px 0;">'
        if has_image
        else ""
    )
    return (
        '<div style="background:#0f1923;padding:24px;border-radius:16px;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;'
        'max-width:760px;">'
        '<div style="color:#ff4655;font-size:12px;letter-spacing:2px;">VALORANT · SHOP</div>'
        '<div style="color:#fff;font-size:22px;font-weight:700;margin-top:4px;">今日商店</div>'
        f"{image_html}"
        + "".join(blocks)
        + '<div style="color:#5d6d7e;font-size:12px;margin-top:16px;">'
        "价格单位：点券（VP）· 个人自用，仅供参考</div></div>"
    )


async def _notify_expired(settings: dict[str, Any]) -> None:
    text = (
        "无畏契约登录凭证已失效，今日推送已暂停。\n"
        "请打开「无畏契约商店推送」托盘菜单 → 打开设置面板，重新扫码登录。"
    )
    message = OutgoingMessage(subject="无畏契约商店推送 · 需要重新登录", text=text, html="")
    for channel in (settings["channel"], "email" if settings["channel"] == "wecom" else "wecom"):
        if not config.channel_configured(settings, channel):
            continue
        try:
            await _send(channel, settings, message)
            return
        except Exception as exc:  # 通知失败不影响主流程
            logger.warning("失效通知发送失败（%s）：%s", channel, exc)


async def _send(channel: str, settings: dict[str, Any], message: OutgoingMessage) -> None:
    if channel == "wecom":
        await wecom_channel.send(settings, message)
    else:
        await email_channel.send(settings, message)


async def _deliver(settings: dict[str, Any], message: OutgoingMessage) -> tuple[str, str]:
    """按配置发送，返回 (实际使用的渠道, 回退说明)。"""
    primary = settings["channel"]
    fallback_reason = ""

    if not config.channel_configured(settings, primary):
        other = "email" if primary == "wecom" else "wecom"
        if config.channel_configured(settings, other):
            fallback_reason = (
                f"{CHANNEL_NAMES[primary]}渠道还没配置完整，本次已改用{CHANNEL_NAMES[other]}"
            )
            primary = other
        else:
            raise RuntimeError("推送渠道还没配置，请先在设置面板填写并测试")

    try:
        await _send(primary, settings, message)
    except ChannelError as exc:
        if (
            primary == "wecom"
            and exc.code == 60020
            and config.email_configured(settings)
        ):
            logger.warning("企业微信 60020（可信 IP 校验失败），自动改用邮件")
            await email_channel.send(settings, message)
            return "email", "企业微信可信 IP 校验失败（60020），本次已自动改用邮件"
        raise

    return primary, fallback_reason


async def run_push(trigger: str = "manual") -> PushResult:
    """执行一次完整推送。凭证失效时抛 LoginRequired。"""
    paths.ensure_dirs()
    settings = config.load_settings()
    now = datetime.now()

    if not config.has_credentials():
        raise LoginRequired("尚未登录，请先在设置面板扫码登录")

    credentials = cn_client.load_credentials()
    if not credentials:
        raise LoginRequired("登录凭证不存在，请重新扫码登录")

    try:
        response = await cn_client.fetch_response(credentials)
    except cn_client.AuthExpiredError as exc:
        config.clear_credentials()
        config.update_state(
            auth_expired=True,
            last_push_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            last_push_ok=False,
            last_error=str(exc),
        )
        await _notify_expired(settings)
        raise LoginRequired(f"{exc}，请重新扫码登录") from exc

    sections = cn_client.parse_sections(response, settings["sections"])
    if not sections:
        raise RuntimeError("今天没有获取到任何商店商品，请稍后再试")

    image_path = await render.render_sections(sections)
    summary = build_summary(sections, now)
    subject = f"无畏契约 · {now.strftime('%m-%d')} 商店"
    message = OutgoingMessage(
        subject=subject,
        text=summary,
        html=build_html(sections, has_image=True),
        image=image_path,
    )

    logger.info("开始推送（触发方式：%s，渠道：%s）", trigger, settings["channel"])
    channel, fallback_reason = await _deliver(settings, message)
    if fallback_reason:
        logger.warning(fallback_reason)

    config.update_state(
        last_push_date=now.strftime("%Y-%m-%d"),
        last_push_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        last_push_ok=True,
        last_channel=channel,
        last_error="",
        last_summary=summary[:800],
        auth_expired=False,
    )
    logger.info("推送完成，渠道：%s", CHANNEL_NAMES.get(channel, channel))
    return PushResult(
        ok=True,
        channel=channel,
        image=image_path,
        summary=summary,
        message="推送成功",
        fallback_reason=fallback_reason,
    )
