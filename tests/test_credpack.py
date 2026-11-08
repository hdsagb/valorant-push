"""凭证包契约 v1 · 个人版导出端测试。运行：python -m pytest tests -v"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import credpack

AAD = b"val-push-credpack:v1"


def manual_decrypt(raw: bytes, password: str) -> dict:
    """按契约规格独立解包（不依赖任何一端实现），验证产物符合规格。"""
    header = json.loads(raw.decode("utf-8"))
    salt = base64.b64decode(header["salt"])
    nonce = base64.b64decode(header["nonce"])
    ciphertext = base64.b64decode(header["ciphertext"])
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, header["iterations"], dklen=32)
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, AAD)
    return {"header": header, "payload": json.loads(plaintext)}


def sample_rows() -> list[dict]:
    return [
        {
            "id": "a1", "name": "大号", "user_id": "u-0001", "login_type": "qq",
            "credentials": {"userId": "u-0001", "tid": "t-0001", "openid": "o1", "final_cookie": "c"},
            "auth_expired": False, "added_at": "2026-11-08 10:00:00",
        },
        {
            "id": "a2", "name": "小号", "user_id": "u-0002", "login_type": "wechat",
            "credentials": {"userId": "u-0002", "tid": "t-0002", "login_type": "wechat"},
            "auth_expired": True, "added_at": "2026-11-08 10:00:00",
        },
        {
            "id": "a3", "name": "残缺", "user_id": "", "login_type": "qq",
            "credentials": {}, "auth_expired": False, "added_at": "",
        },
    ]


def test_validate_password():
    credpack.validate_password("12345678")
    with pytest.raises(credpack.CredpackError):
        credpack.validate_password("1234567")
    with pytest.raises(credpack.CredpackError):
        credpack.validate_password("")
    with pytest.raises(credpack.CredpackError):
        credpack.validate_password("x" * 129)


def test_payload_accounts_picks_fields_only():
    rows = credpack.payload_accounts(sample_rows())
    assert len(rows) == 2  # 残缺账号不入包
    assert rows[0] == {
        "name": "大号", "user_id": "u-0001", "login_type": "qq",
        "credentials": {"userId": "u-0001", "tid": "t-0001", "openid": "o1", "final_cookie": "c"},
    }
    assert rows[1]["login_type"] == "wechat"


def test_build_pack_header_and_roundtrip():
    rows = credpack.payload_accounts(sample_rows())
    raw = credpack.build_pack(rows, "口令-12345678")
    parsed = manual_decrypt(raw, "口令-12345678")
    header, payload = parsed["header"], parsed["payload"]
    assert header["format"] == "val-push-credpack"
    assert header["version"] == 1
    assert header["kdf"] == "pbkdf2-sha256"
    assert header["iterations"] == 600000
    assert len(base64.b64decode(header["salt"])) == 16
    assert len(base64.b64decode(header["nonce"])) == 12
    assert payload["accounts"] == rows
    assert datetime.fromisoformat(payload["exported_at"]).tzinfo is not None


def test_build_pack_no_plaintext_leak():
    raw = credpack.build_pack(credpack.payload_accounts(sample_rows()), "口令-12345678")
    assert "u-0001".encode() not in raw
    assert "t-0001".encode() not in raw
    assert "大号".encode() not in raw


def test_build_pack_rejects_empty_and_bad_password():
    with pytest.raises(credpack.CredpackError):
        credpack.build_pack([], "口令-12345678")
    with pytest.raises(credpack.CredpackError):
        credpack.build_pack(
            [{"name": "x", "user_id": "u", "login_type": "qq",
              "credentials": {"userId": "u", "tid": "t"}}], "短"
        )


def test_wrong_password_fails_roundtrip():
    raw = credpack.build_pack(credpack.payload_accounts(sample_rows()), "口令-12345678")
    with pytest.raises(Exception):
        manual_decrypt(raw, "错误的口令-999999")


def test_pack_filename():
    assert credpack.pack_filename(datetime(2026, 11, 8, 9, 5)) == "valorant-push-credentials-20261108.vpcred"
