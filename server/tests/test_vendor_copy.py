"""协议层拷贝完整性 + 协议层纯函数测试。"""
from __future__ import annotations

from conftest import SERVER_DIR

import push_server.models
import push_server.cn_client  # noqa: F401  确认可导入
import push_server.vendor.val_shop_ref  # noqa: F401  确认可导入


def _source(rel: str) -> str:
    return (SERVER_DIR / "push_server" / rel).read_text(encoding="utf-8")


def test_vendor_imports_clean():
    # STORE_URL 是 ValorantShopEmail 的类属性（vendored 脚本结构如此）
    assert push_server.vendor.val_shop_ref.ValorantShopEmail.STORE_URL.startswith("https://")


def test_vendor_has_strong_copy_header():
    text = _source("vendor/val_shop_ref.py")
    assert "禁改" in text
    assert "9e3f621" in text


def test_trust_env_is_sole_change():
    text = _source("vendor/val_shop_ref.py")
    # 5 处出站流量（QQ 登录、微信二维码、微信轮询、商店接口、图片下载）全部适配
    assert text.count("ClientSession(trust_env=True)") == 5
    assert "async with aiohttp.ClientSession() as session:" not in text


def test_vendor_does_not_import_app_or_parent():
    text = _source("vendor/val_shop_ref.py")
    assert "from .." not in text
    assert "from app" not in text
    assert "import app" not in text


def test_copies_carry_source_header():
    for rel in ("models.py", "cn_client.py"):
        assert "9e3f621" in _source(rel), rel


def test_models_zone_is_shanghai():
    assert str(push_server.models.zone()) == "Asia/Shanghai"


def test_parse_sections_filters_missing_and_empty():
    response = {
        "data": [
            {"key": "dailystore", "title": "每日商店", "end_ts": 0,
             "list": [{"goods_name": "暴徒", "rmb_price": "890", "goods_pic": "",
                       "bg_image": "", "quality": "罕见", "goods_id": "g1",
                       "like_num": "12"}]},
            {"key": "kingdomstore", "list": []},
        ]
    }
    sections = push_server.cn_client.parse_sections(response, ["dailystore", "kingdomstore"])
    assert len(sections) == 1
    assert sections[0].items[0].name == "暴徒"
    assert sections[0].items[0].price_vp == 890
