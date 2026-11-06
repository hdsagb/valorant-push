"""多账号管理：登录凭证统一保存在 data/accounts.json，最多 10 个。

单个账号即一份 vendored 脚本的登录配置（userId / tid / cookies），
额外记录账号名、登录方式、凭证是否失效，用于推送时区分。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from . import paths

logger = logging.getLogger(__name__)

MAX_ACCOUNTS = 10
NAME_LIMIT = 24


class AccountLimitError(RuntimeError):
    """已达账号数量上限。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_store() -> dict[str, Any]:
    if not paths.ACCOUNTS_FILE.exists():
        return {"accounts": []}
    try:
        data = json.loads(paths.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return data
    except (OSError, ValueError) as exc:
        logger.error("读取账号文件失败：%s", exc)
    return {"accounts": []}


def _write_store(data: dict[str, Any]) -> None:
    paths.ensure_dirs()
    tmp = paths.ACCOUNTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, paths.ACCOUNTS_FILE)


def _migrate_legacy() -> None:
    """首次运行时，把旧版单账号的 data/config.json 导入为新账号。"""
    if paths.ACCOUNTS_FILE.exists():
        return

    accounts: list[dict[str, Any]] = []
    legacy = paths.CREDENTIALS_FILE
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("旧版登录凭证读取失败，跳过迁移：%s", exc)
            data = {}
        if data.get("userId") and data.get("tid"):
            accounts.append(_new_account(data, "账号1", str(data.get("login_type") or "qq")))
            logger.info("已把旧版单账号凭证迁移为「账号1」")

    _write_store({"accounts": accounts})


def _new_account(credentials: dict[str, Any], name: str, login_type: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "user_id": str(credentials.get("userId") or ""),
        "login_type": "wechat" if login_type == "wechat" else "qq",
        "credentials": copy.deepcopy(credentials),
        "auth_expired": False,
        "added_at": _now(),
    }


def _clean_name(name: str, fallback: str) -> str:
    text = " ".join(str(name or "").split())[:NAME_LIMIT]
    return text or fallback


def _default_name(existing: list[dict[str, Any]]) -> str:
    used = {item.get("name") for item in existing}
    for index in range(1, MAX_ACCOUNTS + 1):
        candidate = f"账号{index}"
        if candidate not in used:
            return candidate
    return "账号"


def mask_user_id(user_id: str) -> str:
    return (user_id[:3] + "***" + user_id[-3:]) if len(user_id) > 8 else user_id


def all() -> list[dict[str, Any]]:
    """返回账号原始列表（含凭证，仅程序内部使用）。"""
    _migrate_legacy()
    return copy.deepcopy(_read_store()["accounts"])


def count() -> int:
    _migrate_legacy()
    return len(_read_store()["accounts"])


def summary() -> list[dict[str, Any]]:
    """给页面展示的账号信息，不含凭证。"""
    result = []
    for account in all():
        user_id = str(account.get("user_id") or "")
        result.append(
            {
                "id": account.get("id") or "",
                "name": account.get("name") or "账号",
                "login_type": account.get("login_type") or "",
                "user_id_masked": mask_user_id(user_id),
                "auth_expired": bool(account.get("auth_expired")),
                "added_at": account.get("added_at") or "",
            }
        )
    return result


def add(credentials: dict[str, Any], login_type: str = "qq") -> tuple[dict[str, Any], bool]:
    """新增账号；同一 userId 再次绑定时只更新凭证。返回 (账号, 是否为更新)。"""
    _migrate_legacy()
    user_id = str(credentials.get("userId") or "")
    store = _read_store()
    items: list[dict[str, Any]] = store["accounts"]

    for account in items:
        if account.get("user_id") == user_id:
            account["credentials"] = copy.deepcopy(credentials)
            account["login_type"] = "wechat" if login_type == "wechat" else "qq"
            account["auth_expired"] = False
            _write_store(store)
            logger.info("账号凭证已更新：%s", account.get("name"))
            return copy.deepcopy(account), True

    if len(items) >= MAX_ACCOUNTS:
        raise AccountLimitError(
            f"最多只能绑定 {MAX_ACCOUNTS} 个账号，请先在「已绑定账号」里解绑一个"
        )

    account = _new_account(credentials, _default_name(items), login_type)
    items.append(account)
    _write_store(store)
    logger.info("已绑定新账号：%s（共 %s 个）", account["name"], len(items))
    return copy.deepcopy(account), False


def rename(account_id: str, name: str) -> dict[str, Any] | None:
    store = _read_store()
    for account in store["accounts"]:
        if account.get("id") == account_id:
            account["name"] = _clean_name(name, account.get("name") or "账号")
            _write_store(store)
            return copy.deepcopy(account)
    return None


def remove(account_id: str) -> bool:
    store = _read_store()
    remaining = [item for item in store["accounts"] if item.get("id") != account_id]
    if len(remaining) == len(store["accounts"]):
        return False
    store["accounts"] = remaining
    _write_store(store)
    logger.info("已解绑账号，剩余 %s 个", len(remaining))
    return True


def mark_expired(account_id: str, expired: bool = True) -> None:
    store = _read_store()
    changed = False
    for account in store["accounts"]:
        if account.get("id") == account_id:
            account["auth_expired"] = bool(expired)
            changed = True
    if changed:
        _write_store(store)


def all_expired() -> bool:
    items = all()
    return bool(items) and not any(not item.get("auth_expired") for item in items)
