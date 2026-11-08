"""服务器版测试基座：隔离数据目录与主密钥。"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]  # server/
sys.path.insert(0, str(SERVER_DIR))

from push_server import crypto, paths  # noqa: E402

# 32 字节固定测试主密钥（base64）。任何测试进程内加密结果稳定、可复现。
TEST_MASTER_KEY = base64.b64encode(bytes(range(32))).decode("ascii")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据目录指到临时目录，并固定环境变量主密钥。"""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("VAL_SERVER_MASTER_KEY", TEST_MASTER_KEY)
    crypto.reset_key_cache()
    monkeypatch.setattr(paths, "DATA_DIR", data)
    monkeypatch.setattr(paths, "TEMP_DIR", data / "temp")
    monkeypatch.setattr(paths, "OUT_DIR", data / "out")
    monkeypatch.setattr(paths, "SETTINGS_FILE", data / "settings.json")
    monkeypatch.setattr(paths, "STATE_FILE", data / "state.json")
    monkeypatch.setattr(paths, "ACCOUNTS_FILE", data / "accounts.json")
    monkeypatch.setattr(paths, "AUTH_FILE", data / "auth.json")
    monkeypatch.setattr(paths, "LOG_FILE", data / "app.log")
    yield data
    crypto.reset_key_cache()
