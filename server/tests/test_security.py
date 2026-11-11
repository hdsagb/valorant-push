"""安全自查：日志与状态文件不得出现明文凭证（M5 安全清单自动化部分）。

人工部分（R8 九项部署机清单）见 M5 计划 Task 3 / server/README.md。

注：settings.json 中的 domain 字段为明文非机密（M4.5 决策 3，域名本就公开），
泄漏扫描断言只针对 SECRETS 中的凭证值，不涉及 domain。
"""
import asyncio
import json

import pytest

from push_server import config, notify, paths

SECRETS = {"email_pw": "SECRETPW123", "wecom_secret": "WECOMSECRET"}


@pytest.fixture()
def secret_settings(data_dir):
    """写入含已知明文 secret 的真实 settings.json（email 渠道）。"""
    settings = config.load_settings()
    settings["channel"] = "email"
    settings["email"].update(
        {"smtp_host": "smtp.example.com", "username": "u@example.com",
         "password": SECRETS["email_pw"]}
    )
    settings["wecom"].update({"secret": SECRETS["wecom_secret"]})
    config.save_settings(settings)
    return settings


def test_logs_and_state_have_no_plaintext_secrets(data_dir, secret_settings, monkeypatch):
    paths.setup_logging()  # 日志写入 data_dir/app.log（M1 行为，测试目录隔离）

    async def fail(settings, message):
        raise RuntimeError("send down")  # 触发 notify 的 warning 路径
    monkeypatch.setattr(notify.email_channel, "send", fail)
    monkeypatch.setattr(notify.wecom_channel, "send", fail)

    asyncio.run(notify.notify_push_failure(secret_settings, "boom", 3))
    # 模拟 scheduler 失败后写 state（last_error 场景）
    config.update_state(last_push_ok=False, last_error="boom")

    log_text = paths.LOG_FILE.read_text(encoding="utf-8", errors="ignore")
    state_text = paths.STATE_FILE.read_text(encoding="utf-8")
    settings_text = paths.SETTINGS_FILE.read_text(encoding="utf-8")
    for name, value in SECRETS.items():
        assert value not in log_text, f"{name} 泄漏进日志"
        assert value not in state_text, f"{name} 泄漏进 state"
    # settings.json 中 secret 必须为加密格式（enc:v1: 前缀，M1 crypto 行为）
    stored = json.loads(settings_text)
    assert stored["email"]["password"].startswith("enc:v1:")
    assert stored["wecom"]["secret"].startswith("enc:v1:")
