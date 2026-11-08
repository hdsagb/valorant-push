"""服务器版账号存储测试。"""
from __future__ import annotations

import json
from pathlib import Path

from push_server import accounts


def test_add_and_all_roundtrip(data_dir: Path):
    accounts.add({"userId": "u-secret", "tid": "t-secret", "login_type": "qq"}, "qq", name="大号")
    items = accounts.all()
    assert len(items) == 1
    assert items[0]["user_id"] == "u-secret"
    assert items[0]["credentials"]["tid"] == "t-secret"
    assert items[0]["name"] == "大号"
    assert items[0]["login_type"] == "qq"
    assert items[0]["auth_expired"] is False


def test_credentials_encrypted_on_disk(data_dir: Path):
    accounts.add({"userId": "u-secret", "tid": "t-secret", "login_type": "qq"}, "qq")
    raw = json.loads((data_dir / "accounts.json").read_text(encoding="utf-8"))
    account = raw["accounts"][0]
    assert account["user_id"].startswith("enc:v1:")
    for value in account["credentials"].values():
        assert str(value).startswith("enc:v1:")
    assert "u-secret" not in json.dumps(raw)  # 明文不落盘


def test_add_same_user_updates_existing(data_dir: Path):
    accounts.add({"userId": "u1", "tid": "old"}, "qq")
    accounts.update_same_user("u1", {"userId": "u1", "tid": "new"}, "qq")
    items = accounts.all()
    assert len(items) == 1
    assert items[0]["credentials"]["tid"] == "new"
    assert items[0]["auth_expired"] is False


def test_rename_and_remove(data_dir: Path):
    account = accounts.add({"userId": "u1", "tid": "t"}, "qq")
    accounts.rename(account["id"], "小号")
    assert accounts.all()[0]["name"] == "小号"
    accounts.remove(account["id"])
    assert accounts.count() == 0


def test_mark_expired_and_all_expired(data_dir: Path):
    account = accounts.add({"userId": "u1", "tid": "t"}, "qq")
    assert accounts.all_expired() is False
    accounts.mark_expired(account["id"], True)
    assert accounts.all()[0]["auth_expired"] is True
    assert accounts.all_expired() is True
    accounts.mark_expired(account["id"], False)
    assert accounts.all()[0]["auth_expired"] is False


def test_mask_user_id(data_dir: Path):
    assert accounts.mask_user_id("JA-09db2000480f479a") == "***479a"
    assert accounts.mask_user_id("short") == "***hort"


def test_all_expired_false_when_no_accounts(data_dir: Path):
    assert accounts.all_expired() is False


def test_max_accounts_limit(data_dir: Path):
    for i in range(10):
        accounts.add({"userId": f"u{i}", "tid": "t"}, "qq")
    try:
        accounts.add({"userId": "u10", "tid": "t"}, "qq")
        raise AssertionError("应拒绝第 11 个账号")
    except ValueError as exc:
        assert "10" in str(exc)
    assert accounts.count() == 10


def test_no_legacy_migration(data_dir: Path):
    (data_dir / "config.json").write_text(
        json.dumps({"userId": "legacy", "tid": "t"}), encoding="utf-8"
    )
    assert accounts.count() == 0  # 服务器版不做个人版旧数据迁移


def test_summary_masks_user_id(data_dir: Path):
    accounts.add({"userId": "JA-09db2000480f479a", "tid": "t"}, "qq", name="大号")
    rows = accounts.summary()
    assert rows[0]["masked_id"] == "***479a"
    assert "credentials" not in rows[0]
