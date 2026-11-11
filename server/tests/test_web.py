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


def test_index_drops_desktop_sections(web_client_factory):
    """M2 裁剪的桌面专属项仍不在页面里（M6 只恢复了扫码卡片）。"""
    _setup_password()
    client = _login(web_client_factory)
    resp = client.get("/")
    assert resp.status == 200
    for absent in ("exportBtn", "exportModal", "autoStart", "openDirBtn"):
        assert absent not in resp.text


def test_index_has_scan_login_card(web_client_factory):
    """M6：扫码登录恢复为卡片 1，含 tab / 二维码区 / 状态机与轮询引用。"""
    _setup_password()
    client = _login(web_client_factory)
    resp = client.get("/")
    assert resp.status == 200
    for anchor in (
        "loginTabs",
        "loginBtn",
        "qrBox",
        "qrImg",
        "qrMsg",
        "startRescan",
        "/api/login/start",
        "/api/login/status",
    ):
        assert anchor in resp.text


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
    """import web 不应触发桌面专属模块（autostart）；M6 起 login 属于服务器版正常依赖。"""
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


# ============================================================
# M4.5 域名绑定：/api/setup domain 参数 / caddy-ask / domain-check / http→https 引导
# ============================================================

def test_api_setup_with_domain_binds(web_client_factory):
    code = auth.ensure_setup_code()
    client = web_client_factory(web.build_app())
    resp = client.post(
        "/api/setup",
        json={"setup_code": code, "password": PASSWORD, "domain": " https://Shop.Example.com/ "},
        headers=RW,
    )
    assert resp.status == 200 and resp.data["ok"]
    assert resp.data["bound_domain"] == "shop.example.com"  # 规范化后返回
    from push_server import config
    assert config.load_settings()["domain"] == "shop.example.com"  # 随设置落盘


def test_api_setup_invalid_domain_rejected_before_password(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.post(
        "/api/setup",
        json={"setup_code": "00000000", "password": PASSWORD, "domain": "192.168.1.1"},
        headers=RW,
    )
    assert resp.status == 400
    assert "域名" in resp.data["message"]
    assert not auth.is_initialized()  # 校验先行：口令尚未设置
    # 修正后可正常完成初始化
    code = auth.ensure_setup_code()
    resp = client.post(
        "/api/setup", json={"setup_code": code, "password": PASSWORD}, headers=RW
    )
    assert resp.status == 200


def test_caddy_ask_bound_200(web_routes_client):
    from push_server import config
    config.save_settings({"domain": "bound.example.com"})
    resp = web_routes_client.get("/api/caddy-ask?domain=bound.example.com")
    assert resp.status == 200


def test_caddy_ask_rejects_other_and_unbound(web_routes_client):
    from push_server import config
    # 未绑定任何域名：一律 403
    resp = web_routes_client.get("/api/caddy-ask?domain=anything.example.com")
    assert resp.status == 403
    # 已绑定：其它域名 / 空参数 / 非法值均 403
    config.save_settings({"domain": "bound.example.com"})
    assert web_routes_client.get("/api/caddy-ask?domain=other.example.com").status == 403
    assert web_routes_client.get("/api/caddy-ask").status == 403
    assert web_routes_client.get("/api/caddy-ask?domain=192.168.1.1").status == 403


def test_caddy_ask_no_auth_via_middleware(web_client_factory):
    """免鉴权回归：完整中间件下无任何会话/自定义头也可访问（Caddy 回源形态）。"""
    from push_server import config
    config.save_settings({"domain": "bound.example.com"})
    client = web_client_factory(web.build_app())
    assert client.get("/api/caddy-ask?domain=bound.example.com").status == 200
    assert client.get("/api/caddy-ask?domain=other.example.com").status == 403


def test_https_redirect_for_bound_domain(web_client_factory):
    from push_server import config
    config.save_settings({"domain": "bound.example.com"})
    client = web_client_factory(web.build_app())
    # Host == 绑定域名 → 302 到 https 同路径
    for path in ("/", "/login", "/setup"):
        resp = client.get(path, headers={"Host": "bound.example.com"}, allow_redirects=False)
        assert resp.status == 302, path
        assert resp.headers["Location"] == f"https://bound.example.com{path}", path


def test_https_redirect_not_triggered(web_client_factory):
    from push_server import config
    config.save_settings({"domain": "bound.example.com"})
    client = web_client_factory(web.build_app())
    # Host ≠ 绑定域名
    resp = client.get("/", headers={"Host": "other.example.com"}, allow_redirects=False)
    assert not resp.headers.get("Location", "").startswith("https://")
    # 反代形态：X-Forwarded-Proto 恒存在 → 永不重定向（无回环）
    resp = client.get(
        "/", headers={"Host": "bound.example.com", "X-Forwarded-Proto": "https"},
        allow_redirects=False,
    )
    assert not resp.headers.get("Location", "").startswith("https://")
    # API 路径不重定向（ask 回源 Host 不匹配天然不触发，此处验证 API 白名单路径）
    resp = client.get("/api/status", headers={"Host": "bound.example.com"}, allow_redirects=False)
    assert not resp.headers.get("Location", "").startswith("https://")


def test_https_redirect_not_triggered_when_unbound(web_client_factory):
    client = web_client_factory(web.build_app())
    resp = client.get("/", headers={"Host": "bound.example.com"}, allow_redirects=False)
    assert not resp.headers.get("Location", "").startswith("https://")


def test_domain_check_requires_session(web_client_factory):
    client = web_client_factory(web.build_app())
    assert client.get("/api/domain-check").status == 401


def test_domain_check_not_bound(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    resp = client.get("/api/domain-check")
    assert resp.status == 200
    assert resp.data["ok"] is False
    assert resp.data["state"] == "not_bound"


def test_domain_check_probes_and_classifies(web_client_factory, monkeypatch):
    from push_server import config
    _setup_password()
    config.save_settings({"domain": "bound.example.com"})
    client = _login(web_client_factory)

    cases = {
        "ok": {"state": "ok", "message": "HTTPS 访问正常"},
        "dns_error": {"state": "dns_error", "message": "DNS 解析失败"},
        "tls_error": {"state": "tls_error", "message": "证书未就绪"},
        "http_error": {"state": "http_error", "message": "反代返回 HTTP 502"},
        "timeout": {"state": "timeout", "message": "连接超时"},
    }
    for state, payload in cases.items():
        async def fake_probe(domain, _payload=payload):
            assert domain == "bound.example.com"
            return dict(_payload)

        monkeypatch.setattr(web, "_probe_domain", fake_probe)
        resp = client.get("/api/domain-check")
        assert resp.status == 200
        assert resp.data["domain"] == "bound.example.com"
        assert resp.data["state"] == state
        assert resp.data["ok"] is (state == "ok")
        assert resp.data["message"] == payload["message"]


# ============================================================
# M6 扫码登录：/api/login/start · /api/login/status · /api/qr
# （会话编排本身在 test_login.py，这里只测 Web 端点与鉴权）
# ============================================================

import asyncio  # noqa: E402

from push_server import login as login_module  # noqa: E402


class _FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeApp:
    """只实现 QQ 二维码生成 + 挂起的轮询：不触网，会话停在 waiting。"""

    DEFAULT_LOGIN_U1_URL = "https://graph.qq.com/oauth2.0/unittest"

    def __init__(self, png: Path):
        self._png = png
        self.session = _FakeSession()

    async def generate_qq_qr_code(self):
        return {
            "qr_path": str(self._png),
            "session": self.session,
            "ptqrtoken": "t",
            "login_sig": "s",
            "u1_url": "u",
            "login_url": "l",
            "pt_openlogin_data": "o",
            "aegis_uid": "a",
        }

    async def wait_for_qq_login_result(self, **kwargs):
        await asyncio.Event().wait()  # 挂起直到会话被取消

    async def generate_wechat_qr_code(self):
        return None


@pytest.fixture
def _qr_png(data_dir):
    paths.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    png = paths.TEMP_DIR / "qr-test.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    return png


def test_login_scan_flow_routes(web_routes_client, monkeypatch, _qr_png):
    monkeypatch.setattr(login_module, "_new_app", lambda: _FakeApp(_qr_png))
    try:
        resp = web_routes_client.get("/api/login/status")
        assert resp.status == 200 and resp.data["state"] == "idle"

        resp = web_routes_client.post("/api/login/start", json={"method": "qq"})
        assert resp.status == 200
        assert resp.data["state"] == "waiting"
        assert resp.data["has_qr"] is True
        assert "QQ" in resp.data["message"]

        resp = web_routes_client.get("/api/login/status")
        assert resp.data["state"] == "waiting" and resp.data["has_qr"] is True

        resp = web_routes_client.get("/api/qr")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("image/png")
        assert resp.headers["Cache-Control"] == "no-store"
    finally:
        login_module.reset()
    assert web_routes_client.get("/api/qr").status == 404  # reset 后无二维码


def test_login_scan_endpoints_require_session(web_client_factory):
    """扫码三端点不进 SAFE_API_PATHS，未登录一律 401。"""
    client = web_client_factory(web.build_app())
    assert client.get("/api/login/status", allow_redirects=False).status == 401
    assert client.get("/api/qr", allow_redirects=False).status == 401
    resp = client.post(
        "/api/login/start", json={"method": "qq"}, headers=RW, allow_redirects=False
    )
    assert resp.status == 401


def test_login_start_requires_csrf(web_client_factory):
    _setup_password()
    client = _login(web_client_factory)
    resp = client.post("/api/login/start", json={"method": "qq"}, headers=RW)
    assert resp.status == 403  # 写接口缺 X-CSRF-Token
