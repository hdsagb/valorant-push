"""多账号管理：登录凭证统一保存在 data/accounts.json，最多 10 个。

服务器版凭证来源：个人版导出的凭证包导入（M2 提供）或手工写入；
本模块与个人版 app/accounts.py 同构，加密层为 AES-256-GCM（crypto.py），
不含个人版的旧版单账号迁移逻辑（服务器版无历史包袱）。

单条账号结构：
{id, name, user_id(密文), login_type, credentials(密文 dict), auth_expired, added_at}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from . import crypto, paths

logger = logging.getLogger(__name__)

MAX_ACCOUNTS = 10


def _read_store() -> dict:
    try:
        data = json.loads(paths.ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"accounts": []}
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        logger.warning("账号文件结构异常，按空库处理")
        return {"accounts": []}
    return data


def _write_store(store: dict) -> None:
    paths.ensure_dirs()
    paths.ACCOUNTS_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def all() -> list[dict]:
    """返回全部账号，凭证已解密（仅内存）。"""
    accounts = []
    for item in _read_store()["accounts"]:
        item = dict(item)
        item["user_id"] = crypto.decrypt_text(item.get("user_id", ""))
        item["credentials"] = crypto.decrypt_fields(item.get("credentials") or {})
        accounts.append(item)
    return accounts


def count() -> int:
    return len(_read_store()["accounts"])


def add(credentials: dict, login_type: str, name: str = "") -> dict:
    """新增账号：user_id 与 credentials 加密落盘。"""
    store = _read_store()
    if len(store["accounts"]) >= MAX_ACCOUNTS:
        raise ValueError(f"最多支持 {MAX_ACCOUNTS} 个账号")
    account = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "name": name,
        "user_id": crypto.encrypt_text(str(credentials.get("userId", ""))),
        "login_type": login_type,
        "credentials": crypto.encrypt_fields(credentials),
        "auth_expired": False,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }
    store["accounts"].append(account)
    _write_store(store)
    return account


def update_same_user(user_id: str, credentials: dict, login_type: str) -> bool:
    """若已存在相同 user_id 的账号则更新其凭证；返回是否更新。"""
    store = _read_store()
    updated = False
    for account in store["accounts"]:
        if crypto.decrypt_text(account.get("user_id", "")) == user_id:
            account["credentials"] = crypto.encrypt_fields(credentials)
            account["login_type"] = login_type
            account["auth_expired"] = False
            updated = True
    if updated:
        _write_store(store)
    return updated


def rename(account_id: str, name: str) -> None:
    store = _read_store()
    for account in store["accounts"]:
        if account["id"] == account_id:
            account["name"] = name
    _write_store(store)


def remove(account_id: str) -> None:
    store = _read_store()
    store["accounts"] = [a for a in store["accounts"] if a["id"] != account_id]
    _write_store(store)


def mark_expired(account_id: str, expired: bool = True) -> None:
    """按账号 id 标记凭证失效/恢复（与个人版同名同签名，pipeline 直接调用）。"""
    store = _read_store()
    for account in store["accounts"]:
        if account["id"] == account_id:
            account["auth_expired"] = bool(expired)
    _write_store(store)


def mask_user_id(user_id: str) -> str:
    """推送图片/摘要里的脱敏账号 ID（沿用 crypto 统一规则）。"""
    return crypto.mask_identifier(user_id)


def all_expired() -> bool:
    """是否所有账号都已失效（无账号时返回 False）。"""
    items = all()
    # 注意：不能用内置 all() —— 模块级 all() 会遮蔽它（个人版同样写法）
    return bool(items) and not any(not item.get("auth_expired") for item in items)


def summary() -> list[dict]:
    """给界面用的掩码摘要（不含任何凭证）。"""
    return [
        {
            "id": a["id"],
            "name": a.get("name", ""),
            "masked_id": crypto.mask_identifier(a.get("user_id", "")),
            "login_type": a.get("login_type", ""),
            "auth_expired": a.get("auth_expired", False),
            "added_at": a.get("added_at", ""),
        }
        for a in all()
    ]
