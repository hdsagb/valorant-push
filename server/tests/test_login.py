"""push_server.login 会话模块测试：fake app 不触网，方法立即返回避免残留任务。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from push_server import accounts, config, login

NEW_CRED = {"userId": "JA-12345678", "tid": "tid-9", "openid": "o-1"}
REBIND_CRED = {"userId": "JA-87654321", "tid": "new-tid", "wt": "w"}


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeApp:
    """替换 push_server.login._new_app 的假实现（三态：timeout/success/error）。"""

    DEFAULT_LOGIN_U1_URL = "http://connect.qq.com"

    def __init__(self, tmp: Path, method: str, wait: str, final: dict[str, Any]):
        self.method = method
        self.wait = wait
        self.final = final
        self.png = tmp / f"{method}-qr.png"
        self.png.write_bytes(b"fake-png")
        self.session = FakeSession()

    async def generate_qq_qr_code(self) -> dict[str, Any] | None:
        if self.method != "qq":
            return None
        return {
            "qr_path": str(self.png),
            "session": self.session,
            "ptqrtoken": "tok",
            "login_sig": "sig",
            "login_url": "http://x/login",
        }

    async def generate_wechat_qr_code(self) -> dict[str, Any] | None:
        if self.method != "wechat":
            return None
        return {"qr_path": str(self.png), "uuid": "uuid-1"}

    async def wait_for_qq_login_result(self, **kwargs: Any):
        if self.wait == "timeout":
            return None
        if self.wait == "error":
            raise RuntimeError("boom")
        return {"login_url": "http://redirect"}

    async def wait_for_wechat_login_result(self, uuid: str, timeout: int | None = None):
        if self.wait == "timeout":
            return None
        if self.wait == "error":
            raise RuntimeError("boom")
        return self.final

    async def get_final_cookies_by_qq(self, login_data: dict[str, Any]):
        if self.wait == "success":
            return self.final
        return None


def _fake(monkeypatch: pytest.MonkeyPatch, tmp: Path, method: str, wait: str,
          final: dict[str, Any]) -> FakeApp:
    fake = FakeApp(tmp, method, wait, final)
    monkeypatch.setattr(login, "_new_app", lambda: fake)
    return fake


async def _drain(session: login.LoginSession | None) -> None:
    if session and session.task and not session.task.done():
        try:
            await session.task
        except asyncio.CancelledError:
            pass


def _run(main, *args: Any) -> Any:
    """独立事件循环跑场景；退出前清空会话，避免 loop.close 残留任务。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(main(*args))
    finally:
        login.reset()
        loop.close()
        asyncio.set_event_loop(None)


def test_qq_success_new_account(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    fake = _fake(monkeypatch, data_dir, "qq", "success", NEW_CRED)

    async def main():
        session = await login.start("qq")
        assert session.state == "waiting"
        assert session.message == "请使用手机 QQ 扫描二维码"
        assert login.current() is session
        snap = session.snapshot()
        assert snap["method"] == "qq" and snap["has_qr"] is True
        assert snap["account"] is None
        await _drain(session)
        assert session.state == "success"
        assert session.account["name"] == ""
        assert session.account["updated"] is False
        assert session.account["id"]

    _run(main)
    assert fake.session.closed is True
    assert accounts.count() == 1
    stored = accounts.all()[0]
    assert stored["login_type"] == "qq"
    assert stored["user_id"] == "JA-12345678"
    assert stored["auth_expired"] is False
    assert config.load_settings()["login_method"] == "qq"


def test_wechat_success_rebind_updates_existing(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    existing = accounts.add({"userId": "JA-87654321", "tid": "old-tid"}, "qq", name="梅庄")
    accounts.mark_expired(existing["id"])
    _fake(monkeypatch, data_dir, "wechat", "success", REBIND_CRED)

    async def main():
        session = await login.start("wechat")
        assert session.state == "waiting"
        assert session.message == "请使用微信扫描二维码"
        await _drain(session)
        assert session.state == "success"
        assert session.message == "已更新「梅庄」的登录凭证"
        assert session.account == {"id": existing["id"], "name": "梅庄", "updated": True}

    _run(main)
    stored = accounts.all()[0]
    assert stored["login_type"] == "wechat"
    assert stored["credentials"]["tid"] == "new-tid"
    assert stored["auth_expired"] is False
    assert accounts.count() == 1


def test_scan_same_user_twice_no_duplicate(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    _fake(monkeypatch, data_dir, "qq", "success", NEW_CRED)

    async def main():
        first = await login.start("qq")
        await _drain(first)
        _fake(monkeypatch, data_dir, "qq", "success", {**NEW_CRED, "tid": "tid-10"})
        second = await login.start("qq")
        await _drain(second)
        assert second.account["updated"] is True

    _run(main)
    assert accounts.count() == 1
    assert accounts.all()[0]["credentials"]["tid"] == "tid-10"


def test_account_limit_fails_session(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    for i in range(accounts.MAX_ACCOUNTS):
        accounts.add({"userId": f"JA-{i:08d}", "tid": f"t{i}"}, "qq", name=f"占位{i}")
    _fake(monkeypatch, data_dir, "qq", "success", NEW_CRED)

    async def main():
        session = await login.start("qq")
        await _drain(session)
        assert session.state == "failed"
        assert "最多支持" in session.message
        assert session.account is None

    _run(main)
    assert accounts.count() == accounts.MAX_ACCOUNTS


def test_qq_timeout_and_error(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    _fake(monkeypatch, data_dir, "qq", "timeout", {})

    async def main():
        session = await login.start("qq")
        await _drain(session)
        assert session.state == "timeout"
        assert "二维码已过期" in session.message

    _run(main)

    _fake(monkeypatch, data_dir, "qq", "error", {})

    async def main2():
        session = await login.start("qq")
        await _drain(session)
        assert session.state == "failed"
        assert "QQ 登录失败" in session.message

    _run(main2)
    assert accounts.count() == 0


def test_reuse_then_switch_method(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    qq_fake = _fake(monkeypatch, data_dir, "qq", "timeout", {})
    wechat_fake = FakeApp(data_dir, "wechat", "timeout", {})

    async def main():
        first = await login.start("qq")
        again = await login.start("qq")
        assert again is first
        monkeypatch.setattr(login, "_new_app", lambda: wechat_fake)
        second = await login.start("wechat")
        assert second is not first
        assert login.current() is second
        assert second.method == "wechat"
        with pytest.raises(asyncio.CancelledError):
            assert first.task is not None
            await first.task
        await _drain(second)

    _run(main)


def test_bad_final_payload_fails(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    _fake(monkeypatch, data_dir, "qq", "success", {"userId": "JA-12345678"})  # 缺 tid

    async def main():
        session = await login.start("qq")
        await _drain(session)
        assert session.state == "failed"
        assert "未获取到账号凭证" in session.message

    _run(main)
    assert accounts.count() == 0
