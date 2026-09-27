# UpScale Scouting Tool — Brand Identity

One source of truth for the mark, the palette, the type and the naming.
Every surface (dashboard, gate, README, pipeline identity) draws from here;
`.streamlit/config.toml` and `generate_brand_assets.py` must stay in sync.

## Name

**UpScale Scouting Tool** — no emoji in the product name, no "Rbx"/"Studio"
leftovers. "UpScale" is the promise to the buyer (grow the game); "Scouting
Tool" is what it is. Written as three words, each capitalized; "UpScale" is
one word with a capital U and S. Short form in prose: **UpScale**. The old
codenames (RbxScout, Studio Scouts) survive only in technical plumbing that
is expensive to rename (the SQLite catalog filename `rbx_scout.db`, the repo
slug, cache paths) — never in anything a user reads. Internal identifiers may
use the `us_` prefix.

## The mark

The owner's logo image, used verbatim: a bold white **U** whose right stem
launches an **orange upward arrow** — up (the U) and scale (the arrow). The
canonical file is `assets/brand/source-mark.png` (transparent background);
every other raster is composited from it, never redrawn.

| File | Use |
| --- | --- |
| `assets/brand/source-mark.png` | The canonical mark — do not edit; replace to rebrand |
| `assets/brand/logo-mark.png` | Mark alone, normalized 512px transparent |
| `assets/brand/logo-mark-badge.png` | Mark on the black app-icon badge (512) |
| `assets/brand/logo-mark-192.png` | Badge raster for sidebar / gate |
| `assets/brand/favicon/*` | Browser favicon set + apple-touch-icon |

Regenerate everything with `python generate_brand_assets.py` (Pillow).

## Palette

| Token | Hex | Role |
| --- | --- | --- |
| Ink | `#0B0E14` | Page background and the badge fill — quiet, lets the mark pop |
| Panel | `#12161E` | Raised surfaces: cards, sidebar, table body |
| Hairline | `#262A33` | Borders, dividers, table grid |
| UpScale Orange | `#FF6E01` | THE brand color. Primary buttons, active states, the arrow, positive deltas. Sampled from the owner's logo (`#FF6E01`) |
| Text | `#E7ECF3` | Primary text |
| Muted | `#9AA4B2` | Captions, secondary text |

UpScale Orange replaces Scout Green (`#2BD98A`) as the primary. Discord's
logo keeps its blue inside the contact column — that is Discord's brand,
not ours. Red stays `#EF4444` for negative deltas only. Never put orange
text on white; the palette is built dark-first.

## Type

System sans stack (Streamlit default) — no webfont, keeps Cloud deploys fast.
The UI wordmark is HTML/CSS: "Up**Scale**" bold 700 with "Scale" in
UpScale Orange; labels/eyebrows are uppercase with wide letter-spacing
(0.05em+).

## Voice

Tool, not toy. No emoji in headings, buttons or the product name (emoji stay
only in game titles coming from Roblox data). Short imperative labels:
"Sync live data", "Check Discord servers".
