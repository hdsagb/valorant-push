"""服务器版路径模块测试。"""
from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

from conftest import SERVER_DIR
from push_server import paths


def test_ensure_dirs_creates_layout(data_dir: Path):
    paths.ensure_dirs()
    assert paths.TEMP_DIR.is_dir()
    assert paths.OUT_DIR.is_dir()
    assert paths.DATA_DIR == data_dir


def test_harden_permissions(data_dir: Path):
    paths.ensure_dirs()
    if os.name == "posix":
        assert stat.S_IMODE(paths.DATA_DIR.stat().st_mode) & 0o777 == 0o700
    else:
        # Windows 上跳过 chmod，目录存在且不报错即可
        assert paths.DATA_DIR.is_dir()


def test_data_dir_env_override(tmp_path: Path):
    target = tmp_path / "elsewhere"
    env = {**os.environ, "VAL_SERVER_DATA_DIR": str(target)}
    code = "from push_server import paths; print(paths.DATA_DIR)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(SERVER_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == target


def test_setup_logging_writes_file(data_dir: Path):
    paths.setup_logging()
    logging.getLogger("test_paths").info("日志自检")
    logging.shutdown()
    assert paths.LOG_FILE.exists()
    assert paths.LOG_FILE.stat().st_size > 0
