"""推送流水线：取商店 → 渲染图片 → 通过渠道发送。"""
from __future__ import annotations

import html as html_lib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import accounts, cn_client, config, paths, render
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


def build_summary(
    entries: list[tuple[str, str, list[ShopSection]]],
    now: datetime | None = None,
    notes: list[str] | None = None,
) -> str:
    now = now or datetime.now()
    lines = [f"无畏契约 今日商店 · {now.strftime('%Y-%m-%d')}", ""]
    for name, meta, sections in entries:
        label = name or "账号"
        if meta:
            label += f"（{meta}）"
        lines.append(f"◆ {label}")
        for section in sections:
            title = section.title
            if section.end_text:
                title += f"（截止 {section.end_text}）"
            lines.append(f"  【{title}】")
            for index, item in enumerate(section.items, start=1):
                lines.append(f"  {index}. {item.name} · {item.price_vp} VP")
        lines.append("")
    lines.append("价格单位：点券（VP）· 个人自用，仅供参考")
    for note in notes or []:
        lines.append("")
        lines.append(note)
    return "\n".join(lines).strip()


def build_html(
    entries: list[tuple[str, str, list[ShopSection]]],
    has_image: bool,
    notes: list[str] | None = None,
) -> str:
    blocks: list[str] = []
    for name, meta, sections in entries:
        meta_html = (
            f'<span style="color:#8a9bb0;font-size:12px;margin-left:8px;">{html_lib.escape(meta)}</span>'
            if meta
            else ""
        )
        blocks.append(
            '<div style="margin:22px 0 6px;padding-left:10px;border-left:3px solid #ff4655;">'
            f'<span style="color:#fff;font-size:17px;font-weight:700;">{html_lib.escape(name or "账号")}</span>'
            f"{meta_html}</div>"
        )
        for section in sections:
            rows = []
            for index, item in enumerate(section.items, start=1):
                item_name = html_lib.escape(item.name)
                quality = html_lib.escape(item.quality)
                rows.append(
                    "<tr>"
                    f'<td style="padding:6px 12px;color:#8a9bb0;">{index}</td>'
                    f'<td style="padding:6px 12px;color:#f2f5f8;">{item_name}'
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

    note_html = "".join(
        f'<div style="color:#ffb84d;font-size:13px;margin-top:10px;">{html_lib.escape(note)}</div>'
        for note in notes or []
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
        + note_html
        + '<div style="color:#5d6d7e;font-size:12px;margin-top:16px;">'
        "价格单位：点券（VP）· 个人自用，仅供参考</div></div>"
    )


async def _notify_expired(settings: dict[str, Any], names: list[str]) -> None:
    text = (
        f"以下账号的登录凭证已失效：{'、'.join(names)}。\n"
        "今日推送已暂停。请打开「无畏契约商店推送」托盘菜单 → 打开设置面板，重新扫码登录。"
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

    account_list = accounts.all()
    if not account_list:
        raise LoginRequired("尚未绑定账号，请先在设置面板扫码登录")

    entries: list[tuple[str, str, list[ShopSection]]] = []
    expired: list[str] = []
    failed: list[str] = []
    for account in account_list:
        name = account.get("name") or "账号"
        try:
            response = await cn_client.fetch_response(account.get("credentials") or {})
        except cn_client.AuthExpiredError as exc:
            accounts.mark_expired(account.get("id", ""), True)
            expired.append(name)
            logger.warning("账号「%s」登录凭证已失效：%s", name, exc)
            continue
        except Exception as exc:
            failed.append(f"{name}（{exc}）")
            logger.warning("账号「%s」获取商店失败：%s", name, exc)
            continue

        accounts.mark_expired(account.get("id", ""), False)
        sections = cn_client.parse_sections(response, settings["sections"])
        if not sections:
            failed.append(f"{name}（今天没有获取到商店商品）")
            continue
        meta = accounts.mask_user_id(str(account.get("user_id") or ""))
        entries.append((name, meta, sections))

    if not entries:
        error_text = "；".join(
            [f"{name}：登录凭证已失效" for name in expired]
            + failed
        ) or "没有获取到任何商店商品"
        config.update_state(
            auth_expired=bool(expired) and len(expired) == len(account_list),
            last_push_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            last_push_ok=False,
            last_error=f"所有账号均推送失败：{error_text}",
        )
        if expired:
            await _notify_expired(settings, expired)
        if len(expired) == len(account_list):
            raise LoginRequired("所有账号的登录凭证均已失效，请重新扫码登录")
        raise RuntimeError(f"获取商店失败：{error_text}")

    notes: list[str] = []
    if expired:
        notes.append(f"注意：以下账号的登录凭证已失效，本次未推送：{'、'.join(expired)}。请重新扫码登录。")
    if failed:
        notes.append(f"注意：以下账号本次获取失败：{'、'.join(failed)}。")

    image_path = await render.render_accounts(entries)
    summary = build_summary(entries, now, notes)
    if len(entries) == 1:
        subject = f"无畏契约 · {now.strftime('%m-%d')} 商店 · {entries[0][0]}"
    else:
        subject = f"无畏契约 · {now.strftime('%m-%d')} 商店（{len(entries)} 个账号）"
    message = OutgoingMessage(
        subject=subject,
        text=summary,
        html=build_html(entries, has_image=True, notes=notes),
        image=image_path,
    )

    logger.info(
        "开始推送（触发方式：%s，渠道：%s，账号数：%s）",
        trigger,
        settings["channel"],
        len(entries),
    )
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
