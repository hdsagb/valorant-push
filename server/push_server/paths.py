"""路径、日志与基础运行环境（服务器版）。

- 数据目录：环境变量 VAL_SERVER_DATA_DIR，缺省为仓库 server/data/（容器内设为 /data）
- 目录加固：POSIX 下 chmod 0700（替代个人版 Windows icacls）
- 敏感字段加密主密钥见 crypto.py
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME = "无畏契约商店推送 · 服务器版"

# 时区固定为北京时间：目标用户只玩国服
TIMEZONE_NAME = "Asia/Shanghai"

# 扫码等待时长（秒）。服务器版 v1 不扫码（凭证由个人版导出导入），
# 保留该参数以与个人版 vendored 脚本的调用方式保持一致。
LOGIN_TIMEOUT = 300

# 商店接口单次请求上限（秒）。参考实现在网络异常时会无限重试，这里必须设上限。
STORE_TIMEOUT = 120


def app_root() -> Path:
    """仓库根目录（server/ 的上一级）。"""
    return Path(__file__).resolve().parents[2]


ROOT = app_root()
DATA_DIR = Path(os.getenv("VAL_SERVER_DATA_DIR") or (ROOT / "server" / "data")).expanduser().resolve()
TEMP_DIR = DATA_DIR / "temp"
OUT_DIR = DATA_DIR / "out"
SETTINGS_FILE = DATA_DIR / "settings.json"
STATE_FILE = DATA_DIR / "state.json"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
LOG_FILE = DATA_DIR / "app.log"

_dirs_ready: set[str] = set()


def ensure_dirs() -> None:
    for path in (DATA_DIR, TEMP_DIR, OUT_DIR):
        path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix" and str(DATA_DIR) not in _dirs_ready:
        _dirs_ready.add(str(DATA_DIR))
        try:
            os.chmod(DATA_DIR, 0o700)
        except OSError as exc:
            logger.warning("收紧数据目录权限失败（不影响使用）：%s", exc)


def setup_logging(debug: bool = False) -> None:
    """文件日志（滚动 2MB × 3 份）+ 控制台输出。"""
    ensure_dirs()
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    datefmt = "%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    for handler in handlers:
        handler.setFormatter(logging.Formatter(fmt, datefmt))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers, force=True)
