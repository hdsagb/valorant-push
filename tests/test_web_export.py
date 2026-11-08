"""「导出凭证包」Web 接口测试。运行：python -m pytest tests -v"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re

from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import accounts, web

AAD = b"val-push-credpack:v1"


def _make_app():
    application = web.web.Application(middlewares=[web.local_only])
    application.add_routes(web.routes)
    return application


def _headers() -> dict:
    return {"X-Requested-With": web.REQUESTED_WITH, web.CSRF_HEADER: web.CSRF_TOKEN}


def _run(calls) -> None:
    async def scenario():
        client = TestClient(TestServer(_make_app()))
        await client.start_server()
        try:
            await calls(client)
        finally:
            await client.close()

    asyncio.run(scenario())


def _unpack(raw: bytes, password: str) -> dict:
    header = json.loads(raw.decode("utf-8"))
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"),
        base64.b64decode(header["salt"]), header["iterations"], dklen=32,
    )
    plaintext = AESGCM(key).decrypt(
        base64.b64decode(header["nonce"]), base64.b64decode(header["ciphertext"]), AAD
    )
    return {"header": header, "payload": json.loads(plaintext)}


def test_export_success(data_dir):
    accounts.add({"userId": "u-0001", "tid": "t-0001", "openid": "o1"}, "qq")
    accounts.add({"userId": "u-0002", "tid": "t-0002"}, "wechat")

    async def calls(client: TestClient):
        resp = await client.post(
            "/api/credentials/export", json={"password": "口令-12345678"}, headers=_headers()
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert re.fullmatch(r"valorant-push-credentials-\d{8}\.vpcred", data["filename"])
        parsed = _unpack(base64.b64decode(data["content_b64"]), "口令-12345678")
        assert parsed["header"]["format"] == "val-push-credpack"
        assert len(parsed["payload"]["accounts"]) == 2
        assert parsed["payload"]["accounts"][0]["credentials"]["userId"] == "u-0001"

    _run(calls)


def test_export_rejects_without_csrf(data_dir):
    accounts.add({"userId": "u-0001", "tid": "t-0001"}, "qq")

    async def calls(client: TestClient):
        resp = await client.post("/api/credentials/export", json={"password": "口令-12345678"})
        assert resp.status == 403

    _run(calls)


def test_export_rejects_short_password(data_dir):
    accounts.add({"userId": "u-0001", "tid": "t-0001"}, "qq")

    async def calls(client: TestClient):
        resp = await client.post(
            "/api/credentials/export", json={"password": "短"}, headers=_headers()
        )
        assert resp.status == 400
        assert "口令长度" in (await resp.json())["message"]

    _run(calls)


def test_export_conflict_without_accounts(data_dir):
    async def calls(client: TestClient):
        resp = await client.post(
            "/api/credentials/export", json={"password": "口令-12345678"}, headers=_headers()
        )
        assert resp.status == 409

    _run(calls)
