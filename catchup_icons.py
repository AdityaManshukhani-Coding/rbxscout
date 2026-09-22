#!/usr/bin/env python3
"""One-off thumbnail catch-up — fill every icon-less catalog row from home IP.

Reuses the exact pipeline mechanics (scout_core.fetch_game_icons /
fetch_vote_totals / upsert_icons / upsert_votes — the same cookieless
endpoints the daily 200-row sweep in atlas_home.backfill_thumbnails uses),
just without the per-run cap, to clear the initial 10k-row backlog in one
sitting (~4 batched calls per 100 rows). Run manually, then push the catalog
with db_sync.py so the hosted dashboard picks the thumbnails up.
"""

import sqlite3
import sys
import time
from pathlib import Path

import scout_core

DB = Path(__file__).resolve().parent / "rbx_scout.db"
GROUP = 2500           # rows per group = 50 batched calls of 50
MAX_GROUPS = 20        # hard stop (~50k rows)
EMPTY_STREAK_STOP = 2  # consecutive groups with zero fetched icons -> abort


def remaining(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM game_analytics "
        "WHERE (icon_url IS NULL OR icon_url = '')"
    ).fetchone()[0]


def main() -> int:
    print(f"rows missing icons at start: {remaining(sqlite3.connect(str(DB)))}")
    scout = scout_core.RobloxPlatformScout(db_path=str(DB))
    total_filled = 0
    empty_streak = 0
    for g in range(MAX_GROUPS):
        with sqlite3.connect(str(DB)) as conn:
            doomed = [
                int(r[0]) for r in conn.execute(
                    "SELECT universe_id FROM game_analytics "
                    "WHERE (icon_url IS NULL OR icon_url = '') "
                    "ORDER BY universe_id LIMIT ?",
                    (GROUP,),
                ).fetchall()
            ]
        if not doomed:
            print("nothing left — catalog fully thumbnailed")
            break
        t0 = time.time()
        icons = scout.fetch_game_icons(doomed)
        votes = scout.fetch_vote_totals(doomed)
        scout.upsert_icons(icons)
        scout.upsert_votes(votes)
        total_filled += len(icons)
        print(
            f"group {g}: {len(doomed)} rows -> {len(icons)} icons, "
            f"{len(votes)} vote rows ({time.time() - t0:.0f}s)",
            flush=True,
        )
        if not icons:
            empty_streak += 1
            if empty_streak >= EMPTY_STREAK_STOP:
                print("endpoint returned nothing twice in a row — stopping")
                break
        else:
            empty_streak = 0
        time.sleep(1.0)
    left = remaining(sqlite3.connect(str(DB)))
    print(f"done: filled {total_filled} this run; rows still icon-less: {left}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
