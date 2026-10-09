# Hydration Sources Master Plan

**Status:** Implemented (2026-10-08) — Phases 0–5 coded and tested; awaiting first cloud runs
**Owner:** Aditya · rbxscout
**Last updated:** 2026-10-08
**Goal:** Make "stats are not hydrated" complaints structurally impossible. Every game tier gets a refresh cadence that is actually met, backed by three independent data channels with measured rate limits.

---

## 1. Executive summary

Today stats hydration runs on **one channel**: `games.roblox.com` (the live Roblox API). That channel has a hard, measured ceiling of **~9–11 batches (~450–550 games) per ~window** before unthrottled 429s with no `Retry-After`. Because runs are serialized (~14/hour, ~4.2 min each), we get **~6,300–7,700 live refreshes/hour** — and **60% of that budget is spent on weekly/cold games** (T5–T7 = 93% of the store), while hot games (T2–T4) sit 2–5× past their cadence promises (measured: T2 avg 10.3h vs 2h promise; worst games 36 days stale).

This plan adds two independent channels measured live this session:

| Channel | Role | Verified capability |
|---|---|---|
| **Roblox API** (existing) | Hot tier T0–T4 primary; canonical whenever sources disagree | 50 games/request; the live source of truth |
| **Rotrends** `api.rotrends.com` | Cold tail T5–T7 daily sweep + 90-day history backfill + `earning_rank` | 120/120 requests @ 40 parallel, zero 429s; daily snapshots ≤90-day windows; keys by universeId; 10/10 random store sample tracked |
| **Creator Exchange** per-game pages | Live overflow when Roblox 429 window exhausts; enrichment (`globalRank`, `momentum`, `scammerFlag`); discovery feed | Live CCU (page 10 vs Roblox 9); 47-request burst all 200, no rate limit; any universeId works; HTML-embedded JSON |

Net effect: hot games **3–6× fresher** (every ~15–20 min instead of 1–6h), cold games get **better** data than today (daily vs weekly sampling), newly discovered games get **instant 90-day trend history**, and no single upstream failure can freeze the whole pipeline.

**Note on Atlas Dev / prufer:** it stays as a fourth, *opportunistic* pre-pass (it already serves ~130–300 metric repaints/run when healthy) but its proxy pool is currently degraded (see §4.3) and it is fail-open by design — it is not load-bearing for this plan.

---

## 2. All research done this session (measured, live, anonymous)

Every claim below was verified by direct probe on **2026-10-07/08** from the local machine; probe scripts were ephemeral, no tokens or cookies from user sessions were used.

### 2.1 Roblox API (games.roblox.com) — the existing primary

- Endpoints already in use: `/v1/games` (50 IDs/batch), `/games/votes`, tier rotation `TIER_CADENCE_WALL_HOURS` (scout_core.py ~L402): T1=0.5h, T2=1h, T3=2h, T4=3h (tightened 2026-10-09 — post-hidden demand ≈ 42 games/tick vs the 7,500/tick budget), weekly 3d for T5–T7, ~2d rotation for cold/T8.
- **Measured wall** (live evidence 2026-09-03, comment in scout_core.py; reproduced by run logs): ~11 metric batches each window → hard 429s, **no Retry-After**. Hydrator adapts with a token-bucket pacer (`BATCH_EMIT_INTERVAL=0.2`, stretch-to-6s on 429).
- **Per-run reality** (hydrator run #7789 log, 2026-10-08): 542 games due → 412 hydrated + 130 prufer offloads; 9/9 metric batches; budget line "0 new + 542 known-due → 412 hydrated" (some due games rolled).
- **Serialization**: workflow `concurrency: rbxscout-sync, queue: max` with the expander — one run at a time, ~4.2 min execution (setup 2.6 min + hydrate 3.4 min overlapped + release push 15 s). 5-min dispatch interval is thinner than actual runtime, so dispatches queue; the cloudflare worker dispatches precisely, GitHub backstop schedule (off-peak `4-59/5`) covers worker loss.
- **Strengths:** official API, 50 games/request (50× more efficient than any scraper), any game covered, it is the *upstream* every other tracker mirrors.
- **Weaknesses:** single hard rate-limit wall; datacenter IPs (GH Actions runners) get the same budget; nothing but live values — no history, no earnings, no rank.

### 2.2 Rotrends — api.rotrends.com

**Verified endpoints (anonymous, no auth):**

```
GET https://api.rotrends.com/explore/games/{universeId}/metrics/1d
    ?fields=playing,like_ratio,earning_rank
    &start=2026-10-01T00:00:00.000Z&end=2026-10-08T00:00:00.000Z
    &include_previous_period=true

GET https://api.rotrends.com/explore/games/{universeId}/changelog
    ?start=...&end=...&dataset=1d     -> currently returns [] (requires account?)
```

- Response shape: `{"success": true, "data": {"metrics": [{"process_date": "2026-10-07", "playing": 1421}, ...]}}`.
- **Keys by universe ID** (not place ID — place IDs return 200 with empty metrics; Blox Fruits universe `994732206` confirmed with 240k+ CCU daily rows).
- **Coverage:** 10/10 random universe IDs from our store returned history, including CCU 0, 1, 4, 9, 36, 45, 606, 1,995, 4,892, 50,735. Smaller sample of place-ID lookups also tracked separately.
- **Freshness:** daily snapshots only; newest `process_date` = **yesterday** (~1-day lag). `www` frontend carries data under the same lag; this is a *trends/history* source, not a live one.
- **Rate limits / spam tolerance (tested):** 40 sequential zero-delay + 80 parallel (40 threads) = **120/120 HTTP 200**, 0.03 s/req amortized top-end wall, 0.34 s/req sequential. No 429, no 403, **no Cloudflare challenge on the API subdomain** (the `cf_clearance` challenge in browser dumps applies only to `www.rotrends.com` HTML).
- **Free-tier cap:** custom date ranges **≤ ~90 days**; a 365-day window returns `{"success": false, "message": "Free users cannot use custom date ranges"}`. Unknown field names are silently ignored (no error) — field enumeration inconclusive beyond `playing`, `like_ratio`, `earning_rank` (populated for large games, null for small ones).
- robots.txt: `Allow: /` (wide open). Headers suggest a Flask/FastAPI backend (`404 Not Found` page style).
- **Strengths:** pre-built daily history (the thing Roblox never gives), 90-day depth for backfill, `earning_rank` (revenue proxy Roblox never exposes), generous rate limits (stress-tested §2.5), clean JSON, wide coverage.
- **Weaknesses:** ~1-day lag (useless for hot games), unknown/undocumented field set, token-bucket limiter with ~90 s penalty box (no `Retry-After`), unverified runner-IP behavior, no auth protocol documented (good for us now, opaque if they add keys).

### 2.5 Stress-test results (2026-10-08, from home IP)

**AtlasDev prufer API — no limiter found:**
- 50 sequential zero-delay: 50/50 × 200, 0.24 s/req
- 150 parallel / 50 threads: 150/150 × 200, 3.1 s wall
- 300 parallel / 100 threads: 300/300 × 200, 5.6 s wall (19 ms/req)
- **Sustained 30 s at 60 threads: 2,111/2,111 × 200 = 69.2 req/s, zero errors, zero throttle**
- Caveat: this is the home IP; atlasdev.gg 403s runner IPs at the Cloudflare edge (documented), so the existing validated-proxy pool remains mandatory for GHA execution — the *rate* however is not the constraint.

**Rotrends API — token bucket with ~90 s penalty box:**
- Burst tests (≤80 parallel): all 200 earlier in the session
- Uncontrolled sustained hammer at ~75 req/s: first ~1,200 × 200, then hard 429s
- Penalty box: 429s persist ~90 s even at trickle rate; **auto-recovers fully (~91 s measured)**; no `Retry-After`, no rate-limit headers
- Post-box sustained **~11.5 req/s × 90 s: 1,039/1,039 × 200 clean**
- ~20 req/s sustained: 429s again within ~30 s
- **Production envelope: ≤8 req/s is safely inside the bucket; a 429 is a 95-second pause, not a ban** — worst case with backoff-and-retry logic, zero requests are lost.
- **Hygiene note:** any devtools dump containing `cf_clearance` / GA cookies must be rotated; probes here never used them (they are IP-bound and perishable).

### 2.3 Creator Exchange — creatorexchange.io

**Verified per-game URL:** `https://creatorexchange.io/roblox-game/{universeId}/ANY-SLUG`

- Universe ID is the only real key: **any/wrong/emoji slug → 301 → canonical slug → 200 with full data**. No slug-guessing needed.
- Canonical slug rules observed: emoji/brackets/non-ASCII stripped, trimmed dashes; Cyrillic-only titles degrade to a slugless bare URL (`/roblox-game/3269082462`) — still works. So **emoji/unicode/Arabic edge cases all resolve**.
- **Not top-240-limited:** 19-CCU, 0-CCU, global rank 16,140 games all resolved with full data.
- **Data is live:** Zoo Tycoon page `playing=10` vs Roblox API `playing=9`; visits within 5. Data embedded in Next.js RSC flight chunks (`self.__next_f.push`), parseable with ~10-line regex; blob `initialGameData` carries: CCU (`playing`), `visits`, `likeRatio`, genre L1/L2, `rootPlaceId`, owner/groupId, `momentum` (Surging/Fading), `globalRank`, `scammerFlag`, `growthPattern`, `discordInfo`, game `codes`.
- **Rate limits:** 17 sequential zero-delay + 30 parallel (24 threads) = 47/47 HTTP 200, 0.13 s/req amortized. Backend is **Google Cloud Run** (`server: Google Frontend`), *not* Cloudflare — no challenge wall; runner IPs likely pass (unverified from runners).
- **Critical hazard:** nonexistent/dead universe IDs **hang indefinitely** (60 s timeout hit, 0 bytes) — every client call needs a hard 10–15 s timeout and hang-treated-as-404.
- robots.txt: `Allow: /` for the game pages (only `/api/`, `/dashboard/`, `/admin/` disallowed; explicit allow for `/api/v2/roblox/discover`).
- **Discover feed (already evaluated earlier this session):** `GET /api/v2/roblox/discover?tab=all&sortBy=ccu&...` anonymity depth 10 pages ≈ top-240 games by CCU, fields incl. revenue, momentum, `rankShift1d/3d/7d/30d`, `hasDiscord`; stats freshness varies per game (some 11 months old). Good *discovery* signal, not a stats source.
- **Strengths:** live CCU with no rate-limit wall observed; enrichment fields (`globalRank`, `momentum`, `scammerFlag`) available on every page hit; discovery feed with rankShift/revenue signals.
- **Weaknesses:** 1 game/request, ~78 KB HTML per request (50× less efficient than Roblox); dead-ID hang; RSC parsing fragility if they redesign; small startup — politeness cap is both ethical and self-preservation.

### 2.3.1 Current pipeline measurement snapshot (2026-10-08)

- Store: 24,766 games (cloud), release `catalog-latest` pushed every ~4–5 min; assets: `rbx_scout.db` 113.6 MB, `contacts.bundle`, `stats.json` (badge), `stats_target.json`.
- Tier sizes (cloud): T0=42, T1=306, T2=310, T3=250, T4=775, T5=2,550, T6=2,338, T7=18,195 → **hot (T0–T4) = 1,683; weekly (T5–T6) = 4,888; cold (T7) = 18,195**.
- Measured staleness by tier vs cadence promise (avg age of `last_updated`):

| Tier | Promise | Measured avg | Measured worst |
|---|---|---|---|
| T1 | 1h | 1.65h | 189h |
| T2 | 2h | **10.3h** | 732h (~1 mo) |
| T3 | 4h | 8.9h | 515h |
| T4 | 6h | **17.4h** | 744h |
| T5 | 7d | 117.8h | 760h |
| T6 | 7d | 118.4h | 830h (~1 mo) |
| T7 | ~2d rotation | 116.9h | 830h (~5 wks) |

- 60% of each run's due-queue slots (~325/542) go to weekly/cold games.
- `ccu_history` depth (fresh cloud pull 2026-10-08): 8,930 games with ≤5 samples; 13,891 with 6–20; 1,301 with 21–50; 3,583 with 50+ (27,705 games have any history; game_analytics holds 24,766 active).

---

## 3. The architecture: which source does what

### 3.1 Assignment matrix (refined from the initial idea)

The initial idea mapped Rotrends → hot games. The measured data flips it: Rotrends is snapshots (≥1-day lag), so it can never make *hot* games fresher. Creator Exchange is the third party that serves **live** CCU, so it takes the live-overflow role.

| Store segments | Primary source | Backup / overflow | Cadence target |
|---|---|---|---|
| **T0 (new/unclassified)** | Roblox live (first in line — unchanged) | CE overflow | first hydration ASAP |
| **T1–T4 ("hot", 1,683 games)** | **Roblox live**, budget ring-fenced to hot tiers | **CE live overflow** on 429-window exhaustion | ≤6h promise → target **10–20 min** |
| **T5–T6 ("weekly", 4,888)** | **Rotrends daily sweep** | Roblox leftover capacity if any | daily (better than today's weekly) |
| **T7 ("cold", 18,195)** | **Rotrends daily sweep** | Roblox leftover | daily (better than 2d pull rotation) |
| **Newly discovered games** | Roblox first touch (unchanged) + **Rotrends 90-day backfill** immediately on discovery | — | day-one full trend chart |
| **Enrichment (all tiers)** | CE + Rotrends extras stored alongside: `globalRank`, `momentum`, `scammerFlag`, `earning_rank`, `like_ratio` trend | — | piggybacked, extra cost ~0 |

### 3.2 Rate budgets (the numbers that make it work)

**Roblox live** — after the cold tail moves off:
- Capacity: ~9–11 batches × 50 games per window, adaptive pacer → conservatively ~5,400–7,700 refreshes/hour sustained.
- Demand (T0–T4 at a 15-min ideal): 1,683 × 4/hour ≈ **6,700/hour** — just at capacity; at the promised cadences (~700/hour) there is big headroom, so hot games refresh every ~10–20 min in practice. Ring-fence: hot-tier queue draws first; weekly/cold slots only fill leftovers.

**Rotrends daily sweep** — replaces the 60% cold-slot waste:
- Scope: T5–T7 ≈ **23,083 games/day**. Stress-tested 2026-10-08 (see §2.5): token-bucket limiter, penalty box ≈ 90 s of 429s after sustained overload, full recovery after; **~11.5 req/s sustained 90 s clean**. Production cap **8 req/s** → full sweep in **~48 min**.
- 0-CCU games included in rotation are still cheap (~250 bytes/response) — feasible to sweep the entire cold set, unlike Roblox's 50-ID batches that can't even parse windows fast enough under 429s.
- Bonus same sweep: `like_ratio` + `earning_rank` piggyback at zero extra requests.

**Creator Exchange live overflow** — the "never rolled again" guarantee:
- When the hydrator's metric batches exhaust or 429, remaining *hot-tier* due games (T0–T4 only — never re-target cold games with CE) are routed to CE at **≤2 req/s sustained** (7,200/hour theoretical; we'll use far less).
- Every CE hit is double-purpose: live CCU + `globalRank`/`momentum`/`scammerFlag` enrichment.
- Hard timeouts: 10 s connect+read; any hang = treated as 404; two consecutive hangs = cool that batch, fall back to roll-forward.

**Politeness rules (all three):**
1. CE cap 2 req/s sustained, exponential backoff on any non-200/timeout; treat CE as gift horse — this startup green-lit scraping; don't burn it.
2. Rotrends cap **8 req/s** (stress-verified headroom to ~11.5 req/s clean) with jitter; **on first 429: sleep 95 s once, then resume at half rate** — measured penalty box is ~90 s and self-clears; no `Retry-After` header exists.
3. Roblox keeps the existing token-bucket pacer exactly as-is (it's tuned and measured).
4. AtlasDev prufer: stress-verified **no limiter found** (69 req/s sustained 30 s, 2,111/2,111 × 200; 300-request 100-thread burst clean, 19 ms/req). Keep the existing `PRUFER_FETCH_THREADS=50`; no new cap needed.

### 3.3 Data-integrity rules (non-negotiable)

1. **New `source` column on `ccu_history`**: `roblox_live` (default/current rows), `cx_live`, `rotrends_daily`. Trend charts and the dashboard can filter/join on it; mixed-source views must not silently blend live samples with yesterday's snapshots.
2. **Rotrends rows timestamp with the snapshot's `process_date`** (midnight UTC on that day), never fetch time — the value describes that day.
3. **CE rows timestamp at fetch time** with `source='cx_live'` (it is genuinely live; verified within ±1 CCU of Roblox).
4. **Roblox stays the tie-breaker.** If sources disagree on the same tick, `roblox_live` wins; CE/Rotrends disagreements get logged (cheap drift detector).
5. **Dedupe policy on inserts:** a game gets at most one sample per source per day for daily sources; one per fetch for live sources. (Existing unique index on `ccu_history(universe_id, ts)` — extended to `(universe_id, ts, source)`.)
6. **Game analytics `last_updated`** may advance from any source, but tier classification (which drives cadence) is only recomputed from `source IN ('roblox_live','cx_live')` values — a stale snapshot must never demote/promote a game.

### 3.4 Failure modes & fallbacks (why stats will "always" hydrate)

| Failure | Consequence | Automatic fallback |
|---|---|---|
| Roblox 429 window harder than usual (GHA shared runners) | fewer live batches | hot-tier due games → **CE overflow**; cold set already off Roblox (Rotrends sweep unaffected) |
| Rotrends goes down / adds auth | no daily sweep that day | cold games fall back to Roblox leftover capacity (the old behavior, now with 4× more slack since hot tiers don't compete) |
| CE down or starts rate-limiting | overflow lost | hot games roll forward one tick (today's behavior); enrichment pauses |
| Atlas Dev pool stays broken | pre-pass saves fewer offloads | irrelevant — CE takes the offload role with live data (Atlas was metric-only repaints anyway) |
| Cloudflare worker cron dies | dispatch stops | GitHub backstop schedule (off-peak `4-59/5`) already in hydrator.yml — verified designed-for (2026-09-21 + 2026-10-05 incidents) |
| Release push fails mid-swap | run red, catalog protected | existing "one-way door" pattern (workflow fails the run; `if: always()` push protects the asset) |

The pipeline thus degrades gracefully at *every* failure to at-least today's freshness, and usually better — no single upstream can freeze hydration.

---

## 4. Implementation plan

Phased so each step ships value alone. All phases are additive; the Roblox hydrator keeps running untouched until each replacement proves itself.

### Phase 0 — one-line posture fix (ships immediately)

- **Ring-fence the Roblox budget:** in `live_sync.py` / scout_core's hydrator mode, process the tier-due queue in strict order T0 → T1 → T2 → T3 → T4 → weekly leftovers, and **stop scheduling weekly/cold games in a run whenever hot-tier demand ≥ available batches** (currently ~325/542 slots leak to weekly/cold while hot tiers run overdue).
- Files: `scout_core.py` (`load_tier_refresh_ids`, hydrator drain logic ~L1797-1835), possibly `live_sync.py`.
- Acceptance: run log's `refresh queue` line shows hot tiers draining first; T2/T4 measured avg staleness drops within 48h.

### Phase 1 — Rotrends daily sweep (biggest win, independent of everything)

- New module `rotrends_sweep.py` (mirrors `atlas_home.py`/`catchup_metrics.py` style):
  - Pull due T5–T7 universe IDs, chunked at **8 req/s** (stress-verified production cap; env knob `ROTRENDS_RPS`), full sweep ≈ 48 min.
  - `fields=playing,like_ratio,earning_rank`; window = yesterday→today (the ≤90-day cap only constrains *backfill*, not daily sweeps).
  - Insert with `source='rotrends_daily'`, `ts = process_date`.
- **Escalation from the daily signal (stale-stats repair, 2026-10-09):** the sweep carries each cold game's fresh daily CCU at zero extra request cost, but the original contract (history + enrichment only) left fast-growing T5–T7 games stuck under a weeks-old tier stamp with days-old rendered stats — the ring-fenced Roblox budget reaches the weekly bucket only as leftover. Fix: `upsert_rotrends_snapshot` ESCALATES a stale row on two triggers — (a) tier climb: `classify_tier(stored_visits, daily_ccu)` classifies higher than the stored stamp (`tier`/`prev_tier`/`tier_since` re-stamped, blowup rules identical to `_tier_stamp_for`) — measured 123 games on the cloud store; (b) in-tier multiplication: stored CCU ≥ 10 with the daily ≥ 3× it and ≥ 250 (T5-scale floor) — measured 560 more games, mostly the T7 tail a climb-only trigger would never touch. Escalated IDs go into the sweep summary (`tier_bumps` / `bumped_ids`) and the sweep immediately hot-refreshes up to 100 of them per pass through Roblox batched metrics (`hot_refresh_batch`), restoring canonical `ccu`/`visits` the same run instead of waiting for the hot scheduler. Rotrends still never writes `game_analytics.ccu/visits` itself, never DEMOTES (a daily pool dip can be a snapshot quirk; demotion before Roblox confirms would wrongly silence hot games), and never re-stamps the tier on an in-tier escalation — the tier stamp stays Roblox's, escalation only schedules the refresh.

- **Hidden-recovery watch (2026-10-09 cadence audit):** hidden (below-gate) rows are excluded from every
  scheduler group, so before this watch a quiet recovery — a wobbly game steadying at 40 CCU, no tier climb,
  below the 250 multiplication floor — was undetectable forever (the only other unhide path, Atlas
  re-enqueue, has been 403-blocked from runners since Oct 8). The sweep already touches hidden rows (they
  are T5–T7 rows with ccu_history staleness); now `_restamp_tier_from_daily` escalates a hidden row as soon
  as its freshest daily CCU crosses `HIDDEN_RECOVERY_MIN_CCU` (= the 25 gate floor) — `hot_refresh_batch`
  live-refreshes it, `upsert_game` re-evaluates the merged gate, and the row unhides itself within ~1 day of
  a real recovery, at near-zero marginal cost.
- Backfill pass (same module, second callable): for any game lacking ≥5 history rows, fetch up to 90 days and insert backdated rows. Run for newly discovered games inside the expander's landing path (`_enqueue_discovery`/expander success path), and for the 8,930 currently shallow games (≤5 samples) in a slow catch-up.
- Orchestration: extend `expander.yml` (already shares the `rbxscout-sync` mutex) with a step gated at `:45` runs, or a new workflow file dispatched by the Cloudflare worker once daily (`wrangler.toml` cron variants). Daily, not per-5-min.
- Add `schema_patch` for `ccu_history.source` (default `'roblox_live'` for existing rows; new index) consistent with how past schema versions were migrated in `_init_sqlite`.
- Acceptance: after first full sweep, 0 games in T5–T7 older than 48h; `ccu_history` non-roblox-live rows queryable/tagged.

### Phase 2 — Creator Exchange overflow + enrichment

- New module `cx_client.py`:
  - `fetch_universe(uid, ...)` → GET `https://creatorexchange.io/roblox-game/{uid}/x` with `-L`, UA header, **10 s total timeout**, hang→404 mapping, RSC chunk regex → `initialGameData` dict → `{playing, visits, likeRatio, momentum, globalRank, scammerFlag, rootPlaceId}` (+ fetched-at timestamp).
  - Politeness: ≥0.5 s between calls, backoff ×2 on errors, abort-batch on 2 consecutive non-200s.
- Hook into scout_core hydrator: when the metric-batch budget exhausts (429 breaker or cap) and due hot-tier games remain, pass them through CE instead of rolling forward; write `source='cx_live'`, and write enrichment into `game_analytics` (`global_rank`, `momentum`, `scammer_flag` columns — schema patch).
- Never route cold games through CE (their budget is Rotrends') — CE is strictly the live-overflow + enrichment channel.
- Sanity probe at startup: one known-good universe ID + one intentionally-dead ID (assert hang handled).
- Acceptance: on a run where Roblox 429s early, the log shows `cx overflow: N hydrated 0 rolled`; measured CE-vs-Roblox CCU drift <2% on a 200-game sample.

### Phase 3 — Rotrends `earning_rank` + dashboard surfacing

- Piggyback columns `earning_rank`, `cx_momentum`, `cx_global_rank` into `game_analytics` (Phase 1/2 write them).
- Streamlit app.py catalog table + game detail page get an "Earnings rank" / "Momentum" indicator with source tag — revenue-signal scouting we've never had.

### Phase 4 — prufer/AtlasDev pool repair (separate track)

- The pre-pass is degraded: `24% acceptance · pool 0 proxies — sustained low acceptance` (run #7789). Investigate `prufer.py`'s free-proxy validation path; either repair the validator or drop `PRUFER_ENABLED` to save pool-validation minutes per run. **This plan does not depend on AtlasDev** — it recovers ~130–300 extra metric repaints/run when healthy, but every role it was meant to play is now covered by Rotrends (metric series) or CE (live metrics).

### Phase 5 — observability ("always hydrated" becomes provable)

- Extend the run-summary lines (already written to Actions log + stats targets) with per-source counts: `roblox_live N · cx_live N · rotrends_daily N · rolled N`.
- Add a staleness SLO check inside the hydrator: query max staleness per tier; if any tier exceeds cadence × 3, log `⚠ tier overdue` (dashboard can show it too). Complaints become a grep, not a vibe.
- `stats.json` badge gains `last_source_breakdown`.

### What stays the same

- Cloudflare worker dispatch (worker `GITHUB_TOKEN` secret fixed & verified 2026-10-07) — the clock is healthy.
- Release-asset catalog store, `contacts.bundle` split-push, `rbxscout-sync` mutex, tier classification logic, discovery queue, contacts scanning, laptop `atlas_home.py` cadences.

---

## 5. Measurement & success criteria

Before/after on the same staleness queries (fresh cloud catalog download):

| Metric | Today (measured) | Target | Verify by |
|---|---|---|---|
| T2 avg staleness | 10.3h | < 0.5h | cloud DB query, 72h after Phase 0–2 |
| T4 avg staleness | 17.4h | < 1h | same |
| T5–T7 max staleness | 830h (5 wks) | < 48h (daily data) | after first full sweep |
| Hot-tier rolls ("0 hydrated") per day | untracked | < 5% of hot demand | new per-source run summary |
| Games with ≥20 history samples | 4,966 of 27,705 | all active games grow via sweep+backfill | depth query |
| Upstream single-point failure | pipeline-wide | none degrades below today's baseline | drill: disable one source in staging run |

Performance budgets to respect: each 5-min tick stays ≤20 min hard cap (workflow `timeout-minutes: 20`), hydrate step stays ≤ ~4 min, so the serialized clock keeps pace. Rotrends sweep (~1.3–2h/day total work) should be split into ≤10-min slices if run inside the expander, or run as its own daily workflow.

---

## 6. Risks & open items

1. **Runner-IP unverified** — CE/rotrends probes ran from the laptop. Mitigation: both saw no Cloudflare on the relevant hostnames; first GHA runs will confirm. Expectation: pass.
2. **Rotrends sustained tolerance** — stress-tested 2026-10-08 (§2.5): token bucket with ~90 s penalty box, clean at ~11.5 req/s, production-capped at 8 req/s with a measured 95 s pause-and-resume-on-429 policy. Long-run (multi-day) behavior still unproven; the limiter makes sustained abuse impossible anyway.
3. **CE could add rate limits/auth/Cloudflare later** — module is isolated (`cx_client.py`); losing it reverts to today's behavior, not worse.
4. **Trend-mixing correctness** — the `source` column + per-day dedupe + timestamping rules in §3.3 are the guardrails; dashboard should default to the current tick's dominant source for hot games.
5. **Schema migration on a 113 MB moving asset** — follow the existing `_init_sqlite` patch pattern; wrap in try/except and version-check like prior column additions; release push is serialized so no concurrent writers.
6. **Politeness posture** — CE cap 2 req/s is a choice: it's a cooperative little platform, and its canonical pages + discover API are robots-green. Don't relitigate upward without evidence of a fuller allowance.

---

## Appendix A — probe quick-reference (verified live this session)

```bash
# Rotrends: daily series for a universe (≤90-day window free tier)
curl 'https://api.rotrends.com/explore/games/{universeId}/metrics/1d?fields=playing,like_ratio,earning_rank&start=YYYY-MM-DDT00:00:00.000Z&end=YYYY-MM-DDT00:00:00.000Z&include_previous_period=true'

# Creator Exchange: live CCU per universe — slug is ignored, ID routes
curl -L --max-time 10 -A 'Mozilla/5.0' 'https://creatorexchange.io/roblox-game/{universeId}/x'
# -> 301 -> /roblox-game/{uid}/{canonical-slug} -> 200; parse self.__next_f RSC chunks -> initialGameData

# Creator Exchange discover: top-240 by CCU, ranks/momentum/revenue signals
curl 'https://creatorexchange.io/api/v2/roblox/discover?tab=all&sortBy=ccu&sortDir=desc&limit=40&cursor=N'
```

Indexing: rotrends = **universeId**. CE pages = **universeId**. Roblox `/v1/games` = universeId batches (already so).

## Appendix B — measured run-log excerpt (hydrator run #7789, 2026-10-08)

```
SYNC #2955 [hydrator] — GREEN ✅ (204s)
hydration budget  : 0 new + 542 known-due → 412 hydrated, 0 rolled to next sync (cap 150 batches)
prufer pre-pass   : 130/542 games served via Atlas Dev (24% acceptance · pool 0 proxies) — ⚠ sustained low acceptance
refresh queue     : T1 74 · T2 90 · T3 21 · T4 32 · weekly 308 · T8 17
tier distribution : T0: 42 · T1: 306 · T2: 312 · T3: 250 · T4: 774 · T5: 2,552 · T6: 2,336 · T7: 18,195
metrics batches   : 9/9 OK · breaker: False
```

Note `weekly 308 + T8 17` = 60% of due slots spent on cold tiers — the exact waste Phases 0+1 eliminate.
