"""auth.py 测试：口令哈希 / 设置码 / 会话 / 限速。运行：python -m pytest server/tests/test_auth.py -v"""
from __future__ import annotations

import pytest

from push_server import auth


@pytest.fixture(autouse=True)
def _reset_auth_state():
    auth.reset_state()
    yield
    auth.reset_state()


PASSWORD = "口令-12345678"


def test_not_initialized_writes_setup_code(data_dir, caplog):
    assert auth.is_initialized() is False
    with caplog.at_level("WARNING"):
        code = auth.ensure_setup_code()
    assert len(code) == 8 and all(c in "0123456789abcdef" for c in code)
    assert code in caplog.text  # 设置码必须出现在日志（用户唯一的获取途径）
    # 二次调用返回同一码（幂等），不重复生成
    assert auth.ensure_setup_code() == code


def test_set_password_requires_setup_code(data_dir):
    auth.ensure_setup_code()
    with pytest.raises(auth.AuthError):
        auth.set_password(PASSWORD, "00000000")  # 错误设置码
    with pytest.raises(auth.AuthError):
        auth.set_password(PASSWORD, "")           # 缺失
    assert auth.is_initialized() is False


def test_set_password_and_verify(data_dir):
    code = auth.ensure_setup_code()
    auth.set_password(PASSWORD, code)
    assert auth.is_initialized() is True
    assert auth.verify_password(PASSWORD) is True
    assert auth.verify_password("错的口令-999999") is False
    assert auth.verify_password("") is False


def test_set_password_policy(data_dir):
    code = auth.ensure_setup_code()
    with pytest.raises(auth.AuthError):
        auth.set_password("1234567", code)    # 过短
    with pytest.raises(auth.AuthError):
        auth.set_password("x" * 129, code)    # 过长


def test_setup_code_invalidated_after_init(data_dir):
    code = auth.ensure_setup_code()
    auth.set_password(PASSWORD, code)
    with pytest.raises(auth.AuthError):
        auth.set_password("另一个口令-123456", code)  # 已初始化后不可重复设置


def test_password_persisted_and_survives_reload(data_dir):
    code = auth.ensure_setup_code()
    auth.set_password(PASSWORD, code)
    auth.reset_state()  # 模拟进程重启（内存态清零）
    assert auth.is_initialized() is True
    assert auth.verify_password(PASSWORD) is True


def test_hash_format(data_dir):
    code = auth.ensure_setup_code()
    auth.set_password(PASSWORD, code)
    import json
    record = json.loads(paths_auth_record())
    assert record["algo"] == "pbkdf2-sha256"
    assert record["iterations"] == 600_000


def paths_auth_record() -> bytes:
    from push_server import paths
    return paths.AUTH_FILE.read_text(encoding="utf-8")
