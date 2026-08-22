import base64
from functools import lru_cache
from io import BytesIO
from typing import Tuple, Union

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ..config import SHANGGUMONO, Path, coverdir


@lru_cache(maxsize=128)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """缓存字体对象：TTF 每次 truetype 都重新读盘解析（多 MB CJK 字体），
    一张 b50 图有数百次文字绘制。单进程 Bot 中 FreeTypeFont 可安全复用。"""
    return ImageFont.truetype(path, size)


# The production font pack is intentionally CJK-focused and the Linux hosts do
# not provide a color-emoji font.  Keep image output readable by translating
# the small set of UI emoji to glyphs available in every bundled font instead
# of letting Pillow draw a tofu square.  Unknown supplementary-plane symbols
# become a neutral bullet rather than a missing-glyph box.
_IMAGE_SYMBOL_FALLBACKS = {
    '✅': '[OK]', '❌': '[X]', '⚠': '!', '⚠️': '!', '🔄': '↻',
    '🎵': '♪', '🎲': '*', '✨': '*', '💳': '卡', '💰': '$', '📅': '日',
    '📦': '箱', '📋': '表', '🧾': '票', '📤': '出', '📥': '入', '🔑': '钥',
    '🔒': '锁', '🔓': '开', '🚪': '门', '🛠': '工', '🛠️': '工', '🧧': '红包',
    '🎁': '礼', '🎯': '靶', '🛡': '盾', '🛡️': '盾', '❄': '*', '❄️': '*',
    '🤔': '?', '🥇': '1', '🥈': '2', '🥉': '3', '🟢': '●', '🟡': '●',
    '🔴': '●', '🟣': '●', '⚪': '○', '⭐': '★', '🌟': '★', '📈': '^',
    '📊': '图', '🔗': '链', '📌': '点', '🎮': '游', '💥': '!', '📝': '记',
    '✪': '★', '✦': '★', '✧': '☆', '✡': '★', '❂': '★',
    '†': '+', '‡': '+', '♡': '♥', '☼': '*', '☀': '*',
}


def image_safe_text(text) -> str:
    """Return text that can be rendered by the bundled monochrome fonts."""
    raw = str(text)
    # Apply longest keys first so a variation-selector sequence is translated
    # as one symbol, then remove any remaining variation selectors.
    for source in sorted(_IMAGE_SYMBOL_FALLBACKS, key=len, reverse=True):
        raw = raw.replace(source, _IMAGE_SYMBOL_FALLBACKS[source])
    out: list[str] = []
    for char in raw:
        code = ord(char)
        if 0xFE00 <= code <= 0xFE0F:
            continue
        # Emoji and many newer pictographs live outside the BMP.  The bundled
        # CJK fonts do not cover them; a bullet keeps names and labels intact.
        if code > 0xFFFF:
            out.append('•')
            continue
        # A few symbol blocks still contain glyphs in the font, so only replace
        # characters that are explicitly known to be pictographs here.
        out.append(char)
    return ''.join(out)


class DrawText:

    def __init__(self, image: ImageDraw.ImageDraw, font: Path) -> None:
        self._img = image
        self._font = str(font)

    def get_box(self, text: str, size: int) -> Tuple[float, float, float, float]:
        return _load_font(self._font, size).getbbox(image_safe_text(text))

    def draw(
        self,
        pos_x: int,
        pos_y: int,
        size: int,
        text: Union[str, int, float],
        color: Tuple[int, int, int, int] = (255, 255, 255, 255),
        anchor: str = 'lt',
        stroke_width: int = 0,
        stroke_fill: Tuple[int, int, int, int] = (0, 0, 0, 0),
        multiline: bool = False
    ) -> None:
        font = _load_font(self._font, size)
        text = image_safe_text(text)
        if multiline:
            self._img.multiline_text(
                (pos_x, pos_y), 
                str(text), 
                color, 
                font, 
                anchor, 
                stroke_width=stroke_width, 
                stroke_fill=stroke_fill
            )
        else:
            self._img.text(
                (pos_x, pos_y), 
                str(text), 
                color, 
                font, 
                anchor, 
                stroke_width=stroke_width, 
                stroke_fill=stroke_fill
            )


def fit_font_size(
    font_path: str,
    text: str,
    max_width: int,
    start: int = 18,
    min_size: int = 11,
) -> int:
    """在 max_width 内从 start 向下试探字号，避免页脚等长文案溢出。"""
    size = start
    while size >= min_size:
        font = _load_font(str(font_path), size)
        try:
            text_w = font.getlength(text)
        except AttributeError:
            bbox = font.getbbox(text)
            text_w = bbox[2] - bbox[0]
        stroke_pad = max(2, size // 8)
        if text_w + stroke_pad * 2 <= max_width:
            return size
        size -= 1
    return min_size


def draw_centered_design_footer(
    im: Image.Image,
    dt: DrawText,
    text: str,
    *,
    design_bg: Image.Image | None = None,
    color: Tuple[int, int, int, int] = (124, 129, 255, 255),
    margin_x: int = 80,
    bar_height: int = 52,
    start_font_size: int = 16,
    min_font_size: int = 11,
    bottom_gap: int = 36,
) -> None:
    """底部居中页脚：可选 design 底条 + 自适应字号。"""
    w, h = im.size
    bar_w = max(240, w - margin_x * 2)
    bar_x = (w - bar_w) // 2
    pad = 32
    size = fit_font_size(dt._font, text, bar_w - pad * 2, start_font_size, min_font_size)
    stroke = max(1, size // 10)

    if design_bg is not None:
        bar_y = h - bar_height - bottom_gap
        im.alpha_composite(design_bg.resize((bar_w, bar_height)), (bar_x, bar_y))
        text_y = bar_y + bar_height // 2
    else:
        text_y = h - bottom_gap - size // 2

    dt.draw(w // 2, text_y, size, text, color, 'mm', stroke, (255, 255, 255, 255))


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    hex_str = hex_str.lstrip('#')
    return tuple(int(hex_str[i: i + 2], 16) for i in (0, 2, 4))


def tricolor_gradient_prism_plus(width: int, height: int) -> Image.Image:
    """PRiSM PLUS 渐变背景（与 beta 分支一致）"""
    colors_list = [
        (0.0, _hex_to_rgb('#ffffff')),
        (0.14, _hex_to_rgb('#ffffff')),
        (0.24, _hex_to_rgb('#ffd5cf')),
        (0.46, _hex_to_rgb('#ffd5cf')),
        (0.56, _hex_to_rgb('#ffc5d5')),
        (0.67, _hex_to_rgb('#eaabff')),
        (0.85, _hex_to_rgb('#72bcfe')),
        (0.95, _hex_to_rgb('#65f2df')),
        (1.0, _hex_to_rgb('#65f2df')),
    ]
    line = Image.new('RGBA', (1, height))
    for y in range(height):
        t = 1.0 - (y / (height - 1)) if height > 1 else 0
        for i in range(len(colors_list) - 1):
            p1, c1 = colors_list[i]
            p2, c2 = colors_list[i + 1]
            if p1 <= t <= p2:
                rel_t = (t - p1) / (p2 - p1) if p2 > p1 else 0
                rgb = tuple(int(c1[j] + (c2[j] - c1[j]) * rel_t) for j in range(3))
                line.putpixel((0, y), rgb)
                break
    return line.resize((width, height), resample=Image.Resampling.BICUBIC)


def generate_frosted_card(
    im: Image.Image,
    box: tuple[int, int, int, int],
    shadow_offset: tuple[int, int] = (10, 10),
    alpha: float = 0.4,
) -> Image.Image:
    """毛玻璃卡片（与 beta 分支一致）"""
    roi = im.crop(box)
    roi_w, roi_h = roi.size
    frosted = roi.filter(ImageFilter.GaussianBlur(4))
    white_layer = Image.new('RGBA', (roi_w, roi_h), (255, 255, 255, int(255 * alpha)))
    card = Image.alpha_composite(frosted, white_layer)
    mask = Image.new('L', (roi_w, roi_h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, roi_w, roi_h), radius=25, fill=255)
    shadow_w = roi_w + 5 * 2 + abs(shadow_offset[0])
    shadow_h = roi_h + 5 * 2 + abs(shadow_offset[1])
    shadow = Image.new('RGBA', (shadow_w, shadow_h), (0, 0, 0, 0))
    draw_shadow = ImageDraw.Draw(shadow)
    draw_shadow.rounded_rectangle(
        (15, 15, 15 + roi_w, 15 + roi_h), radius=25, fill=(0, 0, 0, 50)
    )
    shadow_layer = shadow.filter(ImageFilter.GaussianBlur(3))
    temp_layer = Image.new('RGBA', im.size, (0, 0, 0, 0))
    shadow_pos = (box[0] + shadow_offset[0] - 15, box[1] + shadow_offset[1] - 15)
    temp_layer.paste(shadow_layer, shadow_pos)
    temp_layer.paste(card, (box[0], box[1]), mask=mask)
    return Image.alpha_composite(im, temp_layer)


def tricolor_gradient(
    width: int, 
    height: int, 
    color1: Tuple[int, int, int] = (124, 129, 255), 
    color2: Tuple[int, int, int] = (193, 247, 225), 
    color3: Tuple[int, int, int] = (255, 255, 255)
) -> Image.Image:
    """绘制渐变色"""
    array = np.zeros((height, width, 3), dtype=np.uint8)
    
    for y in range(height):
        if y < height * 0.4:
            ratio = y / (height * 0.4)
            color = (1 - ratio) * np.array(color1) + ratio * np.array(color2)
        else:
            ratio = (y - height * 0.4) / (height * 0.6)
            color = (1 - ratio) * np.array(color2) + ratio * np.array(color3)
        array[y, :] = np.clip(color, 0, 255)
    
    image = Image.fromarray(array).convert('RGBA')
    return image


def rounded_corners(
    image: Image.Image,
    radius: int, 
    corners: Tuple[bool, bool, bool, bool] = (False, False, False, False)
) -> Image.Image:
    """
    绘制圆角
    
    Params:
        `image`: `PIL.Image.Image`
        `radius`: 圆角半径
        `corners`: 四个角是否绘制圆角，分别是左上、右上、右下、左下
    Returns:
        `PIL.Image.Image`
    """
    mask = Image.new('L', image.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, image.size[0], image.size[1]), radius, fill=255, corners=corners)

    new_im = ImageOps.fit(image, mask.size)
    new_im.putalpha(mask)

    return new_im


def music_picture(music_id: Union[int, str]) -> Path:
    """
    获取谱面图片路径
    
    查找顺序：
    1. 直接查找 {music_id}.png
    2. 如果是宴谱(>100000)，尝试 {music_id - 100000}.png
    3. 统一转换为四位数 (music_id % 10000)
    4. 尝试 .jpg 格式
    5. 返回默认占位图
    
    Params:
        `music_id`: 谱面 ID
    Returns:
        `Path`
    """
    original_id = music_id
    music_id = int(music_id)
    
    # 1. 直接查找 PNG
    if (_path := coverdir / f'{music_id}.png').exists():
        return _path
    
    # 2. 宴谱处理 (ID >= 100000)
    if music_id >= 100000:
        base_id = music_id - 100000
        if (_path := coverdir / f'{base_id}.png').exists():
            return _path
        if (_path := coverdir / f'{base_id}.jpg').exists():
            return _path
        # 宴谱也尝试四位数
        four_digit = base_id % 10000
        if (_path := coverdir / f'{four_digit}.png').exists():
            return _path
        if (_path := coverdir / f'{four_digit}.jpg').exists():
            return _path
    
    # 3. 统一转换为四位数（所有曲绘现在都是四位数）
    four_digit_id = music_id % 10000
    if four_digit_id != music_id:  # 避免重复查找
        if (_path := coverdir / f'{four_digit_id}.png').exists():
            return _path
        if (_path := coverdir / f'{four_digit_id}.jpg').exists():
            return _path
    
    # 4. 尝试 JPG 格式（原始 ID）
    if (_path := coverdir / f'{music_id}.jpg').exists():
        return _path
    
    # 5. 默认占位封面
    for _fallback in ('11000.png', '0.png', '11000.jpg', '0.jpg'):
        if (_path := coverdir / _fallback).exists():
            return _path
    
    # 最后返回 0.png 路径（即使不存在，让调用方处理错误）
    return coverdir / '0.png'


def text_to_image(text: str) -> Image.Image:
    # Static packs normally provide ShangguMono; keep lightweight text boards
    # usable during first boot or in a minimal deployment without that pack.
    font_path = Path(SHANGGUMONO)
    if not font_path.is_file():
        bundled = Path(__file__).resolve().parents[1] / 'GenSenMaruGothicTW-Regular.ttf'
        font_path = bundled if bundled.is_file() else font_path
    try:
        font = _load_font(str(font_path), 24)
    except OSError:
        font = ImageFont.load_default()
    padding = 10
    margin = 4
    lines = image_safe_text(text).strip().split('\n')
    max_width = 0
    b = 0
    for line in lines:
        l, t, r, b = font.getbbox(line)
        max_width = max(max_width, r)
    wa = max_width + padding * 2
    ha = b * len(lines) + margin * (len(lines) - 1) + padding * 2
    im = Image.new('RGB', (wa, ha), color=(255, 255, 255))
    draw = ImageDraw.Draw(im)
    for index, line in enumerate(lines):
        draw.text((padding, padding + index * (margin + b)), line, font=font, fill=(0, 0, 0))
    return im


def text_to_bytes_io(text: str) -> BytesIO:
    bio = BytesIO()
    text_to_image(text).save(bio, format='PNG')
    bio.seek(0)
    return bio


def image_to_base64(img: Image.Image, format='PNG') -> str:
    output_buffer = BytesIO()
    img.save(output_buffer, format)
    byte_data = output_buffer.getvalue()
    base64_str = base64.b64encode(byte_data).decode()
    return 'base64://' + base64_str
