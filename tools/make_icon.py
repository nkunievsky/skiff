"""Generate Skiff's app icon as a 1024x1024 PNG.

Run this and then `build_app.sh` to update the bundled icon.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT_PNG = ROOT / "assets" / "icon_1024.png"

SIZE = 1024
RADIUS = 224  # ~22% — Apple's modern app-icon corner radius

# Palette
BG_DEEP = (16, 84, 161, 255)        # deep ocean blue
BG_SHEEN = (60, 140, 230, 90)       # subtle top-light overlay
WAVE_LIGHT = (255, 255, 255, 220)
WAVE_FAINT = (255, 255, 255, 110)
WHITE = (255, 255, 255, 255)
SAIL_ACCENT = (250, 220, 100, 255)  # warm yellow on the small jib for pop
SHADOW = (0, 0, 0, 70)


def make_icon() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded background
    d.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=RADIUS, fill=BG_DEEP)

    # Top-half lighter overlay so the icon has a bit of "sky" feel
    overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle((0, 0, SIZE - 1, SIZE // 2), radius=RADIUS, fill=BG_SHEEN)
    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img)

    # ----- waves (decorative, behind hull) -----
    wave_baseline = 720
    for offset, alpha in ((0, WAVE_FAINT), (40, WAVE_LIGHT)):
        y = wave_baseline + offset + 60
        pts = []
        amp = 18
        period = 160
        for x in range(80, SIZE - 80, 8):
            import math
            yy = y - amp * math.sin((x / period) * math.pi * 2)
            pts.append((x, yy))
        d.line(pts, fill=alpha, width=10)

    # ----- shadow under hull -----
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse((180, 740, SIZE - 180, 820), fill=SHADOW)
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    img = Image.alpha_composite(img, shadow)
    d = ImageDraw.Draw(img)

    # ----- hull (white, slightly curved trapezoid) -----
    hull_top_y = 700
    hull_bot_y = 790
    hull_top_inset = 200
    hull_bot_inset = 290
    hull = [
        (hull_top_inset, hull_top_y),
        (SIZE - hull_top_inset, hull_top_y),
        (SIZE - hull_bot_inset, hull_bot_y),
        (hull_bot_inset, hull_bot_y),
    ]
    d.polygon(hull, fill=WHITE)
    # Hull underline accent stripe
    d.rectangle(
        (hull_top_inset, hull_top_y - 14, SIZE - hull_top_inset, hull_top_y - 4),
        fill=SAIL_ACCENT,
    )

    # ----- mast -----
    mast_x = SIZE // 2 - 18
    mast_top = 230
    d.rectangle((mast_x - 8, mast_top, mast_x + 8, hull_top_y), fill=WHITE)

    # ----- mainsail (large white triangle, billowing right) -----
    mainsail = [
        (mast_x + 8, mast_top + 20),
        (mast_x + 8, hull_top_y - 10),
        (SIZE - hull_top_inset - 70, hull_top_y - 10),
    ]
    d.polygon(mainsail, fill=WHITE)
    # Subtle sail "fold" line for depth
    d.line(
        [(mast_x + 8, mast_top + 60),
         (SIZE - hull_top_inset - 110, hull_top_y - 14)],
        fill=(220, 230, 245, 200), width=6,
    )

    # ----- jib (small accent sail to the left of mast) -----
    jib = [
        (mast_x - 8, mast_top + 80),
        (mast_x - 8, hull_top_y - 10),
        (hull_top_inset + 90, hull_top_y - 10),
    ]
    d.polygon(jib, fill=SAIL_ACCENT)

    return img


def main() -> int:
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    img = make_icon()
    img.save(OUT_PNG, "PNG")
    print(f"wrote {OUT_PNG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
