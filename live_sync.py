"""Live sync runner — the pipeline's "on switch" outside the Streamlit UI.

Runs the full pipeline (discovery charts + Rolimons + the rotating keyword
slice, then the tier-due hydration queue) and prints a green/red summary:
keyword slice coverage, catalog growth, hydration budget spend, tier
stamping, blow-up flags, 429/error counters. The dashboard's "🔄 Sync live
data" button does NOT run this — it only reads the catalog from SQLite
(scout_core.load_catalog_matches); discovery is owned by the 24/7 pipeline
(Cloudflare cron → the GitHub Actions finder/hydrator workflows).

Usage:
    .venv/bin/python live_sync.py [--min-visits 20000] [--min-ccu 25]
                                  [--only finder|hydrator]

    --only finder    discovery + keyword crawl only; finds and hydrates NEW
                     games, never drains the known-game refresh queue.
    --only hydrator  refreshes games already in the catalog (tier-due queue);
                     zero discovery traffic — the cheap, frequent pass.
    --only expander  the catalog-expansion pilot (EXPANSION_PILOT.md): creator
                     spiderwebbing → discovery-queue drain → frontier scan,
                     strict 20k/25 gate. Pilot-rate bounded by env knobs.
    (no flag)        full pipeline: find + hydrate in one run (UI parity).

The Cloudflare Worker invokes the GitHub workflows via workflow_dispatch: it
fires the hydrator every 5 minutes and the finder at UTC :00/:30. Both
workflows share one Actions concurrency group so two runs can never commit the
SQLite DB at the same time.

Safe to re-run at any time: every sync advances the keyword slice (finder
runs only), hydrates by tier cadence, and upserts what it finds. The DB is
the source of truth.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from scout_core import KEYWORD_DICTIONARY, RobloxPlatformScout  # noqa: E402
import expansion_pilot  # noqa: E402
DB_PATH = str(APP_DIR / "rbx_scout.db")


def snapshot() -> dict:
    """Pre-sync DB counters for growth reporting."""
    import sqlite3

    with sqlite3.connect(DB_PATH) as conn:
        q = lambda s: conn.execute(s).fetchone()[0]
        return {
            "games": q("SELECT COUNT(*) FROM game_analytics"),
            "ccu_history": q("SELECT COUNT(*) FROM ccu_history"),
            "place_map": q("SELECT COUNT(*) FROM place_map"),
        }


def reset_keyword_cursor_if_stale(scout: RobloxPlatformScout) -> None:
    """One-time fix: the cursor sits at position 160 from the OLD keyword
    list. The dictionary was replaced (661 words, 9 slices), so the stale
    position would skip Slice 1 + the brainrot slice until the list wraps.
    A cursor in [0, len(list)) pointing mid-list with an old timestamp gets
    rewound to 0 exactly once."""
    try:
        with scout._connect() as conn:
            row = conn.execute(
                "SELECT next_index, updated_at FROM keyword_crawl_state WHERE id = 1"
            ).fetchone()
        if not row:
            return
        index, updated = int(row[0] or 0), str(row[1] or "")
        total = len(KEYWORD_DICTIONARY)
        # Old-list position from before 2026-09-03 (the day the new list
        # shipped). Rewind it once; after the first live sync the timestamp
        # is current and this becomes a no-op.
        if 0 < index < total and updated < "2026-09-03":
            with scout._connect() as conn:
                conn.execute(
                    "UPDATE keyword_crawl_state SET next_index = 0, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = 1"
                )
            print(f"[fix] keyword cursor rewound {index} -> 0 (old-list position)")
    except Exception as exc:  # never block a sync on the rewind
        print(f"[warn] cursor rewind skipped: {exc}")


def summarize(scout: RobloxPlatformScout, before: dict, elapsed: float, mode: str) -> int:
    scan = scout.last_scan or {}
    diag = scout.source_diagnostics or {}
    status = scan.get("status", "unknown")
    ok = status == "complete"

    print("\n" + "=" * 62)
    print(f"SYNC #{scan.get('sync_number', '?')} [{mode}] — {'GREEN ✅' if ok else 'FAILED ❌'} ({elapsed:.0f}s)")
    print("=" * 62)
    print(f"status            : {status}" + (f" — {scan['error']}" if scan.get("error") else ""))
    print(f"keyword slice     : {scan.get('keyword_slice_start', 0) + 1}–"
          f"{scan.get('keyword_slice_end', 0)} of {len(KEYWORD_DICTIONARY)} · "
          f"{scan.get('keyword_discovered', 0)} games discovered"
          + ("  (skipped: " + mode + " run)" if mode in ("hydrator", "expander") else ""))
    print(f"candidates        : {scan.get('candidate_count', 0):,}")
    hyd = scan.get("hydration_budget") or {}
    print(f"hydration budget  : {hyd.get('new', 0)} new + {hyd.get('known_due', 0)} known-due "
          f"→ {hyd.get('hydrated', 0)} hydrated, {hyd.get('deferred', 0)} rolled to next sync "
          f"(cap {hyd.get('budget_batches', '?')} batches)")
    before_games = before.get("games", 0)
    print(f"catalog           : {before_games:,} → {scan.get('catalog_count', before_games):,} games "
          f"(Δ {scan.get('catalog_count', before_games) - before_games:+,})")
    print(f"matches/target    : {scan.get('matched_count', 0):,} · pruned stale: {scan.get('pruned_stale', 0):,}")
    sched = scan.get("tier_schedule") or {}
    if sched:
        print(f"refresh queue     : T1 {sched.get('t1_t2', 0):,} · T2 {sched.get('t2', 0):,} · T3 {sched.get('t3', 0):,} · "
              f"T4 {sched.get('t4', 0):,} · weekly {sched.get('weekly', 0):,} · T8 {sched.get('t8', 0):,}")
    counts = scan.get("tier_counts") or {}
    if counts:
        tiers = " · ".join(f"T{t}: {n:,}" for t, n in sorted(counts.items()))
        print(f"tier distribution : {tiers}")
    if scan.get("blowup_watch_count"):
        print(f"🚀 BLOW-UP WATCH  : {scan['blowup_watch_count']} game(s) flagged — New and Upcoming")
    metrics = diag.get("metrics") or {}
    print(f"metrics batches   : {metrics.get('successful_batches', 0)}/{metrics.get('batches', 0)} OK "
          f"· breaker: {metrics.get('breaker_tripped', False)}")
    kw = diag.get("keyword_crawl") or {}
    print(f"keyword crawler   : {kw.get('successful', kw.get('successful_keywords', 0))}/"
          f"{kw.get('keywords', 0)} OK · breaker: {kw.get('breaker_tripped', False)}")
    if kw.get("search_depth", 1) > 1 or kw.get("page2_new") is not None:
        print(f"keyword pages     : depth {kw.get('search_depth', 1)} · {kw.get('pages_fetched', 0)} pages "
              f"fetched · +{kw.get('page2_new', 0)} new games from page 2+")
    pool = kw.get("pool") or []
    if pool:
        benched = kw.get("benched") or []
        pool_desc = " -> ".join(
            "direct" if entry == "direct" else entry.split("//")[-1].split("/")[0]
            for entry in pool
        )
        if benched:
            pool_desc += f"  (benched: {', '.join(benched)})"
        print(f"search IP pool    : {pool_desc}")
    disc = diag.get("discovery") or {}
    print(f"discovery         : HTTP {disc.get('status', '?')} · {disc.get('records', 0)} games · "
          f"{disc.get('sorts', 0)} sorts across {disc.get('pages', 0)} chart page(s)")

    # Catalog-expansion pilot summary (EXPANSION_PILOT.md) — only on expander
    # runs, where scan['expansion'] is populated.
    expansion = scan.get("expansion") or {}
    if expansion:
        atlas = expansion.get("atlas") or {}
        web = expansion.get("spiderweb") or {}
        recs = expansion.get("rec_mining") or {}
        drain = expansion.get("drain") or {}
        frontier = expansion.get("frontier") or {}
        print("\n" + "-" * 62)
        print("EXPANSION PILOT (strict gate: 20k visits / 25 CCU)")
        print("-" * 62)
        print(f"atlas dev         : {atlas.get('pages_fetched', 0)} pages · "
              f"{atlas.get('fetched_ids', 0):,} IDs · {atlas.get('enqueued', 0):,} NEW enqueued · "
              f"{atlas.get('provisional_rows', 0)} first-paint rows"
              + ("  · THROTTLED (next window)" if atlas.get("throttled") else "")
              + ("  · ABORTED (retry next run)" if atlas.get("aborted") else ""))
        print(f"spiderweb         : {web.get('crawled', 0)} crawled · {web.get('empty', 0)} empty · "
              f"{web.get('failed', 0)} failed (retried next run)")
        print(f"  games found     : {web.get('games_found', 0):,} · pre-gate pass: "
              f"{web.get('pregate_passed', 0):,} · enqueued: {web.get('enqueued', 0):,}")
        print(f"rec mining        : {recs.get('seeds', 0)} seeds · {recs.get('failed', 0)} failed · "
              f"{recs.get('recs_seen', 0):,} recs · {recs.get('known', 0):,} known/skipped · "
              f"{recs.get('enqueued', 0)} NEW enqueued")
        print(f"queue drain       : {drain.get('claimed', 0)} claimed · {drain.get('duplicates', 0)} known · "
              f"{drain.get('qualified', 0)} QUALIFIED · {drain.get('below_gate', 0)} below gate · "
              f"{drain.get('metrics_failed', 0)} failed")
        if frontier.get("start_id"):
            print(f"frontier scan     : {frontier.get('start_id', 0):,} → {frontier.get('end_id', 0):,} · "
                  f"{frontier.get('scanned', 0)} fresh IDs · {frontier.get('qualified', 0)} QUALIFIED · "
                  f"{frontier.get('below_gate', 0)} below gate")
        import sqlite3
        with sqlite3.connect(DB_PATH) as conn:
            q = lambda s: conn.execute(s).fetchone()[0]
            total_rows = q("SELECT COUNT(*) FROM game_analytics")
            qualified_rows = q(
                "SELECT COUNT(*) FROM game_analytics WHERE visits>=20000 AND ccu>=25")
            queue_pending = q(
                "SELECT COUNT(*) FROM discovery_queue WHERE status='pending'")
            queue_total = q("SELECT COUNT(*) FROM discovery_queue")
            logged_creators = q("SELECT COUNT(*) FROM creator_spiderweb_log")
        print(f"catalog           : {total_rows:,} rows · {qualified_rows:,} qualified")
        print(f"queue depth       : {queue_pending:,} pending · {queue_total:,} total")
        print(f"spiderweb log     : {logged_creators:,} creators logged")

    # 429 sanity: counts come from the diagnostics the engine records.
    total_batches = metrics.get("batches", 0) or 0
    failed = metrics.get("failed_batches", 0) or 0
    rate = (failed / total_batches * 100) if total_batches else 0.0
    print(f"failure rate      : {rate:.1f}% of metric batches ({failed}/{total_batches})"
          + ("  — above the 2% escalation line, watch next sync" if rate > 2 else ""))
    # Expansion pilot: record this run's telemetry, then — on expander runs
    # only — print the observe-only KEEP/BORDERLINE/REVERT verdict block.
    try:
        expansion_pilot.record_run(
            DB_PATH, mode, int(scan.get("run_id") or 0), scan, diag,
            tier_schedule=scan.get("tier_schedule") or {},
        )
    except Exception as exc:  # telemetry must never fail a sync
        print(f"[pilot] record skipped: {exc}")
    if mode == "expander":
        print(expansion_pilot.report(DB_PATH))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one live sync of the scouting pipeline.")
    parser.add_argument("--min-visits", type=int, default=20_000)
    parser.add_argument("--min-ccu", type=int, default=25)
    parser.add_argument(
        "--only",
        choices=("finder", "hydrator", "expander"),
        default=None,
        help="finder = discover new games only; hydrator = refresh known games "
        "only; expander = catalog-expansion pilot; omit for the full pipeline.",
    )
    args = parser.parse_args()
    phases = {
        "finder": ("find",),
        "hydrator": ("hydrate",),
        "expander": ("expand",),
        None: None,
    }[args.only]
    mode = args.only or "full"

    print(f"db: {DB_PATH}")
    print(f"mode: {mode}")
    before = snapshot()
    # Optional credential via environment (RBXSCOUT_COOKIE). The .ROBLOSECURITY
    # cookie is scoped to *.roblox.com only and is never sent to third parties;
    # leave unset for anonymous scans (public endpoints all work without it).
    scout = RobloxPlatformScout(
        db_path=DB_PATH,
        roblox_cookie=os.environ.get("RBXSCOUT_COOKIE") or None,
    )
    if args.only != "hydrator":
        # Keyword-cursor maintenance only matters when the crawler runs.
        reset_keyword_cursor_if_stale(scout)

    started = time.time()

    def progress(pct: float, msg: str) -> None:
        print(f"  [{pct * 3:5.1f}%] {msg}")

    try:
        scout.scan(
            min_visits=args.min_visits,
            min_ccu=args.min_ccu,
            deep_contacts=False,          # contacts stay lazy, per page, in the UI
            progress_cb=progress,
            phases=phases,
        )
    except Exception as exc:
        print(f"\nSYNC CRASHED: {exc}")
        return summarize(scout, before, time.time() - started, mode) or 1
    return summarize(scout, before, time.time() - started, mode)

if __name__ == "__main__":
    raise SystemExit(main())
