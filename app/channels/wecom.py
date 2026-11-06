"""企业微信自建应用推送（图片 + 文本）。

注意：自建应用要求把服务器出口 IP 加入「企业可信 IP」。家用宽带通常是动态 IP，
一旦变化会返回 60020；此时本程序会自动改用邮件（如果已配置），面板也会给出提示。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp

from .. import paths
from .base import ChannelError, OutgoingMessage

logger = logging.getLogger(__name__)

API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_CACHE_FILE = paths.DATA_DIR / "wecom_token.json"

_HINTS = {
    40014: "Secret 不正确或 access_token 无效",
    40056: "AgentId 不合法",
    42001: "access_token 已过期，请重试",
    60020: "本机出口 IP 不在应用「企业可信 IP」白名单中（家用宽带 IP 变动会导致此错误）",
    81013: "接收人不在应用可见范围内，请检查 userid 或应用可见范围",
}


def is_configured(settings: dict[str, Any]) -> bool:
    wecom = settings["wecom"]
    return bool(wecom["corpid"] and wecom["agentid"] and wecom["secret"])


def _error(payload: dict[str, Any], api: str) -> ChannelError:
    code = payload.get("errcode", -1)
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = -1
    errmsg = payload.get("errmsg", "")
    hint = _HINTS.get(code)
    message = f"企业微信接口失败（{api}）errcode={code} errmsg={errmsg}"
    if hint:
        message += f"；{hint}"
    return ChannelError(code, message)


def _cache_key(wecom: dict[str, Any]) -> str:
    return hashlib.sha256(f"{wecom['corpid']}|{wecom['secret']}".encode()).hexdigest()


def _read_cached_token(wecom: dict[str, Any]) -> str | None:
    try:
        cached = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if cached.get("key") != _cache_key(wecom):
        return None
    if cached.get("expires_at", 0) > time.time() + 120:
        return str(cached.get("access_token") or "") or None
    return None


async def get_access_token(
    session: aiohttp.ClientSession, wecom: dict[str, Any], force: bool = False
) -> str:
    if not force:
        cached = _read_cached_token(wecom)
        if cached:
            return cached

    params = {"corpid": wecom["corpid"], "corpsecret": wecom["secret"]}
    async with session.get(f"{API_BASE}/gettoken", params=params) as response:
        payload = await response.json(content_type=None)

    if payload.get("errcode", 0) != 0:
        raise _error(payload, "gettoken")

    token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 7200))
    try:
        TOKEN_CACHE_FILE.write_text(
            json.dumps(
                {
                    "key": _cache_key(wecom),
                    "access_token": token,
                    "expires_at": time.time() + expires_in - 300,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    return token


async def _upload_image(session: aiohttp.ClientSession, token: str, path: Path) -> str:
    content_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    form = aiohttp.FormData()
    form.add_field("media", path.read_bytes(), filename=path.name, content_type=content_type)
    async with session.post(
        f"{API_BASE}/media/upload",
        params={"access_token": token, "type": "image"},
        data=form,
    ) as response:
        payload = await response.json(content_type=None)
    if payload.get("errcode", 0) != 0:
        raise _error(payload, "media/upload")
    media_id = payload.get("media_id")
    if not media_id:
        raise ChannelError(-1, "企业微信图片上传未返回 media_id")
    return str(media_id)


async def _post_message(
    session: aiohttp.ClientSession,
    token: str,
    wecom: dict[str, Any],
    body: dict[str, Any],
) -> None:
    body.setdefault("touser", wecom["touser"] or "@all")
    body.setdefault("agentid", int(wecom["agentid"]))
    body.setdefault("safe", 0)
    async with session.post(
        f"{API_BASE}/message/send", params={"access_token": token}, json=body
    ) as response:
        payload = await response.json(content_type=None)
    if payload.get("errcode", 0) != 0:
        raise _error(payload, "message/send")


async def send(settings: dict[str, Any], msg: OutgoingMessage) -> None:
    wecom = settings["wecom"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        token = await get_access_token(session, wecom)
        if msg.image and Path(msg.image).exists():
            media_id = await _upload_image(session, token, Path(msg.image))
            await _post_message(
                session, token, wecom, {"msgtype": "image", "image": {"media_id": media_id}}
            )
        await _post_message(
            session,
            token,
            wecom,
            {"msgtype": "text", "text": {"content": msg.text[:2000]}},
        )
    logger.info("企业微信消息已发送")


async def send_test(settings: dict[str, Any]) -> None:
    await send(
        settings,
        OutgoingMessage(
            subject="测试",
            text="这是一条来自「无畏契约商店推送」的测试消息，渠道配置正确。",
            html="",
        ),
    )
