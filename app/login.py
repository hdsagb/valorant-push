"""二维码登录会话：给本地 Web 页面提供二维码与状态。

只在本机进行：页面显示二维码，用户用手机扫，程序轮询登录结果。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from . import accounts, cn_client, config, paths

logger = logging.getLogger(__name__)

_current: "LoginSession | None" = None


class LoginSession:
    def __init__(self, method: str):
        self.method = "wechat" if method == "wechat" else "qq"
        self.state = "starting"  # starting | waiting | success | failed | timeout
        self.message = "正在生成二维码…"
        self.qr_path: Path | None = None
        self.task: asyncio.Task | None = None
        self.account: dict[str, Any] | None = None

    async def start(self) -> None:
        app = cn_client.new_app()
        if self.method == "wechat":
            ctx = await app.generate_wechat_qr_code()
            if not ctx:
                self.state, self.message = "failed", "生成微信二维码失败，请重试"
                return
            self.qr_path = Path(ctx["qr_path"])
            self.state, self.message = "waiting", "请使用微信扫描二维码"
            self.task = asyncio.create_task(self._run_wechat(app, ctx))
        else:
            ctx = await app.generate_qq_qr_code()
            if not ctx:
                self.state, self.message = "failed", "生成 QQ 二维码失败，请重试"
                return
            self.qr_path = Path(ctx["qr_path"])
            self.state, self.message = "waiting", "请使用手机 QQ 扫描二维码"
            self.task = asyncio.create_task(self._run_qq(app, ctx))

    async def _run_qq(self, app: Any, ctx: dict[str, Any]) -> None:
        session = ctx["session"]
        try:
            login_data = await app.wait_for_qq_login_result(
                session=session,
                ptqrtoken=ctx["ptqrtoken"],
                login_sig=ctx.get("login_sig", ""),
                login_u1=ctx.get("u1_url", app.DEFAULT_LOGIN_U1_URL),
                referer_url=ctx.get("login_url")
                or app._build_login_url(app.DEFAULT_LOGIN_CALLBACK_URL),
                pt_openlogin_data=ctx.get("pt_openlogin_data", ""),
                aegis_uid=ctx.get("aegis_uid", ""),
                jsver=ctx.get("jsver", "28d22679"),
                pt_uistyle=ctx.get("pt_uistyle", "35"),
                ptlang=ctx.get("ptlang", "2052"),
                timeout=paths.LOGIN_TIMEOUT,
            )
            if not login_data:
                self.state, self.message = "timeout", "二维码已过期，请重新生成"
                return
            self.state, self.message = "waiting", "扫码成功，正在换取登录凭证…"
            final_config = await app.get_final_cookies_by_qq(login_data)
            if not final_config:
                self.state, self.message = "failed", "扫码成功，但换取登录凭证失败，请重试"
                return
            self._finish(final_config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("QQ 登录失败")
            self.state, self.message = "failed", f"QQ 登录失败：{exc}"
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def _run_wechat(self, app: Any, ctx: dict[str, Any]) -> None:
        try:
            final_config = await app.wait_for_wechat_login_result(
                ctx["uuid"], timeout=paths.LOGIN_TIMEOUT
            )
            if not final_config:
                self.state, self.message = "timeout", "二维码已过期，请重新生成"
                return
            self._finish(final_config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("微信登录失败")
            self.state, self.message = "failed", f"微信登录失败：{exc}"

    def _finish(self, final_config: dict[str, Any]) -> None:
        if not final_config.get("userId") or not final_config.get("tid"):
            self.state, self.message = "failed", "登录失败：未获取到账号凭证"
            return
        try:
            account, updated = accounts.add(final_config, self.method)
        except accounts.AccountLimitError as exc:
            self.state, self.message = "failed", str(exc)
            return

        settings = config.load_settings()
        config.save_settings({**settings, "login_method": self.method})
        config.update_state(auth_expired=False, last_error="")
        self.account = {
            "id": account["id"],
            "name": account["name"],
            "updated": updated,
        }
        if updated:
            self.state, self.message = "success", f"已更新「{account['name']}」的登录凭证"
        else:
            self.state, self.message = "success", f"登录成功，已绑定「{account['name']}」"
        logger.info("扫码登录成功，已写入账号库：%s", account["name"])

    def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()

    def snapshot(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "state": self.state,
            "message": self.message,
            "has_qr": bool(self.qr_path and self.qr_path.exists()),
            "account": self.account,
        }


async def start(method: str) -> LoginSession:
    """开始（或复用）一个登录会话。"""
    global _current
    if (
        _current
        and _current.method == ("wechat" if method == "wechat" else "qq")
        and _current.state in {"starting", "waiting"}
        and _current.task
        and not _current.task.done()
    ):
        return _current

    if _current:
        _current.cancel()
    session = LoginSession(method)
    await session.start()
    _current = session
    return session


def current() -> LoginSession | None:
    return _current


def qr_path() -> Path | None:
    return _current.qr_path if _current else None
