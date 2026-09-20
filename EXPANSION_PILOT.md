# 🌐 Expansion Pilot — Atlas Dev Harvest + Discovery-Queue Drain

> **✅ REMOVAL COMPLETE (2026-09-20):** the creator spiderweb, frontier scan,
> and recommendations-mining engines have been **deleted from the codebase**
> (commit "Remove legacy discovery engines…"). `finder.yml` is deleted, the
> Cloudflare scheduler no longer dispatches a finder tick, and the expander is
> now **Atlas Dev harvest → queue drain** only. Everything below the banner is
> the historical record of the retired engines; the **drain + strict gate**
> sections still describe live behavior.

**Status:** LIVE code · **Gate:** strict 20k visits / 25 CCU
**Update 2026-09-19:** **Atlas Dev seed ingestion is LIVE** (see
`ATLAS_PLAN_REVIEW.md`) and is now the primary discovery source. Full
275-page sweep measured: 12,119 games indexed, **654 genuinely new games
crossed the strict gate** (catalog 23,883 → 24,537). Head pages run ~65–74%
new but the full index averages ~23% new — the drain's free catalog-check
absorbs the overlap.
**Where the results appear:** the `EXPANSION PILOT` block at the bottom of
every expander run log, plus the `expansion pilot : warming up …` line in the
capacity-pilot block of *every* workflow's log (it reads `sync_health_log`).

---

## Why this exists (the one-paragraph version)

The catalog holds 18,734 games and 10,389 already meet the 20k/25 target —
but 9,250 of those are 1M+ visit giants, and only **82 live in the small,
buyable 20k–100k band** (5 in the whole 20k–30k slice). Search, charts and
Rolimons structurally surface big games; small games only appear there after
they've already grown. RoTrends/Atlas-style sites get small games via
**structural** discovery, not search. This pilot adds exactly that, in two
engines, without abandoning the scout philosophy (nothing dead is stored).

## The two engines (now three — rec mining joined 2026-09-19)

| Engine | What it does | Cost |
|---|---|---|
| **Creator spiderwebbing** | Crawls the public portfolios of every creator already in the catalog (14,649 unique creators: 11,931 groups / 2,718 users). Portfolio payloads carry `placeVisits` free, so candidates below 20k visits are discarded **before** any hydration request. Survivors enter the discovery queue at priority 1. | ~1 request per creator (page size 100 groups / 50 users, cursor-followed) |
| **Recommendations mining** (2026-09-19) | Harvests Roblox's player-overlap graph: for each seed game, `/v1/games/recommendations/game/{id}` returns ~6 games its players also play. Seeds are chosen ONLY from the small band (20k–100k visits) plus the 200 most recent expansion qualifiers — giant seeds return 100% already-known games. New IDs enter the queue at priority 2. A rotation cursor in `scan_pointers` (id `rec_seed`) sweeps the pool so runs never repeat seeds. | 1 lightweight GET per seed (default 50/run) |
| **Atlas Dev ingestion** (2026-09-19) | Once-daily harvest of atlasdev.gg/analyze (games pre-filtered to ≥20k visits, ≥25 CCU) → discovery_queue at `atlas_dev` priority 2. 24h self-throttle, deep-sweep page cursor, honest UA, ~1 req/3 s, 429 bail-out, proxy flip via `RBXSCOUT_SEARCH_PROXY_URLS`. Optional budgeted provisional first-paint rows from game-page meta text (never into `ccu_history`). | 1 request per index page + `ATLAS_STAT_PAGES` game pages |
| **Frontier scan** | Walks universe IDs upward from the highest known ID (seeded at 10,765,584,604). Roblox assigns IDs as increasing integers, so this range is where brand-new games appear — the ones that can cross 20k visits within days of launch. 1 request per 50 IDs. | 1 request per 50 IDs |

Both feed a **priority discovery queue** (spiderweb=1, seed=2, sequential=3),
which is drained through the shared, paced metrics endpoint. **Strict gate:**
only games meeting 20k visits AND 25 CCU are upserted into `game_analytics`
(with `found_via='expansion'`, full tier stamping and blow-up flag). Everything
else is recorded in `discovery_queue` as dedup memory — never stored, never
re-hydrated until a creator re-spider re-enqueues it after 14 days.

## Phase-0 spike findings (live, 2026-09-17)

These were verified against real endpoints before building:

1. `GET /v2/groups/{id}/games?accessFilter=Public&limit=100` — works, paginates
   via `nextPageCursor`, returns `id`, `rootPlace`, `placeVisits`.
2. `GET /v2/users/{id}/games?...` — works but **limit caps at 50** (100 → HTTP 400).
3. The metrics batch endpoint **rejects >50 universe IDs** ("Too many universe
   IDs were requested."). The blueprint's "100 per request" would have failed
   every batch; the existing 50-ID batching was already correct.
4. `placeVisits` in portfolio responses makes the pre-gate free — the seed
   pass's hydration cost collapses to only games already showing 20k+ visits.

## Recommendations-endpoint spike (live, 2026-09-19)

Verified before building the third engine — the external blueprint was wrong
again on the numbers:

1. `GET /v1/games/recommendations/game/{universeId}` — works unauthenticated,
   HTTP 200, shape `{games: [...], nextPaginationKey}`.
2. **maxRows is ignored** — 6 rows per page regardless of 10/25/50/200.
3. **Pagination is fake** — 6 pages returned the same 6 games (36 rows → 6
   unique). One request per seed is all you get.
4. Small/low-CCU games can return **0 rows** (empty rec graph).
5. Overlap is the decisive number: a giant seed returned **100% known** games;
   small-band seeds returned ~2 new relevant IDs per 5 requests. Hence: seed
   from the small band only, expect a quality trickle not a firehose.

## Velocity watch — PARKED (measured 2026-09-19, do not build yet)

> **TODO (Aditya's reminder list):** revisit "growth-velocity pre-gating"
> (re-check CCU≥25 / <20k-visit games every 24h instead of discarding them for
> 14 days). It is parked — NOT because the idea is bad, but because on
> 2026-09-19 we measured the streams it would watch and they contain **zero**
> such games:
>
> - frontier (`sequential_scan`) discards: 200 sampled, **100% dead (0 CCU)**
>   — fresh universe IDs are mostly unpublished Studio placeholders.
> - spiderweb discards: 200 sampled, 92% dead, rest under 25 CCU (below-gate
>   there means low CCU — the visits pre-gate already removed low-visit rows).
> - portfolio games the visits pre-gate drops: dead in the sampled portfolios.
>
> **When to reconsider:** if the EXPANSION PILOT block ever shows a `below_gate`
> population with real CCU (or you lower the gate), a 24h re-check TTL on
> `discovery_queue` status `velocity_watch` becomes worthwhile — the queue and
> drain already exist, so it would be a small change. The cheaper lever that
> captures most of the same benefit today: shorten `SPIDERWEB_RESCRAPE_DAYS`
> from 14 to 7 (one constant).

## Pilot rates (and the knobs)

Defaults live in `scout_core.py` and are env-tunable via the expander
workflow's repo Variables (Settings → Secrets and variables → Actions →
Variables):

| Knob | Default | Meaning |
|---|---|---|
| `EXPAND_SPIDERWEB_CREATORS` | 100 | creators crawled per run (~100–150 requests) |
| `EXPAND_QUEUE_BATCHES` | 10 | ×50 candidates hydrated per run (≤500) |
| `EXPAND_FRONTIER_BATCHES` | 10 | ×50 fresh IDs scanned per run (≤500 → ~24k/day) |
| `EXPAND_REC_SEEDS` | 50 | recommendation seeds mined per run |

At the 30-minute cadence that is ~1,500–2,000 requests/day of expansion
traffic on top of the existing finder/hydrator load — deliberately modest
until the verdict says otherwise. The seed pass over all 14.6k creators takes
~146 expander runs (~3 days); after that the 14-day TTL makes steady state
~100 creators/run. `EXPAND_FRONTIER_BATCHES=0` + `EXPAND_QUEUE_BATCHES=5`
is the prescribed half-revert (frontier off, spiderweb on).

## The verdict — what you check in 2–3 days

Every expander run logs its stats; the capacity-pilot block prints the
accumulated verdict after 72 expander runs (~36h at the 30-min cadence):

- ✅ **KEEP pilot rates** — yield ≥0.2% of evaluated IDs qualified and metric
  failures ≤10% → the pilot is finding real games cheaply. Tell Codebuff to
  scale up (more frontier batches; optionally extend the scan backward into
  the 9–10B historical band, which our own ID distribution says is dense).
- ⚠️ **BORDERLINE** — yield or health between green and red. Keep watching;
  consider raising `EXPAND_SPIDERWEB_CREATORS` (the cheapest, highest-yield
  engine) while holding the frontier steady.
- ⛔ **REVERT** — no real yield or >30% failed metric batches (chronic 429s).
  Apply the prescribed fix printed in the log itself (env vars, no code edit),
  or revert the expansion commit.

The decision rule lives in `expansion_pilot.expansion_trial_verdict` and is a
pure function of `sync_health_log` — identical inputs give identical verdicts.

## What was built (file map)

| File | Change |
|---|---|
| `scout_core.py` | 3 new tables (`scan_pointers`, `creator_spiderweb_log`, `discovery_queue`); `fetch_creator_portfolio`, `spiderweb_creators`, `drain_discovery_queue`, `scan_frontier`, `run_expansion`; `scan(phases=("expand",))`; `METRICS_BATCH_SIZE` constant (verified 50-cap); pilot knobs |
| `live_sync.py` | `--only expander` mode + the `EXPANSION PILOT` summary block |
| `expansion_pilot.py` | expansion telemetry capture + `expansion_trial_verdict` + the verdict block on expander runs (replaces the retired `capacity_pilot.py`) |
| `.github/workflows/expander.yml` | NEW "Expander — Game Finder 2.0 (30 min)" workflow — **joined to the `rbxscout-sync` concurrency group** (the binary-DB mutex is mandatory; `queue: max`); pull → pytest → expand → push |
| `cloudflare-worker/src/index.js` | dispatches the expander on the `:15`/`:45` UTC ticks — its own clean minutes, never coinciding with finder's even-minute ticks |
| `tests/test_expansion.py` | NEW: strict-gate invariant, pre-gate math, TTL dedup, failed-fetch retry, priority claims + crash self-heal, pointer persistence, env knobs, scan-phase integration |

## Safety properties (why this can't poison the catalog)

1. **Strict gate:** below-target games never reach `game_analytics`. Test-enforced.
2. **Dedup memory:** every evaluated ID lives in `discovery_queue` with its
   outcome, so no candidate is hydrated twice inside the 14-day window; the
   queue trims itself at 2M rows (oldest processed first).
3. **Crash-safe frontier:** the pointer advances only after a successful
   evaluation+store; a crashed run re-scans its range instead of skipping it.
4. **Crash-safe claims:** queue rows claimed as `processing` are stamped at
   claim time; claims older than 2h self-heal back to `pending`.
5. **Failed ≠ logged:** a creator whose portfolio fetch fails is NOT logged,
   so it retries next run; a genuinely empty portfolio logs with 0 games.
6. **Mutex discipline:** the expander shares `rbxscout-sync` with
   finder/hydrator and runs on clean `:15`/`:45` minutes, so it can never
   interleave a DB write or crowd out the hydrator's T1/T2 floors.
7. **Existing engines untouched:** find/hydrate phases, tier cadences and the
   keyword crawl behave exactly as before.

## Known limitations

- **Strict-gate trade-off (accepted):** the small band fills as games *cross*
  20k visits between 14-day re-spiders — not instantly. If that proves too
  slow, a "re-check queue rows older than 14 days" lever is a small add-on;
  the queue schema already supports it.
- **Historical back-scan deferred:** scanning backward into the 9–10B band
  would enumerate mostly dead IDs (cheap per ID, but low yield); it's gated
  behind a KEEP verdict on purpose.
- **Group-only deep portfolios:** creators with >1000 public games stop at
  `SPIDERWEB_MAX_PAGES=10` pages per visit; the 14-day TTL catches the rest
  on later passes.
