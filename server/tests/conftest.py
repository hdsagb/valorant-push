"""服务器版测试基座：隔离数据目录与主密钥。"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web as aiohttp_web
from aiohttp.test_utils import TestClient, TestServer

SERVER_DIR = Path(__file__).resolve().parents[1]  # server/
sys.path.insert(0, str(SERVER_DIR))

from push_server import crypto, paths  # noqa: E402

# 32 字节固定测试主密钥（base64）。任何测试进程内加密结果稳定、可复现。
TEST_MASTER_KEY = base64.b64encode(bytes(range(32))).decode("ascii")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据目录指到临时目录，并固定环境变量主密钥。"""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("VAL_SERVER_MASTER_KEY", TEST_MASTER_KEY)
    crypto.reset_key_cache()
    monkeypatch.setattr(paths, "DATA_DIR", data)
    monkeypatch.setattr(paths, "TEMP_DIR", data / "temp")
    monkeypatch.setattr(paths, "OUT_DIR", data / "out")
    monkeypatch.setattr(paths, "SETTINGS_FILE", data / "settings.json")
    monkeypatch.setattr(paths, "STATE_FILE", data / "state.json")
    monkeypatch.setattr(paths, "ACCOUNTS_FILE", data / "accounts.json")
    monkeypatch.setattr(paths, "AUTH_FILE", data / "auth.json")
    monkeypatch.setattr(paths, "LOG_FILE", data / "app.log")
    yield data
    crypto.reset_key_cache()


class WebClient:
    """aiohttp TestClient 的同步门面：事件循环由 fixture 管理，用例保持普通同步函数。

    （服务器测试基座未引入 pytest-asyncio，避免为 Web 测试新增依赖。）
    """

    def __init__(self, client: TestClient, loop: asyncio.AbstractEventLoop):
        self._client = client
        self._loop = loop

    def request(self, method: str, path: str, **kwargs):
        async def _do():
            resp = await self._client.request(method, path, **kwargs)
            text = await resp.text()
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            return SimpleNamespace(status=resp.status, text=text, data=data)

        return self._loop.run_until_complete(_do())

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)


@pytest.fixture
def web_client_factory():
    """创建绑定任意 aiohttp 应用的 WebClient（保留 cookie 罐，可做多步会话测试）。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    made: list[TestClient] = []

    def factory(app: aiohttp_web.Application) -> WebClient:
        async def _make():
            client = TestClient(TestServer(app))
            await client.start_server()
            return client

        client = loop.run_until_complete(_make())
        made.append(client)
        return WebClient(client, loop)

    yield factory

    for client in made:
        loop.run_until_complete(client.close())
    loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def web_routes_client(web_client_factory) -> WebClient:
    """直连 push_server.web.routes（无中间件）的 WebClient。"""
    from push_server import web

    app = aiohttp_web.Application()
    app.add_routes(web.routes)
    return web_client_factory(app)
