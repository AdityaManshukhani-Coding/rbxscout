# 🕹️ RbxScout — 24/7 Roblox Game Scout

A self-running scout for Roblox games: it **discovers** games from live charts
and a keyword crawler, **hydrates** them with real-time metrics (CCU, visits,
favorites, creator), **classifies** every game into a refresh tier, and flags
the ones blowing up into a **New and Upcoming** watchlist — with Discord
contact discovery on top.

The catalog lives in this repo (`rbx_scout.db`, SQLite) and is refreshed
around the clock by a **Cloudflare Cron Trigger dispatching GitHub Actions**.
Cloudflare owns the clock; GitHub Actions remains the execution engine and
commits the database back to this repo.

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Streamlit](https://img.shields.io/badge/streamlit-1.39%2B-red)

## How it runs 24/7

A Cloudflare Worker is the only automatic scheduler. Its one Cron Trigger
runs every five minutes in UTC:

- **Hydrator** — the Worker dispatches `.github/workflows/hydrator.yml` every
  5 minutes through GitHub's `workflow_dispatch` API. It refreshes stats for
  games *already* in the catalog by draining the tier-due queue. No discovery
  traffic; tiers that are not due cost zero requests.
- **Finder** — at UTC minute `00` and `30`, the same Worker also dispatches
  `.github/workflows/finder.yml`. It discovers games from front-page charts,
  the Rolimons pool, and the next 100-keyword slice, then hydrates new games.

The GitHub workflow files intentionally contain **no `schedule:` triggers**.
They retain `workflow_dispatch` for Cloudflare and manual runs only, so an
old GitHub cron cannot wake up later and fight the Cloudflare scheduler.

Each run: work → tier-stamp → blow-up-flag → prune → **commit the updated
DB back to this repo**. The repo *is* the persistent database: every run
starts from the last committed catalog, so nothing is ever lost between
runs.

**Race safety:** the DB is one binary SQLite file — git cannot line-merge
it, so two workflows pushing simultaneously would silently drop one run's
data. Both workflows therefore declare the same Actions concurrency group
(`rbxscout-sync`), which GitHub enforces as a repo-wide mutex. Cloudflare may
dispatch Hydrator and Finder together at `:00`/`:30`; GitHub serializes them.
The workflows also use GitHub's `queue: max` setting so pending dispatches are
not silently replaced while another run owns the lock.


## The tiered catalog

| Tier | Meaning | Refresh cadence |
|---|---|---|
| New | not yet hydrated | first, every finder run |
| T1 | hot watchlist | when stale > 1h |
| T2 | pre-blowup watchlist | when stale > 2h |
| T3 | warm | when stale > 4h |
| T4 | warm | when stale > 6h |
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

## Cloudflare scheduler setup

The existing Worker in [`cloudflare-worker/`](cloudflare-worker/) now combines
the Roblox search proxy with the scheduler. Deploy it once, then Cloudflare
becomes the only automatic clock for RbxScout.

### One-time setup checklist

1. Push the workflow and Worker changes to the repository's `main` branch.
   Until the new workflow files are on `main`, GitHub may still have the old
   schedule definitions.
2. Create a GitHub **fine-grained personal access token** for only
   `AdityaManshukhani-Coding/rbxscout` with **Actions: Read and write**
   permission. Use an expiration and rotate it periodically.
3. From `cloudflare-worker/`, install Wrangler and authenticate:
   ```bash
   npm install
   npx wrangler login
   ```
4. Store the token as a Cloudflare secret. Do not put it in this repository,
   `wrangler.toml`, GitHub Actions variables, or frontend code:
   ```bash
   npx wrangler secret put GITHUB_TOKEN
   ```
   Paste the token only when Wrangler prompts for it.
5. Deploy the Worker:
   ```bash
   npx wrangler deploy
   ```
   The `wrangler.toml` configuration creates one UTC Cron Trigger:
   `*/5 * * * *`. Cloudflare invokes Hydrator on every tick and Finder at
   UTC `:00` and `:30`. Trigger changes can take several minutes to propagate.
6. Confirm the Worker deployment's **Cron Triggers** page shows exactly one
   trigger: `*/5 * * * *`.
7. Confirm GitHub Actions shows both workflows as active and that their files
   list only `workflow_dispatch` under `on:`—there must be no `schedule:`.
8. Use **Run workflow** once manually for each workflow to validate the
   GitHub token, permissions, Python environment, database commit, and push.
   Then watch the next Cloudflare tick in the Actions tab.

The scheduler dispatches the GitHub API endpoints for `hydrator.yml` and
`finder.yml` with `{"ref":"main"}`. A successful API dispatch means GitHub
accepted the run; the Actions page remains the source of truth for whether
the runner completed successfully.

### Optional search-proxy setup

The same Worker still mirrors only the public, cookieless Roblox omni-search
route. After deployment, note its `workers.dev` URL and configure the GitHub
repository variable `RBXSCOUT_SEARCH_PROXY_URLS` under **Settings → Secrets
and variables → Actions → Variables**. Use the URL (or comma-separated URLs)
without a trailing slash. The Finder workflow forwards this variable to the
crawler; the Hydrator never uses search traffic.

Smoke-test the proxy:
```bash
curl -s "https://rbx-search-proxy.<you>.workers.dev/search-api/omni-search?searchQuery=obby&pageType=all&sessionId=test" | head -c 300
```

No proxy variable? The crawler falls back to direct Roblox requests.

## Keyword-crawler IP pool (Cloudflare Worker proxy)

GitHub Actions runners share a small egress IP range, so the omni-search
endpoint can throttle syncs. The keyword crawler therefore routes each
request through a **fallback IP pool**: your Cloudflare Worker mirror(s)
first, then direct Roblox. A proxy that fails three consecutive keywords is
benched for five minutes. This is independent of the Worker's scheduler;
the same deployment provides both capabilities.

## Dashboard

`streamlit run app.py` gives you the full dashboard: filterable catalog,
per-page Discord contact checks, per-tier diagnostics, and the **New and
Upcoming** tab (the blow-up watch). It reads the same SQLite catalog this
repo keeps fresh.
