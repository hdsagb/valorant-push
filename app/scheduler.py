"""每日定时推送。

纯本地程序没有服务器常驻，所以采用「到点检查 + 补推」：
每天只推一次，电脑关机错过时间也没关系，下次开机后会立刻补推当天内容。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from . import accounts, config, pipeline
from .models import zone

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 30
RETRY_DELAY = 300
MAX_ATTEMPTS = 3

_attempt_date = ""
_attempts = 0
_retry_at = 0.0


def target_time(settings: dict, day: datetime | None = None) -> datetime:
    now = day or datetime.now(zone())
    return now.replace(
        hour=int(settings["push_hour"]),
        minute=int(settings["push_minute"]),
        second=0,
        microsecond=0,
    )


def next_run(settings: dict, state: dict, now: datetime | None = None) -> datetime | None:
    """页面展示用的“下次推送时间”。"""
    if not settings.get("schedule_enabled"):
        return None
    now = now or datetime.now(zone())
    today = now.strftime("%Y-%m-%d")
    target = target_time(settings, now)
    if state.get("last_push_date") == today:
        return target + timedelta(days=1)
    return target


async def tick(now: datetime | None = None) -> None:
    global _attempt_date, _attempts, _retry_at

    settings = config.load_settings()
    state = config.load_state()

    if not settings.get("schedule_enabled") or accounts.all_expired():
        return
    if accounts.count() == 0:
        return

    now = now or datetime.now(zone())
    today = now.strftime("%Y-%m-%d")
    if _attempt_date != today:
        _attempt_date, _attempts, _retry_at = today, 0, 0.0

    if state.get("last_push_date") == today:
        return
    if now < target_time(settings, now):
        return
    if _attempts >= MAX_ATTEMPTS or time.time() < _retry_at:
        return

    try:
        result = await pipeline.run_push(trigger="schedule")
        logger.info("定时推送成功：%s", result.message)
        _attempts, _retry_at = 0, 0.0
    except pipeline.LoginRequired as exc:
        logger.error("定时推送跳过：%s", exc)
    except Exception as exc:
        _attempts += 1
        _retry_at = time.time() + RETRY_DELAY
        logger.exception("定时推送失败（第 %s/%s 次）：%s", _attempts, MAX_ATTEMPTS, exc)
        config.update_state(
            last_push_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            last_push_ok=False,
            last_error=str(exc),
        )


async def loop() -> None:
    logger.info("定时任务已启动（每 %s 秒检查一次）", CHECK_INTERVAL)
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("定时检查异常")
        await asyncio.sleep(CHECK_INTERVAL)
