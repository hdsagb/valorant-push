"""定时调度测试：run_push 打桩，模块级状态显式重置。"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import push_server.accounts as accounts
import push_server.config as config
import push_server.pipeline as pipeline
import push_server.scheduler as scheduler
from push_server.models import zone


@pytest.fixture(autouse=True)
def _reset_globals():
    scheduler._attempt_date = ""
    scheduler._attempts = 0
    scheduler._retry_at = 0.0
    yield
    scheduler._attempt_date = ""
    scheduler._attempts = 0
    scheduler._retry_at = 0.0


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
