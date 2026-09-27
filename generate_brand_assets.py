#!/usr/bin/env python3
"""UpScale Scouting Tool brand asset generator.

The canonical mark is the owner's logo image: assets/brand/source-mark.png
(white U launching an orange arrow on transparency). This script never
redraws the mark — it composes the real image into every raster the product
needs, so the brand can't drift from the source:

    python generate_brand_assets.py

Outputs (all under assets/brand/):
    logo-mark.png            the source mark, normalized to 512px transparent
    logo-mark-badge.png      mark on the black app-icon badge (512)
    logo-mark-192.png        badge raster for the sidebar / gate
    favicon/favicon.ico      multi-size Windows/browser favicon
    favicon/favicon-16.png   favicon-32.png, apple-touch-icon.png (180)

Palette tokens live in BRAND.md and must stay in sync with
.streamlit/config.toml. UpScale Orange #FF6E01 is sampled from this image.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BRAND_DIR = Path(__file__).resolve().parent / "assets" / "brand"
FAVICON_DIR = BRAND_DIR / "favicon"
SOURCE = BRAND_DIR / "source-mark.png"

# Brand tokens (BRAND.md is the source of truth for usage rules).
INK = (11, 14, 20)        # #0B0E14 page/background and badge fill
HAIRLINE = (38, 42, 51)   # #262A33 badge border

CANVAS = 512


def load_mark() -> Image.Image:
    """Load the owner's logo, normalized to a square RGBA image."""
    if not SOURCE.exists():
        raise SystemExit(
            f"missing {SOURCE} — drop the owner's logo there (transparent "
            "background, square) and re-run"
        )
    img = Image.open(SOURCE).convert("RGBA")
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return square


def draw_badge(mark: Image.Image, size: int) -> Image.Image:
    """Composite the real mark onto the black rounded-square app badge."""
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Badge: near-black rounded square with a hairline inner border.
    d.rounded_rectangle([0, 0, CANVAS - 1, CANVAS - 1], radius=116, fill=INK + (255,))
    d.rounded_rectangle([12, 12, CANVAS - 13, CANVAS - 13], radius=106,
                        outline=HAIRLINE + (255,), width=4)

    # The mark itself: ~78% of the badge, centered.
    mark_size = int(CANVAS * 0.78)
    scaled = mark.resize((mark_size, mark_size), Image.LANCZOS)
    offset = (CANVAS - mark_size) // 2
    img.alpha_composite(scaled, (offset, offset))

    if size != CANVAS:
        img = img.resize((size, size), Image.LANCZOS)
    return img


def main() -> None:
    FAVICON_DIR.mkdir(parents=True, exist_ok=True)
    mark = load_mark()

    mark.resize((CANVAS, CANVAS), Image.LANCZOS).save(BRAND_DIR / "logo-mark.png")
    draw_badge(mark, CANVAS).save(BRAND_DIR / "logo-mark-badge.png")
    draw_badge(mark, 192).save(BRAND_DIR / "logo-mark-192.png")

    icon_256 = draw_badge(mark, 256)
    icon_256.save(
        FAVICON_DIR / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )
    draw_badge(mark, 16).save(FAVICON_DIR / "favicon-16.png")
    draw_badge(mark, 32).save(FAVICON_DIR / "favicon-32.png")
    draw_badge(mark, 180).save(FAVICON_DIR / "apple-touch-icon.png")

    # The hand-drawn SVGs from the previous identity are retired — the real
    # image is the brand now. Remove them so no surface references a redraw.
    for old in ("logo-mark.svg", "logo-mark-badge.svg", "wordmark.svg"):
        (BRAND_DIR / old).unlink(missing_ok=True)

    print(f"brand assets written to {BRAND_DIR} (source: {SOURCE.name})")


if __name__ == "__main__":
    main()
