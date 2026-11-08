"""CLI 集成测试：subprocess 起真实进程，验证 --version / --push-now / --import-credentials。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import SERVER_DIR, TEST_MASTER_KEY
from push_server import accounts, paths
from test_credpack import PASSWORD, make_pack, sample_payload


def _run(args: list[str], data_dir: Path, input: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ,
           "VAL_SERVER_DATA_DIR": str(data_dir),
           "VAL_SERVER_MASTER_KEY": TEST_MASTER_KEY,
           "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-m", "push_server", *args],
                          cwd=str(SERVER_DIR), env=env, capture_output=True,
                          input=input, text=True, encoding="utf-8", timeout=120)


def test_version_flag(tmp_path: Path):
    result = _run(["--version"], tmp_path)
    assert result.returncode == 0
    assert "0.1.0" in result.stdout


def test_push_now_without_accounts_is_clean_skip(tmp_path: Path):
    result = _run(["--push-now"], tmp_path)
    assert result.returncode == 0
    assert "尚未绑定账号" in (result.stdout + result.stderr)


# ---- --import-credentials ----

def _write_pack(tmp_path: Path, payload: dict, password: str = PASSWORD) -> Path:
    pack = tmp_path / "valorant-push-credentials-20261108.vpcred"
    pack.write_bytes(make_pack(payload, password))
    return pack


def test_cli_import_success(data_dir, tmp_path: Path):
    pack = _write_pack(tmp_path, sample_payload())
    result = _run(["--import-credentials", str(pack)], data_dir, input=f"{PASSWORD}\n")
    out = result.stdout + result.stderr
    assert result.returncode == 0
    assert "新增 1 个" in out
    assert "请在个人版停止" in out          # R6 提示句式
    assert "删除本地导出" in out            # 删除提醒（规划报告 §5.3）
    assert accounts.count() == 1
    assert "enc:v1:" in paths.ACCOUNTS_FILE.read_text(encoding="utf-8")


def test_cli_import_wrong_password(data_dir, tmp_path: Path):
    pack = _write_pack(tmp_path, sample_payload())
    result = _run(["--import-credentials", str(pack)], data_dir, input="错误的口令-999999\n")
    assert result.returncode == 1
    assert "口令错误或凭证包已损坏" in (result.stdout + result.stderr)


def test_cli_import_missing_file(data_dir, tmp_path: Path):
    result = _run(["--import-credentials", str(tmp_path / "nope.vpcred")], data_dir,
                  input=f"{PASSWORD}\n")
    assert result.returncode == 1


def test_cli_import_empty_password(data_dir, tmp_path: Path):
    pack = _write_pack(tmp_path, sample_payload())
    result = _run(["--import-credentials", str(pack)], data_dir, input="\n")
    assert result.returncode == 1


def test_cli_import_zero_imported_exit_2(data_dir, tmp_path: Path):
    payload = {"exported_at": "x", "accounts": []}
    pack = _write_pack(tmp_path, payload)
    result = _run(["--import-credentials", str(pack)], data_dir, input=f"{PASSWORD}\n")
    assert result.returncode == 2


def test_cli_import_all_invalid_exit_2(data_dir, tmp_path: Path):
    payload = {
        "exported_at": "x",
        "accounts": [{"name": "残缺", "user_id": "u-9", "login_type": "qq", "credentials": {}}],
    }
    pack = _write_pack(tmp_path, payload)
    result = _run(["--import-credentials", str(pack)], data_dir, input=f"{PASSWORD}\n")
    assert result.returncode == 2
