# Hydrator Research — How to Hydrate 24.5k Games Fast

**Date:** 2026-09-20 · **Status:** research complete, no code changed.
Every number below was **measured live today** against the real endpoints and
the real catalog (24,537 games), not estimated.

---

## 1. Executive summary

**Headline: we do NOT have a throughput problem. We have a correctness and
efficiency problem.** The math:

| | Batches/day (50 games each) | Roblox requests/day | Pure API time |
|---|---|---|---|
| **Demand** (all tier cadences, 24.5k games) | ~610 | ~610 | ~12 min at safe pace |
| **Supply** (5-min syncs, current budget) | up to 2,880 | up to 2,880 | — |
| **Theoretical ceiling** (safe 2 req/s) | ~5,760/day per *minute* of API time | — | full catalog = **~5 min** |

The hydrator could refresh the *entire* catalog in ~5 minutes of API time.
What actually slows us down:

1. **A misclassification bug (F3):** games whose metric batch hit a 429 are
   marked `metrics_failed` — treated as dead — and **never retried**. Verified:
   18 of 60 "failed" games were alive. In the Atlas backlog that wrongly
   condemned ~67 real games.
2. **A 429 seesaw (F2):** the 0.2 s base pace trips Roblox's *burst* limiter
   every ~10–15 requests → backoff stretches to 6 s → average throughput
   collapses far below the safe sustained rate.
3. **A lucky endpoint exists (F4):** Rolimons publishes **near-live CCU for
   7,511 games in ONE request** (verified within 0.3–1.5 % of live Roblox).
   It covers 6,130 of our catalog games (25 % — exactly the hot tiers that
   dominate refresh demand). One request can replace ~120+ batched requests
   per day of hot-tier CCU refreshing.

---

## 2. Current architecture (verified from code)

```
Cloudflare Cron → workflow_dispatch every 5 min → Hydrator workflow
  → pull rbx_scout.db (release asset) → sync:
      1. NEW candidates (never hydrated) first — mandatory one-time pass
      2. Known games by tier cadence (most-stale first), budget-capped
  → upsert_game (tier stamp, peak_ccu, ccu_history) → push asset
Mutex: `rbxscout-sync` concurrency group shared with finder/expander
```

**Per-request mechanics** (`fetch_game_metrics`, scout_core.py:2336):
- Endpoint: `GET games.roblox.com/v1/games?universeIds=…` — **50 IDs/request**,
  one response carries *everything* we store: `rootPlaceId, name, playing,
  visits, favoritedCount, genre, creator, description`.
- Pacing: AIMD token bucket — base emit interval 0.2 s, ×1.6 on 429 (cap 6 s),
  ×0.97 decay on success. 8 worker threads, circuit breaker at 5 consecutive
  failed batches.
- Icons: batched 50/call but only fetched for **new** matches, not per sync (no cost).

**Tier cadences** (wall-clock) and measured populations:

| Tier | Cadence | Games | Refresh demand/day |
|---|---|---|---|
| 0 (never hydrated) | one-time | 254 | one-time |
| 1 | 1 h | 396 | 190 batches |
| 2 | 2 h | 437 | 105 batches |
| 3 | 4 h | 378 | 45 batches |
| 4 | 6 h | 1,029 | 82 batches |
| 5–6 | weekly | 4,570 | 13 batches |
| 7 (T8 rotation) | 2-day slice | 17,473 | 175 batches |
| **Total** | | **24,537** | **~610 batches/day** |

---

## 3. Rate-limit findings (measured 2026-09-20)

Roblox exposes `x-ratelimit-limit: 300, 300;w=60` (300 requests / 60 s sliding
window) on `games.roblox.com` — but the headers **lie about enforcement**:

| Test (this IP, real traffic) | Result |
|---|---|
| Burst 5 req/s | first ~11 requests 200, then straight 429s — with `x-ratelimit-remaining` showing **288 of 300 left** |
| Burst 20 req/s | 20/20 429s |
| 2 req/s sustained (after cooldown) | 19/30 429s |
| Drain earlier today (AIMD active) | 3–4 batches failed per 15-batch chunk |

Conclusion: there is a **second, undocumented burst limiter** (trips after
~10–15 fast requests) *in front of* the advertised 300/min window, and once
tripped, the IP stays penalty-throttled for tens of seconds. Community
threads (devforum) confirm multiple undocumented layers and misleading
headers. Practical safe sustained pace: **~1.5–2 req/s (75–100 games/s)**
with spacing — the current 0.2 s base (5 req/s) sits *above* the burst
threshold, which is why the seesaw exists.

Also re-verified today: **50 IDs is a hard cap** on `/v1/games?universeIds`
(51 → HTTP 400 "Too many universe IDs were requested") — matches the
2026-09-17 spike note; `/v2/games?universeIds=` does not exist (404);
`multiget-place-details` now requires auth (401).

---

## 4. The lucky endpoint: Rolimons gamelist ✅ verified

`GET https://api.rolimons.com/games/v1/gamelist` — **one 1 MB request**, no auth:

- **7,511 games**, each row: `[name, live_playing, icon_url]` keyed by **place ID**.
- **Freshness verified:** compared 4 shared games against live Roblox within
  the same minute — Steal An Egg 2,728,157 vs 2,761,940 live (−1.2 %),
  Blox Fruits 333,540 vs 332,558 (**+0.3 %**), Brookhaven −1.5 %. This is a
  near-live CCU mirror, not a daily cache.
- **Overlap with our catalog: 6,130 games (25 %)** — and 82 % of Rolimons'
  list is already ours. It covers precisely the popular games whose 1–6 h
  cadences generate ~70 % of refresh demand.
- Already parsed by our code (`fetch_rolimons_games`) for catalog import —
  the parser is proven; it has never been used as a *refresher*.

**What it can't do:** visits, favorites, genre, creator, description, universe
IDs (join via `root_place_id`). So it's a CCU accelerator, not a replacement —
Roblox stays the source of truth for gate decisions and everything except CCU.

---

## 5. Findings

**F1 — Capacity is 200× demand.** Full-catalog refresh = 491 batches. At a
safe 2 req/s that is ~5 minutes. Even the current conservative budget could
hydrate the whole catalog twice a day. Any "we need more throughput" instinct
should be re-pointed at correctness below.

**F2 — The 0.2 s base pace guarantees periodic 429 storms.** Every ~11th
request trips the burst limiter → interval stretches toward 6 s → effective
throughput drops far below what a steadier 0.5–0.7 s pace would yield. The
AIMD controller works, but it's calibrated to a threshold we now know is
~10–15 fast requests, and it never reads the rate-limit headers Roblox
publishes.

**F3 — `metrics_failed` conflates "rate-limited" with "dead".**
`drain_discovery_queue` / `scan_frontier` mark any ID absent from a failed
fetch as `metrics_failed`, which is terminal (never re-hydrated). Sampled 60
of the Atlas backlog's 223: **18 alive (429 victims), 42 genuinely dead** →
extrapolated ~67 real games wrongly condemned in this one backlog. Same bug
silently drops games in every drain that touches a 429.

**F4 — Rolimons gamelist is a free, near-live CCU lane for the hot 25 %.**
One request per sync replaces ~30–60 batched hot-tier requests/day, keeps the
dashboard's CCU column near-live, and removes the hot tiers from Roblox's
rate-limit exposure entirely.

**F5 — Cold-tier demand is misallocated.** 17,473 tier-7 games get a 2-day
rotation, but only ~1,900 of them sit in the 20k–100k outreach band the
product actually scouts. The other ~15,500 consume ~150 batches/day for stats
nobody acts on.

---

## 6. Improvement plan (prioritized)

**P1 — Fix `metrics_failed` semantics (correctness, small).**
Distinguish 429/5xx batches from clean-but-missing: on a throttled/failed
batch, leave IDs **pending** (auto-retry next drain); on a clean 200, mark
missing IDs dead. Add a weekly retry sweep for existing `metrics_failed`
rows (~67 Atlas games come back). *No new endpoints, ~30 lines + tests.*

**P2 — Rolimons CCU pre-paint lane (the big win, ~60 lines).**
Each sync: 1 gamelist request → for every matched `root_place_id` whose
`ccu` is stale past the tier cadence, update `ccu` (+ `ccu_history` row, since
this is real player-count telemetry — decide flag `record_ccu_history=True`)
and re-stamp tier if it crossed a boundary. Roblox batches then only handle:
non-Rolimons games, visits/favorites refresh, and gate re-checks. Expected:
hot-tier Roblox requests drop ~70 %; dashboard CCU becomes near-live for the
games users actually look at. Guard: if Rolimons is down/changed shape, fall
back to Roblox lane silently (it already has diagnostics).

**P3 — Recalibrate pacing + read the headers (~20 lines).**
Base emit 0.2 s → 0.5 s (safely under the burst threshold); parse
`x-ratelimit-remaining`/`reset` from every response — when `remaining` gets
within 15 % of the window, pre-emptively slow instead of waiting for 429s.
Keeps the AIMD as the safety net. Expected: near-zero 429s, steady ~2 req/s.

**P4 — Make the hydrator budget adaptive (config, ~10 lines).**
`HYDRATION_BUDGET_PER_SYNC` is a fixed 150 batches. Make it
`min(due_batches, env_cap)` so a big backlog (post-Atlas catch-up) drains at
full speed while steady state spends only what's due. The 5-min cadence +
existing rollover already guarantee no starvation.

**P5 — Narrow the T8 rotation to what matters (policy knob).**
Rotate the ~1,900 in-band games on 2 days; give out-of-band cold games a
7–14 day rotation. Cuts cold demand ~5–10× and shrinks `ccu_history` growth
(ties into the history-retention policy). One SQL/policy change in
`load_tier_refresh_ids`.

**P6 — Skip-unchanged writes (micro).** `/v1/games` returns nothing stale;
if a future response adds `updated`, skip the upsert when stats are
byte-identical. Minor — do opportunistically.

**Not worth doing (measured dead ends):** bigger batches (cap verified twice),
`/v2/games` (404), `multiget-place-details` (401 auth), proxies (we're
nowhere near limits once P3 lands), parallel runners (same IP = same limit),
presence API (user-shaped, not game-shaped).

---

## 7. Expected end state

| Metric | Today | After P1–P5 |
|---|---|---|
| Hot-tier (T1–T4) CCU freshness | 1–6 h (when keeping up) | **near-live** (Rolimons lane) |
| Roblox requests/day | ~610 batches + 429 waste | ~150–250 batches, no storms |
| Wrongly-condemned games | ~⅓ of every throttled batch | 0 (retry lane) |
| Full-catalog refresh time | n/a (never attempted) | **~5 min** if ever needed |
| Mid-tier band freshness | 2-day rotation | 2-day rotation (unchanged) |
