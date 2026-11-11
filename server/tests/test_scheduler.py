"""定时调度测试：run_push 打桩，模块级状态显式重置。"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import push_server.accounts as accounts
import push_server.config as config
import push_server.notify as notify
import push_server.pipeline as pipeline
import push_server.scheduler as scheduler
from push_server.models import zone


@pytest.fixture(autouse=True)
def _reset_globals():
    scheduler._attempt_date = ""
    scheduler._attempts = 0
    scheduler._retry_at = 0.0
    notify.reset_state()
    yield
    scheduler._attempt_date = ""
    scheduler._attempts = 0
    scheduler._retry_at = 0.0
    notify.reset_state()


def _prepare(data_dir) -> None:
    config.save_settings({"channel": "email",
                          "email": {"smtp_host": "smtp.qq.com", "username": "a@qq.com",
                                    "password": "k", "to": "b@qq.com"}})
    accounts.add({"userId": "u1", "tid": "t1"}, "qq")


def _due_now() -> datetime:
    # 默认推送时刻 08:05，09:00 已到点
    return datetime(2026, 11, 8, 9, 0, tzinfo=zone())


def test_tick_pushes_once_per_day(data_dir, monkeypatch):
    _prepare(data_dir)
    calls = []

    async def fake_run_push(trigger=""):
        calls.append(trigger)

        class Result:
            message = "推送成功"
        return Result()

    monkeypatch.setattr(pipeline, "run_push", fake_run_push)

    asyncio.run(scheduler.tick(_due_now()))
    assert calls == ["schedule"]

    config.update_state(last_push_date="2026-11-08")  # 模拟 pipeline 成功写状态
    asyncio.run(scheduler.tick(_due_now()))
    assert calls == ["schedule"]  # 当天已推过，不再推


def test_tick_retry_backoff(data_dir, monkeypatch):
    _prepare(data_dir)

    async def fake_run_push(trigger=""):
        raise RuntimeError("网络故障")

    monkeypatch.setattr(pipeline, "run_push", fake_run_push)

    asyncio.run(scheduler.tick(_due_now()))
    assert scheduler._attempts == 1
    assert scheduler._retry_at > 0
    assert config.load_state()["last_push_ok"] is False

    asyncio.run(scheduler.tick(_due_now()))  # 重试窗口内不重复尝试
    assert scheduler._attempts == 1


def test_target_time_uses_settings():
    settings = {"push_hour": 8, "push_minute": 5}
    moment = datetime(2026, 11, 8, 9, 0, tzinfo=zone())
    target = scheduler.target_time(settings, moment)
    assert (target.hour, target.minute) == (8, 5)


def test_final_failure_triggers_notify(data_dir, monkeypatch):
    _prepare(data_dir)

    async def fake_run_push(trigger=""):
        raise RuntimeError("网络故障")

    monkeypatch.setattr(pipeline, "run_push", fake_run_push)
    notify_calls = []

    async def fake_notify(settings, error, attempts):
        notify_calls.append((error, attempts))

    monkeypatch.setattr(notify, "notify_push_failure", fake_notify)

    for _ in range(3):
        asyncio.run(scheduler.tick(_due_now()))
        scheduler._retry_at = 0.0  # 跳过重试等待窗口

    assert scheduler._attempts == 3
    assert len(notify_calls) == 1
    assert notify_calls[0][1] == 3  # attempts 参数


def test_final_failure_notify_only_once_per_day(data_dir, monkeypatch):
    _prepare(data_dir)

    async def fake_run_push(trigger=""):
        raise RuntimeError("网络故障")

    monkeypatch.setattr(pipeline, "run_push", fake_run_push)
    notify_calls = []

    async def fake_notify(settings, error, attempts):
        notify_calls.append((error, attempts))

    monkeypatch.setattr(notify, "notify_push_failure", fake_notify)

    for _ in range(3):
        asyncio.run(scheduler.tick(_due_now()))
        scheduler._retry_at = 0.0
    assert len(notify_calls) == 1

    # 重试计数已用尽，tick 不再推送也不重复通知
    for _ in range(2):
        asyncio.run(scheduler.tick(_due_now()))
    assert len(notify_calls) == 1
