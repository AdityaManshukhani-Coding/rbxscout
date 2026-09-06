# 🎯 Scout Philosophy — Living Games, Not a Graveyard

**Written for Aditya, 2026-09-06 — read this when you're back.**
**Catalog at writing: 13,511 games · 69 blow-up-flagged · DB 21.4 MB (sync #172)**

---

## 1. Why RoTrends / Atlas.dev.gg show 100,000+ games (and why most are dead)

Their scale comes from a different **capture policy**, not a better net:

| | RoTrends / Atlas-style trackers | RbxScout (you) |
|---|---|---|
| Capture rule | Every universe that *ever appeared* in any public endpoint gets a row | Games must pass quality gates (visits/CCU) and keep proving life |
| Why they balloon | Roblox hosts ~40M+ universes; search/chart crawls scrape endless abandoned baseplates | Crawl candidates below threshold are dropped before insert |
| Why their corpses persist | Once captured, a row is never deleted — "history" is the product | Strike-prune deletes observed-dead games (~8 days of consecutive 0-CCU) |
| What 100k means there | "Everything we ever saw" | — |
| What 100k would mean here | — | "100k games that recently had real players" — a genuinely different, harder, more valuable dataset |

So the honest framing: **they archive; you scout.** Their metric is coverage of
the past. Your metric is finding the future. Do not chase their number by
adopting their policy — the graveyard *is* their product, and it would poison
yours.

## 2. Your stated goal, translated into pipeline terms

> "I value living and thriving games. I need to **buy those games and make them
> more popular**. My goal is small/good games and then blow them up."

Your target asset is: **small (low visits), alive (non-trivial CCU), rising
(momentum)** — cheap to acquire, proven demand, not yet saturated. In the
current schema that is approximately:

- **Tier 3–4** (warm: thousands of visits, real CCU, not yet big) — 535 games today
- **Newly tiered rows that climb fast** — the blow-up flag catches 2+ tier
  climbs and 3× CCU jumps; **69 games flagged** right now
- **T0 new games with instant CCU** — first hydration already showed players

The scouting product is really the **New & Upcoming watchlist**, not the total
count.

## 3. Implications already implemented (so you don't re-litigate)

1. **Strike-prune (live):** 4 consecutive observed 0-CCU visits ⇒ deleted.
   Revival resets. 14-day unobserved staleness as fallback. The count stays
   honest — no cosmetic 100k.
2. **T8 2-day rotation (live):** every cold game gets re-checked fast, so
   death is *observed* quickly, and revivals are caught within ~2 days.
3. **200 keywords/run trial (live, monitored):** wider net for *new* small
   games; the pilot verdict decides keep/revert with no action needed.

## 4. To maximize "small/good games about to blow up," the highest-value next moves are

Ranked by signal-per-request for your specific goal (buy-early):

1. **Lower entry gates for NEW games only** (e.g. 5k visits / 10 CCU first
   sight, keep 20k/25 for known games). Your target game at discovery time is
   small by definition — the current 20k-visit gate hides exactly what you
   want to buy.
2. **Momentum score, not level score:** rank watchlist candidates by CCU
   slope (last-6h vs previous-24h) rather than absolute CCU. Small game going
   12 → 40 → 130 is the buy signal; a stable 300 is not.
3. **Creator radar (revisit):** a dev whose last game blew up gets their next
   release watched from first hydration — the single most predictive signal
   of a future hit. (You rejected this for flops; note flops now self-prune
   in ~8 days, which changes the old 50/50 math.)
4. **Rolimons new-arrivals diff:** brand-new games with players found without
   search traffic — day-one capture of rockets.
5. **Discord velocity on the watchlist:** small games with an active Discord
   are the ones with a community to amplify — your contact checker already
   collects this; scoring it would directly rank "games I can grow."

## 5. The 100k question, restated honestly

- With gates as-is: reachable search/chart/Rolimons pool plateaus ~60–80k,
  and after strike-pruning, the *living* count is far lower — likely 30–50k.
- With new-game gates lowered + all discovery levers: 100k *total* rows is
  reachable in ~4–7 weeks, but the number that matters for you is the
  **watchlist quality** (small, alive, rising), not the row count.
- Recommendation: **optimize the watchlist, let the total land where it
  lands.** A 40k catalog of living games with a daily blow-up list of 50–100
  candidates beats a 100k graveyard with the same 50 candidates — and unlike
  RoTrends, nothing in your DB is dead weight a buyer could fact-check you on.

## 6. Where things stand while you were away

- All runs green (25/25 checked at last audit), pilot telemetry accumulating.
- Keyword trial verdict prints itself in every log — read one line, act only
  if it says REVERT (one-line fix, prescribed in the log).
- First prune wave expected within ~4 days of 2026-09-06: the 822 cold rows
  at writing either show life or get deleted. The total may *dip* — that is
  the graveyard draining, not the scout failing.

*The pipeline runs unattended. Nothing here needs you until you decide which
of Section 4's levers to pull first.*
