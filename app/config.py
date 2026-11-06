"""本地配置与运行状态读写。

配置放在 data/settings.json，运行状态放在 data/state.json；
登录凭证由 app/accounts.py 保存到 data/accounts.json。
所有读写都做默认值合并，保证升级后新增字段不会让程序崩溃。
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

from . import paths, protect

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, Any] = {
    "channel": "email",  # email | wecom
    "login_method": "qq",  # qq | wechat
    "schedule_enabled": True,
    "push_hour": 8,
    "push_minute": 5,
    "sections": ["dailystore", "kingdomstore"],
    "email": {
        "smtp_host": "",
        "smtp_port": 465,
        "security": "ssl",  # ssl | starttls（不再支持不加密连接）
        "username": "",
        "password": "",
        "to": "",
        "from_name": "无畏契约商店",
    },
    "wecom": {
        "corpid": "",
        "agentid": "",
        "secret": "",
        "touser": "@all",
    },
}

DEFAULT_STATE: dict[str, Any] = {
    "last_push_date": "",
    "last_push_at": "",
    "last_push_ok": False,
    "last_channel": "",
    "last_error": "",
    "last_summary": "",
    "auth_expired": False,
}

# 常见邮箱服务商：填完邮箱地址自动带出 SMTP 参数
SMTP_GUESS: dict[str, tuple[str, int, str]] = {
    "qq.com": ("smtp.qq.com", 465, "ssl"),
    "foxmail.com": ("smtp.qq.com", 465, "ssl"),
    "163.com": ("smtp.163.com", 465, "ssl"),
    "126.com": ("smtp.126.com", 465, "ssl"),
    "yeah.net": ("smtp.yeah.net", 465, "ssl"),
    "gmail.com": ("smtp.gmail.com", 587, "starttls"),
    "googlemail.com": ("smtp.gmail.com", 587, "starttls"),
    "outlook.com": ("smtp.office365.com", 587, "starttls"),
    "hotmail.com": ("smtp.office365.com", 587, "starttls"),
    "live.com": ("smtp.office365.com", 587, "starttls"),
    "sina.com": ("smtp.sina.com", 465, "ssl"),
    "aliyun.com": ("smtp.aliyun.com", 465, "ssl"),
    "139.com": ("smtp.139.com", 465, "ssl"),
    "189.cn": ("smtp.189.cn", 465, "ssl"),
}

SECRET_FIELDS = (("email", "password"), ("wecom", "secret"))


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError) as exc:
        logger.error("读取 %s 失败：%s，将使用默认值", path.name, exc)
    return copy.deepcopy(default)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    paths.ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------- 设置 ----------


def _decrypt_secrets(settings: dict[str, Any]) -> dict[str, Any]:
    for section, field in SECRET_FIELDS:
        target = settings.get(section)
        if isinstance(target, dict) and isinstance(target.get(field), str):
            target[field] = protect.decrypt_text(target[field])
    return settings


def _encrypt_secrets(settings: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(settings)
    for section, field in SECRET_FIELDS:
        target = data.get(section)
        if isinstance(target, dict) and isinstance(target.get(field), str):
            target[field] = protect.encrypt_text(target[field])
    return data


def load_settings() -> dict[str, Any]:
    return _decrypt_secrets(
        _merge(DEFAULT_SETTINGS, _read_json(paths.SETTINGS_FILE, DEFAULT_SETTINGS))
    )


def save_settings(data: dict[str, Any]) -> dict[str, Any]:
    settings = normalize(_merge(DEFAULT_SETTINGS, data))
    _write_json(paths.SETTINGS_FILE, _encrypt_secrets(settings))
    logger.info("配置已保存：%s", paths.SETTINGS_FILE)
    return settings


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """把前端传来的字符串转换成正确的类型。"""
    settings = _merge(DEFAULT_SETTINGS, data)

    settings["channel"] = "wecom" if settings.get("channel") == "wecom" else "email"
    settings["login_method"] = "wechat" if settings.get("login_method") == "wechat" else "qq"

    try:
        settings["push_hour"] = min(23, max(0, int(settings.get("push_hour", 8))))
    except (TypeError, ValueError):
        settings["push_hour"] = 8
    try:
        settings["push_minute"] = min(59, max(0, int(settings.get("push_minute", 5))))
    except (TypeError, ValueError):
        settings["push_minute"] = 5

    sections = settings.get("sections")
    if not isinstance(sections, list):
        sections = DEFAULT_SETTINGS["sections"]
    settings["sections"] = [key for key in ("dailystore", "kingdomstore") if key in sections]
    if not settings["sections"]:
        settings["sections"] = ["dailystore"]

    settings["schedule_enabled"] = bool(settings.get("schedule_enabled", True))

    email = settings["email"]
    try:
        email["smtp_port"] = int(email.get("smtp_port") or 465)
    except (TypeError, ValueError):
        email["smtp_port"] = 465
    if email.get("security") not in {"ssl", "starttls"}:
        # 旧版本的 "none"（明文 25 端口）一律升级为 STARTTLS，避免凭证明文传输
        email["security"] = "starttls" if email.get("security") == "none" else "ssl"
    for key in ("smtp_host", "username", "password", "to"):
        email[key] = str(email.get(key) or "").strip()
    email["from_name"] = str(email.get("from_name") or "无畏契约商店").strip()

    wecom = settings["wecom"]
    for key in ("corpid", "agentid", "secret", "touser"):
        wecom[key] = str(wecom.get(key) or "").strip()
    if not wecom["touser"]:
        wecom["touser"] = "@all"

    return settings


def mask_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """返回给页面展示的配置：密码/密钥不下发，只告诉前端“已填写”。"""
    view = copy.deepcopy(settings)
    for section, field in SECRET_FIELDS:
        value = view.get(section, {}).get(field, "")
        view[section][field] = ""
        view[section][f"{field}_set"] = bool(value)
    return view


def apply_update(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """合并页面提交的修改；密钥字段留空表示保持原值。"""
    patch = copy.deepcopy(patch or {})
    updated = _merge(current, patch)
    for section, field in SECRET_FIELDS:
        incoming = (patch.get(section) or {}).get(field)
        if incoming in ("", None):
            updated.setdefault(section, {})[field] = current.get(section, {}).get(field, "")
        updated[section].pop(f"{field}_set", None)
    return updated


def validate(settings: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if settings["channel"] == "email":
        email = settings["email"]
        if not email["username"] or "@" not in email["username"]:
            problems.append("请填写邮箱账号（发件邮箱）")
        if not email["password"]:
            problems.append("请填写邮箱授权码")
        if not (email["to"] or email["username"]):
            problems.append("请填写收件邮箱")
        if not email["smtp_host"]:
            problems.append("请填写 SMTP 服务器地址")
    else:
        wecom = settings["wecom"]
        if not wecom["corpid"]:
            problems.append("请填写企业 ID（corpid）")
        if not wecom["agentid"]:
            problems.append("请填写应用 AgentId")
        if not wecom["secret"]:
            problems.append("请填写应用 Secret")
    return problems


# ---------- 运行状态 ----------


def load_state() -> dict[str, Any]:
    return _merge(DEFAULT_STATE, _read_json(paths.STATE_FILE, DEFAULT_STATE))


def update_state(**changes: Any) -> dict[str, Any]:
    state = _merge(load_state(), changes)
    _write_json(paths.STATE_FILE, state)
    return state


# ---------- 渠道可用性 ----------


def email_configured(settings: dict[str, Any] | None = None) -> bool:
    email = (settings or load_settings())["email"]
    return bool(email["smtp_host"] and email["username"] and email["password"])


def wecom_configured(settings: dict[str, Any] | None = None) -> bool:
    wecom = (settings or load_settings())["wecom"]
    return bool(wecom["corpid"] and wecom["agentid"] and wecom["secret"])


def channel_configured(settings: dict[str, Any], channel: str) -> bool:
    return email_configured(settings) if channel == "email" else wecom_configured(settings)


def guess_smtp(address: str) -> dict[str, Any]:
    domain = address.rsplit("@", 1)[-1].lower().strip() if "@" in address else ""
    if domain in SMTP_GUESS:
        host, port, security = SMTP_GUESS[domain]
    else:
        host, port, security = f"smtp.{domain}" if domain else "", 465, "ssl"
    return {"smtp_host": host, "smtp_port": port, "security": security}
