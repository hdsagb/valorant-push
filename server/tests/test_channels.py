"""渠道层测试：纯本地逻辑（消息构造/配置判断），不联网。"""
from __future__ import annotations

from conftest import SERVER_DIR

from push_server.channels import ChannelError, OutgoingMessage
from push_server.channels import email as email_channel
from push_server.channels import wecom as wecom_channel


def _email_settings() -> dict:
    return {"email": {"smtp_host": "smtp.qq.com", "smtp_port": 465, "security": "ssl",
                      "username": "sender@qq.com", "password": "k",
                      "from_name": "推送", "to": "a@x.com, b@x.com; c@x.com"}}


def test_email_recipients_parsing():
    assert email_channel._recipients(_email_settings()["email"]) == [
        "a@x.com", "b@x.com", "c@x.com"
    ]


def test_email_build_message_headers():
    msg = OutgoingMessage(subject="今日商店", text="正文", html="<p>正文</p>")
    message = email_channel.build_message(_email_settings()["email"], msg)
    assert message["Subject"] == "今日商店"
    assert "sender@qq.com" in message["From"]
    assert "a@x.com" in message["To"]
    assert message.is_multipart()


def test_email_is_configured():
    assert email_channel.is_configured(_email_settings()) is True
    settings = _email_settings()
    settings["email"]["smtp_host"] = ""
    assert email_channel.is_configured(settings) is False


def test_wecom_is_configured_and_mask_ip():
    settings = {"wecom": {"corpid": "c", "agentid": "1", "secret": "s"}}
    assert wecom_channel.is_configured(settings) is True
    assert wecom_channel.mask_ip("1.2.3.4") == "1.2.***.***"


def test_channel_error_code():
    exc = ChannelError(60020, "可信 IP")
    assert exc.code == 60020


def test_channels_do_not_reference_old_security_module():
    # 适配点必须改干净：渠道层只允许出现 crypto
    for rel in ("channels/email.py", "channels/wecom.py"):
        text = (SERVER_DIR / "push_server" / rel).read_text(encoding="utf-8")
        assert "protect" not in text, rel
        assert "crypto" in text, rel
