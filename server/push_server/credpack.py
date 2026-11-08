"""凭证包（.vpcred）契约 v1：服务器版导入端实现。

与个人版 app/credpack.py 是同一契约的两端独立实现
（两版唯一刻意耦合点：《服务器版开发规划报告》§3 / §5.3）。
个人版修改格式时必须同步本文件并升级 version，否则按「文件损坏」拒绝。

导入编排 import_accounts() 供两处调用：
- CLI：python -m push_server --import-credentials <文件>（M2）
- Web 导入卡片：M3 鉴权就位后接入，直接复用本函数

安全口径：
- 口令错误 / 文件损坏统一报「口令错误或凭证包已损坏」，不区分原因
- 口令绝不落日志；导入日志只写账号名与掩码 ID（crypto.mask_identifier）
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import accounts, crypto

logger = logging.getLogger(__name__)

FORMAT = "val-push-credpack"
VERSION = 1
KDF = "pbkdf2-sha256"
AAD = b"val-push-credpack:v1"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
MIN_PASSWORD = 8
MAX_PASSWORD = 128
# 解析端防御参数：下限拒绝弱参数，上限拒绝极端值（拒绝服务）
ITERATIONS_MIN = 100_000
ITERATIONS_MAX = 10_000_000
MAX_FILE_BYTES = 1024 * 1024

_CRYPTO_FAIL = "口令错误或凭证包已损坏"
_NAME_LIMIT = 24


class CredpackError(ValueError):
    """凭证包无法解析或口令不正确（统一口径）。"""


def parse_pack(raw: bytes, password: str) -> dict:
    """解析并解密凭证包，返回载荷 {"exported_at", "accounts": [...]}。"""
    if not MIN_PASSWORD <= len(password or "") <= MAX_PASSWORD:
        raise CredpackError(f"口令长度需在 {MIN_PASSWORD}-{MAX_PASSWORD} 位之间")
    if len(raw) > MAX_FILE_BYTES:
        raise CredpackError("文件过大，不是有效的凭证包")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CredpackError(_CRYPTO_FAIL) from exc
    if not isinstance(header, dict):
        raise CredpackError(_CRYPTO_FAIL)
    if header.get("format") != FORMAT or header.get("version") != VERSION or header.get("kdf") != KDF:
        raise CredpackError(f"不支持的凭证包格式（本程序支持 {FORMAT} v1）")
    iterations = header.get("iterations")
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or not ITERATIONS_MIN <= iterations <= ITERATIONS_MAX
    ):
        raise CredpackError(_CRYPTO_FAIL)
    try:
        salt = base64.b64decode(header.get("salt") or "", validate=True)
        nonce = base64.b64decode(header.get("nonce") or "", validate=True)
        ciphertext = base64.b64decode(header.get("ciphertext") or "", validate=True)
    except (ValueError, TypeError) as exc:
        raise CredpackError(_CRYPTO_FAIL) from exc
    if len(salt) != SALT_LEN or len(nonce) != NONCE_LEN or not ciphertext:
        raise CredpackError(_CRYPTO_FAIL)

    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=KEY_LEN)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, AAD)
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:  # InvalidTag / UnicodeDecodeError / ValueError
        raise CredpackError(_CRYPTO_FAIL) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list):
        raise CredpackError(_CRYPTO_FAIL)
    if not all(isinstance(item, dict) for item in payload["accounts"]):
        raise CredpackError(_CRYPTO_FAIL)
    return payload


def import_accounts(payload: dict) -> dict:
    """把凭证包载荷导入账号库，返回 {"total", "added", "updated", "skipped"}。

    - credentials 缺 userId/tid 的账号跳过，不阻塞其余账号
    - 已存在相同 user_id：更新凭证并清除失效标记（accounts.update_same_user）
    - 达到账号上限（accounts.MAX_ACCOUNTS）：跳过该账号，继续处理其余
    """
    added = updated = 0
    skipped: list[dict] = []
    items = payload.get("accounts") or []
    for index, item in enumerate(items, start=1):
        raw_name = " ".join(str(item.get("name") or "").split())[:_NAME_LIMIT]
        name = raw_name or f"账号{index}"
        credentials = item.get("credentials")
        if (
            not isinstance(credentials, dict)
            or not credentials.get("userId")
            or not credentials.get("tid")
        ):
            skipped.append({"account": name, "reason": "凭证字段缺失（userId/tid）"})
            continue
        user_id = str(credentials["userId"])
        login_type = "wechat" if item.get("login_type") == "wechat" else "qq"
        declared = str(item.get("user_id") or "")
        if declared and declared != user_id:
            # 契约上两者应一致；不一致视为文件被手改，以实际凭证为准并告警
            logger.warning("账号 %s 的 user_id 与凭证不一致，以凭证 userId 为准", name)
        try:
            if accounts.update_same_user(user_id, credentials, login_type):
                updated += 1
                logger.info("已更新账号凭证：%s（%s）", name, crypto.mask_identifier(user_id))
            else:
                accounts.add(credentials, login_type, name=name)
                added += 1
                logger.info("已导入新账号：%s（%s）", name, crypto.mask_identifier(user_id))
        except ValueError as exc:  # 账号上限
            skipped.append(
                {
                    "account": f"{name}（{crypto.mask_identifier(user_id)}）",
                    "reason": f"已达账号上限：{exc}",
                }
            )
    return {"total": len(items), "added": added, "updated": updated, "skipped": skipped}
