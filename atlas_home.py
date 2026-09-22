#!/usr/bin/env python3
"""RbxScout home-IP Atlas harvester — the daily discovery run from this laptop.

Why this exists: atlasdev.gg 403s every request from GitHub Actions (datacenter
IPs) AND from the Cloudflare Worker relay (Cloudflare-to-Cloudflare), but works
from this laptop's residential IP (verified 2026-09-21). Actions owns the
always-on work — hydration, tier refresh, catalog pushes — while THIS machine
is the only thing that can discover new games. It runs once a day from launchd
(com.plist/atlas-home-harvest.plist), well inside Actions' daily runner quota.

Contract with the 24/7 pipeline:
  1. PULL the release catalog (the laptop copy may lag Actions by hours).
  2. VERIFY discovery is Atlas-Dev-only (no legacy finders may ever return).
  3. HARVEST via scout_core.harvest_atlas_seeds — discovery ONLY:
     EXPAND_QUEUE_BATCHES=0 makes the queue drain a no-op (new IDs are
     ENQUEUED as pending rows, never drained here; hydration stays on the
     always-on Actions hydrator).
  4. MERGE into the pulled catalog on push races — union-by-primary-key,
     never clobber: a whole-file push of a stale copy would delete every game
     Actions discovered since the pull (db_sync's counter guard refuses that).
  5. PUSH the catalog; Actions' next expander/hydrator drains + hydrates.

No sign-in, no tunnel, no new accounts: db_sync.py reuses this machine's git
credential store (macOS Keychain) for the release assets.

Usage:
    .venv/bin/python atlas_home.py            # the scheduled daily run
    .venv/bin/python atlas_home.py --force    # ignore the 24h Atlas throttle
"""

from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

LOG_DIR = APP_DIR / "logs"
LOG_FILE = LOG_DIR / "atlas_home.log"

# Discovery ONLY: the queue drain must stay a no-op on this machine —
# hydration and gate evaluation belong to the always-on Actions expander.
EXPAND_QUEUE_BATCHES = "0"

PY = str(APP_DIR / ".venv" / "bin" / "python")

# Union-by-primary-key, local rows win. Run-scoped bookkeeping tables
# (scan_runs, sync_health_log, contact_diagnostics) and legacy leftovers are
# deliberately NOT merged.
MERGE_TABLES = (
    "game_analytics",
    "ccu_history",
    "discovery_queue",
    "place_map",
    "scan_pointers",
)

# Insert-or-ignore alone cannot carry scan_pointers: their rows already exist
# on the store (the Atlas cursor has been written for weeks), so a merge would
# keep Actions' stale copy. The laptop is the SOLE writer of the Atlas
# pointers — Actions' harvest_atlas_seeds is throttled off — so after the
# insert pass these columns are upserted from the harvest copy. ccu_history is
# keyed (universe_id, snapshot_at); the laptop writes no snapshots, so the
# insert-or-ignore pass already carries every row it has.
UPSERT_MERGE_COLUMNS = {
    "scan_pointers": ("last_universe_id",),
}

# Thumbnails + votes for games that reached the catalog WITHOUT them —
# provisional Atlas rows and hydrated-but-icon-less rows (the hydrator only
# backfilled icons for "matched" games, which newer targets left behind).
# Runs at home-IP after every harvest so Actions stays free of thumbnail
# traffic; merge-safe because these UPDATEs only FILL NULLs on rows the
# laptop did not create (unlike game_analytics, they are safe to union).
ICON_BACKFILL_BATCH = 200          # rows per harvest run
ICON_BACKFILL_HOURS = 24           # minimum gap between backfill sweeps


def log(msg: str) -> None:
    """Append one timestamped line to the rolling log + stdout."""
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # a broken log file must never kill a harvest


def verify_sole_engine() -> None:
    """Discovery must be Atlas-Dev-only — fail loudly if any legacy finder
    (deep charts, keyword crawler, spiderweb, frontier, recommendations)
    reappears in scout_core as a scan phase."""
    import scout_core

    core_src = (APP_DIR / "scout_core.py").read_text(encoding="utf-8")
    legacy = [
        name
        for name in (
            # the finders deleted by commit f0880a7 (2026-09-20)
            "fetch_discovery_games",
            "fetch_search_games",
            "fetch_rolimons_games",
            "import_rolimons_catalog",
            "fetch_trending_universe_ids",
            "_search_pool_request",
        )
        if f"def {name}" in core_src
    ]
    if legacy:
        raise SystemExit(
            f"DISCOVERY CONTRACT BROKEN: legacy finder(s) present: {legacy}. "
            "Atlas Dev is the sole discovery engine (removed 2026-09-20)."
        )
    if not hasattr(scout_core.RobloxPlatformScout, "harvest_atlas_seeds"):
        raise SystemExit("DISCOVERY CONTRACT BROKEN: harvest_atlas_seeds missing.")


def catalog_stats(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        games = conn.execute("SELECT COUNT(*) FROM game_analytics").fetchone()[0]
        atlas_pending = conn.execute(
            "SELECT COUNT(*) FROM discovery_queue "
            "WHERE status='pending' AND source='atlas_dev'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"games": games, "atlas_pending": atlas_pending}


def merge_harvest_into(target: Path, harvest: Path) -> dict:
    """ATTACH the post-harvest DB and union it into ``target``.

    Rows insert only when their primary key is absent in the target; existing
    rows are never overwritten. New seeds arrive 'pending' (the laptop never
    drains), so Actions evaluates exactly the new IDs; rows Actions already
    evaluated keep their 'processed' outcome — dedup memory is preserved.
    """
    stats = {t: 0 for t in MERGE_TABLES}
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("ATTACH DATABASE ? AS hv", (str(harvest),))
        for table in MERGE_TABLES:
            cols = [
                r[1] for r in conn.execute(f"PRAGMA hv.table_info({table})").fetchall()
            ]
            if not cols:
                log(f"merge: table {table} missing in harvest copy — skipped")
                continue
            collist = ", ".join(cols)
            conn.execute(
                f"INSERT OR IGNORE INTO main.{table} ({collist}) "
                f"SELECT {collist} FROM hv.{table}"
            )
            stats[table] = conn.execute("SELECT changes()").fetchone()[0]
            for col in UPSERT_MERGE_COLUMNS.get(table, ()):
                if col not in cols:
                    continue
                conn.execute(
                    f"UPDATE main.{table} SET {col} = "
                    f"(SELECT hv.{table}.{col} FROM hv.{table} "
                    f"WHERE hv.{table}.id = main.{table}.id) "
                    f"WHERE id IN (SELECT id FROM hv.{table})"
                )
        conn.commit()
        conn.execute("DETACH DATABASE hv")
    finally:
        conn.close()
    return stats


def _hours_since_backfill(conn) -> float:
    try:
        row = conn.execute(
            "SELECT updated_at FROM scan_pointers WHERE id = 'home_icon_backfill_at'"
        ).fetchone()
        stamp = str(row[0]) if row else ""
        # CURRENT_TIMESTAMP is UTC; compare it against a UTC clock, not the
        # laptop's local time (a CEST machine would read a fresh stamp as
        # 2h old and run the sweep 2h early every cycle).
        last = datetime.datetime.fromisoformat(stamp[:19])
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        return (now_utc - last).total_seconds() / 3600.0
    except (sqlite3.Error, ValueError, TypeError):
        return float("inf")


def backfill_thumbnails(local: Path) -> int:
    """Fetch missing game icons (and vote totals for the Rating column).

    Atlas seeds enter the catalog via provisional rows and the queue drain,
    neither of which carried thumbnails — the dashboard showed the placeholder
    tile for every such game. Fills up to ICON_BACKFILL_BATCH icon-less rows
    per run (oldest first) using the same cookieless thumbnail + votes
    endpoints the pipeline uses, then stamps the throttle pointer.
    Never raises: a failed sweep only postpones thumbnails by a day.
    """
    import scout_core

    try:
        scout = scout_core.RobloxPlatformScout(db_path=str(local))
        with sqlite3.connect(str(local)) as conn:
            doomed = [int(r[0]) for r in conn.execute(
                "SELECT universe_id FROM game_analytics "
                "WHERE (icon_url IS NULL OR icon_url = '') "
                "ORDER BY universe_id LIMIT ?",
                (ICON_BACKFILL_BATCH,),
            ).fetchall()]
        if not doomed:
            return 0
        icons = scout.fetch_game_icons(doomed)
        votes = scout.fetch_vote_totals(doomed)
        scout.upsert_icons(icons)
        scout.upsert_votes(votes)
        with sqlite3.connect(str(local)) as conn:
            conn.execute(
                "INSERT INTO scan_pointers (id, last_universe_id, updated_at) "
                "VALUES ('home_icon_backfill_at', 0, CURRENT_TIMESTAMP) "
                "ON CONFLICT(id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP"
            )
            conn.commit()
        log(
            f"icon backfill: {len(icons)}/{len(doomed)} thumbnails + "
            f"{len(votes)} vote rows fetched"
        )
        return len(icons)
    except Exception as exc:
        log(f"icon backfill skipped: {exc}")
        return 0


def db_sync_out(*args: str) -> tuple[int, str]:
    """Run db_sync.py; returns (returncode, combined output). Never raises."""
    res = subprocess.run(
        [PY, str(APP_DIR / "db_sync.py"), *args],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    return res.returncode, (res.stdout + res.stderr).strip()


def db_sync(*args: str) -> None:
    rc, out = db_sync_out(*args)
    if out:
        log("  " + out.replace("\n", "\n  "))
    if rc != 0:
        raise SystemExit(f"db_sync.py {' '.join(args)} failed (rc={rc})")


def push_with_merge_retry(harvest_copy: Path) -> None:
    """Push; if the store moved ahead mid-run, re-pull, merge the harvest in,
    retry. The merge can only ADD rows, so a retry can never lose data and
    the push guard can no longer fire."""
    for attempt in range(1, 4):
        rc, out = db_sync_out("push")
        if out:
            log("  " + out.replace("\n", "\n  "))
        if rc == 0:
            return
        if "refusing to push" in out and attempt < 3:
            log(f"push raced with Actions (attempt {attempt}) — re-pulling + re-merging…")
            db_sync("pull")
            merged = merge_harvest_into(APP_DIR / "rbx_scout.db", harvest_copy)
            log(f"re-merge: {merged}")
            continue
        raise SystemExit(f"db_sync.py push failed (rc={rc})")
    raise SystemExit("push failed after 3 merge-retries")


def main() -> int:
    parser = argparse.ArgumentParser(description="Home-IP Atlas Dev harvest (discovery only).")
    parser.add_argument("--force", action="store_true",
                        help="ignore the 24h Atlas self-throttle (throttle_hours=0)")
    args = parser.parse_args()

    log("=" * 62)
    log("ATLAS HOME HARVEST — start")
    started = time.time()

    verify_sole_engine()
    log("engines: Atlas-Dev-only verified (no legacy finders)")

    # -- 1. pull the release catalog --------------------------------------
    db_sync("pull")
    local = APP_DIR / "rbx_scout.db"
    before = catalog_stats(local)
    log(f"pulled catalog: {before['games']:,} games · {before['atlas_pending']:,} atlas seeds pending")

    # -- 2. harvest (discovery ONLY; drain disabled) -----------------------
    os.environ["EXPAND_QUEUE_BATCHES"] = EXPAND_QUEUE_BATCHES

    import scout_core

    scout = scout_core.RobloxPlatformScout(db_path=str(local))
    log("harvesting Atlas Dev index…")
    stats = scout.harvest_atlas_seeds(
        throttle_hours=(0 if args.force else None),
        progress_cb=lambda p, m: log(f"  [{p * 100:4.1f}%] {m}"),
    )
    log(
        "harvest: "
        f"{stats.get('pages_fetched', 0)} pages · {stats.get('fetched_ids', 0):,} IDs · "
        f"{stats.get('enqueued', 0):,} NEW enqueued · {stats.get('provisional_rows', 0)} first-paint rows"
        + ("  · THROTTLED" if stats.get("throttled") else "")
        + ("  · ABORTED" if stats.get("aborted") else "")
        + f"  ({time.time() - started:.0f}s)"
    )
    if stats.get("throttled"):
        log("24h throttle active — nothing to do today (this is normal).")
        return 0
    if stats.get("fetched_ids", 0) == 0:
        log("harvest produced nothing — NOT pushing (store unchanged).")
        return 2
    if stats.get("aborted"):
        # Partial sweep (e.g. a DNS blip mid-run): the IDs fetched so far are
        # valid candidates (dedup is by ID, the cursor marks the resume
        # point) — push them; the next run resumes the remaining pages.
        log("partial sweep — pushing what was harvested; next run resumes")

    after = catalog_stats(local)
    log(f"catalog now: {after['games']:,} games · {after['atlas_pending']:,} atlas seeds PENDING → Actions")

    # -- 3. backfill thumbnails for icon-less rows (24h-throttled) ---------
    # Home IP only: atlasdev.gg 403s runner IPs, and the thumbnail/vote
    # endpoints follow the same pattern. Actions never pays this traffic.
    with sqlite3.connect(str(local)) as conn:
        backfill_due = _hours_since_backfill(conn) >= ICON_BACKFILL_HOURS
    if backfill_due:
        backfill_thumbnails(local)
    else:
        log("icon backfill throttled (next sweep in ~24h)")

    # -- 4. snapshot the post-harvest DB (used only if a push races) -------
    harvest_copy = local.with_suffix(".db.harvest")
    harvest_copy.write_bytes(local.read_bytes())

    # -- 4. push (merge-retry against Actions races) -----------------------
    push_with_merge_retry(harvest_copy)
    harvest_copy.unlink(missing_ok=True)
    log(f"DONE in {time.time() - started:.0f}s — Actions will drain + hydrate the new seeds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
