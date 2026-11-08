# ============================================================
# [拷贝来源] app/cn_client.py @ 个人版 commit 9e3f621（2026-11-08，v1.0.1）
# [冻结拷贝] 服务器版独立演进，禁止直接修改本文件逻辑；如需同步个人版改动，
#            重新整文件拷贝并更新来源 commit。适配点仅限本头部列出的条目。
# [适配点]   1 处 docstring：来源路径 app/vendor → push_server/vendor
# ============================================================
"""国服商店客户端：包装 vendored 登录脚本，只暴露本程序需要的接口。

登录/接口实现复用 push_server/vendor/val_shop_ref.py
（来源：https://github.com/Ceylan233/valorant-daily-shop-email，MIT）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from . import paths
from .models import SECTION_TITLES, ShopItem, ShopSection

logger = logging.getLogger(__name__)

_ref = None


def ref():
    """延迟导入 vendored 脚本：它会在 import 时读取数据目录等环境变量。"""
    global _ref
    if _ref is not None:
        return _ref

    paths.ensure_dirs()
    os.environ["VAL_SHOP_DATA_DIR"] = str(paths.DATA_DIR)
    os.environ["VAL_SHOP_TEMP_DIR"] = str(paths.TEMP_DIR)
    os.environ["VAL_SHOP_LOGIN_TIMEOUT"] = str(paths.LOGIN_TIMEOUT)

    from .vendor import val_shop_ref  # noqa: PLC0415

    _ref = val_shop_ref
    return _ref


class AuthExpiredError(RuntimeError):
    """登录凭证已失效，需要重新扫码。"""


def new_app():
    return ref().ValorantShopEmail()


async def fetch_response(
    credentials: dict[str, Any], timeout: int | None = None
) -> dict[str, Any]:
    """请求商店接口。超时或凭证失效都会抛出异常，不会无限等待。"""
    app = new_app()
    limit = timeout or paths.STORE_TIMEOUT
    try:
        response, err_msg, auth_invalid = await asyncio.wait_for(
            app.request_store_api(credentials), timeout=limit
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"商店接口超时（{limit} 秒），请稍后重试") from exc

    if response is None and auth_invalid:
        raise AuthExpiredError(f"登录凭证已失效：{err_msg}")
    if response is None:
        raise RuntimeError(f"获取商店失败：{err_msg or '接口未返回数据'}")
    return response


def raw_sections(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data") or []
    if isinstance(data, dict):
        data = [data]
    return [section for section in data if isinstance(section, dict)]


def parse_sections(response: dict[str, Any], keys: list[str]) -> list[ShopSection]:
    """按配置的 key 顺序解析商店区块，缺失的区块直接跳过。"""
    found = {section.get("key"): section for section in raw_sections(response)}
    parsed: list[ShopSection] = []
    for key in keys:
        section = found.get(key)
        if not section:
            logger.warning("响应中没有商店区块：%s", key)
            continue
        items = [
            ShopItem.from_raw(raw)
            for raw in (section.get("list") or [])
            if isinstance(raw, dict)
        ]
        if not items:
            continue
        parsed.append(
            ShopSection(
                key=key,
                title=str(section.get("title") or SECTION_TITLES.get(key, key)),
                items=items,
                end_ts=int(section.get("end_ts") or 0),
            )
        )
    return parsed
