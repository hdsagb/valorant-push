"""测试公共配置：把数据目录指向临时目录，避免碰到真实 data/。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import paths  # noqa: E402


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """为每个用例准备一个隔离的数据目录，并替换 paths 里的路径常量。"""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", data)
    monkeypatch.setattr(paths, "TEMP_DIR", data / "temp")
    monkeypatch.setattr(paths, "OUT_DIR", data / "out")
    monkeypatch.setattr(paths, "ACCOUNTS_FILE", data / "accounts.json")
    monkeypatch.setattr(paths, "CREDENTIALS_FILE", data / "config.json")
    monkeypatch.setattr(paths, "SETTINGS_FILE", data / "settings.json")
    monkeypatch.setattr(paths, "STATE_FILE", data / "state.json")
    monkeypatch.setattr(paths, "LOG_FILE", data / "app.log")
    return data
