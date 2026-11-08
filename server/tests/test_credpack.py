"""凭证包契约 v1 · 服务器版导入端测试。

测试用「按契约规格独立构造」的凭证包字节流（规格见本模块头注释与
规划报告 §5.3），不依赖个人版导出实现——双端口径由
tests/test_credpack_interop.py 守护。
运行：python -m pytest server/tests -v
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from push_server import accounts, credpack, paths

PASSWORD = "口令-12345678"


def make_pack(
    payload: dict,
    password: str = PASSWORD,
    *,
    iterations: int = 600_000,
    aad: bytes = credpack.AAD,
    salt_len: int = 16,
    nonce_len: int = 12,
) -> bytes:
    """按契约规格独立构造凭证包（用于喂给被测的导入端）。"""
    salt = base64.b64encode(bytes(range(salt_len))).decode()
    nonce = base64.b64encode(bytes(range(nonce_len))).decode()
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), base64.b64decode(salt), iterations, dklen=32
    )
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(base64.b64decode(nonce), plaintext, aad)
    header = {
        "format": "val-push-credpack",
        "version": 1,
        "kdf": "pbkdf2-sha256",
        "iterations": iterations,
        "salt": salt,
        "nonce": nonce,
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    return json.dumps(header, ensure_ascii=False, indent=2).encode("utf-8")


def sample_payload() -> dict:
    return {
        "exported_at": "2026-11-08T12:00:00+08:00",
        "accounts": [
            {
                "name": "大号", "user_id": "u-0001", "login_type": "qq",
                "credentials": {"userId": "u-0001", "tid": "t-0001", "openid": "o1"},
            },
        ],
    }


# ---------- parse_pack ----------

def test_parse_pack_success(data_dir):
    payload = credpack.parse_pack(make_pack(sample_payload()), PASSWORD)
    assert payload["exported_at"] == "2026-11-08T12:00:00+08:00"
    assert payload["accounts"][0]["credentials"]["tid"] == "t-0001"


def test_parse_pack_wrong_password_unified_message(data_dir):
    with pytest.raises(credpack.CredpackError, match="口令错误或凭证包已损坏"):
        credpack.parse_pack(make_pack(sample_payload()), "错误的口令-999999")


def test_parse_pack_tampered_ciphertext(data_dir):
    raw = bytearray(make_pack(sample_payload()))
    raw[raw.index(b'"ciphertext"') :]  # 定位（保险起见不修改 header 键名）
    raw[-3] ^= 0x20  # 翻转密文尾部一个字符（tag 区域）
    with pytest.raises(credpack.CredpackError):
        credpack.parse_pack(bytes(raw), PASSWORD)


def test_parse_pack_rejects_bad_header(data_dir):
    good = json.loads(make_pack(sample_payload()).decode())

    def raw_with(**overrides) -> bytes:
        header = {**good, **overrides}
        return json.dumps(header).encode()

    with pytest.raises(credpack.CredpackError):
        credpack.parse_pack(raw_with(format="other-pack"), PASSWORD)
    with pytest.raises(credpack.CredpackError):
        credpack.parse_pack(raw_with(version=2), PASSWORD)
    with pytest.raises(credpack.CredpackError):
        credpack.parse_pack(raw_with(kdf="pbkdf2-sha1"), PASSWORD)
    with pytest.raises(credpack.CredpackError):  # 低于下限的弱参数
        credpack.parse_pack(make_pack(sample_payload(), iterations=50_000), PASSWORD)
    with pytest.raises(credpack.CredpackError):  # 极端大参数
        credpack.parse_pack(make_pack(sample_payload(), iterations=20_000_000), PASSWORD)
    with pytest.raises(credpack.CredpackError):  # salt 长度不对
        credpack.parse_pack(make_pack(sample_payload(), salt_len=8), PASSWORD)
    with pytest.raises(credpack.CredpackError):  # nonce 长度不对
        credpack.parse_pack(make_pack(sample_payload(), nonce_len=8), PASSWORD)
    with pytest.raises(credpack.CredpackError):
        credpack.parse_pack(b"not-json", PASSWORD)
    with pytest.raises(credpack.CredpackError):
        credpack.parse_pack(json.dumps(["not", "a", "dict"]).encode(), PASSWORD)


# ---------- import_accounts ----------

def test_import_adds_new_account(data_dir):
    result = credpack.import_accounts(sample_payload())
    assert result == {"total": 1, "added": 1, "updated": 0, "skipped": []}
    rows = accounts.all()
    assert len(rows) == 1
    assert rows[0]["name"] == "大号"
    assert rows[0]["login_type"] == "qq"
    # 落盘即加密：明文不出现
    disk = paths.ACCOUNTS_FILE.read_text(encoding="utf-8")
    assert "u-0001" not in disk
    assert "t-0001" not in disk
    assert "enc:v1:" in disk


def test_import_updates_same_user(data_dir):
    accounts.add({"userId": "u-0001", "tid": "old-tid"}, "qq")
    account_id = accounts.all()[0]["id"]
    accounts.mark_expired(account_id)
    result = credpack.import_accounts(sample_payload())
    assert result == {"total": 1, "added": 0, "updated": 1, "skipped": []}
    row = accounts.all()[0]
    assert row["credentials"]["tid"] == "t-0001"
    assert row["auth_expired"] is False


def test_import_skips_missing_fields(data_dir):
    payload = {
        "exported_at": "x",
        "accounts": [
            {"name": "缺 tid", "user_id": "u-9", "login_type": "qq", "credentials": {"userId": "u-9"}},
        ],
    }
    result = credpack.import_accounts(payload)
    assert result["added"] == 0 and result["updated"] == 0
    assert result["skipped"] == [{"account": "缺 tid", "reason": "凭证字段缺失（userId/tid）"}]
    assert accounts.count() == 0


def test_import_normalizes_login_type_and_default_name(data_dir):
    payload = {
        "exported_at": "x",
        "accounts": [
            {"name": "", "user_id": "u-1", "login_type": "whatever", "credentials": {"userId": "u-1", "tid": "t-1"}},
            {"name": "  ", "user_id": "u-2", "login_type": "wechat", "credentials": {"userId": "u-2", "tid": "t-2"}},
        ],
    }
    result = credpack.import_accounts(payload)
    assert result["added"] == 2
    rows = {r["user_id"]: r for r in accounts.all()}
    assert rows["u-1"]["login_type"] == "qq" and rows["u-1"]["name"] == "账号1"
    assert rows["u-2"]["login_type"] == "wechat" and rows["u-2"]["name"] == "账号2"


def test_import_respects_account_limit(data_dir):
    for i in range(accounts.MAX_ACCOUNTS):
        accounts.add({"userId": f"u-{i:02d}", "tid": f"t-{i:02d}"}, "qq")
    payload = {
        "exported_at": "x",
        "accounts": [
            {"name": "甲", "user_id": "u-new1", "login_type": "qq", "credentials": {"userId": "u-new1", "tid": "t-new1"}},
            {"name": "乙", "user_id": "u-new2", "login_type": "qq", "credentials": {"userId": "u-new2", "tid": "t-new2"}},
        ],
    }
    result = credpack.import_accounts(payload)
    assert result["added"] == 0 and result["updated"] == 0
    assert len(result["skipped"]) == 2
    assert "上限" in result["skipped"][0]["reason"]
