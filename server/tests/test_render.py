"""渲染测试（R5）：本地生成含中文的样张；中文观感由人工确认。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image

from push_server.models import ShopItem, ShopSection
from push_server.render import render_sections


def _sections() -> list[ShopSection]:
    def item(name: str, price: int, q: str) -> ShopItem:
        return ShopItem(name=name, price_vp=price, image_url="", bg_url="",
                        quality=q, goods_id="", like_num="8888")

    daily = ShopSection(key="dailystore", title="每日商店", items=[
        item("暴徒 · 无妄之灾", 890, "罕见"),
        item("幽影 · 夜岚", 2170, "史诗"),
    ])
    kingdom = ShopSection(key="kingdomstore", title="王国商店", items=[
        item("夜露 · 淬魂", 1775, "传说"),
    ])
    return [daily, kingdom]


def test_render_outputs_chinese_sample(tmp_path: Path):
    out = asyncio.run(render_sections(_sections(), date_text="2026-11-08",
                                      out_dir=tmp_path))
    assert out.exists() and out.suffix == ".jpg"
    assert out.stat().st_size > 10_000
    image = Image.open(out)
    assert image.size[0] == 736
