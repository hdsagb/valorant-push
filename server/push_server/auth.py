"""Web 管理界面鉴权：访问密码、会话、登录限速（服务器版全新编写，非拷贝）。

安全模型（《服务器版开发规划报告》§5.2）：
- 访问密码：首次访问强制设置；PBKDF2-HMAC-SHA256(600k) + 16B 随机盐，存 data/auth.json
- 一次性设置码：未初始化时随启动生成并打印到日志，设置密码必须携带——
  防止公网上先到者抢占设置密码（§0 决策 1）
- 会话：随机 32B token，HttpOnly + SameSite=Strict cookie，7 天滑动过期；仅存内存
- 登录限速：连续失败计数，达阈值锁定，锁定期内再失败剩余时长翻倍（R3 策略）
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime

from . import paths

ALGO = "pbkdf2-sha256"
ITERATIONS = 600_000
KEY_LEN = 32
SALT_LEN = 16
MIN_PASSWORD = 8
MAX_PASSWORD = 128

SESSION_COOKIE = "vps_session"
SESSION_TTL = 7 * 24 * 3600      # 滑动过期：7 天无活动即失效
SESSION_REISSUE = 24 * 3600      # cookie 距签发超 24h 重签（滑动续期）

LOCK_THRESHOLD = 5               # 连续失败 5 次 → 锁定
LOCK_SECONDS = 15 * 60           # 锁定 15 分钟
FAIL_DELAY = 0.5                 # 每次失败固定延迟，钝化时序探测

SETUP_CODE_LEN = 8

_auth: dict | None = None        # auth.json 的内存缓存（未初始化时含 setup_code）
_sessions: dict[str, dict] = {}
_fail_count = 0
_lock_until = 0.0


class AuthError(ValueError):
    """口令/设置码不合法或状态不允许。"""


def reset_state() -> None:
    """清空内存态（测试用；生产进程重启等价于此）。"""
    global _auth, _fail_count, _lock_until
    _auth = None
    _fail_count, _lock_until = 0, 0.0
    _sessions.clear()


def _load() -> dict:
    global _auth
    if _auth is not None:
        return _auth
    if paths.AUTH_FILE.exists():
        _auth = json.loads(paths.AUTH_FILE.read_text(encoding="utf-8"))
    else:
        _auth = {}
    return _auth


def _save() -> None:
    paths.ensure_dirs()
    paths.AUTH_FILE.write_text(
        json.dumps(_auth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def is_initialized() -> bool:
    return "hash" in _load()


def ensure_setup_code() -> str:
    """未初始化时生成（幂等）一次性设置码并打日志；已初始化则报错。"""
    state = _load()
    if is_initialized():
        raise AuthError("访问密码已设置，无需初始化")
    code = state.get("setup_code")
    if not code:
        code = secrets.token_hex(SETUP_CODE_LEN // 2)
        state["setup_code"] = code
        state.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        _save()
        # 设置码只通过日志传达；级别 warning 保证控制台与文件日志都可见
        import logging
        logging.getLogger(__name__).warning(
            "首次启动：请在管理界面输入一次性设置码完成初始化：%s", code
        )
    return code


def set_password(password: str, setup_code: str) -> None:
    global _auth
    state = _load()
    if is_initialized():
        raise AuthError("访问密码已设置")
    if state.get("setup_code") != str(setup_code or ""):
        raise AuthError("设置码不正确，请查看服务器日志")
    password = str(password or "")
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AuthError(f"口令长度需在 {MIN_PASSWORD}-{MAX_PASSWORD} 位之间")
    salt = secrets.token_bytes(SALT_LEN)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, dklen=KEY_LEN)
    _auth = {
        "algo": ALGO,
        "iterations": ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save()


def verify_password(password: str) -> bool:
    state = _load()
    if not is_initialized():
        return False
    salt = base64.b64decode(state["salt"])
    expected = base64.b64decode(state["hash"])
    digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, state["iterations"], dklen=KEY_LEN)
    return secrets.compare_digest(digest, expected)


# ---------- 会话 ----------

def create_session() -> tuple[str, str]:
    """新建会话，返回 (token, csrf)。token 走 cookie，csrf 注入页面。"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    _sessions[token] = {"csrf": secrets.token_urlsafe(32), "issued_at": now, "last_seen": now}
    return token, _sessions[token]["csrf"]


def get_session(token: str) -> dict | None:
    session = _sessions.get(token)
    if session is None:
        return None
    now = time.time()
    if now - session["last_seen"] > SESSION_TTL:
        _sessions.pop(token, None)
        return None
    session["last_seen"] = now
    return session


def drop_session(token: str) -> None:
    _sessions.pop(token, None)


def should_reissue(session: dict) -> bool:
    """距 cookie 签发超 24h 则重签（滑动续期，防止长期活跃用户被登出）。"""
    return time.time() - session["issued_at"] > SESSION_REISSUE


def session_cookie_kwargs(secure: bool, max_age: int = SESSION_TTL) -> dict:
    return {
        "max_age": max_age,
        "path": "/",
        "httponly": True,
        "samesite": "Strict",
        "secure": secure,
    }


# ---------- 登录限速 ----------

def register_failure() -> None:
    global _fail_count, _lock_until
    now = time.time()
    if now < _lock_until:
        _lock_until = now + (_lock_until - now) * 2  # 锁定期内再失败：剩余时长翻倍
        return
    _fail_count += 1
    if _fail_count >= LOCK_THRESHOLD:
        _lock_until = now + LOCK_SECONDS
        _fail_count = 0


def lock_remaining() -> float:
    return max(0.0, _lock_until - time.time())


def reset_failures() -> None:
    """成功登录即清零：失败计数与锁定同时清除（§0 决策 6）。"""
    global _fail_count, _lock_until
    _fail_count, _lock_until = 0, 0.0
