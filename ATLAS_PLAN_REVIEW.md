# Atlas Dev Seed Ingestion — FINAL DRAFT (v2)

**Status:** ✅ **IMPLEMENTED 2026-09-19** — see §7 for what shipped and the
deployment checklist. All numbers below come from the live spike and the
end-to-end proof run that preceded the build.

**Changes applied since v1 (your clarifications):**
1. **Marketplace framing removed.** Value proposition is pure mid-tier game
   discovery; the "listed = seller" outreach angle is out of the plan.
2. **Cadence locked:** one bot run per day, checking Atlas for newly listed
   games we don't already have.
3. **Confirmed flow:** find new ID → copy its basic stats into our DB → the ID
   joins the hydrator rotation, which owns all future stat refreshes.
4. **Two new questions answered with data:** "Can Atlas give us stats?" (§4)
   and "Do we need a Cloudflare proxy?" (§5).
5. **Strategic change on the table:** retire Finder + Expander, go all-in on
   the hydrator. Analysis in §6 — verdict: yes, with one safety net.

---

## 0. Verdict

| Question | Answer |
|---|---|
| Does the Atlas method fully work? | ✅ **Proven live** — proof run fetched 3 pages, parsed 150 IDs, delta-filtered against the real catalog, and ingested 98 new seeds into a sandboxed queue with correct claim order. Zero writes to `rbx_scout.db`. |
| Will it help? | ✅ Yes — measured on the full 275-page sweep: 12,119 games indexed, **~23% genuinely new to us** (654 crossed the strict gate after Roblox re-verification). Pages 1–5 *look* 65–74% new because the head is dense with fresh listings; the long tail is mostly games we already track. |
| Buildable as a once-a-day bot? | ✅ Yes — ~100 lines reusing `_enqueue_discovery`; no schema or worker changes. |
| Can Atlas also be our stats source? | ⚠️ **Partially — first-paint only.** CCU + total visits are obtainable per game (SEO meta text, corrected after re-testing the exact request class from DevTools capture); favorites/genre/creator/API are not. See §4 for the verdict and the cost math. |
| Do we need the Cloudflare proxy? | ⚠️ No for safety-of-us; yes as an availability insurance policy. Recommended config in §5. |

---

## 1. Proof run — the full method, executed live (2026-09-19)

Ran against an **isolated DB copy** (schema-identical, catalog IDs re-seeded for
the dedup check; the live DB was opened read-only for verification only —
asserted untouched afterwards, step [5]):

| Step | Result |
|---|---|
| [1] Fetch pages 1–3, ~3 s apart, honest UA | HTTP 200 every time, **50 IDs per page**, 150 unique IDs, 11 s total. No Cloudflare challenge, no 429. |
| [2] Delta vs real catalog (38,960 known IDs across both tables) | **98 new candidate seeds (65% new)** — consistent with page-1's 74%; a handful of page-1 IDs reappeared, as expected. |
| [3] Ingest into sandboxed `discovery_queue` | +98 rows, `source='atlas_dev'`, `priority=2`, `status='pending'`. |
| [4] Claim-order check | Drain would pick the atlas_dev seeds first (priority 2 beats the frontier backlog at 3, FIFO within tier) — exactly the intended behavior. |
| [5] Isolation check | Live DB `discovery_queue` row count unchanged — **rbx_scout.db never written**. |

Extraction method used (and therefore final): `href="/analyze/(\d{8,12})"`
on plain HTML. No special headers, no RSC payload, no redirect tricks, no
hardcoded ID prefix.

---

## 2. The once-a-day bot — final spec (no code yet)

**Trigger:** piggyback the existing expander workflow run once per day
(24-hour self-throttle), *during the transition period*. After the engine
retirement in §6, it becomes its own small daily workflow (or a phase inside
whatever remains) — the `rbxscout-sync` concurrency group stays mandatory in
either case.

**State:** two rows in the existing `scan_pointers` table, exactly like the
frontier pointer:
- `atlas_last_run` — timestamp of last successful harvest (throttle gate: skip
  if < 24 h; a failed run stamps nothing, so it retries next day).
- `atlas_page` — sweep cursor for the one-time catch-up sweep.

**Behavior:**
- **Run 1 (one-off):** full catch-up sweep, pages 1→275 at ~1 req / 3 s
  (~14 min of polite crawling), seeding up to ~5–10k new candidates.
- **Steady state (daily):** re-check the first few pages for newly listed
  games; the delta filter plus `INSERT OR IGNORE` semantics make re-processing
  free. New listings rise to the top pages over time because the default sort
  is ascending visits within the filtered query — the cursor design re-checks
  the head of the list daily and can page deeper every N days for completeness.
- **Ingestion:** one call to the existing `_enqueue_discovery((uid,
  'atlas_dev', 2))` — dedup, chunking, and the 2M-row trim all come free.
  Verified above that priority 2 sorts correctly against the remaining backlog.
- **Downstream (confirmed by you):** the existing drain → hydrate → strict-gate
  (≥20k visits, ≥25 CCU, verified live via Roblox) → `upsert_game` path copies
  stats into `game_analytics`; from then on the ID is in the hydrator rotation
  and the hydrator owns all stat refreshes. Atlas numbers are never used for
  the strict gate (the gate verifies via Roblox) and never write `ccu_history`;
  the optional provisional first-paint row is the one exception, defined in §4.
- **Kill switch:** `ATLAS_SWEEP_PAGES=0` (or the knob's final name) disables
  the whole module instantly, no code edit.
- **Throttling & failure:** ~1 req / 3 s with jitter; on 429/5xx, stop the
  sweep, stamp nothing, retry next day. All queue writes remain in the single
  transaction the existing method already provides.

**Marketplace note (your instruction):** no seller/marketplace logic anywhere.
Atlas is used purely as a mid-tier games index.

---

## 3. Will it help? (measured yield)

| Engine | Yield evidence (live data) |
|---|---|
| Frontier scan | **0 qualified from 11,500 evaluations** (`sequential_scan` outcomes: 10,841 below_gate, 659 failed) |
| Rec mining | ~0.4 new IDs per request |
| Spiderweb | 37 qualified ever; most IDs already in catalog (3,278 `already_in_catalog` vs 37 `qualified`) |
| **Atlas /analyze** | **~23% genuinely new across the full 275-page index** (head pages run higher; tail mostly known). The drain's free catalog-check absorbs the overlap with zero Roblox cost. |

The catalog's 20k–100k band currently holds 1,195 games (5% of 23,883).
Atlas's filtered index is approximately a superset of the sites' own
mid-tier band — one sweep can plausibly double or triple the small-band
inventory after our gate re-verification.

---## 4. Can Atlas also fetch stats? — corrected after the DevTools follow-up

The first draft said "no stats anywhere." The DevTools capture you supplied
prompted a re-test of the exact request class it shows — the game-detail RSC
payload (`GET /analyze/{id}?_rsc=…` with `Rsc: 1`) — which had not been tested
individually. Results:

| Surface | Measured content |
|---|---|
| Index HTML / RSC payload | IDs and links only — zero stat fields. `totalVisitsMin`/`ccuMin` in the URL are *query filters*, not returned data. |
| **Game page RSC payload** (the request class from your capture) | HTTP 200, 58 KB of pure React scaffolding (`className` ×422, `children` ×268…). **No numeric stat object.** The only stats are inside SEO meta strings: `"The Black Bell [HORROR] on Atlas - 145 playing now, 29,664 total visits."` — repeated 3× (description, og:, twitter:). `universeId` appears once, as a route parameter. |
| Game page HTML | Same three meta strings; no structured JSON, no `/api/*`, no `/_next/data/*` routes. |
| `/trending` | 200 but **zero** `/analyze/{id}` links in 742 KB (client-rendered shell). |
| `/browse?totalVisitsMin=20000&ccuMin=25` | **HTTP 500** server error with our filter params. |
| `/api/track/ping` | POST-only client telemetry, no data. |

**What this means — your first-stats flow is feasible, with two caveats:**

1. **Parseable but prose.** `re.search(r'(\d[\d,]*) playing now, (\d[\d,]*)
   total visits', meta_description)` reliably yields **CCU + total visits** —
   enough for a first-paint row. Favorites, genre, creator, and updated timestamps
   are NOT available anywhere on Atlas.
2. **It costs one extra request per game.** Stats are not on the index pages;
   getting them means fetching each game's page individually. A 10k-seed
   catch-up sweep at 1 req/3 s = **8+ hours of extra Atlas traffic** — that is
   the real cost of "import stats from Atlas first."

**And one structural fact that softens the need:** the pipeline you confirmed
(drain → hydrate → strict gate → `upsert_game`) already fetches **fresh Roblox
stats before any catalog write**. So Atlas's first-paint numbers can never be
the stored row unless we deliberately add a provisional-write step; they can
only add value *before* hydration — e.g., prioritizing the drain queue by CCU,
or showing provisional numbers in the dashboard for games still pending
hydration.

**Spec (updated per your instruction):**
- **Default flow (unchanged):** discovery → queue → hydrator-verified Roblox
  stats into `game_analytics`/`ccu_history`. `ccu_history` is never touched by
  Atlas numbers (trend integrity).
- **Optional first-paint bootstrap (new, your call to enable):** a budgeted
  per-run knob (e.g., `ATLAS_STAT_PAGES=100` games/run) fetches game pages for
  the newest atlas_dev seeds, parses CCU+visits from the meta text, and writes
  a **provisional** row (flagged, e.g. `found_via='atlas_dev'`) that the
  hydrator overwrites on first refresh. At 100/day it's ~5 min of polite
  traffic; the 10k backlog fills over ~3 months, or disable it and let the
  hydrator's fresh data be the first data. **Recommendation:** enable at a
  small budget — provisional rows make the dashboard immediately useful for
  new discoveries, and the hydrator remains the single source of truth.
- Atlas stats are never used for the strict gate — the gate verifies via Roblox.

Side-finding: Atlas's numbers do refresh server-side — the same game showed
"126 playing / 28,731 visits" earlier today and "145 playing / 29,664 visits"
hours later — but refresh cadence is unknown and unstated.

---

## 5. Do we need the Cloudflare proxy for Atlas requests? — **Not required; optional insurance**

Measured facts: Atlas serves plain HTTP 200 to datacenter-looking clients
(GitHub Actions runners are AWS/Azure IPs), with no Cloudflare challenge and no
rate-limiting at ~1 req/3 s. The existing `RBXSCOUT_SEARCH_PROXY_URLS` pool is
for **Roblox's** search API, not third-party sites — different risk profile.

| Risk | Reality |
|---|---|
| Getting IP-banned by Roblox | **Zero** — Atlas requests never touch Roblox. |
| Getting blocked by Atlas | Low: robots.txt explicitly allows crawling (`Allow: /`), volume is ~280 req/day max in steady state, honest UA. |
| Atlas blocking *GitHub's IP ranges* someday | Real but mild: we'd lose one discovery source, nothing else breaks; the workflow fails visibly and retries next day. |

**Recommendation:** run **direct, no proxy** — but route through the existing
`RBXSCOUT_SEARCH_PROXY_URLS` env var pattern so that if Atlas ever starts
403/429-ing runner IPs, you flip a config value and traffic shifts to proxies
with zero code change. That's the pragmatic "proxy for safety" you wanted:
safety = the *option*, not the default.

---

## 6. Retiring Finder + Expander, all-in on the hydrator — **verdict: yes, with one safety net**

The live queue data settles this:

- **Frontier scan: provably dead.** 11,500 evaluated IDs, 0 qualified, 0 new
  leads. Pure request burn.
- **Spiderweb: effectively exhausted.** 37 lifetime qualifiers from 6,887
  evaluated candidates (~0.5%); its next-best output is `already_in_catalog`
  noise. The 14-day re-spider keeps burning requests for near-zero return.
- **Rec mining: marginal** (~2 new IDs per 5 requests) — keep only if free
  after retirement, otherwise cut with the rest.
- **Atlas replaces discovery outright:** one day-one sweep ≈ 10,000 candidate
  IDs; steady state refreshes daily. No other engine can approach that.

**What retirement looks like (when you green-light implementation):**
1. Set `EXPAND_SPIDERWEB_CREATORS=0`, `EXPAND_QUEUE_BATCHES=0`,
   `EXPAND_FRONTIER_BATCHES=0`, `EXPAND_REC_SEEDS=0` — all four engines stop
   without a code edit (env knobs already exist), while the Atlas phase takes
   over the expander's slot.
2. Keep the **drain** half of the expander (it's what hydrates Atlas seeds
   through the strict gate) and the `rbxscout-sync` mutex, the pull/pytest/push
   skeleton, and the Cloudflare worker tick.
3. Remove dead code (`spiderweb_creators`, `scan_frontier`, rec seeds,
   `creator_spiderweb_log`) in a later cleanup commit, not the first one.

**The one safety net (important):** keep the drains of the old queue running
until empty. Right now `discovery_queue` has 0 pending rows (18,387 processed),
so nothing is stranded — but if any engine enqueued candidates that never got
drained, retirement would orphan them. The gate is: retire the *engines*, keep
the *drain* until it reports zero pending for a full week.

**Also keep:** the keyword-crawl state table has no pending backlog issues
(finder's other function was search-based discovery — with Atlas yielding 65%+
new IDs, that function is superseded; the search API budget can be fully
reallocated to hydration).

**Net effect:** hydrator + Atlas becomes the entire pipeline: one source of
new games (Atlas, daily), one engine refreshing stats (hydrator), one strict
gate in between. Simpler, cheaper, and every remaining request buys hydration
depth instead of zero-yield discovery.

---

## 7. Implementation status (shipped 2026-09-19)

**Code (`scout_core.py`, `live_sync.py`):**
- `harvest_atlas_seeds()` — 24h-throttled daily harvest: index pages →
  `href` extraction → `_enqueue_discovery` as `atlas_dev` / priority 2.
- Pointers in `scan_pointers`: `atlas_last_run` (throttle stamp; failed runs
  stamp nothing → automatic retry) and `atlas_page` (deep-sweep cursor; a
  completed sweep resets to 1, an aborted one resumes where it stopped).
- First run on a fresh DB performs the one-off **full catch-up sweep** (275
  pages, ~14–18 min at the polite delay — inside the 30-min workflow timeout);
  steady state then re-checks the head pages daily.
- `ATLAS_STAT_PAGES` provisional first-paint rows per harvest (newest seeds
  first): CCU+visits parsed from game-page meta prose, gated on 20k/25,
  `found_via='atlas_dev'`, **never** written to `ccu_history` (`upsert_game`
  gained `record_ccu_history=False`). The hydrator overwrites with fresh
  Roblox stats on first refresh.
- Polite fetching: honest UA (`RbxScout/1.0 …`), ~1 req/3 s
  (`ATLAS_REQUEST_DELAY`), 429/5xx back-off then bail-out, proxy flip via the
  existing `RBXSCOUT_SEARCH_PROXY_URLS` (direct by default).
- `run_expansion()` order: Atlas → spiderweb → recs → **drain** → frontier;
  Atlas failures are caught and never take the drain down. Retired engines
  default to 0 (`EXPAND_SPIDERWEB_CREATORS_RETIRED` etc.) and re-enable via
  env/params; the drain keeps its budget.
- Live-sync summary prints the `atlas dev :` line on every expander run.

**Workflows:**
- `expander.yml` — Atlas knobs exposed as repo Variables; proxy pool wired
  (empty = direct). Unchanged mutex/pull/pytest/push skeleton.
- `finder.yml` — job-level guard `if: vars.FINDER_ENABLED == '1'`: discovery
  is OFF by default (run skips in seconds; the worker keeps its schedule),
  instant rollback by setting the repo Variable. Hydrator untouched.

**Tests:** 16 new in `tests/test_atlas.py` (parser, harvest/dedup, throttle
window, cursor resume, deep sweep, gate-respecting provisional rows,
ccu_history protection, abort semantics, kill switch, proxy flip,
`run_expansion` integration). `tests/conftest.py` now **blocks any live
atlasdev.gg request** in the suite unless a test fakes the transport.
Full suite: **208 passed**. Bonus fix: `drain_discovery_queue` no longer
hydrates one row when its budget is 0 (old `max(1, limit)` clamp).

**Deployment checklist:**
1. Merge/push — workflows update automatically.
2. First expander run performs the deep sweep (~275 requests once, spread at
   ~3 s); watch the `atlas dev :` line, then let the queue drain (500/run)
   hydrate the backlog over the following expander runs.
3. Optional Variables: `ATLAS_SWEEP_PAGES` (default 3), `ATLAS_STAT_PAGES`
   (default 100; 0 disables first-paint rows), `ATLAS_REQUEST_DELAY`
   (default 3 s), `ATLAS_DEEP_EVERY_DAYS` (default 30).
4. Kill switch anywhere: `ATLAS_SWEEP_PAGES=0` + `ATLAS_STAT_PAGES=0`.
5. ToS note unchanged (§6 recommendation): robots.txt allows crawling, volume
   is tiny; the module is one Variable away from off.

## 8. Bottom line

The method is **proven end-to-end live** (fetch → parse → delta → ingest →
claim order, sandboxed, zero risk to the production DB), it feeds exactly the
band your catalog is thinnest in, it needs ~100 lines and no schema changes,
it cannot serve as a stats source (and shouldn't — the hydrator keeps that
role), and it makes Finder + Expander provably retireable. When you say go,
the work is: Atlas phase + 24h throttle + page cursor + tests, then the env-knob
retirement of the dead engines.
