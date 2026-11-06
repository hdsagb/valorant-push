"""敏感字段保护：Windows DPAPI 加密 + 展示用掩码。

配置里的邮箱授权码、企业微信 Secret、账号凭证等写盘前用 DPAPI 加密，
密钥由 Windows 绑定当前用户，拷走文件在别的电脑 / 别的用户下无法解密。
非 Windows 环境（如 CI/测试）退化为原样保存。
"""
from __future__ import annotations

import base64
import ctypes
import logging
import os
from ctypes import wintypes

logger = logging.getLogger(__name__)

PREFIX = "dpapi:"
# CRYPTPROTECT_UI_FORBIDDEN：加密过程中不弹任何窗口
_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _available() -> bool:
    return os.name == "nt"


def _crypt(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    buffer = ctypes.create_string_buffer(data, max(len(data), 1))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()

    if protect:
        func = crypt32.CryptProtectData
        func.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
    else:
        func = crypt32.CryptUnprotectData
        func.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
    func.restype = wintypes.BOOL

    if not func(ctypes.byref(blob_in), None, None, None, None, _UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def encrypt_text(value: str) -> str:
    """写盘前加密；空值、已加密值、非 Windows 环境原样返回。"""
    if not value or value.startswith(PREFIX) or not _available():
        return value
    try:
        blob = _crypt(value.encode("utf-8"), True)
    except (OSError, ValueError) as exc:
        logger.warning("敏感字段加密失败，仍以明文保存：%s", exc)
        return value
    return PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt_text(value: str) -> str:
    """读取时解密；解密失败（换了用户/电脑）返回空串，让用户重新填写。"""
    if not value or not value.startswith(PREFIX):
        return value
    if not _available():
        return ""
    try:
        blob = base64.b64decode(value[len(PREFIX):].encode("ascii"), validate=True)
        return _crypt(blob, False).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning("敏感字段解密失败（可能换了 Windows 用户或电脑），请重新填写：%s", exc)
        return ""


def encrypt_fields(mapping: dict) -> dict:
    return {
        key: encrypt_text(value) if isinstance(value, str) else value
        for key, value in mapping.items()
    }


def decrypt_fields(mapping: dict) -> dict:
    return {
        key: decrypt_text(value) if isinstance(value, str) else value
        for key, value in mapping.items()
    }


def mask_email(address: str) -> str:
    """日志用：a***@qq.com"""
    text = str(address or "").strip()
    if "@" not in text:
        return "***" if text else ""
    local, domain = text.split("@", 1)
    return f"{local[:1]}***@{domain}"


def mask_identifier(value: str) -> str:
    """日志用：保留末 4 位，避免完整账号 ID 落盘。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "***"
    return "***" + text[-4:]
