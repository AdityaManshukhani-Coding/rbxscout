# 🧪 Capacity Pilot — the 2–3 Day Plan (Phase 1)

**Status:** LIVE since 2026-09-06 · **Mode:** observe-only, zero behavior change
**Where the results appear:** the `CAPACITY PILOT` block at the bottom of every
Actions run log (finder and hydrator), plus the `sync_health_log` table inside
the catalog DB (mirrored by every push to the `catalog-latest` release).

---

## What is running right now

Two experiments share one telemetry system (`capacity_pilot.py` → `sync_health_log`):

| # | Experiment | Change made | Trial length | Automatic verdict |
|---|---|---|---|---|
| 1 | **Keyword trial** | `KEYWORDS_PER_SYNC` 100 → **200** (sweep ~24.5h → ~12h, 2× discovery) | 72 finder runs ≈ 12h minimum; read after **3 days** | KEEP 200 / BORDERLINE / REVERT — printed every run |
| 2 | **T8 rotation** | `TIER8_ROTATION_DAYS` 3 → **2** (already approved & live) | n/a — watch known-due queue grow to ~500–800/run | none needed (no failure mode beyond pacer) |
| 3 | **Scaler pilot** | No behavior change — records utilization + recommendations every sync | 2–3 days minimum, ideally 2 weeks | recommendations accumulate into Phase-2 confidence |

---

## The keyword-trial decision rule (automated)

The pilot computes, over the last 72 finder runs:

- **keyword success rate** = OK ÷ total keywords served
- **bench rate** = % of runs where the proxy was benched at least once
- **breaker trips** across the window

| Verdict | Trigger | Action |
|---|---|---|
| ✅ **KEEP 200/run** | success ≥ 97%, benching ≤ 5% of runs, 0 breakers | do nothing; next lever = Rolimons new-arrivals |
| ⚠️ **BORDERLINE** | between green and red | keep watching; consider a 2nd proxy mirror |
| ⛔ **REVERT** | success < 95%, OR benching > 30% of runs, OR > 2 breaker trips | set `KEYWORDS_PER_SYNC = 100` in `scout_core.py` (one line), or deploy a 2nd Worker mirror and keep 200 |

The verdict text appears in **every** finder run log — you cannot miss it, and
you never have to compute anything by hand.

---

## What the pilot logs every run (sync_health_log)

`run_id, mode, ts, kw_total, kw_ok, kw_benched, kw_breaker, metrics_failed,
metrics_total, known_due, hydrated, deferred, utilization_pct,
recommendations (JSON), extra (catalog size, tier counts, discovery count)`

Read it any time: `python capacity_pilot.py report` (local DB), or query
`sync_health_log` after a `db_sync.py pull`.

---

## Hydrator side (same report block)

- **known-due avg & max vs the 7,500/run cap** — the switch point for
  Phase 2 is a sustained average above ~40% (3,000/run).
- **budget utilization** vs the 2.16M/day theoretical cap (observed runs use a
  tiny fraction; the floor matters, not the ceiling).
- **deferred count** — anything rolling to the next sync means saturation.
- **Phase-2 floors preview:** T1 must never exceed 1h, T2 never 2h.

---

## Phase 2 — what flips when the pilot says so (locked for now)

Trigger to build Phase 2 (any of): keyword trial verdict is KEEP for 3 days,
hydrator due-queue sustains >40% of cap, or utilization grows with catalog
size. The actor then adjusts cadences with the guardrails we agreed:

1. Hard floors: T1 ≥ 1h, T2 ≥ 2h — never looser, regardless of pressure.
2. Bounded steps: ±25% max per adjustment, ≤1 adjustment/hour.
3. Hysteresis: N consecutive runs above band to tighten, M below to loosen.
4. Circuit-breaker coupling: 429 spike ⇒ immediately loosen toward safety.
5. Auditability: every change recorded with its reason; kill-switch env var
   freezes back to fixed cadences instantly.
6. State lives in the DB so all runners agree.

**The rule the actor implements is exactly the rule the pilot logs** — that is
the point of running Phase 1 first.

---

## Daily checklist while the pilot runs

- [ ] Open any Hydrator run log → scroll to `CAPACITY PILOT` block → read the
      keyword-trial verdict and utilization line.
- [ ] If verdict = ⛔ REVERT: apply the one-line revert (or add a 2nd mirror),
      commit, push. The trial restarts accumulating from that point.
- [ ] If verdict = ✅ KEEP for 3 consecutive days: trial passed — plan the
      Rolimons new-arrivals feed as the next discovery lever.
- [ ] Glance at `hydrator 24h` line: due avg should sit in the low hundreds
      (~500–800 with the 2-day T8 rotation), nowhere near 7,500.
- [ ] After 2–3 days of clean data: tell Codebuff to start **Phase 2** with
      "implement the actor per PILOT_PLAN.md, floors and hysteresis included."
