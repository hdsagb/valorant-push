"""notify 模块测试：内容、去重、回退、不冒泡。"""
import asyncio

import pytest

import push_server.config as config
import push_server.notify as notify
from push_server.channels import OutgoingMessage


@pytest.fixture(autouse=True)
def _reset():
    notify.reset_state()
    yield
    notify.reset_state()


def _settings(data_dir, channel="email"):
    """写真实 settings.json（channel_configured 需要完整 email/wecom 子结构）。"""
    config.save_settings({"channel": channel,
                          "email": {"smtp_host": "smtp.qq.com", "username": "a@qq.com",
                                    "password": "k", "to": "b@qq.com"}})
    return config.load_settings()


async def _capture(monkeypatch, calls):
    async def fake_send(settings, message):
        calls.append(message)
    # 两个渠道都替换，便于测回退
    monkeypatch.setattr(notify.email_channel, "send", fake_send)
    monkeypatch.setattr(notify.wecom_channel, "send", fake_send)


def test_notify_dedup_per_day(data_dir, monkeypatch):
    calls = []
    asyncio.run(_capture(monkeypatch, calls))
    settings = _settings(data_dir)
    asyncio.run(notify.notify_push_failure(settings, "boom", 3))
    asyncio.run(notify.notify_push_failure(settings, "boom2", 3))
    assert len(calls) == 1


def test_notify_message_content(data_dir, monkeypatch):
    calls = []
    asyncio.run(_capture(monkeypatch, calls))
    settings = _settings(data_dir)
    asyncio.run(notify.notify_push_failure(settings, "x" * 500, 3))
    msg = calls[0]
    assert isinstance(msg, OutgoingMessage)
    assert msg.image is None
    assert msg.subject == notify.SUBJECT_FAILURE
    assert "已重试 3 次" in msg.text
    assert len(msg.text) < 600  # error 截断生效


def test_notify_cross_channel_fallback(data_dir, monkeypatch):
    calls = []
    asyncio.run(_capture(monkeypatch, calls))
    # 两个渠道都配置，另一渠道才具备回退条件
    config.save_settings({"channel": "email",
                          "email": {"smtp_host": "smtp.qq.com", "username": "a@qq.com",
                                    "password": "k", "to": "b@qq.com"},
                          "wecom": {"corpid": "c", "agentid": "1", "secret": "s"}})
    settings = config.load_settings()

    async def fail(settings, message):
        raise RuntimeError("down")
    monkeypatch.setattr(notify.email_channel, "send", fail)
    asyncio.run(notify.notify_push_failure(settings, "boom", 3))
    assert len(calls) == 1  # 主渠道失败 → 另一渠道送达


def test_notify_failure_does_not_raise(data_dir, monkeypatch):
    asyncio.run(_capture(monkeypatch, []))
    settings = _settings(data_dir)

    async def fail(settings, message):
        raise RuntimeError("down")
    monkeypatch.setattr(notify.email_channel, "send", fail)
    monkeypatch.setattr(notify.wecom_channel, "send", fail)
    # 两渠道都挂：不抛异常（去重标记仍写入）
    asyncio.run(notify.notify_push_failure(settings, "boom", 3))
