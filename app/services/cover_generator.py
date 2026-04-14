"""
Generate book-cover-style images for novels.
Produces a 600x900 PNG with a color gradient, title, and author.
"""

import hashlib
import io
import textwrap
from PIL import Image, ImageDraw, ImageFont

# Color palettes — each tuple is (top_color, bottom_color, accent_color)
PALETTES = [
    ((30, 30, 60),    (10, 10, 30),    (200, 160, 80)),   # navy / gold
    ((40, 20, 20),    (15, 5, 5),      (220, 180, 120)),  # dark red / cream
    ((15, 40, 30),    (5, 15, 10),     (140, 200, 160)),  # forest / mint
    ((35, 25, 50),    (10, 5, 20),     (180, 140, 220)),  # purple / lavender
    ((20, 35, 50),    (5, 10, 20),     (100, 180, 220)),  # steel blue / sky
    ((45, 30, 15),    (15, 10, 5),     (220, 170, 100)),  # brown / amber
    ((40, 15, 35),    (15, 5, 12),     (220, 130, 180)),  # plum / rose
    ((10, 35, 40),    (3, 12, 15),     (80, 200, 200)),   # teal / cyan
]

# Font search paths — Linux (deploy), macOS (dev)
_FONT_PATHS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",    # Linux/Docker
    "/System/Library/Fonts/Helvetica.ttc",                       # macOS
    "/System/Library/Fonts/SFNSDisplay-Bold.otf",                # macOS SF
]
_FONT_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNSDisplay-Regular.otf",
]

WIDTH, HEIGHT = 600, 900


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    paths = _FONT_PATHS_BOLD if bold else _FONT_PATHS_REGULAR
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    # Pillow 10+ supports load_default with size
    return ImageFont.load_default(size=size)


def _pick_palette(title: str):
    idx = int(hashlib.md5(title.encode()).hexdigest(), 16) % len(PALETTES)
    return PALETTES[idx]


def _gradient(draw: ImageDraw.ImageDraw, top, bottom, width, height):
    for y in range(height):
        r = int(top[0] + (bottom[0] - top[0]) * y / height)
        g = int(top[1] + (bottom[1] - top[1]) * y / height)
        b = int(top[2] + (bottom[2] - top[2]) * y / height)
        draw.line([(0, y), (width, y)], fill=(r, g, b))


def _draw_text_block(draw, text, y_start, max_width, font_size, color, bold=False):
    """Draw wrapped text centered, return the y position after the last line."""
    font = _load_font(font_size, bold)

    chars_per_line = max(10, int(max_width / (font_size * 0.55)))
    lines = textwrap.wrap(text, width=chars_per_line)

    y = y_start
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        x = (WIDTH - tw) // 2
        draw.text((x, y), line, fill=color, font=font)
        y += int(font_size * 1.4)

    return y


def generate_cover(title: str, author: str | None = None) -> bytes:
    """Generate a cover image and return PNG bytes."""
    top, bottom, accent = _pick_palette(title)
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)

    # Background gradient
    _gradient(draw, top, bottom, WIDTH, HEIGHT)

    # Decorative top/bottom accent lines
    draw.rectangle([(40, 60), (WIDTH - 40, 65)], fill=accent)
    draw.rectangle([(40, HEIGHT - 65), (WIDTH - 40, HEIGHT - 60)], fill=accent)

    # Decorative border
    draw.rectangle([(30, 50), (WIDTH - 30, HEIGHT - 50)], outline=(*accent, 100), width=2)

    # Title — large, centered, vertically placed in upper third
    title_y = _draw_text_block(draw, title, 180, WIDTH - 100, 44, (255, 255, 255), bold=True)

    # Thin separator line
    sep_y = title_y + 35
    draw.rectangle([(WIDTH // 2 - 80, sep_y), (WIDTH // 2 + 80, sep_y + 2)], fill=accent)

    # Author — smaller, below separator
    if author:
        _draw_text_block(draw, f"by {author}", sep_y + 35, WIDTH - 120, 28, (*accent,))

    # App branding at bottom
    _draw_text_block(draw, "AUDIOBOOK READER", HEIGHT - 130, WIDTH - 80, 18,
                     (accent[0] // 2 + 60, accent[1] // 2 + 60, accent[2] // 2 + 60))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
