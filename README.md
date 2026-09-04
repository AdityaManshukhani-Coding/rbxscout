# 🕹️ RbxScout — 24/7 Roblox Game Scout

A self-running scout for Roblox games: it **discovers** games from live charts
and a keyword crawler, **hydrates** them with real-time metrics (CCU, visits,
favorites, creator), **classifies** every game into a refresh tier, and flags
the ones blowing up into a **New and Upcoming** watchlist — with Discord
contact discovery on top.

The catalog lives in this repo (`rbx_scout.db`, SQLite) and is refreshed
around the clock by **GitHub Actions** — no server, no machine left on.

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Streamlit](https://img.shields.io/badge/streamlit-1.39%2B-red)

## How it runs 24/7

- `.github/workflows/sync.yml` runs the full pipeline **every 30 minutes** on
  GitHub's runners (free for public repos).
- Each run: run tests → discover (front-page charts + next 100-keyword slice
  of the 661-word dictionary) → hydrate new finds + tier-due games within the
  per-sync budget → tier-stamp → blow-up-flag → prune → **commit the updated
  DB back to this repo**.
- The repo *is* the persistent database: every run starts from the last
  committed catalog, so nothing is ever lost between runs.
- Watch any run live in the **Actions** tab; failures email the repo owner.

## The tiered catalog

| Tier | Meaning | Refresh cadence |
|---|---|---|
| New | not yet hydrated | first, every sync |
| T1–T2 | the pre-blowup watchlist (small + qualifying) | every sync |
| T3–T4 | warm | every 2nd / 3rd sync |
| T5–T7 | big games | weekly |
| T8 | below all thresholds | slow rotating slice; pruned after 14 days stale |

Classification is **higher-axis**: a game's tier is the higher of its
visits-axis tier and CCU-axis tier (CCU is the leading indicator of a
blow-up; visits lag). A 1M-visit/30-CCU corpse lands cold; a 30k-visit/300-CCU
rocket lands warm. Games that climb 2+ tiers or triple their CCU between
syncs get flagged → **New and Upcoming**.

## Run it yourself

```bash
pip install -r requirements.txt
python live_sync.py          # one full sync from the terminal
python -m pytest tests -q    # test suite
streamlit run app.py         # the dashboard (reads the committed DB too)
```

## Data sources

All public, cookieless endpoints — no credentials anywhere in this repo:

| Stage | Source |
|---|---|
| Discovery | `apis.roblox.com/explore-api/v1/get-sorts`, `search-api/omni-search` |
| Bulk index | `api.rolimons.com/games/v1/gamelist` |
| Metrics | `games.roblox.com/v1/games` (50 universes/batch) |
| Icons | `thumbnails.roblox.com/v1/games/icons` |

Rate limiting is handled by an adaptive token-bucket pacer (stretches on 429,
decays on 200) with `Retry-After` backoff; hydration respects Roblox's
per-window quota and rolls overflow to the next sync.

## Dashboard

`streamlit run app.py` gives you the full dashboard: filterable catalog,
per-page Discord contact checks, per-tier diagnostics, and the **New and
Upcoming** tab (the blow-up watch). It reads the same SQLite catalog this
repo keeps fresh.
