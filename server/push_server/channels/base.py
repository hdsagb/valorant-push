# ============================================================
# [拷贝来源] app/channels/base.py @ 个人版 commit 9e3f621（2026-11-08，v1.0.1）
# [冻结拷贝] 服务器版独立演进，禁止直接修改本文件逻辑；如需同步个人版改动，
#            重新整文件拷贝并更新来源 commit。适配点仅限本头部列出的条目。
# [适配点]   无（除来源头外逐行一致）
# ============================================================
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
