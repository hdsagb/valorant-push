"""账号管理（app/accounts.py）的单元测试。

覆盖：旧凭证迁移、绑定额度、重复账号更新、改名、解绑、失效标记、脱敏展示。
运行：python -m pytest
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import accounts


def creds(user_id: str, tid: str = "tid-1") -> dict:
    return {"userId": user_id, "tid": tid, "login_type": "qq"}


def read_store(data_dir: Path) -> dict:
    return json.loads((data_dir / "accounts.json").read_text(encoding="utf-8"))


# ---------- 脱敏 ----------


def test_mask_user_id_keeps_short_id():
    assert accounts.mask_user_id("abc") == "abc"
    assert accounts.mask_user_id("12345678") == "12345678"
    assert accounts.mask_user_id("") == ""
    assert accounts.mask_user_id("JA-09db2000480f479a-b8a3") == "JA-***8a3"


# ---------- 新增账号 ----------


def test_add_creates_account_with_default_name(data_dir: Path):
    account, updated = accounts.add(creds("user-0001"), "wechat")

    assert updated is False
    assert account["name"] == "账号1"
    assert account["user_id"] == "user-0001"
    assert account["login_type"] == "wechat"
    assert account["auth_expired"] is False
    assert account["credentials"]["tid"] == "tid-1"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", account["added_at"])
    assert len(account["id"]) == 12

    store = read_store(data_dir)
    assert len(store["accounts"]) == 1
    assert store["accounts"][0]["id"] == account["id"]
    # 原子写入不应留下临时文件
    assert not (data_dir / "accounts.json.tmp").exists()


def test_add_assigns_sequential_names(data_dir: Path):
    names = [accounts.add(creds(f"user-{i}"))[0]["name"] for i in range(3)]
    assert names == ["账号1", "账号2", "账号3"]
    assert accounts.count() == 3


def test_default_name_reuses_freed_slot(data_dir: Path):
    first, _ = accounts.add(creds("user-1"))
    accounts.add(creds("user-2"))

    assert accounts.remove(first["id"]) is True
    third, _ = accounts.add(creds("user-3"))

    assert third["name"] == "账号1"


def test_add_same_user_updates_credentials_only(data_dir: Path):
    account, _ = accounts.add(creds("user-1", tid="old"), "qq")
    accounts.rename(account["id"], "大号")
    accounts.mark_expired(account["id"], True)

    updated_account, updated = accounts.add(
        {"userId": "user-1", "tid": "new", "login_type": "wechat"}, "wechat"
    )

    assert updated is True
    assert updated_account["id"] == account["id"]
    assert updated_account["name"] == "大号"
    assert updated_account["login_type"] == "wechat"
    assert updated_account["credentials"]["tid"] == "new"
    assert updated_account["auth_expired"] is False
    assert accounts.count() == 1


def test_add_copies_credentials(data_dir: Path):
    source = creds("user-1", tid="original")
    accounts.add(source)

    source["tid"] = "mutated"
    assert accounts.all()[0]["credentials"]["tid"] == "original"


def test_all_returns_copy(data_dir: Path):
    accounts.add(creds("user-1"))

    snapshot = accounts.all()
    snapshot[0]["name"] = "被篡改"
    snapshot[0]["credentials"]["tid"] = "mutated"

    assert accounts.all()[0]["name"] == "账号1"
    assert accounts.all()[0]["credentials"]["tid"] == "tid-1"


# ---------- 数量上限 ----------


def test_add_rejects_eleventh_account(data_dir: Path):
    for index in range(accounts.MAX_ACCOUNTS):
        accounts.add(creds(f"user-{index}"))

    with pytest.raises(accounts.AccountLimitError) as excinfo:
        accounts.add(creds("user-overflow"))

    assert str(accounts.MAX_ACCOUNTS) in str(excinfo.value)
    assert accounts.count() == accounts.MAX_ACCOUNTS


def test_existing_account_can_update_when_full(data_dir: Path):
    for index in range(accounts.MAX_ACCOUNTS):
        accounts.add(creds(f"user-{index}"))

    account, updated = accounts.add(
        {"userId": "user-5", "tid": "new", "login_type": "wechat"}, "wechat"
    )

    assert updated is True
    assert account["credentials"]["tid"] == "new"
    assert account["login_type"] == "wechat"
    assert accounts.count() == accounts.MAX_ACCOUNTS


# ---------- 改名 ----------


def test_rename_normalizes_whitespace(data_dir: Path):
    account, _ = accounts.add(creds("user-1"))

    renamed = accounts.rename(account["id"], "  大   号  ")

    assert renamed is not None
    assert renamed["name"] == "大 号"
    assert accounts.summary()[0]["name"] == "大 号"


def test_rename_truncates_long_name(data_dir: Path):
    account, _ = accounts.add(creds("user-1"))

    renamed = accounts.rename(account["id"], "字" * 40)

    assert renamed is not None
    assert len(renamed["name"]) == accounts.NAME_LIMIT


def test_rename_empty_keeps_previous_name(data_dir: Path):
    account, _ = accounts.add(creds("user-1"))
    accounts.rename(account["id"], "大号")

    renamed = accounts.rename(account["id"], "   ")

    assert renamed is not None
    assert renamed["name"] == "大号"


def test_rename_unknown_account_returns_none(data_dir: Path):
    accounts.add(creds("user-1"))
    assert accounts.rename("not-exist", "新名字") is None
    assert accounts.count() == 1


# ---------- 解绑 ----------


def test_remove_account(data_dir: Path):
    first, _ = accounts.add(creds("user-1"))
    second, _ = accounts.add(creds("user-2"))

    assert accounts.remove(first["id"]) is True
    remaining = accounts.summary()
    assert [item["id"] for item in remaining] == [second["id"]]
    assert accounts.remove(first["id"]) is False
    assert accounts.remove("not-exist") is False


def test_remove_last_account_keeps_empty_store(data_dir: Path):
    account, _ = accounts.add(creds("user-1"))

    assert accounts.remove(account["id"]) is True

    assert accounts.count() == 0
    assert accounts.summary() == []
    assert (data_dir / "accounts.json").exists()
    assert read_store(data_dir) == {"accounts": []}


# ---------- 失效标记 ----------


def test_mark_expired_and_all_expired(data_dir: Path):
    first, _ = accounts.add(creds("user-1"))
    second, _ = accounts.add(creds("user-2"))

    # 没有账号时不算“全部失效”
    assert accounts.all_expired() is False

    accounts.mark_expired(first["id"], True)
    assert accounts.summary()[0]["auth_expired"] is True
    assert accounts.all_expired() is False

    accounts.mark_expired(second["id"], True)
    assert accounts.all_expired() is True

    accounts.mark_expired(first["id"], False)
    assert accounts.all_expired() is False


def test_mark_expired_ignores_unknown_account(data_dir: Path):
    account, _ = accounts.add(creds("user-1"))

    accounts.mark_expired("not-exist", True)

    assert accounts.summary()[0]["auth_expired"] is False
    assert accounts.summary()[0]["id"] == account["id"]


# ---------- 页面摘要 ----------


def test_summary_hides_credentials(data_dir: Path):
    account, _ = accounts.add(
        {"userId": "JA-09db2000480f479a-b8a3", "tid": "secret", "login_type": "wechat"},
        "wechat",
    )

    item = accounts.summary()[0]

    assert set(item) == {
        "id",
        "name",
        "login_type",
        "user_id_masked",
        "auth_expired",
        "added_at",
    }
    assert item["id"] == account["id"]
    assert item["name"] == "账号1"
    assert item["login_type"] == "wechat"
    assert item["user_id_masked"] == "JA-***8a3"
    assert "credentials" not in item
    assert "tid" not in item


# ---------- 旧版凭证迁移 ----------


def test_migrates_legacy_single_account(data_dir: Path):
    legacy = {"userId": "1234567890abcd", "tid": "legacy-tid", "login_type": "wechat"}
    (data_dir / "config.json").write_text(json.dumps(legacy), encoding="utf-8")

    assert accounts.count() == 1

    migrated = accounts.all()[0]
    assert migrated["name"] == "账号1"
    assert migrated["user_id"] == "1234567890abcd"
    assert migrated["login_type"] == "wechat"
    assert migrated["credentials"] == legacy
    assert migrated["auth_expired"] is False

    # 迁移结果落盘，且不会重复迁移
    assert (data_dir / "accounts.json").exists()
    assert len(read_store(data_dir)["accounts"]) == 1
    assert accounts.count() == 1


def test_migration_runs_only_once(data_dir: Path):
    (data_dir / "config.json").write_text(
        json.dumps({"userId": "user-1", "tid": "t1"}), encoding="utf-8"
    )
    assert accounts.count() == 1

    (data_dir / "config.json").write_text(
        json.dumps({"userId": "user-2", "tid": "t2"}), encoding="utf-8"
    )

    assert accounts.count() == 1
    assert accounts.all()[0]["user_id"] == "user-1"


def test_migration_skips_incomplete_legacy(data_dir: Path):
    (data_dir / "config.json").write_text(
        json.dumps({"userId": "", "tid": "t1"}), encoding="utf-8"
    )

    assert accounts.count() == 0
    assert read_store(data_dir) == {"accounts": []}


def test_migration_skips_broken_legacy(data_dir: Path):
    (data_dir / "config.json").write_text("{not json", encoding="utf-8")

    assert accounts.count() == 0
    assert read_store(data_dir) == {"accounts": []}


def test_no_legacy_file_creates_empty_store(data_dir: Path):
    assert accounts.count() == 0
    assert read_store(data_dir) == {"accounts": []}


def test_broken_accounts_file_is_treated_as_empty(data_dir: Path):
    (data_dir / "accounts.json").write_text("{broken", encoding="utf-8")

    assert accounts.count() == 0
    assert accounts.summary() == []

    # 文件损坏后仍能正常绑定
    account, updated = accounts.add(creds("user-1"))
    assert updated is False
    assert account["name"] == "账号1"
    assert accounts.count() == 1
