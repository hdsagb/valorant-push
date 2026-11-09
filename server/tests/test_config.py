"""服务器版配置模块测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

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


# ---- 绑定域名（M4.5） ----


def test_old_settings_without_domain_key(data_dir: Path):
    # 旧格式 settings 文件（无 domain 键）load 后自动补空串，无需迁移
    (data_dir / "settings.json").write_text(
        json.dumps({"channel": "wecom", "push_hour": 9}), encoding="utf-8"
    )
    settings = config.load_settings()
    assert settings["channel"] == "wecom"
    assert settings["push_hour"] == 9
    assert settings["domain"] == ""


def test_normalize_bound_domain_valid_forms():
    assert config.normalize_bound_domain(" https://Shop.Example.com/ ") == "shop.example.com"
    assert config.normalize_bound_domain("PUSH.Example.COM.") == "push.example.com"
    assert config.normalize_bound_domain("push.example.com:8443") == "push.example.com"
    assert config.normalize_bound_domain("a-b.example.cn") == "a-b.example.cn"


def test_normalize_bound_domain_invalid_forms():
    bad_inputs = [
        "", "   ",  # 空
        "192.168.1.1",  # 裸 IPv4
        "localhost",  # 单段
        "my_shop.example.com",  # 下划线
        "-bad.example.com",  # 段以连字符开头
        "push..example.com",  # 空段
        "x" * 64 + ".example.com",  # 单段超 63
        "a." + "b" * 250 + ".com",  # 总长超 253
        "推送.example.com",  # 非 ASCII
    ]
    for bad in bad_inputs:
        with pytest.raises(ValueError):
            config.normalize_bound_domain(bad)


def test_seed_domain_from_env_writes_when_unbound(data_dir: Path, monkeypatch):
    monkeypatch.setenv("DOMAIN", "Seed.Example.com")
    assert config.seed_domain_from_env() is True
    assert config.load_settings()["domain"] == "seed.example.com"
    # 幂等：已设置后不再覆盖
    assert config.seed_domain_from_env() is False
    assert config.load_settings()["domain"] == "seed.example.com"


def test_seed_domain_from_env_noop_without_env(data_dir: Path, monkeypatch):
    monkeypatch.delenv("DOMAIN", raising=False)
    assert config.seed_domain_from_env() is False


def test_seed_domain_from_env_keeps_existing_binding(data_dir: Path, monkeypatch):
    config.save_settings({"domain": "bound.example.com"})
    monkeypatch.setenv("DOMAIN", "other.example.com")
    assert config.seed_domain_from_env() is False
    assert config.load_settings()["domain"] == "bound.example.com"


def test_seed_domain_from_env_skips_invalid(data_dir: Path, monkeypatch):
    monkeypatch.setenv("DOMAIN", "192.168.1.1")
    assert config.seed_domain_from_env() is False
    assert config.load_settings()["domain"] == ""
