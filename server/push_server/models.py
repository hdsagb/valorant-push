# ============================================================
# [拷贝来源] app/models.py @ 个人版 commit 9e3f621（2026-11-08，v1.0.1）
# [冻结拷贝] 服务器版独立演进，禁止直接修改本文件逻辑；如需同步个人版改动，
#            重新整文件拷贝并更新来源 commit。适配点仅限本头部列出的条目。
# [适配点]   无（除来源头外逐行一致）
# ============================================================
"""数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# 商店区块 key 与展示名
SECTION_TITLES = {
    "dailystore": "每日商店",
    "kingdomstore": "王国商店",
}


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


@dataclass
class ShopItem:
    """一件商品。

    注意：price_vp 来自接口的 rmb_price 字段，单位是点券（VP），不是人民币。
    """

    name: str
    price_vp: int
    image_url: str
    bg_url: str
    quality: str
    goods_id: str
    like_num: str
    skin_tag: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "ShopItem":
        return cls(
            name=str(raw.get("goods_name") or "未知商品"),
            price_vp=_to_int(raw.get("rmb_price") or raw.get("price")),
            image_url=str(raw.get("goods_pic") or ""),
            bg_url=str(raw.get("bg_image") or raw.get("package_bg") or ""),
            quality=str(raw.get("quality") or ""),
            goods_id=str(raw.get("goods_id") or ""),
            like_num=str(raw.get("like_num") or ""),
            skin_tag=str(raw.get("skin_tag") or ""),
        )


@dataclass
class ShopSection:
    """一个商店区块（每日商店 / 王国商店）。"""

    key: str
    title: str
    items: list[ShopItem] = field(default_factory=list)
    end_ts: int = 0

    @property
    def end_text(self) -> str:
        from datetime import datetime

        if not self.end_ts:
            return ""
        try:
            moment = datetime.fromtimestamp(int(self.end_ts), tz=zone())
        except (TypeError, ValueError, OSError):
            return ""
        return moment.strftime("%m-%d %H:%M")


def zone():
    try:
        from zoneinfo import ZoneInfo

        from .paths import TIMEZONE_NAME

        return ZoneInfo(TIMEZONE_NAME)
    except Exception:
        from datetime import timedelta, timezone

        return timezone(timedelta(hours=8))
