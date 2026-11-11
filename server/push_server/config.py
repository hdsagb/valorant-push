"""配置与运行状态读写（服务器版）。

- data/settings.json：推送设置（渠道、时间、分区等）；敏感字段以 AES-GCM 密文落盘
- data/state.json：运行状态（上次推送时间/结果、错误信息等）
- 加密层见 crypto.py（主密钥：VAL_SERVER_MASTER_KEY 或 data/master.key）
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import crypto, paths

logger = logging.getLogger(__name__)

# settings.json 中需要加密的字段（点号路径），与个人版语义一致
SECRET_FIELDS = ("email.password", "wecom.secret")

SMTP_GUESS = {
    "qq.com": {"smtp_host": "smtp.qq.com", "smtp_port": 465, "security": "ssl"},
    "163.com": {"smtp_host": "smtp.163.com", "smtp_port": 465, "security": "ssl"},
    "126.com": {"smtp_host": "smtp.126.com", "smtp_port": 465, "security": "ssl"},
    "outlook.com": {"smtp_host": "smtp-mail.outlook.com", "smtp_port": 587, "security": "starttls"},
    "gmail.com": {"smtp_host": "smtp.gmail.com", "smtp_port": 465, "security": "ssl"},
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "channel": "email",
    "push_hour": 8,
    "push_minute": 5,
    "schedule_enabled": True,
    "sections": ["dailystore", "kingdomstore"],
    "notify_on_failure": True,
    "domain": "",  # 绑定域名（向导写入，明文非机密；Caddy on-demand ask 与状态页自检用）
    "login_method": "qq",  # 最近一次扫码登录方式（M6，仅决定扫码页默认选中的 tab；apply_update 白名单不含它，扫码成功时由 login 模块直写）
    "email": {
        "smtp_host": "",
        "smtp_port": 465,
        "security": "ssl",
        "username": "",
        "password": "",
        "from_name": "无畏契约商店推送",
        "to": "",
    },
    "wecom": {
        "corpid": "",
        "agentid": "",
        "secret": "",
        "to_user": "@all",
        "msg_type": "image",
    },
}


def _read_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        logger.error("读取 %s 失败，使用默认值：%s", path, exc)
        return default


def _write_json(path: Path, payload: dict) -> None:
    paths.ensure_dirs()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(settings: dict) -> dict:
    """补齐缺失字段并纠正非法值，返回可直接使用的设置。"""
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # 深拷贝
    for key, value in settings.items():
        if key in ("email", "wecom"):
            if isinstance(value, dict):
                merged[key].update({k: v for k, v in value.items() if k in merged[key]})
        elif key in merged:
            merged[key] = value
    try:
        merged["push_hour"] = min(23, max(0, int(merged["push_hour"])))
    except (TypeError, ValueError):
        merged["push_hour"] = 8
    try:
        merged["push_minute"] = min(59, max(0, int(merged["push_minute"])))
    except (TypeError, ValueError):
        merged["push_minute"] = 5
    merged["schedule_enabled"] = bool(merged["schedule_enabled"])
    if merged["channel"] not in ("email", "wecom"):
        merged["channel"] = "email"
    valid_sections = {"dailystore", "kingdomstore"}
    merged["sections"] = [s for s in merged["sections"] if s in valid_sections] or list(
        DEFAULT_SETTINGS["sections"]
    )
    email_cfg = merged["email"]
    try:
        email_cfg["smtp_port"] = int(email_cfg["smtp_port"])
    except (TypeError, ValueError):
        email_cfg["smtp_port"] = 465
    if email_cfg["security"] not in ("ssl", "starttls", "none"):
        email_cfg["security"] = "ssl"
    return merged


def _apply_to_secret_fields(settings: dict, transform) -> dict:
    out = json.loads(json.dumps(settings))
    for field in SECRET_FIELDS:
        section, _, key = field.partition(".")
        if isinstance(out.get(section), dict) and isinstance(out[section].get(key), str):
            out[section][key] = transform(out[section][key])
    return out


def _decrypt_secrets(settings: dict) -> dict:
    return _apply_to_secret_fields(settings, crypto.decrypt_text)


def _encrypt_secrets(settings: dict) -> dict:
    return _apply_to_secret_fields(settings, crypto.encrypt_text)


def load_settings() -> dict:
    return _decrypt_secrets(normalize(_read_json(paths.SETTINGS_FILE, {})))


def save_settings(settings: dict) -> dict:
    merged = normalize(settings)
    _write_json(paths.SETTINGS_FILE, _encrypt_secrets(merged))
    return merged


def mask_settings(settings: dict) -> dict:
    """生成给界面用的副本：secret 置空并附 *_set 布尔。"""
    out = json.loads(json.dumps(settings))
    for field in SECRET_FIELDS:
        section, _, key = field.partition(".")
        if isinstance(out.get(section), dict):
            has_value = bool(out[section].get(key))
            out[section][key] = ""
            out[section][f"{key}_set"] = has_value
    return out


def apply_update(current: dict, updates: dict) -> dict:
    """合并界面提交：secret 传空表示保持不变；剔除 *_set 标记。"""
    merged = json.loads(json.dumps(current))
    for key, value in updates.items():
        if key in ("email", "wecom") and isinstance(value, dict):
            section = merged.setdefault(key, {})
            for sub_key, sub_value in value.items():
                if sub_key.endswith("_set"):
                    continue
                if sub_value == "" and f"{key}.{sub_key}" in SECRET_FIELDS:
                    continue  # 空值 = 保持现有 secret
                section[sub_key] = sub_value
        elif key in ("channel", "push_hour", "push_minute",
                     "schedule_enabled", "sections", "notify_on_failure"):
            merged[key] = value
    for key in ("email", "wecom"):
        merged.get(key, {}).pop("password_set", None)
        merged.get(key, {}).pop("secret_set", None)
    return merged


def channel_configured(settings: dict, name: str) -> bool:
    if name == "email":
        cfg = settings["email"]
        return bool(cfg["smtp_host"] and cfg["username"] and cfg["password"])
    if name == "wecom":
        cfg = settings["wecom"]
        return bool(cfg["corpid"] and cfg["agentid"] and cfg["secret"])
    return False


def email_configured(settings: dict) -> bool:
    """pipeline 回退逻辑使用的便捷判断（与个人版同名语义）。"""
    return channel_configured(settings, "email")


def validate(settings: dict) -> list[str]:
    """返回缺失/非法项提示列表；为空表示通过。"""
    problems: list[str] = []
    channel = settings.get("channel")
    email_cfg = settings.get("email", {})
    wecom_cfg = settings.get("wecom", {})
    if channel == "email":
        if not email_cfg.get("username"):
            problems.append("邮箱渠道：缺少邮箱账号")
        if not email_cfg.get("password"):
            problems.append("邮箱渠道：缺少授权码")
        if not email_cfg.get("to"):
            problems.append("邮箱渠道：缺少收件邮箱")
    elif channel == "wecom":
        if not wecom_cfg.get("corpid"):
            problems.append("企业微信渠道：缺少 corpid")
        if not wecom_cfg.get("agentid"):
            problems.append("企业微信渠道：缺少 agentid")
        if not wecom_cfg.get("secret"):
            problems.append("企业微信渠道：缺少 secret")
    else:
        problems.append(f"未知推送渠道：{channel}")
    if not settings.get("sections"):
        problems.append("至少选择一个商店分区")
    return problems


def load_state() -> dict:
    state = _read_json(paths.STATE_FILE, {})
    state.setdefault("last_push_date", "")
    state.setdefault("last_push_ok", None)
    state.setdefault("last_push_at", "")
    state.setdefault("last_channel", "")  # 首次推送前由 pipeline 写入，页面要兜底
    state.setdefault("last_summary", "")
    state.setdefault("last_error", "")
    return state


def update_state(**changes) -> dict:
    state = load_state()
    state.update(changes)
    _write_json(paths.STATE_FILE, state)
    return state


def guess_smtp(email_address: str) -> dict:
    """按邮箱域名猜 SMTP 参数；未知域名回退 smtp.<域名> + SSL。"""
    domain = email_address.rpartition("@")[2].strip().lower()
    if domain in SMTP_GUESS:
        return dict(SMTP_GUESS[domain])
    return {"smtp_host": f"smtp.{domain}" if domain else "", "smtp_port": 465, "security": "ssl"}


# ---- 绑定域名（M4.5：向导写入 settings.domain，供 Caddy on-demand ask 与状态页自检） ----

# 单段域名：字母数字开头结尾，中间可含连字符，≤63 字符（正则上限 63：1+61+1）
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_bound_domain(raw: str) -> str:
    """规范化并校验用户提交的绑定域名；非法抛 ValueError（M4.5 决策 6）。

    规则：trim → 小写 → 带 scheme/端口/路径时经 urlsplit 取 hostname → 去末尾点 →
    校验：至少 2 段、每段 [a-z0-9-] 且 ≤63、总长 ≤253、拒绝裸 IP
    （Let's Encrypt 不给裸 IP 发证书，直连 IP 用户本就不该绑定）。
    """
    text = (raw or "").strip().lower()
    if not text:
        raise ValueError("域名为空")
    if "//" not in text:
        text = f"http://{text}"  # 统一经 urlsplit 剥离端口/路径/凭据
    try:
        host = urlsplit(text).hostname or ""
    except ValueError as exc:
        raise ValueError(f"域名格式不正确：{exc}") from exc
    host = host.rstrip(".")
    if not host or "." not in host:
        raise ValueError("域名至少需要两段（如 push.example.com）")
    if len(host) > 253:
        raise ValueError("域名总长超过 253 字符")
    for label in host.split("."):
        if len(label) > 63 or not _DOMAIN_LABEL_RE.fullmatch(label):
            raise ValueError(f"域名段 {label!r} 非法")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("不能绑定裸 IP 地址（证书无法签发），请留空改用直连")
    return host


def seed_domain_from_env() -> bool:
    """旧部署迁移种子：serve 启动时若尚未绑定域名且环境变量 DOMAIN 非空，写入 settings（决策 4）。

    仅在真正发生写入时返回 True；种入后 .env 的 DOMAIN 即可移除。
    环境变量值非法时跳过并告警，不阻塞启动。
    """
    env_domain = os.getenv("DOMAIN", "").strip()
    if not env_domain:
        return False
    settings = load_settings()
    if settings.get("domain"):
        return False
    try:
        domain = normalize_bound_domain(env_domain)
    except ValueError as exc:
        logger.warning("忽略环境变量 DOMAIN=%r：非法域名（%s）", env_domain, exc)
        return False
    settings["domain"] = domain
    save_settings(settings)
    logger.info("已从环境变量 DOMAIN 迁移绑定域名：%s（.env 中该变量现在可以删除）", domain)
    return True
