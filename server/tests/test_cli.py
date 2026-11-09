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
    assert "0.4.1" in result.stdout


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


# ---------- serve（常驻进程：只测绑定解析与装配，真实起服务的冒烟放 Task 7 手工验收） ----------

def test_resolve_web_bind_defaults(data_dir, monkeypatch):
    monkeypatch.delenv("VAL_SERVER_WEB_HOST", raising=False)
    monkeypatch.delenv("VAL_SERVER_WEB_PORT", raising=False)
    from push_server.__main__ import resolve_web_bind
    assert resolve_web_bind(None, None) == ("0.0.0.0", 8787)


def test_resolve_web_bind_env_override(data_dir, monkeypatch):
    monkeypatch.setenv("VAL_SERVER_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("VAL_SERVER_WEB_PORT", "9999")
    from push_server.__main__ import resolve_web_bind
    assert resolve_web_bind(None, None) == ("127.0.0.1", 9999)


def test_resolve_web_bind_args_beat_env(data_dir, monkeypatch):
    monkeypatch.setenv("VAL_SERVER_WEB_PORT", "9999")
    from push_server.__main__ import resolve_web_bind
    assert resolve_web_bind(None, 7777) == ("0.0.0.0", 7777)


def test_main_no_args_invokes_serve(data_dir, monkeypatch):
    import push_server.__main__ as cli

    captured = {}

    async def fake_serve(host, port):
        captured["host"], captured["port"] = host, port

    monkeypatch.setattr(cli, "_serve_async", fake_serve)
    assert cli.main([]) == 0
    assert captured == {"host": "0.0.0.0", "port": 8787}


def test_version_bumped():
    from push_server import __version__
    assert __version__ == "0.4.1"
