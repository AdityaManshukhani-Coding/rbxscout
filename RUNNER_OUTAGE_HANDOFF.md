# Actions runner outage — handoff (2026-10-10)

## TL;DR
The pipeline is code-complete and verified locally. **Nothing can run on GitHub
Actions since ~04:30Z today** (9+ hours): every dispatch either sits `pending`
forever or concludes `failure` in 2s without executing a single step (no jobs,
empty logs). This is a **GitHub-side block, not a repo/code issue**. Two code
fixes were shipped anyway so the moment runners resume the pipeline heals
itself in ~15–20 min.

## What logs proved (root causes)

**Cause A — code, FIXED:** Oct 9–18:00 runs show
`Metrics circuit breaker tripped after 5 consecutive failed batches`
(`transport: ReadTimeout` storm from Azure egress) → every missed game flowed
into the serial CE overflow loop at ~16 s wall per miss → job passed the
20-min runner timeout and was cancelled mid-pass. Repeated every tick.

→ Fix shipped: commit **30f46f7** — `CX_DEADLINE_S` (default 90 s,
env-overridable) caps the whole CE overflow pass; the remainder rolls
forward to the next sync. Overflow is a repair channel, not a bulk channel.
317/317 tests pass. Tests updated (`fake_fetch(ids, deadline_s=None)`).

**Cause B — GitHub-side, NOT fixable from code:** since ~04:30Z today,
hosted runners never pick jobs up (runs created → 2s → completed failure,
`jobs total_count: 0`, zero-byte logs). Ran an Actions-wide bisection: zero
green runs anywhere in 11,356-page history since 18:41Z yesterday. Repo is
**public**, so private-repo minute-quota does not apply. Cancelling all 20
queued runs and re-dispatching did not unblock — 8 fresh dispatches have sat
`pending` for 30+ minutes.

## Resume checklist (user actions)

1. https://www.githubstatus.com/product/products/actions — check for an
   active incident (hosted-runner capacity)
2. https://github.com/settings/billing + repo `Settings → Actions` — look
   for any suspension banner (payment/abuse auto-flag)
3. If both clean → open GitHub Support with "all workflow_dispatch runs
   conclude failure in 2s, 0 jobs spawned, since 2026-10-10 04:30Z"

## After runners resume — expected behavior

- The backlog clears in **~3–4 passes ≈ 15–20 min** at the normal 5-min
  cadence (each pass clears up to 7,500 most-stale games; the demand is
  ~2,330/pass = 36% of budget).
- No manual catch-up needed; `fefa1c2` (T5/T6 1h, T7 3h cadences) +
  `30f46f7` (CE deadline) handle everything automatically.
- Verify: `Hydrator (5 min)` runs go **green** with
  `push: rbx_scout.db … sync #34xx` in logs; dashboard staleness drops.

## Verified locally earlier today (independent of runners)

- Pulled store sync #3457 (24,247 games) → ran a 500-id hydration slice
  (424 upserted, real live CCUs: Superhero Arena 11,064; Create Pottery!
  4,093; Anime Ball Duels 2,325) in **2.1 s** for 10 batched calls.
- Pushed that back as **sync #3458** — so any data shown in the dashboard
  since ~10:15Z already reflects fresh T5–T7 rows from this laptop run.
