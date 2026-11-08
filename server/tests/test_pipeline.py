"""推送流水线测试：外部依赖（接口/渲染/渠道）全部打桩，不联网。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import push_server.accounts as accounts
import push_server.cn_client as cn_client
import push_server.config as config
import push_server.pipeline as pipeline
import push_server.render as render
from push_server.models import ShopSection


class FakeSend:
    """记录调用参数的假渠道发送函数。"""

    def __init__(self):
        self.sent = []

    async def __call__(self, settings, msg):
        self.sent.append(msg)


async def _fake_fetch_ok(credentials, timeout=None):
    return {"data": [{"key": "dailystore", "title": "每日商店", "end_ts": 0,
                      "list": [{"goods_name": "暴徒", "rmb_price": "890",
                                "goods_pic": "", "bg_image": "", "quality": "罕见",
                                "goods_id": "g", "like_num": ""}]}]}


async def _fake_fetch_expired(credentials, timeout=None):
    raise cn_client.AuthExpiredError("登录凭证已失效")


def _fake_render(out_dir: Path):
    async def _render(entries):
        path = out_dir / "shop_fake.jpg"
        path.write_bytes(b"\xff\xd8fakejpg")
        return path
    return _render


def _seed(data_dir) -> None:
    """准备账号 + 已配置的邮件渠道（默认 settings 渠道均未配置，无法投递）。"""
    config.save_settings({"channel": "email",
                          "email": {"smtp_host": "smtp.qq.com", "username": "a@qq.com",
                                    "password": "k", "to": "b@qq.com"}})
    accounts.add({"userId": "u1", "tid": "t1"}, "qq", name="大号")


def test_build_summary_lists_names_and_notes():
    section = ShopSection(key="dailystore", title="每日商店", items=[])
    text = pipeline.build_summary([("大号", "***1234", [section])], notes=["注意"])
    assert "大号" in text
    assert "注意" in text


def test_run_push_without_accounts_raises_login_required(data_dir):
    try:
        asyncio.run(pipeline.run_push())
        raise AssertionError("无账号应抛 LoginRequired")
    except pipeline.LoginRequired as exc:
        assert "尚未绑定账号" in str(exc)


def test_run_push_success(data_dir, monkeypatch, tmp_path: Path):
    _seed(data_dir)
    sent = FakeSend()
    monkeypatch.setattr(cn_client, "fetch_response", _fake_fetch_ok)
    monkeypatch.setattr(render, "render_accounts", _fake_render(tmp_path))
    monkeypatch.setattr(pipeline.email_channel, "send", sent)

    result = asyncio.run(pipeline.run_push(trigger="manual"))

    assert result.ok is True
    assert result.channel == "email"
    assert len(sent.sent) == 1
    assert sent.sent[0].image == result.image
    assert config.load_state()["last_push_ok"] is True


def test_run_push_all_expired_notifies(data_dir, monkeypatch, tmp_path: Path):
    _seed(data_dir)
    accounts.mark_expired(accounts.all()[0]["id"], True)
    sent = FakeSend()
    monkeypatch.setattr(cn_client, "fetch_response", _fake_fetch_expired)
    monkeypatch.setattr(pipeline.email_channel, "send", sent)

    try:
        asyncio.run(pipeline.run_push())
        raise AssertionError("全部失效应抛 LoginRequired")
    except pipeline.LoginRequired:
        pass

    assert len(sent.sent) == 1
    assert "凭证已失效" in sent.sent[0].text
    assert "管理界面" in sent.sent[0].text  # 服务器版文案适配点
    assert accounts.all()[0]["auth_expired"] is True
