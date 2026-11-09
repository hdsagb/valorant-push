"""web.py 测试：路由剥离 / 页面结构 / auth_guard / 登录设置 / 健康检查。

路由剥离类用例直连 routes（无中间件，Task 3）；
鉴权与页面类用例走 build_app（Task 4 起）。
运行：python -m pytest server/tests/test_web.py -v
"""
from __future__ import annotations

import ast
import re
import sys
import time
from pathlib import Path

import pytest

from push_server import __version__, auth, web

PASSWORD = "口令-12345678"
RW = {"X-Requested-With": "ValorantPushPersonal"}


@pytest.fixture(autouse=True)
def _reset_auth_state(data_dir):
    """依赖 data_dir：即使用例未显式请求，也强制隔离真实数据目录。"""
    auth.reset_state()
    yield
    auth.reset_state()


def _setup_password() -> None:
    code = auth.ensure_setup_code()
    auth.set_password(PASSWORD, code)


def _login(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.post("/api/login", json={"password": PASSWORD}, headers=RW)
    assert resp.status == 200 and resp.data.get("ok")
    return client


def _csrf_of(client) -> str:
    resp = client.get("/")
    assert resp.status == 200
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
    assert match, "主页必须注入会话级 csrf"
    return match.group(1)


# ============================================================
# Task 3：路由剥离（删掉的不能再出现）
# ============================================================

def test_export_route_removed(web_routes_client):
    resp = web_routes_client.post(
        "/api/credentials/export", json={"password": "whatever123"}
    )
    assert resp.status == 404


def test_login_routes_removed(web_routes_client):
    assert web_routes_client.post("/api/login/start", json={"method": "qq"}).status == 404
    assert web_routes_client.get("/api/login/status").status == 404
    assert web_routes_client.get("/api/qr").status == 404


def test_autostart_routes_removed(web_routes_client):
    assert web_routes_client.get("/api/autostart").status == 404
    assert web_routes_client.post("/api/autostart", json={"enabled": True}).status == 404


def test_open_data_dir_removed(web_routes_client):
    assert web_routes_client.post("/api/open-data-dir", json={}).status == 404


# ============================================================
# Task 3：页面结构（登录后可见，保留项在、删除项不在）
# ============================================================

def test_index_keeps_core_sections(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    resp = client.get("/")
    assert resp.status == 200
    for anchor in ("channelTabs", "accList", "pushBtn", "testBtn", "publicIp", "csrf-token"):
        assert anchor in resp.text


def test_index_drops_removed_sections(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    resp = client.get("/")
    assert resp.status == 200
    for absent in (
        "loginBtn",
        "qrImg",
        "exportBtn",
        "exportModal",
        "autoStart",
        "openDirBtn",
        "/api/login/status",
    ):
        assert absent not in resp.text


def test_index_renders_session_csrf(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    resp = client.get("/")
    assert resp.status == 200
    assert re.search(r'<meta name="csrf-token" content="[^"]+"', resp.text)


# ---------- 来源注记与 build_app ----------

def test_module_has_source_note():
    source = Path(web.__file__).read_text(encoding="utf-8")
    assert "app/web.py" in source.split('"""')[0]


def test_build_app_exists():
    source = Path(web.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "build_app" in names
    assert "start_server" not in names


def test_import_is_clean():
    """import web 不应触发个人版登录/autostart 相关模块加载。"""
    assert "push_server.login" not in sys.modules
    assert "push_server.autostart" not in sys.modules


# ============================================================
# Task 4：auth_guard 中间件 / 登录与设置 / 健康检查
# ============================================================

# ---------- 免鉴权路径 ----------

def test_healthz_no_auth(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.get("/healthz")
    assert resp.status == 200
    assert resp.data["ok"] is True
    assert resp.data["version"] == __version__
    assert resp.data["scheduler"] == "stopped"


def test_healthz_reports_running(web_client_factory):
    class _FakeTask:
        def __init__(self, done: bool):
            self._done = done

        def done(self) -> bool:
            return self._done

    client = web_client_factory(web.build_app())
    web.set_scheduler_task(_FakeTask(False))
    try:
        assert client.get("/healthz").data["scheduler"] == "running"
    finally:
        web.set_scheduler_task(None)
    assert client.get("/healthz").data["scheduler"] == "stopped"


def test_index_redirects_when_uninitialized(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/setup"


def test_index_redirects_to_login_when_initialized(web_client_factory):
    _setup_password()
    client = web_client_factory(web.build_app())
    resp = client.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login"


def test_api_requires_session(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.get("/api/status", allow_redirects=False)
    assert resp.status == 401
    assert resp.data == {"ok": False, "code": "unauthorized"}


# ---------- 设置页 / 登录页 ----------

def test_setup_and_login_pages_render(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.get("/setup")
    assert resp.status == 200 and "设置码" in resp.text
    resp = client.get("/login")
    # 未初始化时 /login 引导回 /setup
    assert resp.status == 200
    assert "访问口令" in resp.text or "设置码" in resp.text


def test_setup_redirects_to_login_when_initialized(web_client_factory):
    _setup_password()
    client = web_client_factory(web.build_app())
    resp = client.get("/setup", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/login"


def test_api_setup_requires_requested_with(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.post("/api/setup", json={"setup_code": "x", "password": PASSWORD})
    assert resp.status == 403


def test_api_setup_wrong_code_rejected(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.post(
        "/api/setup", json={"setup_code": "00000000", "password": PASSWORD}, headers=RW
    )
    assert resp.status == 400


def test_api_setup_success_creates_session(web_client_factory):
    code = auth.ensure_setup_code()
    client = web_client_factory(web.build_app())
    resp = client.post(
        "/api/setup", json={"setup_code": code, "password": PASSWORD}, headers=RW
    )
    assert resp.status == 200 and resp.data["ok"]
    resp = client.get("/")
    assert resp.status == 200
    assert '<meta name="csrf-token"' in resp.text


# ---------- 登录 ----------

def test_api_login_success_and_failure(web_client_factory):
    _setup_password()
    client = web_client_factory(web.build_app())
    resp = client.post("/api/login", json={"password": "错误口令-000"}, headers=RW)
    assert resp.status == 401
    resp = client.post("/api/login", json={"password": PASSWORD}, headers=RW)
    assert resp.status == 200 and resp.data["ok"]
    assert client.get("/").status == 200


def test_login_rate_limit_lockout_and_recovery(web_client_factory, monkeypatch):
    monkeypatch.setattr(auth, "FAIL_DELAY", 0)
    _setup_password()
    client = web_client_factory(web.build_app())
    statuses = []
    for _ in range(auth.LOCK_THRESHOLD):
        resp = client.post("/api/login", json={"password": "wrong-pass-1"}, headers=RW)
        statuses.append(resp.status)
    assert 401 in statuses and 429 in statuses  # 前几次 401，触发锁定后 429
    resp = client.post("/api/login", json={"password": "wrong-pass-1"}, headers=RW)
    assert resp.status == 429
    assert "分钟" in resp.data["message"]
    # 正确口令仍可登录（成功登录即清零）
    resp = client.post("/api/login", json={"password": PASSWORD}, headers=RW)
    assert resp.status == 200
    resp = client.post("/api/login", json={"password": "wrong-pass-1"}, headers=RW)
    assert resp.status == 401  # 计数已重置，重新累计


# ---------- CSRF 会话级 ----------

def test_write_api_requires_session_csrf(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    resp = client.post("/api/logout", json={})
    assert resp.status == 403  # 缺 CSRF / X-Requested-With
    token = _csrf_of(client)
    resp = client.post("/api/logout", json={}, headers={**RW, "X-CSRF-Token": token})
    assert resp.status == 200


def test_csrf_is_session_scoped(web_client_factory):
    _setup_password()
    a = _login(web_client_factory)
    b = _login(web_client_factory)
    token_a, token_b = _csrf_of(a), _csrf_of(b)
    assert token_a != token_b
    resp = b.post("/api/logout", json={}, headers={**RW, "X-CSRF-Token": token_a})
    assert resp.status == 403  # A 的令牌不能用于 B 的会话


def test_logout_drops_session(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    token = _csrf_of(client)
    resp = client.post("/api/logout", json={}, headers={**RW, "X-CSRF-Token": token})
    assert resp.status == 200
    resp = client.get("/api/status", allow_redirects=False)
    assert resp.status == 401


# ---------- 同源校验 ----------

def test_cross_site_origin_blocked(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.get("/api/status", headers={"Origin": "https://evil.example"})
    assert resp.status == 403


def test_cross_site_fetch_site_blocked(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status == 403


def test_same_origin_allowed(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    token = _csrf_of(client)
    resp = client.post(
        "/api/logout",
        json={},
        headers={**RW, "X-CSRF-Token": token, "Origin": "http://127.0.0.1:54321"},
    )
    assert resp.status == 200  # Host 同主机名的 Origin 视为同源


# ---------- Cookie 属性 ----------

def _cookie_header(resp) -> str:
    return resp.headers.get("Set-Cookie", "")


def test_session_cookie_attributes_plain_http(web_client_factory):
    _setup_password()
    client = web_client_factory(web.build_app())
    resp = client.post("/api/login", json={"password": PASSWORD}, headers=RW)
    cookie = _cookie_header(resp).lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert "max-age=604800" in cookie
    assert "secure" not in cookie  # 明文 HTTP 不加 Secure


def test_session_cookie_secure_on_forwarded_https(web_client_factory):
    _setup_password()
    client = web_client_factory(web.build_app())
    resp = client.post(
        "/api/login",
        json={"password": PASSWORD},
        headers={**RW, "X-Forwarded-Proto": "https"},
    )
    assert "secure" in _cookie_header(resp).lower()


def test_cookie_secure_env_override(web_client_factory, monkeypatch):
    _setup_password()
    client = web_client_factory(web.build_app())
    monkeypatch.setenv("VAL_SERVER_COOKIE_SECURE", "0")
    resp = client.post(
        "/api/login",
        json={"password": PASSWORD},
        headers={**RW, "X-Forwarded-Proto": "https"},
    )
    assert "secure" not in _cookie_header(resp).lower()
    monkeypatch.setenv("VAL_SERVER_COOKIE_SECURE", "1")
    resp = client.post("/api/login", json={"password": PASSWORD}, headers=RW)
    assert "secure" in _cookie_header(resp).lower()


# ---------- 会话生命周期 ----------

def test_session_expires_after_ttl(web_client_factory, monkeypatch):
    _setup_password()
    client = _login(web_client_factory)
    future = time.time() + auth.SESSION_TTL + 10
    monkeypatch.setattr(auth.time, "time", lambda: future)
    resp = client.get("/api/status", allow_redirects=False)
    assert resp.status == 401


def test_session_sliding_reissue(web_client_factory, monkeypatch):
    _setup_password()
    client = _login(web_client_factory)
    future = time.time() + auth.SESSION_REISSUE + 10
    monkeypatch.setattr(auth.time, "time", lambda: future)
    resp = client.get("/", allow_redirects=False)
    assert resp.status == 200
    cookie = _cookie_header(resp).lower()
    assert "max-age=604800" in cookie  # 重签 cookie，浏览器续期


# ============================================================
# Task 5：凭证导入卡片 + /api/credentials/import
# ============================================================

import base64  # noqa: E402

from push_server import accounts, paths  # noqa: E402
from test_credpack import (  # noqa: E402
    PASSWORD as PACK_PASSWORD,
    make_pack,
    sample_payload,
)


def test_import_credentials_success(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    token = _csrf_of(client)
    content_b64 = base64.b64encode(make_pack(sample_payload(), PACK_PASSWORD)).decode()
    resp = client.post(
        "/api/credentials/import",
        json={"content_b64": content_b64, "password": PACK_PASSWORD},
        headers={**RW, "X-CSRF-Token": token},
    )
    assert resp.status == 200
    assert resp.data["added"] == 1 and resp.data["updated"] == 0
    # R6 句式 + 删除提醒必须出现在返回消息里
    assert "请在个人版停止" in resp.data["message"]
    assert "删除" in resp.data["message"]
    assert accounts.count() == 1
    disk = paths.ACCOUNTS_FILE.read_text(encoding="utf-8")
    assert "enc:v1:" in disk and "t-0001" not in disk  # 凭证加密落盘、明文 tid 不出现


def test_import_credentials_wrong_password(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    token = _csrf_of(client)
    content_b64 = base64.b64encode(make_pack(sample_payload(), PACK_PASSWORD)).decode()
    resp = client.post(
        "/api/credentials/import",
        json={"content_b64": content_b64, "password": "错误的口令-999999"},
        headers={**RW, "X-CSRF-Token": token},
    )
    assert resp.status == 400
    assert "口令错误或凭证包已损坏" in resp.data["message"]


def test_import_credentials_oversized_rejected(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    token = _csrf_of(client)
    resp = client.post(
        "/api/credentials/import",
        json={
            "content_b64": base64.b64encode(b"x" * (2 * 1024 * 1024)).decode(),
            "password": PACK_PASSWORD,
        },
        headers={**RW, "X-CSRF-Token": token},
    )
    assert resp.status == 400


def test_import_requires_csrf(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    content_b64 = base64.b64encode(make_pack(sample_payload(), PACK_PASSWORD)).decode()
    resp = client.post(
        "/api/credentials/import",
        json={"content_b64": content_b64, "password": PACK_PASSWORD},
        headers=RW,  # 无 X-CSRF-Token
    )
    assert resp.status == 403


def test_api_status_smoke_after_login(web_client_factory):
    """集成冒烟：登录后（含导入账号后）/api/status 必须正常返回。

    （M3 手工验收曾发现三处拷贝改造遗漏：config.wecom_configured 引用、
    state 缺 last_channel/last_summary 兜底、summary 字段 masked_id
    与个人版 user_id_masked 不一致——单测只覆盖空账号态，漏掉了登录态集成路径。）
    """
    _setup_password()
    client = _login(web_client_factory)
    token = _csrf_of(client)
    content_b64 = base64.b64encode(make_pack(sample_payload(), PACK_PASSWORD)).decode()
    resp = client.post(
        "/api/credentials/import",
        json={"content_b64": content_b64, "password": PACK_PASSWORD},
        headers={**RW, "X-CSRF-Token": token},
    )
    assert resp.status == 200
    resp = client.get("/api/status")
    assert resp.status == 200
    assert resp.data["account_count"] == 1
    assert resp.data["user_id_masked"] == "***0001"  # 服务器版掩码字段（不落明文 user_id）


# ============================================================
# M4 部署回归：/api/test-channel 邮件分支曾 500
# server config 的 SMTP_GUESS 值为 dict，web.py 沿用个人版 tuple 下标取法 → KeyError: 0
# ============================================================

def test_test_channel_email_with_saved_host_not_500(web_client_factory, monkeypatch):
    from push_server import config
    from push_server.channels import ChannelError
    from push_server.channels import email as email_channel

    _setup_password()
    config.save_settings(
        {
            "channel": "email",
            "email": {
                "username": "me@qq.com",
                "password": "authcode",
                "to": "me@qq.com",
                "smtp_host": "smtp.qq.com",
            },
        }
    )

    async def fake_send(settings):
        raise ChannelError("email", "stub")

    monkeypatch.setattr(email_channel, "send_test", fake_send)

    client = _login(web_client_factory)
    token = _csrf_of(client)
    resp = client.post(
        "/api/test-channel",
        json={"channel": "email", "draft": {}},
        headers={**RW, "X-CSRF-Token": token},
    )
    assert resp.status == 400  # 修复前：500（KeyError: 0）
    assert resp.data.get("ok") is False
