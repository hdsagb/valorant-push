"""web.py 基底裁剪测试：路由剥离与页面结构（Task 3）。

测试直连 routes（无 auth_guard）——auth_guard 在 Task 4 接入后，
未登录访问 `/` 由中间件重定向，本文件只关心「裁剪」与「页面结构」本身。
运行：python -m pytest server/tests/test_web.py -v
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from push_server import web


# ---------- 路由剥离（删掉的不能再出现） ----------

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


# ---------- 页面结构（保留项必须还在，删除项必须不在） ----------

def test_index_keeps_core_sections(web_routes_client):
    resp = web_routes_client.get("/")
    assert resp.status == 200
    for anchor in ("channelTabs", "accList", "pushBtn", "testBtn", "publicIp", "csrf-token"):
        assert anchor in resp.text


def test_index_drops_removed_sections(web_routes_client):
    resp = web_routes_client.get("/")
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


def test_index_renders_session_csrf(web_routes_client):
    resp = web_routes_client.get("/")
    assert resp.status == 200
    # 会话级 csrf 注入 <meta name="csrf-token">（Task 4 接入 auth_guard 后生效）
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
    """import web 不应触发登录/autostart 相关模块加载。"""
    assert "push_server.login" not in sys.modules
    assert "push_server.autostart" not in sys.modules
