"""邮件推送（SMTP）。

没有公网服务器时，邮箱是最省事的推送方式：填好账号和授权码就能用。
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Any

from .base import ChannelError, OutgoingMessage

logger = logging.getLogger(__name__)


def is_configured(settings: dict[str, Any]) -> bool:
    email = settings["email"]
    return bool(email["smtp_host"] and email["username"] and email["password"])


def _recipients(email: dict[str, Any]) -> list[str]:
    raw = email.get("to") or email.get("username") or ""
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def build_message(email: dict[str, Any], msg: OutgoingMessage) -> MIMEMultipart:
    sender = email["username"]
    root = MIMEMultipart("related")
    root["Subject"] = Header(msg.subject, "utf-8")
    root["From"] = formataddr((str(Header(email["from_name"], "utf-8")), sender))
    root["To"] = ", ".join(_recipients(email))
    root["Date"] = formatdate(localtime=True)

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(msg.text, "plain", "utf-8"))
    alternative.attach(MIMEText(msg.html, "html", "utf-8"))
    root.attach(alternative)

    if msg.image and Path(msg.image).exists():
        image = MIMEImage(Path(msg.image).read_bytes(), "jpeg")
        image.add_header("Content-ID", "<shopimage>")
        image.add_header(
            "Content-Disposition", "inline", filename=Path(msg.image).name
        )
        root.attach(image)
    return root


def _send_sync(settings: dict[str, Any], msg: OutgoingMessage) -> None:
    email = settings["email"]
    host = email["smtp_host"]
    port = int(email["smtp_port"])
    security = email["security"]
    recipients = _recipients(email)
    if not recipients:
        raise ChannelError("email", "收件人邮箱为空")

    context = ssl.create_default_context()
    payload = build_message(email, msg).as_string()
    try:
        if security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=30)
        with client:
            client.ehlo()
            if security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if email["username"]:
                client.login(email["username"], email["password"])
            client.sendmail(email["username"], recipients, payload)
    except smtplib.SMTPAuthenticationError as exc:
        raise ChannelError(
            "email",
            "邮箱登录失败。多数邮箱（QQ/163 等）需要填写「授权码」而不是登录密码，"
            f"请到邮箱设置中开启 SMTP 服务并生成授权码。（{exc.smtp_code}）",
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise ChannelError("email", f"邮件发送失败：{exc}") from exc


async def send(settings: dict[str, Any], msg: OutgoingMessage) -> None:
    logger.info("通过邮件发送：%s -> %s", settings["email"]["username"], _recipients(settings["email"]))
    await asyncio.to_thread(_send_sync, settings, msg)
    logger.info("邮件已发送")


async def send_test(settings: dict[str, Any]) -> None:
    await send(
        settings,
        OutgoingMessage(
            subject="无畏契约商店推送 · 邮件测试",
            text="这是一条测试邮件。如果你看到了它，说明邮件渠道配置正确。",
            html="<p>这是一条测试邮件。如果你看到了它，说明<b>邮件渠道</b>配置正确。</p>",
        ),
    )
