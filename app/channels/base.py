"""推送渠道公共定义。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ChannelError(RuntimeError):
    """渠道发送失败。

    code 对邮件是 "email"，对企业微信是接口 errcode（便于识别 60020）。
    """

    def __init__(self, code: int | str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class OutgoingMessage:
    subject: str
    text: str
    html: str
    image: Path | None = None
