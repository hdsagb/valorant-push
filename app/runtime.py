"""后台运行时：一个线程里跑本地 Web 服务 + 定时任务，主线程留给托盘。"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Any

from . import pipeline, scheduler, web

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, port: int | None = None):
        self.port = port
        self.url = ""
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def start(self, timeout: float = 20.0) -> str:
        self._thread = threading.Thread(target=self._run, name="valorant-push-runtime", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        if self._error:
            raise self._error
        if not self.url:
            raise RuntimeError("本地服务启动超时")
        return self.url

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except BaseException as exc:  # 启动失败要把错误带回主线程
            self._error = exc
            self._ready.set()
            logger.exception("后台服务异常退出")
        finally:
            self.loop.close()

    async def _serve(self) -> None:
        runner, self.url = await web.start_server(self.port)
        self._stop = asyncio.Event()
        self._ready.set()
        task = asyncio.create_task(scheduler.loop())
        try:
            await self._stop.wait()
        finally:
            task.cancel()
            await runner.cleanup()

    def stop(self) -> None:
        if self.loop and self._stop:
            self.loop.call_soon_threadsafe(self._stop.set)

    def submit(self, coro) -> Future | None:
        if not self.loop:
            return None
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def push_now(self) -> dict[str, Any]:
        future = self.submit(pipeline.run_push("tray"))
        if future is None:
            return {"ok": False, "message": "后台服务未启动"}
        try:
            result = future.result(timeout=150)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        return {
            "ok": True,
            "message": result.fallback_reason or "推送成功",
            "fallback": bool(result.fallback_reason),
        }
