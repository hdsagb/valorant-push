"""二维码登录会话（M6）：给 Web 设置页提供扫码绑定与失效重绑。

适配自个人版 app/login.py，差异点：
- _finish 编排与 credpack.import_accounts 同范式：update_same_user → 否则 add
  （服务器版 accounts.add 不按 user_id 去重，超限抛 ValueError 而非 AccountLimitError）
- 重绑成功不弹命名框（snapshot.account.updated 区分，前端据此决定是否弹）
- 成功后仅写 settings.login_method，不动全局运行状态
  （auth_expired/last_error 由 pipeline 每轮重算，单账号重绑不应清全局）
- cn_client.new_app() 经模块级 _new_app() 间接调用，供测试替换（不触网）

只做会话编排；二维码生成/轮询/换凭证全部由 vendored 脚本
（vendor/val_shop_ref.py，与商店推送同一份拷贝）完成，二维码图片写入 TEMP_DIR。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from . import accounts, cn_client, config, paths

logger = logging.getLogger(__name__)

_current: "LoginSession | None" = None


def _new_app() -> Any:
    """接缝：测试 monkeypatch 本函数返回 fake，避免真实网络调用。"""
    return cn_client.new_app()


class LoginSession:
    def __init__(self, method: str):
        self.method = "wechat" if method == "wechat" else "qq"
        self.state = "starting"  # starting | waiting | success | failed | timeout
        self.message = "正在生成二维码…"
        self.qr_path: Path | None = None
        self.task: asyncio.Task | None = None
        self.account: dict[str, Any] | None = None

    async def start(self) -> None:
        app = _new_app()
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
        user_id = str(final_config["userId"])
        try:
            if accounts.update_same_user(user_id, final_config, self.method):
                match = next(
                    (a for a in accounts.all() if a["user_id"] == user_id), None
                )
                account = match or {"id": "", "name": ""}
                updated = True
            else:
                account = accounts.add(final_config, self.method)
                updated = False
        except ValueError as exc:  # 账号上限
            self.state, self.message = "failed", str(exc)
            return

        settings = config.load_settings()
        config.save_settings({**settings, "login_method": self.method})
        self.account = {
            "id": account["id"],
            "name": account["name"],
            "updated": updated,
        }
        if updated:
            self.state, self.message = "success", f"已更新「{account['name']}」的登录凭证"
        else:
            self.state, self.message = "success", f"登录成功，已绑定「{account['name'] or '新账号'}」"
        logger.info(
            "扫码登录成功（%s）：%s",
            "更新" if updated else "新增",
            accounts.mask_user_id(user_id),
        )

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


def reset() -> None:
    """清空当前会话并取消后台任务（测试清理用）。"""
    global _current
    if _current:
        _current.cancel()
        _current = None
