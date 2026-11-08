"""服务器版配置模块测试。"""
from __future__ import annotations

import json
from pathlib import Path

from push_server import config


def test_normalize_coerces_types():
    settings = config.normalize({
        "channel": "wecom",
        "push_hour": "30",
        "push_minute": "x",
        "sections": ["dailystore", "bad"],
        "email": {"smtp_port": "465", "security": "tls"},
    })
    assert settings["channel"] == "wecom"
    assert settings["push_hour"] == 23
    assert settings["push_minute"] == 5
    assert settings["sections"] == ["dailystore"]
    assert settings["email"]["smtp_port"] == 465
    assert settings["email"]["security"] == "ssl"
    assert "login_method" not in settings  # 服务器版无扫码登录配置项


def test_settings_secrets_encrypted_on_disk(data_dir: Path):
    config.save_settings({
        "email": {"username": "me@qq.com", "password": "授权码",
                  "smtp_host": "smtp.qq.com", "to": "me@qq.com"},
    })
    raw = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert raw["email"]["password"].startswith("enc:v1:")
    loaded = config.load_settings()
    assert loaded["email"]["password"] == "授权码"
    assert loaded["email"]["username"] == "me@qq.com"


def test_mask_settings_hides_secret(data_dir: Path):
    config.save_settings({"email": {"username": "me@qq.com", "password": "授权码"}})
    view = config.mask_settings(config.load_settings())
    assert view["email"]["password"] == ""
    assert view["email"]["password_set"] is True
    assert view["wecom"]["secret"] == ""
    assert view["wecom"]["secret_set"] is False


def test_apply_update_keeps_secret_when_blank(data_dir: Path):
    current = config.load_settings()
    current["email"]["password"] = "旧授权码"
    updated = config.apply_update(current, {"email": {"username": "new@qq.com", "password": ""}})
    assert updated["email"]["username"] == "new@qq.com"
    assert updated["email"]["password"] == "旧授权码"
    assert "password_set" not in updated["email"]


def test_validate_reports_missing_email_fields():
    problems = config.validate(config.normalize({"channel": "email"}))
    assert any("邮箱账号" in p for p in problems)
    assert any("授权码" in p for p in problems)
    assert any("收件邮箱" in p for p in problems)


def test_validate_reports_missing_wecom_fields():
    problems = config.validate(config.normalize({"channel": "wecom"}))
    assert any("corpid" in p for p in problems)


def test_state_roundtrip(data_dir: Path):
    config.update_state(last_push_date="2026-11-08", last_push_ok=True)
    state = config.load_state()
    assert state["last_push_date"] == "2026-11-08"
    assert state["last_push_ok"] is True
    assert "last_push_at" in state  # 默认字段仍在


def test_email_configured_alias(data_dir: Path):
    settings = config.normalize({})
    assert config.email_configured(settings) is False
    settings["email"].update({"smtp_host": "smtp.qq.com", "username": "a@qq.com", "password": "k"})
    assert config.email_configured(settings) is True


def test_guess_smtp_known_and_unknown_domain():
    assert config.guess_smtp("a@163.com")["smtp_host"] == "smtp.163.com"
    unknown = config.guess_smtp("a@example.org")
    assert unknown == {"smtp_host": "smtp.example.org", "smtp_port": 465, "security": "ssl"}
