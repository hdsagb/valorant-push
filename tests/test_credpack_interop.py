"""双端契约互操作测试：个人版导出 → 服务器版解析/导入。

这是全仓库唯一同时 import app/ 与 push_server/ 的文件——
仅用于守护「凭证包契约 v1」两端口径一致（规划报告 §3 唯一刻意耦合点），
不构成两版运行时依赖；任一端单独修改格式时本测试失败，提醒同步。

运行：python -m pytest tests/test_credpack_interop.py -v
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app import accounts as app_accounts  # noqa: E402
from app import credpack as app_credpack  # noqa: E402
from push_server import accounts as server_accounts  # noqa: E402
from push_server import credpack as server_credpack  # noqa: E402
from push_server import crypto as server_crypto  # noqa: E402
from push_server import paths as server_paths  # noqa: E402

PASSWORD = "口令-12345678"


def test_export_then_import_end_to_end(data_dir, tmp_path, monkeypatch):
    # 个人版侧：tests/conftest.py 的 data_dir fixture 已隔离 app 数据目录
    app_accounts.add({"userId": "u-0001", "tid": "t-0001", "openid": "o1"}, "qq")
    app_accounts.add({"userId": "u-0002", "tid": "t-0002"}, "wechat")
    raw = app_credpack.build_pack(app_credpack.payload_accounts(app_accounts.all()), PASSWORD)

    # 服务器版侧：隔离数据目录与主密钥（对齐 server/tests/conftest.py 的做法）
    server_data = tmp_path / "server-data"
    server_data.mkdir()
    monkeypatch.setenv("VAL_SERVER_MASTER_KEY", base64.b64encode(bytes(range(32))).decode("ascii"))
    server_crypto.reset_key_cache()
    monkeypatch.setattr(server_paths, "DATA_DIR", server_data)
    monkeypatch.setattr(server_paths, "TEMP_DIR", server_data / "temp")
    monkeypatch.setattr(server_paths, "OUT_DIR", server_data / "out")
    monkeypatch.setattr(server_paths, "SETTINGS_FILE", server_data / "settings.json")
    monkeypatch.setattr(server_paths, "STATE_FILE", server_data / "state.json")
    monkeypatch.setattr(server_paths, "ACCOUNTS_FILE", server_data / "accounts.json")
    monkeypatch.setattr(server_paths, "LOG_FILE", server_data / "app.log")
    try:
        payload = server_credpack.parse_pack(raw, PASSWORD)
        result = server_credpack.import_accounts(payload)
        # 错误口令在服务器端被拒（统一口径）
        with pytest.raises(server_credpack.CredpackError):
            server_credpack.parse_pack(raw, "错误的口令-999999")
    finally:
        server_crypto.reset_key_cache()

    assert result["added"] == 2 and result["updated"] == 0 and result["skipped"] == []
    imported = {row["user_id"]: row for row in server_accounts.all()}
    assert imported["u-0001"]["credentials"]["tid"] == "t-0001"
    assert imported["u-0002"]["login_type"] == "wechat"
    assert imported["u-0002"]["name"] == "账号2"  # 个人版默认名跨端延续
    disk = server_paths.ACCOUNTS_FILE.read_text(encoding="utf-8")
    assert "t-0001" not in disk and "enc:v1:" in disk
