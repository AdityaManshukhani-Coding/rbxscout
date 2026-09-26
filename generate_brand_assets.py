#!/usr/bin/env python3
"""Studio Scouts brand asset generator.

One command rebuilds every logo/favicon asset from code, so the brand never
drifts between hand-edited exports:

    python generate_brand_assets.py

Outputs (all under assets/brand/):
    logo-mark.svg            transparent mark, strokes use currentColor
    logo-mark-badge.svg      mark on the dark app-icon badge
    wordmark.svg             full lockup: badge mark + "Studio Scouts" type
    logo-mark-512.png        raster badge (sidebar / gate rendering)
    logo-mark-192.png
    favicon/favicon.ico      multi-size Windows/browser favicon
    favicon/favicon-16.png   favicon-32.png, apple-touch-icon.png (180)

The mark is a radar: the ring is the watch, the sweep is the daily scan, the
blip is the contact found. Palette tokens live in BRAND.md and must stay in
sync with .streamlit/config.toml.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

BRAND_DIR = Path(__file__).resolve().parent / "assets" / "brand"
FAVICON_DIR = BRAND_DIR / "favicon"

# Brand tokens (BRAND.md is the source of truth for usage rules).
INK = (11, 14, 20)        # #0B0E14 page/background
PANEL = (18, 22, 30)      # #12161E raised surface
HAIRLINE = (38, 42, 51)   # #262A33 borders
ACCENT = (43, 217, 138)   # #2BD98A Scout Green
TEXT = (231, 236, 243)    # #E7ECF3 primary text

# Radar geometry, in 512-canvas units.
CANVAS = 512
CENTER = CANVAS // 2
RING_RADIUS = 160         # the watch ring
RING_WIDTH = 20
SWEEP_START = 270         # PIL angles: 0 = 3 o'clock, clockwise; 270 = 12 o'clock
SWEEP_END = 330           # 60-degree sweep, the daily pass
BLIP_ANGLE = -35          # degrees, standard math convention (up-right)
BLIP_DISTANCE = RING_RADIUS * 0.80


def _polar(distance: float, degrees: float) -> tuple[float, float]:
    rad = math.radians(degrees)
    return CENTER + distance * math.cos(rad), CENTER - distance * math.sin(rad)


def draw_badge(size: int) -> Image.Image:
    """Render the full badge (dark rounded square + radar mark) at `size` px."""
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Badge: near-black rounded square with a hairline inner border.
    d.rounded_rectangle([0, 0, CANVAS - 1, CANVAS - 1], radius=116, fill=INK + (255,))
    d.rounded_rectangle([12, 12, CANVAS - 13, CANVAS - 13], radius=106,
                        outline=HAIRLINE + (255,), width=4)

    # The watch: a faint full ring under a bright sweep trail.
    box = [CENTER - RING_RADIUS, CENTER - RING_RADIUS,
           CENTER + RING_RADIUS, CENTER + RING_RADIUS]
    d.arc(box, start=0, end=360, fill=ACCENT + (64,), width=6)

    # Sweep trail: translucent wedge + bright leading edge.
    wedge = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(wedge).pieslice(box, start=SWEEP_START, end=SWEEP_END,
                                   fill=ACCENT + (52,))
    img = Image.alpha_composite(img, wedge)
    d = ImageDraw.Draw(img)
    d.arc(box, start=SWEEP_START, end=SWEEP_END, fill=ACCENT + (255,), width=RING_WIDTH)

    # Contact blip: the scout found something. Soft glow, then solid dot.
    bx, by = _polar(BLIP_DISTANCE, BLIP_ANGLE)
    glow_radius = 46
    d.ellipse([bx - glow_radius, by - glow_radius, bx + glow_radius, by + glow_radius],
              fill=ACCENT + (36,))
    blip_radius = 26
    d.ellipse([bx - blip_radius, by - blip_radius, bx + blip_radius, by + blip_radius],
              fill=ACCENT + (255,))

    # Radar origin.
    origin_radius = 12
    d.ellipse([CENTER - origin_radius, CENTER - origin_radius,
               CENTER + origin_radius, CENTER + origin_radius],
              fill=ACCENT + (150,))

    if size != CANVAS:
        img = img.resize((size, size), Image.LANCZOS)
    return img


MARK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">
  <!-- Studio Scouts mark: the radar. Ring = the watch, wedge = the daily
       sweep, dot = the contact found. Strokes use currentColor so the mark
       recolors with context; the badge version is logo-mark-badge.svg. -->
  <circle cx="32" cy="32" r="19" stroke="currentColor" stroke-opacity="0.35" stroke-width="1.6"/>
  <path d="M32 32 L32 13 A19 19 0 0 1 47.63 22.75 Z" fill="currentColor" fill-opacity="0.14"/>
  <path d="M32 13 A19 19 0 0 1 47.63 22.75" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"/>
  <circle cx="44.5" cy="20.2" r="3.4" fill="currentColor"/>
  <circle cx="32" cy="32" r="1.7" fill="currentColor" fill-opacity="0.55"/>
</svg>
"""

MARK_BADGE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">
  <!-- Studio Scouts badge mark: the radar on the app-icon dark badge. -->
  <rect width="64" height="64" rx="14.5" fill="#0B0E14"/>
  <rect x="1.5" y="1.5" width="61" height="61" rx="13" stroke="#262A33" stroke-width="1"/>
  <g stroke="#2BD98A">
    <circle cx="32" cy="32" r="19" stroke-opacity="0.35" stroke-width="1.6"/>
    <path d="M32 32 L32 13 A19 19 0 0 1 47.63 22.75 Z" fill="#2BD98A" fill-opacity="0.14" stroke="none"/>
    <path d="M32 13 A19 19 0 0 1 47.63 22.75" stroke-width="3.2" stroke-linecap="round"/>
  </g>
  <circle cx="44.5" cy="20.2" r="3.4" fill="#2BD98A"/>
  <circle cx="32" cy="32" r="1.7" fill="#2BD98A" fill-opacity="0.55"/>
</svg>
"""

WORDMARK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 460 84" fill="none">
  <!-- Studio Scouts lockup: badge mark + name treatment.
       "Studio" set regular in text color, "Scouts" set bold in Scout Green. -->
  <g transform="translate(10,10) scale(1.0)">
    <rect width="64" height="64" rx="14.5" fill="#0B0E14"/>
    <rect x="1.5" y="1.5" width="61" height="61" rx="13" stroke="#262A33" stroke-width="1"/>
    <g stroke="#2BD98A">
      <circle cx="32" cy="32" r="19" stroke-opacity="0.35" stroke-width="1.6"/>
      <path d="M32 32 L32 13 A19 19 0 0 1 47.63 22.75 Z" fill="#2BD98A" fill-opacity="0.14" stroke="none"/>
      <path d="M32 13 A19 19 0 0 1 47.63 22.75" stroke-width="3.2" stroke-linecap="round"/>
    </g>
    <circle cx="44.5" cy="20.2" r="3.4" fill="#2BD98A"/>
    <circle cx="32" cy="32" r="1.7" fill="#2BD98A" fill-opacity="0.55"/>
  </g>
  <text x="92" y="40" font-family="'Avenir Next','Segoe UI',Helvetica,Arial,sans-serif"
        font-size="30" font-weight="500" letter-spacing="1" fill="#E7ECF3">Studio</text>
  <text x="92" y="72" font-family="'Avenir Next','Segoe UI',Helvetica,Arial,sans-serif"
        font-size="30" font-weight="700" letter-spacing="1" fill="#2BD98A">SCOUTS</text>
  <text x="196" y="72" font-family="'Avenir Next','Segoe UI',Helvetica,Arial,sans-serif"
        font-size="11" font-weight="500" letter-spacing="2.6" fill="#9AA4B2">ROBLOX GAME SCOUTING</text>
</svg>
"""


def main() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    FAVICON_DIR.mkdir(parents=True, exist_ok=True)

    (BRAND_DIR / "logo-mark.svg").write_text(MARK_SVG)
    (BRAND_DIR / "logo-mark-badge.svg").write_text(MARK_BADGE_SVG)
    (BRAND_DIR / "wordmark.svg").write_text(WORDMARK_SVG)

    draw_badge(512).save(BRAND_DIR / "logo-mark-512.png")
    draw_badge(192).save(BRAND_DIR / "logo-mark-192.png")

    icon_256 = draw_badge(256)
    icon_256.save(
        FAVICON_DIR / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )
    draw_badge(16).save(FAVICON_DIR / "favicon-16.png")
    draw_badge(32).save(FAVICON_DIR / "favicon-32.png")
    draw_badge(180).save(FAVICON_DIR / "apple-touch-icon.png")

    print(f"brand assets written to {BRAND_DIR}")


if __name__ == "__main__":
    main()
