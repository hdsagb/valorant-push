"""路径、日志与基础运行环境。

个人版是纯本地程序：所有数据都放在程序目录下的 data/ 中，方便整个文件夹拷走即用。
也可用环境变量 VAL_PUSH_DATA_DIR 指定数据目录。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME = "无畏契约商店推送"
VERSION = "1.0.0"

# 时区固定为北京时间：目标用户只玩国服
TIMEZONE_NAME = "Asia/Shanghai"

# 扫码等待时长（秒）
LOGIN_TIMEOUT = 300

# 商店接口单次请求上限（秒）。参考实现在网络异常时会无限重试，这里必须设上限。
STORE_TIMEOUT = 120


def app_root() -> Path:
    """程序根目录：打包后是 exe 所在目录，开发时是项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = app_root()
DATA_DIR = Path(os.getenv("VAL_PUSH_DATA_DIR") or (ROOT / "data")).expanduser().resolve()
TEMP_DIR = DATA_DIR / "temp"
OUT_DIR = DATA_DIR / "out"
SETTINGS_FILE = DATA_DIR / "settings.json"
STATE_FILE = DATA_DIR / "state.json"
# 多账号凭证（app/accounts.py 维护）
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
# 旧版单账号凭证，仅用于首次启动时迁移
CREDENTIALS_FILE = DATA_DIR / "config.json"
LOG_FILE = DATA_DIR / "app.log"


def ensure_dirs() -> None:
    for path in (DATA_DIR, TEMP_DIR, OUT_DIR):
        path.mkdir(parents=True, exist_ok=True)
    _harden_acl(DATA_DIR)


_acl_hardened: set[str] = set()


def _harden_acl(directory: Path) -> None:
    """把数据目录权限收紧到仅当前 Windows 用户（去掉继承的其他用户/Everyone）。"""
    key = str(directory)
    if os.name != "nt" or key in _acl_hardened:
        return
    _acl_hardened.add(key)
    user = os.getenv("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", key, "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F", "/C", "/Q"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("收紧数据目录权限失败（不影响使用）：%s", exc)


def setup_logging(debug: bool = False) -> None:
    """日志写文件（滚动）+ 控制台。打包成无控制台 exe 后只看文件即可。"""
    ensure_dirs()
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return

    root.setLevel(level)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
