"""CLI 集成测试：subprocess 起真实进程，验证 --version 与 --push-now 空库行为。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import SERVER_DIR, TEST_MASTER_KEY


def _run(args: list[str], data_dir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ,
           "VAL_SERVER_DATA_DIR": str(data_dir),
           "VAL_SERVER_MASTER_KEY": TEST_MASTER_KEY,
           "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-m", "push_server", *args],
                          cwd=str(SERVER_DIR), env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


def test_version_flag(tmp_path: Path):
    result = _run(["--version"], tmp_path)
    assert result.returncode == 0
    assert "0.1.0" in result.stdout


def test_push_now_without_accounts_is_clean_skip(tmp_path: Path):
    result = _run(["--push-now"], tmp_path)
    assert result.returncode == 0
    assert "尚未绑定账号" in (result.stdout + result.stderr)
