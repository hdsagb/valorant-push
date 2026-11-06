"""商店图片渲染：每个区块 2×2 卡片网格，多个区块纵向拼接。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import paths
from .models import ShopItem, ShopSection

logger = logging.getLogger(__name__)

WIDTH = 736
PADDING = 20
GAP = 14
CARD_W = (WIDTH - PADDING * 2 - GAP) // 2
CARD_H = 220
TITLE_H = 78
ACCOUNT_H = 58
SECTION_H = 46
FOOTER_H = 40

BACKGROUND = (15, 25, 35)
CARD_BG = (26, 36, 48)
ACCENT = (255, 70, 85)
TEXT_MAIN = (245, 247, 250)
TEXT_SUB = (150, 165, 180)
PRICE = (255, 226, 120)

_FONT_CANDIDATES = {
    False: [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ],
    True: [
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ],
}

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for candidate in _FONT_CANDIDATES[bold]:
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, size)
                _font_cache[key] = font
                return font
            except OSError:
                continue
    font = ImageFont.load_default(size=size)
    _font_cache[key] = font
    return font


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(image.convert("RGB"), size, method=Image.LANCZOS, centering=(0.5, 0.3))


def _scale_to_height(image: Image.Image, height: int, max_width: int) -> Image.Image:
    width = max(1, int(image.width * height / image.height))
    if width > max_width:
        width = max_width
        height = max(1, int(image.height * width / image.width))
    return image.convert("RGBA").resize((width, height), Image.LANCZOS)


def _round_corners(image: Image.Image, radius: int = 14) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.width, image.height), radius, fill=255)
    rounded = Image.new("RGBA", image.size, (0, 0, 0, 0))
    rounded.paste(image, (0, 0), mask)
    return rounded


def _gradient(size: tuple[int, int], color: tuple[int, int, int]) -> Image.Image:
    """没有背景图时的兜底渐变，避免图片位置一片纯黑。"""
    width, height = size
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        row = tuple(int(channel * (0.35 + 0.65 * ratio)) for channel in color)
        draw.line((0, y, width, y), fill=row + (255,))
    return image


def _draw_bottom_fade(card: Image.Image) -> Image.Image:
    overlay = Image.new("RGBA", card.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    fade_height = 104
    for offset in range(fade_height):
        alpha = int(215 * (offset / fade_height) ** 1.4)
        y = card.height - fade_height + offset
        draw.line((0, y, card.width, y), fill=(0, 0, 0, alpha))
    return Image.alpha_composite(card, overlay)


def _accent_for(index: int) -> tuple[int, int, int]:
    palette = [(255, 70, 85), (93, 156, 236), (120, 200, 160), (200, 140, 240)]
    return palette[index % len(palette)]


async def _download(session: aiohttp.ClientSession, url: str, path: Path) -> bool:
    if not url:
        return False
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
            response.raise_for_status()
            path.write_bytes(await response.read())
        return True
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return False


def _render_card(
    item: ShopItem,
    index: int,
    bg_path: Path,
    goods_path: Path,
) -> Image.Image:
    if bg_path.exists():
        try:
            card = _cover(Image.open(bg_path), (CARD_W, CARD_H)).convert("RGBA")
        except OSError:
            card = _gradient((CARD_W, CARD_H), _accent_for(index))
    else:
        card = _gradient((CARD_W, CARD_H), _accent_for(index))

    if goods_path.exists():
        try:
            goods = _scale_to_height(Image.open(goods_path), 148, CARD_W - 40)
            card.alpha_composite(
                goods, ((CARD_W - goods.width) // 2, (CARD_H - goods.height) // 2 - 22)
            )
        except OSError:
            pass

    card = _draw_bottom_fade(card)

    name_font = _font(24, bold=True)
    price_font = _font(22, bold=True)
    meta_font = _font(15)

    draw = ImageDraw.Draw(card)

    quality = item.quality.strip()
    if quality:
        tag_w = int(draw.textlength(quality, font=meta_font)) + 18
        draw.rounded_rectangle((14, 14, 14 + tag_w, 40), 6, fill=(0, 0, 0, 150))
        draw.text((23, 19), quality, font=meta_font, fill=(255, 220, 180))

    price_text = f"{item.price_vp} VP"
    price_w = int(draw.textlength(price_text, font=price_font))
    name = _fit_text(draw, item.name, name_font, CARD_W - price_w - 62)

    draw.text((18, CARD_H - 46), name, font=name_font, fill=TEXT_MAIN)
    draw.text((CARD_W - price_w - 18, CARD_H - 44), price_text, font=price_font, fill=PRICE)

    if item.like_num:
        like_text = f"♥ {item.like_num}"
        like_w = int(draw.textlength(like_text, font=meta_font))
        draw.text((CARD_W - like_w - 16, 20), like_text, font=meta_font, fill=(235, 235, 235))

    return _round_corners(card)


def _render_title(count: int, date_text: str, account_count: int = 1) -> Image.Image:
    header = Image.new("RGBA", (WIDTH, TITLE_H), BACKGROUND + (255,))
    draw = ImageDraw.Draw(header)

    draw.text((PADDING + 4, 16), "无畏契约 · 今日商店", font=_font(30, bold=True), fill=TEXT_MAIN)
    draw.text(
        (PADDING + 5, 54),
        "数据来源：掌上无畏契约 App",
        font=_font(15),
        fill=TEXT_SUB,
    )

    sub_text = f"{date_text} · {count} 件"
    if account_count > 1:
        sub_text = f"{date_text} · {account_count} 个账号 · {count} 件"
    sub_w = int(draw.textlength(sub_text, font=_font(17)))
    draw.text((WIDTH - sub_w - PADDING, 30), sub_text, font=_font(17), fill=TEXT_SUB)

    draw.rectangle((0, TITLE_H - 3, WIDTH, TITLE_H), fill=ACCENT)
    return header


def _render_account_header(name: str, meta: str, first: bool) -> Image.Image:
    """账号分区标题：名字 + 脱敏账号 ID。"""
    header = Image.new("RGBA", (WIDTH, ACCOUNT_H), BACKGROUND + (255,))
    draw = ImageDraw.Draw(header)

    if not first:
        draw.line((PADDING, 6, WIDTH - PADDING, 6), fill=(38, 56, 74), width=1)

    top = 14 if not first else 6
    draw.rounded_rectangle((PADDING, top + 8, PADDING + 5, top + 32), 2, fill=ACCENT)
    draw.text(
        (PADDING + 16, top + 1),
        _fit_text(draw, name, _font(24, bold=True), WIDTH - PADDING * 2 - 200),
        font=_font(24, bold=True),
        fill=TEXT_MAIN,
    )
    if meta:
        meta_w = int(draw.textlength(meta, font=_font(15)))
        draw.text((WIDTH - meta_w - PADDING, top + 10), meta, font=_font(15), fill=TEXT_SUB)
    return header


def _render_section_header(section: ShopSection) -> Image.Image:
    header = Image.new("RGBA", (WIDTH, SECTION_H), BACKGROUND + (255,))
    draw = ImageDraw.Draw(header)

    draw.rounded_rectangle((PADDING, 14, PADDING + 4, 34), 2, fill=ACCENT)
    draw.text((PADDING + 14, 8), section.title, font=_font(22, bold=True), fill=TEXT_MAIN)

    meta = f"{len(section.items)} 件"
    if section.end_text:
        meta += f" · 截止 {section.end_text}"
    meta_w = int(draw.textlength(meta, font=_font(15)))
    draw.text((WIDTH - meta_w - PADDING, 18), meta, font=_font(15), fill=TEXT_SUB)
    return header


def _remove_stale_cache(keep: set[Path]) -> None:
    """删掉不属于本次渲染的商品图缓存，避免 data/temp 堆积历史图片。"""
    for pattern in ("card_bg_*.png", "card_goods_*.png"):
        for path in paths.TEMP_DIR.glob(pattern):
            if path in keep:
                continue
            try:
                path.unlink()
            except OSError:
                pass


def _remove_old_outputs(out_dir: Path, keep: Path) -> None:
    """新商店图生成后删掉旧的 out/shop_*.jpg，只保留最新一张。"""
    for path in out_dir.glob("shop_*.jpg"):
        if path == keep:
            continue
        try:
            path.unlink()
        except OSError:
            pass


async def render_accounts(
    entries: list[tuple[str, str, list[ShopSection]]],
    date_text: str | None = None,
    out_dir: Path | None = None,
) -> Path:
    """把多个账号的商店渲染到同一张图，账号名作为分区标题。

    entries 为 (账号名, 脱敏账号 ID, 商店区块)；账号名为空时不画账号标题。
    """
    paths.ensure_dirs()
    date_text = date_text or datetime.now().strftime("%Y-%m-%d")
    out_dir = out_dir or paths.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # 每个区块最多 2×2 的商品图，多出来的继续按两列排
    flat: list[tuple[int, ShopSection, int, ShopItem]] = []
    for entry_index, (_, _, sections) in enumerate(entries):
        for section in sections:
            for index, item in enumerate(section.items):
                flat.append((entry_index, section, index, item))

    bg_paths: list[Path] = []
    goods_paths: list[Path] = []
    for _, _, _, item in flat:
        token = hashlib.md5(f"{item.image_url}|{item.bg_url}".encode()).hexdigest()[:12]
        bg_paths.append(paths.TEMP_DIR / f"card_bg_{token}.png")
        goods_paths.append(paths.TEMP_DIR / f"card_goods_{token}.png")

    _remove_stale_cache(set(bg_paths) | set(goods_paths))

    async with aiohttp.ClientSession() as session:
        tasks = []
        for (_, _, _, item), bg_path, goods_path in zip(flat, bg_paths, goods_paths):
            if not bg_path.exists():
                tasks.append(_download(session, item.bg_url, bg_path))
            if not goods_path.exists():
                tasks.append(_download(session, item.image_url, goods_path))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # 先按区块生成卡片行
    card_lookup: dict[tuple[int, int, int], Image.Image] = {}
    for (entry_index, section, index, item), bg_path, goods_path in zip(
        flat, bg_paths, goods_paths
    ):
        card_lookup[(entry_index, id(section), index)] = _render_card(
            item, index, bg_path, goods_path
        )

    heights = [TITLE_H + PADDING]
    for name, _, sections in entries:
        if name:
            heights.append(ACCOUNT_H)
        for section in sections:
            rows = (len(section.items) + 1) // 2
            heights.append(SECTION_H + 4 + rows * CARD_H + (rows - 1) * GAP + GAP)
    total_height = int(sum(heights) + FOOTER_H + PADDING)
    canvas = Image.new("RGBA", (WIDTH, total_height), BACKGROUND + (255,))
    canvas.alpha_composite(_render_title(len(flat), date_text, len(entries)), (0, PADDING))

    y = PADDING + TITLE_H
    first_account = True
    for entry_index, (name, meta, sections) in enumerate(entries):
        if name:
            canvas.alpha_composite(_render_account_header(name, meta, first_account), (0, y))
            first_account = False
            y += ACCOUNT_H
        for section in sections:
            canvas.alpha_composite(_render_section_header(section), (0, y))
            y += SECTION_H + 4
            for row_start in range(0, len(section.items), 2):
                for column in range(2):
                    index = row_start + column
                    if index >= len(section.items):
                        continue
                    card = card_lookup[(entry_index, id(section), index)]
                    x = PADDING + column * (CARD_W + GAP)
                    canvas.alpha_composite(card, (x, y))
                y += CARD_H + GAP

    draw = ImageDraw.Draw(canvas)
    draw.text(
        (PADDING + 4, total_height - FOOTER_H + 6),
        "价格单位：点券（VP）· 个人自用，仅供参考",
        font=_font(14),
        fill=(110, 125, 140),
    )

    out_path = out_dir / f"shop_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    canvas.convert("RGB").save(out_path, quality=88, optimize=True)
    _remove_old_outputs(out_dir, out_path)
    logger.info("商店图片已生成：%s", out_path)
    return out_path


async def render_sections(
    sections: list[ShopSection],
    date_text: str | None = None,
    out_dir: Path | None = None,
) -> Path:
    """单账号渲染入口，保留给旧调用方。"""
    return await render_accounts([("", "", sections)], date_text, out_dir)
