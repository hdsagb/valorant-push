"""凭证包（.vpcred）契约 v1：个人版导出端实现。

与服务器版 server/push_server/credpack.py 是同一契约的两端独立实现
（两版唯一刻意耦合点：《服务器版开发规划报告》§3 / §5.3）。
修改格式必须两端同步并升级 version，否则导入端按「文件损坏」拒绝；
tests/test_credpack_interop.py 是两端口径的守护测试。

文件：valorant-push-credentials-YYYYMMDD.vpcred（JSON，自描述）
- 密钥：key = PBKDF2-HMAC-SHA256(口令 UTF-8, salt, iterations, dklen=32)
- 加密：AES-256-GCM，AAD = b"val-push-credpack:v1"，tag 附于密文尾部
- 明文载荷：{"exported_at", "accounts": [{name, user_id, login_type, credentials}]}
- 只导出账号凭证；邮箱授权码 / 企微 Secret 不入包（渠道在服务器端重新填写）
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = "val-push-credpack"
VERSION = 1
KDF = "pbkdf2-sha256"
ITERATIONS = 600_000
AAD = b"val-push-credpack:v1"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
MIN_PASSWORD = 8
MAX_PASSWORD = 128


class CredpackError(ValueError):
    """凭证包生成参数不合法。"""


def validate_password(password: str) -> str:
    text = str(password or "")
    if not MIN_PASSWORD <= len(text) <= MAX_PASSWORD:
        raise CredpackError(f"口令长度需在 {MIN_PASSWORD}-{MAX_PASSWORD} 位之间")
    return text


def payload_accounts(accounts_raw: list[dict]) -> list[dict]:
    """从账号库行（app.accounts.all() 的元素）抽取入包字段，剔除其余元数据。

    凭证缺 userId/tid 的残缺账号不入包（调用方负责提示跳过数量）。
    """
    payload = []
    for row in accounts_raw:
        credentials = row.get("credentials")
        if not isinstance(credentials, dict) or not credentials.get("userId") or not credentials.get("tid"):
            continue
        payload.append(
            {
                "name": str(row.get("name") or ""),
                "user_id": str(row.get("user_id") or ""),
                "login_type": "wechat" if row.get("login_type") == "wechat" else "qq",
                "credentials": dict(credentials),
            }
        )
    return payload


def pack_filename(now: datetime | None = None) -> str:
    moment = now or datetime.now()
    return f"valorant-push-credentials-{moment.strftime('%Y%m%d')}.vpcred"


def build_pack(payload: list[dict], password: str, exported_at: str | None = None) -> bytes:
    """生成凭证包字节流。payload 为 payload_accounts() 的返回结构。"""
    validate_password(password)
    if not payload:
        raise CredpackError("没有可导出的账号（凭证缺少 userId/tid）")
    document = {
        "exported_at": exported_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "accounts": payload,
    }
    plaintext = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, dklen=KEY_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, AAD)

    header = {
        "format": FORMAT,
        "version": VERSION,
        "kdf": KDF,
        "iterations": ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return (json.dumps(header, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
