# 🕹️ RbxScout — 24/7 Roblox Game Scout

A self-running scout for Roblox games: it **discovers** games from live charts
and a keyword crawler, **hydrates** them with real-time metrics (CCU, visits,
favorites, creator), **classifies** every game into a refresh tier, and flags
the ones blowing up into a **New and Upcoming** watchlist — with Discord
contact discovery on top.

The catalog (`rbx_scout.db`, SQLite) is stored as the **first asset of a
rolling GitHub Release** (tag `catalog-latest`) in this repo, refreshed
around the clock by a **Cloudflare Cron Trigger dispatching GitHub Actions**.
Cloudflare owns the clock; GitHub Actions remains the execution engine and
swaps the release asset after every run. No credit card, no LFS, no
third-party database — a release asset gives 2 GiB of headroom with no
bandwidth limit, authenticated by the `GITHUB_TOKEN` the workflows already
have.

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Streamlit](https://img.shields.io/badge/streamlit-1.39%2B-red)

### Live catalog counts (auto-refresh)

Every Hydrator/Finder push regenerates `stats.json` + `stats_target.json` as
release assets, so these badges track the catalog in near-real time — no
commits, no manual updates. (They can lag a few minutes behind the last sync
because the badge CDN caches.) The raw feeds are the
[`stats.json`](https://github.com/AdityaManshukhani-Coding/rbxscout/releases/download/catalog-latest/stats.json)
and
[`stats_target.json`](https://github.com/AdityaManshukhani-Coding/rbxscout/releases/download/catalog-latest/stats_target.json)
assets of the [`catalog-latest` release](https://github.com/AdityaManshukhani-Coding/rbxscout/releases/tag/catalog-latest),
whose page header also shows the counts + last-sync time.

[![Catalog size](https://badgen.net/https/github.com/AdityaManshukhani-Coding/rbxscout/releases/download/catalog-latest/stats.json?icon=roblox)](https://github.com/AdityaManshukhani-Coding/rbxscout/releases/tag/catalog-latest)
[![Games matching target](https://badgen.net/https/github.com/AdityaManshukhani-Coding/rbxscout/releases/download/catalog-latest/stats_target.json)](https://github.com/AdityaManshukhani-Coding/rbxscout/releases/tag/catalog-latest)

## How it runs 24/7

A Cloudflare Worker is the only automatic scheduler. Its one Cron Trigger
runs every five minutes in UTC:

- **Hydrator** — the Worker dispatches `.github/workflows/hydrator.yml` every
  5 minutes through GitHub's `workflow_dispatch` API. It refreshes stats for
  games *already* in the catalog by draining the tier-due queue. No discovery
  traffic; tiers that are not due cost zero requests.
- **Expander (Atlas Dev harvest + drain)** — on the clean `:15`/`:45` minutes (every
  30 min), the Worker dispatches `.github/workflows/expander.yml`: the sole
  discovery pipeline. A daily 24h-throttled harvest of
  [atlasdev.gg/analyze](https://atlasdev.gg/analyze) enqueues mid-tier universe
  IDs (≥20k visits, ≥25 CCU), and every run drains a bounded slice through the
  strict gate. The legacy finders (deep-charts/keyword crawl, creator
  spiderweb, frontier scan, recommendations mining) were **removed
  2026-09-20** — Atlas Dev is the only discovery source. Rates and knobs are
  documented in [EXPANSION_PILOT.md](EXPANSION_PILOT.md) and
  [ATLAS_PLAN_REVIEW.md](ATLAS_PLAN_REVIEW.md).

The GitHub workflow files intentionally contain **no `schedule:` triggers**.
They retain `workflow_dispatch` for Cloudflare and manual runs only, so an
old GitHub cron cannot wake up later and fight the Cloudflare scheduler.

Each run: **pull the catalog from the `catalog-latest` release** → work →
tier-stamp → blow-up-flag → prune → **push the catalog back to the release**.
The release asset *is* the persistent database: every run starts from the
last pushed catalog, so nothing is ever lost between runs. Git history now
carries only code — the database never bloats a commit again.

**Race safety:** the DB is one binary SQLite file — git cannot line-merge
it, so two workflows pushing simultaneously would silently drop one run's
data. Both workflows therefore declare the same Actions concurrency group
(`rbxscout-sync`), which GitHub enforces as a repo-wide mutex. Cloudflare may
dispatch Hydrator and Finder together at every ten-minute boundary; GitHub
serializes them.
The workflows also use GitHub's `queue: max` setting so pending dispatches are
not silently replaced while another run owns the lock. The pull → sync →
push cycle runs entirely inside that mutex, so the release asset can never
catch two writers.

## The catalog release store

`rbx_scout.db` lives on the rolling GitHub Release **`catalog-latest`** as
two assets: `rbx_scout.db` (the catalog itself) and `rbx_scout.db.sync_state`
(a tiny sync counter, kept for continuity with the old repo-based DB). The
workflows read and write them through `db_sync.py` using the runner's own
`GITHUB_TOKEN` — nothing to sign up for beyond the GitHub repo you already
have.

Locally:

```bash
export RBXSCOUT_GITHUB_TOKEN=ghp_...   # a PAT with repo access (or skip: your git credential store is tried too)
python db_sync.py pull                 # release asset -> local rbx_scout.db
python db_sync.py status               # compare local copy vs the release
python db_sync.py push                 # local rbx_scout.db -> release asset (clobbers)
```

Without a token in the environment, `db_sync.py` falls back to your local
git credential store — the same credential `git push` uses. The release is
self-describing: `python db_sync.py status` shows which sync produced the
stored catalog.


## The tiered catalog

| Tier | Meaning | Refresh cadence |
|---|---|---|
| New | not yet hydrated | first, every finder run |
| T1 | hot watchlist | when stale > 1h |
| T2 | pre-blowup watchlist | when stale > 2h |
| T3 | warm | when stale > 4h |
| T4 | warm | when stale > 6h |
| T5–T7 | big games | weekly |
| T8 | below all thresholds | 2-day rotating slice; pruned after 14 days stale |

Classification is **higher-axis**: a game's tier is the higher of its
visits-axis tier and CCU-axis tier (CCU is the leading indicator of a
blow-up; visits lag). A 1M-visit/30-CCU corpse lands cold; a 30k-visit/300-CCU
rocket lands warm. Games that climb 2+ tiers or triple their CCU between
syncs get flagged → **New and Upcoming**.

## Run it yourself

```bash
pip install -r requirements.txt
python db_sync.py pull       # fetch the current catalog from the release store
python live_sync.py          # one full sync from the terminal
python -m pytest tests -q    # test suite
streamlit run app.py         # the dashboard (reads the local catalog copy)
```

## Data sources

All public, cookieless endpoints — no credentials anywhere in this repo:

| Stage | Source |
|---|---|
| Discovery | `apis.roblox.com/explore-api/v1/get-sorts` (deep charts: follows `nextSortsPageToken` through the full leaderboard taxonomy — Top Earning, Top Rated, Most Popular and every genre chart, ~26 sorts / ~770 games per run), `search-api/omni-search` (depth 2: page 1 + `nextPageToken` page 2 per keyword) |
| Expansion | `games.roblox.com/v2/groups/{id}/games` + `/v2/users/{id}/games` (creator portfolio spiderwebbing; verified caps 100/50, `placeVisits` free) and sequential universe-ID frontier scanning — see [EXPANSION_PILOT.md](EXPANSION_PILOT.md) |
| Bulk index | `api.rolimons.com/games/v1/gamelist` |
| Metrics | `games.roblox.com/v1/games` (50 universes/batch — verified hard cap) |
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
2. Seed the catalog release once, from your machine (creates the
   `catalog-latest` release and uploads your current `rbx_scout.db`):
   ```bash
   python db_sync.py push
   ```
3. Create a GitHub **fine-grained personal access token** for only
   `AdityaManshukhani-Coding/rbxscout` with **Actions: Read and write**
   permission. Use an expiration and rotate it periodically.
4. From `cloudflare-worker/`, install Wrangler and authenticate:
   ```bash
   npm install
   npx wrangler login
   ```
5. Store the token as a Cloudflare secret. Do not put it in this repository,
   `wrangler.toml`, GitHub Actions variables, or frontend code:
   ```bash
   npx wrangler secret put GITHUB_TOKEN
   ```
   Paste the token only when Wrangler prompts for it.
6. Deploy the Worker:
   ```bash
   npx wrangler deploy
   ```
   The `wrangler.toml` configuration creates one UTC Cron Trigger:
   `*/5 * * * *`. Cloudflare invokes Hydrator on every tick and Finder every
   ten minutes (UTC `:00`, `:10`, `:20`, `:30`, `:40`, `:50`). Trigger changes
   can take several minutes to propagate.
7. Confirm the Worker deployment's **Cron Triggers** page shows exactly one
   trigger: `*/5 * * * *`.
8. Confirm GitHub Actions shows both workflows as active and that their files
   list only `workflow_dispatch` under `on:`—there must be no `schedule:`.
9. Use **Run workflow** once manually for each workflow to validate the
   GitHub token, permissions, Python environment, database commit, and push.
   Then watch the next Cloudflare tick in the Actions tab.

The scheduler dispatches the GitHub API endpoints for `hydrator.yml` and
`expander.yml` with `{"ref":"main"}`. A successful API dispatch means GitHub
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

## Daily home-IP discovery run (this laptop)

atlasdev.gg 403s every GitHub Actions runner IP and the Cloudflare Worker
relay, but allows residential IPs — so **discovery runs here**, once a day:

```bash
.venv/bin/python atlas_home.py            # pull → harvest → push (discovery ONLY)
.venv/bin/python atlas_home.py --force    # ignore the 24h Atlas throttle
```

It is scheduled by a macOS LaunchAgent (`com.rbxscout.atlas-home-harvest.plist`
in this folder, installed in `~/Library/LaunchAgents/`):

- **Fires 19:00 local time daily** (17:00 UTC on a CEST machine) — evening in
  the Netherlands, so the laptop is awake and open (a missed day = no new games
  that day; the 23h throttle keeps a fixed slot from ever skipping). Seeds are
  drained by the Actions expander on its next `:15`/`:45` tick, at most 30
  minutes later.
- Laptop asleep at 06:15? launchd runs it at wake. Lid closed all day = no run
  that day; Atlas just picks up the next day (its cursor only advances on
  success, nothing is lost).
- `EXPAND_QUEUE_BATCHES=0` inside the runner: the laptop **discovers and
  enqueues only** — the always-on Actions expander drains the seeds through the
  strict 20k/25 gate and the hydrator owns all refresh traffic.
- **Thumbnail backfill rides along**: atlasdev.gg 403s runner IPs, and the
  thumbnail/vote endpoints follow the same pattern — so after each harvest the
  laptop fetches icons + like/dislike votes for up to 200 icon-less catalog
  rows (oldest first, 24h-throttled). This is what keeps every game's
  thumbnail and Rating column filled without spending any Actions traffic.
- Merges are union-by-primary-key and push uses a merge-retry, so the laptop
  can never clobber games Actions discovered meanwhile (db_sync's stale-push
  guard is the backstop).
- A source-level tripwire fails the run if any legacy finder ever reappears in
  `scout_core.py` — Atlas Dev stays the sole discovery engine.

Logs: `logs/atlas_home.log` (the run) and `logs/atlas_home_run.out` (launchd).

## Dashboard

`streamlit run app.py` gives you the full dashboard: filterable catalog,
per-page Discord contact checks, per-tier diagnostics, and the **New and
Upcoming** tab (the blow-up watch). It reads the same SQLite catalog this
repo keeps fresh. The results table mirrors atlasdev.gg's analyze layout —
Game · Genre · Total visits · CCU · Avg CCU (1d) · Avg CCU (3d) · Momentum
(1d) · Rating · Discord · Message — with Favorites, Peak CCU and Created
dropped (they duplicate visits / current CCU, and Roblox exposes no
cookieless creation date to show anyway). Averages and momentum derive from
the row's first-seen stamp; Rating comes from
`games.roblox.com/v1/games/votes` (cookieless, batched 50/call, fetched by
the same passes that fetch icons).

### Hosted dashboard (Streamlit Community Cloud, free)

The dashboard also runs hosted at `https://rbxscout.streamlit.app` — no
laptop required. Same app, same catalog, two source modes chosen
automatically:

| Mode | When | Catalog source |
|---|---|---|
| **Local** | `rbx_scout.db` exists in the repo (your laptop), or `RBXSCOUT_LOCAL_DB=1` | the local file, exactly as before |
| **Hosted** | no local DB (Streamlit Cloud container) | anonymous download of the `catalog-latest` release asset into `~/.cache/rbxscout/`, refreshed via a tiny metadata check every 5 min |

Hosted mode needs **no secrets** — the repo and release are public. The
5-minute throttle plus Streamlit's `st.cache_resource` means 2–3 users cause
about one metadata request per 5 minutes (the ~40 MB file is re-downloaded
only when the pipeline actually replaced the asset).

Deploy / redeploy it in two minutes:

1. Push the latest `main` (the app auto-redeploys on every `git push`).
2. [share.streamlit.io](https://share.streamlit.io) → sign in with GitHub →
   **Create app** → *Yup, I have an app*.
3. Repo `AdityaManshukhani-Coding/rbxscout`, branch `main`, file `app.py`.
4. **App URL**: set the subdomain to `rbxscout`. Python: pick the newest
   offered (the Actions runners use 3.14). No secrets needed.
5. Deploy — the URL is `https://rbxscout.streamlit.app`.

The **live catalog tracker** at the top of the dashboard is the
subscriber-counter-style number: total cataloged games, how many meet the
20k visits / 25 CCU target, how many were discovered today (UTC, first seen
in `ccu_history`), and the last pipeline sync time. It reads the cached
catalog copy, so it costs nothing per visitor.

### Access passwords (master + 100 user keys)

Sign-in accepts two kinds of passwords, checked in this order:

1. **Master** — `APP_PASSWORD` in the Streamlit secrets (or env var). Yours
   alone; never subject to the sharing ban.
2. **User keys** — 100 generated friend passwords, hashed into the committed
   `access_passwords.json` (SHA-256 + fixed salt; `gate.py` reads it at
   sign-in). The plaintext list lives in `access_passwords.txt`, which is
   **gitignored** — paste it into a private Google Doc and write each
   classmate's name next to their key so a banned key is traceable.

Anti-sharing: every sign-in with a user key records its client IP and device
id. The moment one key shows **two separate IPs AND two separate device
ids**, it is banned for everyone — both users are dropped back to the
password screen and their remembered unlocks are revoked. (Same IP on many
devices, e.g. classmates on one wifi, and one device across networks, e.g. a
phone on the move, do NOT trigger the ban. One person using two devices on
two different networks WILL — the owner's rule is one key = one person = one
device.) Wrong-password cooldowns work exactly as before; a banned key
returns a clear "disabled" error, not the wrong-password message.

Regenerate the set any time (this invalidates every old key):

    python generate_access_passwords.py

Local development never trips the ban (unknown/localhost IPs are ignored by
the tracker). The master password also still works when the manifest file is
missing, so deployments without user keys behave exactly as before.

### Never-sleep keep-alive (Cloudflare Worker)

Community Cloud hibernates apps after 12 h without traffic; the scheduler
Worker pings the dashboard every 11 hours (00:00, 11:00, 22:00 UTC) so it
never does. The URL lives in `wrangler.toml` under `[vars]`:

```toml
[vars]
DASHBOARD_KEEPALIVE_URL = "https://rbxscout.streamlit.app/"
```

Redeploy with `npx wrangler deploy` after changing it; delete the var (or
leave it unset) to disable the ping.

### Never roll the catalog back

`db_sync.py push` refuses to overwrite a store that holds a HIGHER sync
counter than your local copy — that would delete every game discovered
since your copy was made. If push says "refusing to push", run
`python db_sync.py pull` first. `--force` overrides at your own risk;
pushing into an empty store (outage refill) never needs a flag.
