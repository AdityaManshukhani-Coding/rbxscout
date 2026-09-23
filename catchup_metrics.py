#!/usr/bin/env python3
"""One-off live-metrics catch-up — light up Avg CCU (1d/3d), Momentum, Rating.

Why this exists (2026-09-23): the 24/7 Actions hydrator stopped writing on
2026-09-21 (the store stalled at sync #4293 — no ccu_history row is newer
than 09-21), so every trend column on the dashboard had no fresh samples and
showed "-". On top of that, 11.3k of the 12.4k games that meet the default
20k-visits/25-CCU target carry no up/down votes at all (the hydrator's
upserts never include them and the daily backfill caps at 200 rows), so the
Rating column was almost entirely "-".

Runs from the HOME machine, exactly like atlas_home.py / catchup_icons.py:
the games.roblox.com metrics + votes endpoints are cookieless and work from
the residential IP. Batched 50 universes per call, paced by scout_core's
shared token bucket — ~12.4k qualified games ≈ ~480 requests ≈ 10-15 min.

Pipeline contract (mirrors atlas_home.py):
  1. PULL the release catalog (the laptop copy may lag Actions).
  2. APPLY: fetch_game_metrics + fetch_vote_totals → upsert_metrics_only /
     upsert_votes. Metric-only upserts deliberately do NOT bump
     last_updated, so tier-due hydration schedules stay untouched.
  3. PUSH with the same merge-retry loop atlas_home uses: on a push race,
     pull and re-apply (the upserts are idempotent — a repeat simply adds
     another ccu_history sample) and push again.

Usage:
    .venv/bin/python catchup_metrics.py            # full qualified catalog
    .venv/bin/python catchup_metrics.py --limit 2500
    .venv/bin/python catchup_metrics.py --no-push  # local only
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import scout_core  # noqa: E402

DB = APP_DIR / "rbx_scout.db"
PY = str(APP_DIR / ".venv" / "bin" / "python")

GROUP = 2500           # rows per group = 50 batched calls of 50
EMPTY_STREAK_STOP = 2  # consecutive groups with zero fetched metrics -> abort


def log(msg: str) -> None:
    print(msg, flush=True)


def db_sync_out(*args: str) -> tuple[int, str]:
    """Run db_sync.py; returns (returncode, combined output). Never raises."""
    res = subprocess.run(
        [PY, str(APP_DIR / "db_sync.py"), *args],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    return res.returncode, (res.stdout + res.stderr).strip()


def qualified_missing(conn: sqlite3.Connection, limit: int) -> list[int]:
    """Universe IDs of games meeting the dashboard's entry bar (20k visits /
    25 CCU), ordered so the LEAST-covered games come first: missing Rating
    votes, then fewest ccu_history samples in the last 2 days (avg CCU 1d
    needs ≥2 samples in 24h, avg CCU 3d needs ≥2 in 72h), then by id."""
    rows = conn.execute(
        "SELECT universe_id FROM game_analytics "
        "WHERE COALESCE(visits, 0) >= 20000 AND COALESCE(ccu, 0) >= 25 "
        "ORDER BY (upvotes IS NULL OR downvotes IS NULL) DESC, "
        "(SELECT COUNT(*) FROM ccu_history h WHERE h.universe_id = game_analytics.universe_id "
        " AND h.ts >= datetime('now', '-2 days')) ASC, universe_id "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def apply_group(scout: scout_core.RobloxPlatformScout, ids: list[int]) -> tuple[int, int]:
    metrics = scout.fetch_game_metrics(ids)
    votes = scout.fetch_vote_totals(ids)
    updated = scout.upsert_metrics_only(metrics)
    scout.upsert_votes(votes)
    return updated, len(votes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill avg-CCU/momentum/rating inputs for the qualified catalog.")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap how many qualified games to refresh (0 = all)")
    parser.add_argument("--no-push", action="store_true",
                        help="keep the filled catalog local (no release push)")
    parser.add_argument("--no-pull", action="store_true",
                        help="skip the pre-pull: the local copy is already "
                        "fresher than the store (unpushed fills would be "
                        "discarded by a pull, since the fills only exist "
                        "locally until the push)")
    args = parser.parse_args()

    log("=" * 62)
    log("METRICS CATCH-UP — start")
    started = time.time()

    if not args.no_push and not args.no_pull:
        rc, out = db_sync_out("pull")
        if out:
            log("  " + out.replace("\n", "\n  "))
        if rc != 0:
            log("pull failed — continuing with the local copy (it may be stale)")

    with sqlite3.connect(str(DB)) as conn:
        ids = qualified_missing(conn, args.limit or 10**9)
    log(f"qualified games to refresh: {len(ids):,}")

    scout = scout_core.RobloxPlatformScout(db_path=str(DB))
    total_updated = 0
    total_votes = 0
    empty_streak = 0
    for start in range(0, len(ids), GROUP):
        chunk = ids[start:start + GROUP]
        t0 = time.time()
        try:
            updated, votes = apply_group(scout, chunk)
        except Exception as exc:
            log(f"group at {start}: FAILED ({exc}) — continuing with next group")
            empty_streak += 1
            if empty_streak >= EMPTY_STREAK_STOP:
                log("two consecutive failed groups — stopping early")
                break
            continue
        empty_streak = 0
        total_updated += updated
        total_votes += votes
        log(
            f"group {start // GROUP}: {len(chunk)} rows -> {updated} metric rows, "
            f"{votes} vote rows ({time.time() - t0:.0f}s)"
        )
        time.sleep(1.0)

    log(
        f"done applying: {total_updated:,} metric rows + {total_votes:,} vote rows "
        f"in {time.time() - started:.0f}s"
    )
    if args.no_push:
        log("--no-push: catalog stays local")
        return 0

    for attempt in range(1, 4):
        rc, out = db_sync_out("push")
        if out:
            log("  " + out.replace("\n", "\n  "))
        if rc == 0:
            log(f"DONE in {time.time() - started:.0f}s — dashboard trend columns fill as users reload")
            return 0
        if "refusing to push" in out and attempt < 3:
            log(f"push raced with Actions (attempt {attempt}) — re-pulling + re-applying…")
            db_sync_out("pull")
            with sqlite3.connect(str(DB)) as conn:
                ids = qualified_missing(conn, 10**9)
            for start in range(0, len(ids), GROUP):
                try:
                    apply_group(scout, ids[start:start + GROUP])
                except Exception as exc:
                    log(f"re-apply group at {start} failed: {exc}")
            continue
        log("push failed — data is safe locally; re-run without --no-push later")
        return 1
    log("push failed after 3 merge-retries — data is safe locally")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
