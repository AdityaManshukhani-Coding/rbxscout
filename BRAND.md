# Studio Scouts — Brand Identity

One source of truth for the mark, the palette, the type and the naming.
Every surface (dashboard, gate, README, pipeline identity) draws from here;
`.streamlit/config.toml` and `generate_brand_assets.py` must stay in sync.

## Name

**Studio Scouts** — no emoji in the product name, no "Rbx" prefix. "Studio"
is the buyer; "Scouts" is what the tool does. Written as two words, both
capitalized. The old codename (RbxScout) survives only in technical plumbing
that is expensive to rename (the SQLite catalog filename `rbx_scout.db`, the
repo slug, cache paths) — never in anything a user reads. Internal
identifiers may use the `ss_` prefix (Studio Scouts).

## The mark

A **radar**: the ring is the watch, the sweep is the daily scan, the blip is
the contact found. Drawn as geometric strokes so it stays crisp from 16 px
favicon to 512 px app icon.

| File | Use |
| --- | --- |
| `assets/brand/logo-mark.svg` | Inline/monochrome contexts; strokes use `currentColor` |
| `assets/brand/logo-mark-badge.svg` | App-icon contexts (dark badge baked in) |
| `assets/brand/wordmark.svg` | Full lockup for docs, README, social cards |
| `assets/brand/logo-mark-{192,512}.png` | Raster badge for UI surfaces that need PNG |
| `assets/brand/favicon/*` | Browser favicon set + apple-touch-icon |

Regenerate everything with `python generate_brand_assets.py` (Pillow).

## Palette

| Token | Hex | Role |
| --- | --- | --- |
| Ink | `#0B0E14` | Page background (darker than the old `#0d1117` — quieter, more "instrument panel") |
| Panel | `#12161E` | Raised surfaces: cards, sidebar, table body |
| Hairline | `#262A33` | Borders, dividers, table grid |
| Scout Green | `#2BD98A` | THE brand color. Primary buttons, active states, the sweep, positive deltas |
| Text | `#E7ECF3` | Primary text |
| Muted | `#9AA4B2` | Captions, secondary text |

Scout Green replaces Discord blurple (`#5865F2`) as the primary. Discord's
own logo keeps its blue inside the contact column — that is Discord's brand,
not ours. Red stays `#EF4444` for negative deltas only.

## Type

System sans stack (Streamlit default) — no webfont, keeps Cloud deploys fast.
Wordmark uses the same stack, weight 500 for "Studio" and 700 for "SCOUTS";
labels/eyebrows are uppercase with wide letter-spacing (0.05em+).

## Voice

Tool, not toy. No emoji in headings, buttons or the product name (emoji stay
only in game titles coming from Roblox data). Short imperative labels:
"Sync live data", "Check Discord servers".
