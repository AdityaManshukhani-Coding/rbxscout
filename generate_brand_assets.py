#!/usr/bin/env python3
"""UpScale Scouting Tool brand asset generator.

One command rebuilds every logo/favicon asset from code, so the brand never
drifts between hand-edited exports:

    python generate_brand_assets.py

Outputs (all under assets/brand/):
    logo-mark.svg            transparent mark: U in currentColor + orange arrow
    logo-mark-badge.svg      two-tone mark on the black app-icon badge
    wordmark.svg             full lockup: badge mark + "UpScale" type
    logo-mark-512.png        raster badge (sidebar / gate rendering)
    logo-mark-192.png
    favicon/favicon.ico      multi-size Windows/browser favicon
    favicon/favicon-16.png   favicon-32.png, apple-touch-icon.png (180)

The mark is the owner's logo: a bold "U" whose right stem launches an upward
arrow — up (the U) and scale (the arrow). Palette tokens live in BRAND.md and
must stay in sync with .streamlit/config.toml.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BRAND_DIR = Path(__file__).resolve().parent / "assets" / "brand"
FAVICON_DIR = BRAND_DIR / "favicon"

# Brand tokens (BRAND.md is the source of truth for usage rules).
INK = (11, 14, 20)        # #0B0E14 page/background and badge fill
HAIRLINE = (38, 42, 51)   # #262A33 borders
ACCENT = (255, 110, 1)    # #FF6E01 UpScale Orange (sampled from the owner's logo)
TEXT = (231, 236, 243)    # #E7ECF3 primary text
MUTED = (154, 164, 178)   # #9AA4B2 captions
WHITE = (254, 254, 254)   # #FEFEFE the U, as drawn in the owner's logo

# Mark geometry, in 64-canvas units (scaled up for raster output).
MARK_BOX = 64
U_STEM_X_LEFT = 19        # centerline of the left stem
U_STEM_X_RIGHT = 45       # centerline of the right stem (becomes the arrow shaft)
U_TOP_Y = 15              # stem tops
U_BOWL_Y = 32             # where the bowl arc starts
U_BOWL_R = 13             # bowl radius (centerline)
STROKE_W = 11             # U stroke width (centerline units)
ARROW_SHAFT_TOP = 9       # arrow shaft reaches above the stem top
ARROW_HEAD_TIP = (45, 1)
ARROW_HEAD_HALF_W = 13.5  # head is ~2.5x the stem width, like the logo
ARROW_HEAD_BASE_Y = 15.5


def _draw_mark(img: Image.Image, ox: float, oy: float, scale: float,
               u_color: tuple[int, int, int, int]) -> None:
    """Draw the U + arrow mark onto `img` offset by (ox, oy) at `scale` px/unit."""
    d = ImageDraw.Draw(img)
    w = STROKE_W * scale
    s = scale

    def x(v: float) -> float:
        return ox + v * s

    def y(v: float) -> float:
        return oy + v * s

    half = w / 2

    # The U: left stem, bowl, right stem (white in the two-tone badge version).
    d.line([x(U_STEM_X_LEFT), y(U_TOP_Y), x(U_STEM_X_LEFT), y(U_BOWL_Y)],
           fill=u_color, width=int(w))
    d.arc([x(U_STEM_X_LEFT - U_BOWL_R), y(U_BOWL_Y - U_BOWL_R),
           x(U_STEM_X_RIGHT + U_BOWL_R), y(U_BOWL_Y + U_BOWL_R)],
          start=0, end=180, fill=u_color, width=int(w))
    d.line([x(U_STEM_X_RIGHT), y(U_BOWL_Y), x(U_STEM_X_RIGHT), y(ARROW_SHAFT_TOP + 4)],
           fill=u_color, width=int(w))

    # The arrow: shaft rides the right stem, head launches upward (orange).
    d.rectangle([x(U_STEM_X_RIGHT - STROKE_W / 2), y(ARROW_SHAFT_TOP),
                 x(U_STEM_X_RIGHT + STROKE_W / 2), y(U_BOWL_Y)],
                fill=ACCENT + (255,))
    hx, hy = ARROW_HEAD_TIP
    d.polygon(
        [
            (x(hx), y(hy)),
            (x(hx - ARROW_HEAD_HALF_W), y(ARROW_HEAD_BASE_Y)),
            (x(hx + ARROW_HEAD_HALF_W), y(ARROW_HEAD_BASE_Y)),
        ],
        fill=ACCENT + (255,),
    )


def draw_badge(size: int) -> Image.Image:
    """Render the full badge (black rounded square + two-tone mark) at `size` px."""
    canvas = 512
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Badge: near-black rounded square with a hairline inner border.
    d.rounded_rectangle([0, 0, canvas - 1, canvas - 1], radius=116, fill=INK + (255,))
    d.rounded_rectangle([12, 12, canvas - 13, canvas - 13], radius=106,
                        outline=HAIRLINE + (255,), width=4)

    # Center the mark: its 64-unit box spans x 12.5..58.5, y 1..51.5.
    scale = 300 / 64  # mark occupies ~300px of the 512px badge
    _draw_mark(img, ox=76, oy=116, scale=scale, u_color=WHITE + (255,))

    if size != canvas:
        img = img.resize((size, size), Image.LANCZOS)
    return img


MARK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">
  <!-- UpScale mark: the U launches the arrow. The U uses currentColor so it
       recolors with context; the arrow always stays UpScale Orange. The
       two-tone badge version is logo-mark-badge.svg. -->
  <path d="M19 15 V32 A13 13 0 0 0 45 32 V13" stroke="currentColor" stroke-width="11"/>
  <rect x="39.5" y="9" width="11" height="12" fill="#FF6E01"/>
  <path d="M45 1 L31.5 15.5 H58.5 Z" fill="#FF6E01"/>
</svg>
"""

MARK_BADGE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">
  <!-- UpScale badge mark: the two-tone U + arrow on the black app-icon badge. -->
  <rect width="64" height="64" rx="14.5" fill="#0B0E14"/>
  <rect x="1.5" y="1.5" width="61" height="61" rx="13" stroke="#262A33" stroke-width="1"/>
  <path d="M19 15 V32 A13 13 0 0 0 45 32 V13" stroke="#FEFEFE" stroke-width="11"/>
  <rect x="39.5" y="9" width="11" height="12" fill="#FF6E01"/>
  <path d="M45 1 L31.5 15.5 H58.5 Z" fill="#FF6E01"/>
</svg>
"""

WORDMARK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 84" fill="none">
  <!-- UpScale Scouting Tool lockup: badge mark + name treatment.
       "Up" set bold in text color, "Scale" set bold in UpScale Orange;
       "SCOUTING TOOL" is the eyebrow beneath. -->
  <g transform="translate(10,10) scale(1.0)">
    <rect width="64" height="64" rx="14.5" fill="#0B0E14"/>
    <rect x="1.5" y="1.5" width="61" height="61" rx="13" stroke="#262A33" stroke-width="1"/>
    <path d="M19 15 V32 A13 13 0 0 0 45 32 V13" stroke="#FEFEFE" stroke-width="11"/>
    <rect x="39.5" y="9" width="11" height="12" fill="#FF6E01"/>
    <path d="M45 1 L31.5 15.5 H58.5 Z" fill="#FF6E01"/>
  </g>
  <text x="92" y="46" font-family="'Avenir Next','Segoe UI',Helvetica,Arial,sans-serif"
        font-size="32" font-weight="700" letter-spacing="0.5" fill="#E7ECF3">Up<tspan fill="#FF6E01">Scale</tspan></text>
  <text x="94" y="70" font-family="'Avenir Next','Segoe UI',Helvetica,Arial,sans-serif"
        font-size="11" font-weight="500" letter-spacing="3" fill="#9AA4B2">SCOUTING TOOL</text>
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
