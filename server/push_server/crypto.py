"""AES-256-GCM 敏感字段加密（服务器版，替代个人版 DPAPI）。

- 密文格式：enc:v1:<nonce_b64>:<ciphertext_b64>（GCM tag 附在密文尾部）
- 主密钥优先级：
  1. 环境变量 VAL_SERVER_MASTER_KEY（32 字节 base64，Docker 部署推荐）
  2. 缺省时自动生成 data/master.key（32 字节，POSIX 下 0600）
- ⚠️ 密钥丢失 = 已加密数据不可解密：请备份 master.key 或重走「个人版导出 → 服务器导入」。
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import paths

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"
_NONCE_LEN = 12
_KEY_LEN = 32

_key_cache: bytes | None = None


class CryptoError(RuntimeError):
    """加密密钥不可用。"""


def reset_key_cache() -> None:
    """清空主密钥缓存（测试用；更换密钥来源后需调用）。"""
    global _key_cache
    _key_cache = None


def _key_path() -> Path:
    return Path(paths.DATA_DIR) / "master.key"


def load_master_key() -> bytes:
    """加载主密钥：env 优先，其次密钥文件（不存在则自动生成）。"""
    global _key_cache
    if _key_cache is not None:
        return _key_cache

    env_value = os.getenv("VAL_SERVER_MASTER_KEY", "").strip()
    if env_value:
        try:
            key = base64.b64decode(env_value, validate=True)
        except (ValueError, TypeError) as exc:
            raise CryptoError(f"VAL_SERVER_MASTER_KEY 不是合法 base64：{exc}") from exc
        if len(key) != _KEY_LEN:
            raise CryptoError(
                f"VAL_SERVER_MASTER_KEY 长度必须为 {_KEY_LEN} 字节（当前 {len(key)}）"
            )
        _key_cache = key
        return key

    paths.ensure_dirs()
    path = _key_path()
    if path.exists():
        key = path.read_bytes()
        if len(key) != _KEY_LEN:
            raise CryptoError(
                f"主密钥文件损坏（{path}，长度 {len(key)}）："
                "请删除该文件重新生成，或恢复备份后重走凭证导入"
            )
    else:
        key = secrets.token_bytes(_KEY_LEN)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
        except FileExistsError:
            key = path.read_bytes()  # 并发启动：另一进程刚生成
        if os.name == "posix":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        logger.info("已生成主密钥文件：%s（请妥善备份，丢失后已加密凭证无法解密）", path)
    _key_cache = key
    return key


def encrypt_text(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = AESGCM(load_master_key()).encrypt(nonce, value.encode("utf-8"), None)
    return (
        PREFIX
        + base64.b64encode(nonce).decode("ascii")
        + ":"
        + base64.b64encode(ciphertext).decode("ascii")
    )


def decrypt_text(value: str):
    if not value or not value.startswith(PREFIX):
        return value
    try:
        nonce_b64, ct_b64 = value[len(PREFIX):].split(":", 1)
        nonce = base64.b64decode(nonce_b64, validate=True)
        ciphertext = base64.b64decode(ct_b64, validate=True)
        plaintext = AESGCM(load_master_key()).decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")
    except Exception as exc:  # base64/GCM 校验失败：主密钥不匹配或密文损坏
        logger.error("敏感字段解密失败（主密钥不匹配或密文损坏），该字段需重新填写：%s", exc)
        return ""


def encrypt_fields(mapping: dict) -> dict:
    """加密 dict 中的字符串值；其余类型原样返回。"""
    out = {}
    for key, value in mapping.items():
        out[key] = encrypt_text(value) if isinstance(value, str) else value
    return out


def decrypt_fields(mapping: dict) -> dict:
    out = {}
    for key, value in mapping.items():
        out[key] = decrypt_text(value) if isinstance(value, str) else value
    return out


def mask_email(value: str) -> str:
    """掩码邮箱：a***@qq.com（展示用，不回显明文）。"""
    if not value or "@" not in value:
        return value
    name, _, domain = value.partition("@")
    head = name[:1] or "*"
    return f"{head}***@{domain}"


def mask_identifier(value: str) -> str:
    """掩码用户标识：只留尾部 4 位。"""
    if not value:
        return value
    tail = value[-4:] if len(value) > 4 else value
    return f"***{tail}"
