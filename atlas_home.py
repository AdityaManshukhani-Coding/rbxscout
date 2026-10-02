#!/usr/bin/env python3
"""UpScale Scouting Tool home-IP Atlas harvester — the daily discovery run from this laptop.

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

# Single-instance guard (incident 2026-09-27): a transient duplicate burst at
# 19:43 left several atlas_home processes alive at once and their nightly
# contact sweeps raced on rbx_scout.db ("unable to open database file"),
# killing the sweep 20 minutes later. flock() is auto-released by the OS if
# the holder dies — no stale-lock cleanup is ever needed.
_INSTANCE_LOCK = APP_DIR / "logs" / "atlas_home_instance.lock"
_INSTANCE_FH = None  # module ref keeps the fd (and the lock) alive for life


def _acquire_instance_lock() -> bool:
    """True if this process is the sole atlas_home instance (lock held)."""
    import fcntl

    global _INSTANCE_FH
    _INSTANCE_LOCK.parent.mkdir(exist_ok=True)
    _INSTANCE_FH = _INSTANCE_LOCK.open("w")
    try:
        fcntl.flock(_INSTANCE_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _INSTANCE_FH.close()
        _INSTANCE_FH = None
        return False
    return True

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
    "contact_archive",
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

# Nightly Discord contact sweep for the frontier (home IP + .ROBLOSECURITY
# cookie from RBXSCOUT_COOKIE — the /social-links/list endpoint 401s
# anonymously). Bounded: today's Atlas discoveries first, then gate-qualified
# unchecked stragglers, capped so the nightly run never turns into the
# one-time backfill (that job belongs to catchup_contacts.py).
CONTACT_SWEEP_BUDGET = 200         # games per nightly run
CONTACT_SWEEP_HOURS = 24           # self-throttle, mirroring the icon backfill


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


def contact_sweep(local: Path, stats: dict) -> int:
    """Bounded nightly Discord contact pass over the discovery frontier.

    Population: games harvested TODAY (they are the freshest gate-passers and
    the store push at the end of this run carries their verdicts to the
    dashboard for free), then the oldest still-unchecked gate-qualified rows
    (yesterday's stragglers). Hard budget cap; home IP + cookie only; every
    verdict goes through resolve_game_contact, so the 6h cache, the throttle
    window and the archive write-throughs all apply unchanged. Never raises:
    a failed sweep only postpones contacts by a day.
    """
    import os
    import scout_core

    try:
        # Two accounts = two request lanes = ~2x nightly sweep speed (same
        # shape as the one-time backfill's primary/shard split). The pool is
        # interleaved so the lanes never touch the same game; verdicts go
        # through the same resolver, 6h cache and archive write-throughs.
        cookies = [c.strip() for c in
                   (os.environ.get("RBXSCOUT_COOKIE") or "").split("||") if c.strip()]
        if not cookies:
            for name in ("local_cookie.txt", "local_cookie2.txt"):
                cookie_file = APP_DIR / name
                if cookie_file.exists():
                    candidate = cookie_file.read_text(encoding="utf-8").strip()
                    if candidate and candidate not in cookies:
                        cookies.append(candidate)
        if not cookies:
            log("contact sweep skipped: no RBXSCOUT_COOKIE / local_cookie.txt / "
                "local_cookie2.txt (game social-links need .ROBLOSECURITY)")
            return 0
        with sqlite3.connect(str(local)) as conn:
            already = int((conn.execute(
                "SELECT last_universe_id FROM scan_pointers WHERE id = 'home_contact_sweep_at'"
            ).fetchone() or (0,))[0])
            if already:
                log("contact sweep throttled (already ran today)")
                return 0
            discovered_today = [int(r[0]) for r in conn.execute(
                "SELECT universe_id FROM discovery_queue "
                "WHERE source='atlas_dev' AND seen_at >= datetime('now', '-1 day') "
                "ORDER BY seen_at DESC LIMIT ?",
                (CONTACT_SWEEP_BUDGET,),
            ).fetchall()]
            budget_left = max(0, CONTACT_SWEEP_BUDGET - len(discovered_today))
            stragglers = []
            if budget_left:
                stragglers = [int(r[0]) for r in conn.execute(
                    "SELECT universe_id FROM game_analytics "
                    "WHERE contacts_checked_at IS NULL "
                    "AND COALESCE(visits, 0) >= 20000 AND COALESCE(ccu, 0) >= 25 "
                    "ORDER BY first_seen ASC, universe_id ASC LIMIT ?",
                    (budget_left,),
                ).fetchall()]
            pool = list(dict.fromkeys(discovered_today + stragglers))[:CONTACT_SWEEP_BUDGET]
        if not pool:
            log("contact sweep: nothing unchecked in the frontier — skipping")
            return 0
        lanes = min(len(cookies), len(pool))
        if lanes <= 1:
            scout = scout_core.RobloxPlatformScout(db_path=str(local))
            scout.set_cookie(cookies[0])
            result = scout.scan_contacts(pool)
            hits = int((result.get("has_discord") == True).sum()) if result is not None and not result.empty else 0  # noqa: E712
        else:
            import concurrent.futures

            # Interleave the ordered pool (frontier-first preserved per lane);
            # disjoint halves, so no game is requested twice.
            lane_pools = [pool[i::lanes] for i in range(lanes)]

            def _lane(cookie: str, ids: list) -> int:
                scout = scout_core.RobloxPlatformScout(db_path=str(local))
                scout.set_cookie(cookie)
                result = scout.scan_contacts(ids)
                if result is None or result.empty:
                    return 0
                return int((result.get("has_discord") == True).sum())  # noqa: E712

            with concurrent.futures.ThreadPoolExecutor(max_workers=lanes) as lane_x:
                hits = int(sum(lane_x.map(lambda pair: _lane(*pair),
                                          zip(cookies, lane_pools))))
        with sqlite3.connect(str(local)) as conn:
            conn.execute(
                "INSERT INTO scan_pointers (id, last_universe_id, updated_at) "
                "VALUES ('home_contact_sweep_at', 0, CURRENT_TIMESTAMP) "
                "ON CONFLICT(id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP"
            )
            conn.commit()
        stats["contacts_checked"] = len(pool)
        stats["contacts_hits"] = hits
        log(f"contact sweep: {len(pool)} games checked · {hits} with Discord")
        return len(pool)
    except Exception as exc:
        log(f"contact sweep skipped: {exc}")
        return 0


def scout_core_guard() -> bool:
    """True while the contact backfill is running (holds its lock file)."""
    import scout_core

    return scout_core.ContactBackfillLock.is_busy()


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
    import scout_core

    # Five attempts, not three: on a flaky network evening (VPN re-key,
    # DNS blips, GitHub dropping upload response bodies) three rounds of
    # pull-merge-upload can exhaust before one lands cleanly.
    for attempt in range(1, 6):
        # The backfill holds its lock for the whole run; if it started since
        # our pull, do NOT pull (file replace under its worker) — just retry
        # the push: the counter guard refuses stale pushes anyway.
        if scout_core.ContactBackfillLock.is_busy():
            log("contact backfill is RUNNING — skipping push-race re-pull "
                "(a pull would replace the DB under the worker); retrying push")
            time.sleep(120)
            continue
        rc, out = db_sync_out("push")
        if out:
            log("  " + out.replace("\n", "\n  "))
        if rc == 0:
            return
        if "refusing to push" in out and attempt < 5:
            log(f"push raced with Actions (attempt {attempt}) — re-pulling + re-merging…")
            db_sync("pull")
            merged = merge_harvest_into(APP_DIR / "rbx_scout.db", harvest_copy)
            log(f"re-merge: {merged}")
            continue
        # A failed upload verification (flaky network dropping the response
        # body mid-transfer — twice in one evening on 2026-10-02) is a
        # TRANSIENT failure: the incoming blob is cleaned up by the next
        # attempt and the store is never left worse than before it, so
        # retry instead of aborting the whole run.
        if "upload verification failed" in out and attempt < 5:
            log(f"upload verification failed (attempt {attempt}) — retrying push…")
            time.sleep(30)
            continue
        raise SystemExit(f"db_sync.py push failed (rc={rc})")
    raise SystemExit("push failed after 3 merge-retries")


def main() -> int:
    parser = argparse.ArgumentParser(description="Home-IP Atlas Dev harvest (discovery only).")
    parser.add_argument("--force", action="store_true",
                        help="ignore the 24h Atlas self-throttle (throttle_hours=0)")
    args = parser.parse_args()

    if not _acquire_instance_lock():
        log("another atlas_home instance is already running — exiting "
            "(single-instance guard)")
        return 0

    log("=" * 62)
    log("ATLAS HOME HARVEST — start")
    started = time.time()

    verify_sole_engine()
    log("engines: Atlas-Dev-only verified (no legacy finders)")

    # -- 1. pull the release catalog --------------------------------------
    # Coordination (incident 2026-09-24): the backfill HOLDS a lock while it
    # runs; its end-of-run push carries our writes anyway (one shared local
    # DB), so skip the whole store cycle rather than replace the file under
    # its worker. Everything else proceeds normally against the local DB.
    if scout_core_guard():
        log("CONTACT BACKFILL RUNNING — skipping pull; working on the local "
            "catalog (the backfill's end-of-run push will carry our results)")
    else:
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

    # -- 3b. nightly Discord contact sweep for the frontier (bounded) ------
    # Home IP + cookie only. Runs BEFORE the push: verdicts on today's
    # discoveries ride the end-of-run store push to the dashboard for free.
    contact_stats: dict = {}
    if scout_core_guard():
        log("contact backfill RUNNING — skipping the frontier contact sweep "
            "(same home IP; the backfill's pool covers today's rows anyway)")
    else:
        contact_sweep(local, contact_stats)
    if contact_stats:
        log(
            "frontier contacts: "
            f"{contact_stats.get('contacts_checked', 0)} checked · "
            f"{contact_stats.get('contacts_hits', 0)} with Discord"
        )

    # -- 4. snapshot the post-harvest DB (used only if a push races) -------
    # SQLite backup API, NOT a raw byte copy: WAL-mode commits live in the
    # -wal file and a byte copy can produce a snapshot missing recent writes
    # (exact class of bug that bit the backfill's push-race snapshot).
    harvest_copy = local.with_suffix(".db.harvest")
    src = sqlite3.connect(str(local))
    try:
        dst = sqlite3.connect(str(harvest_copy))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # -- 4. push (merge-retry against Actions races) -----------------------
    push_with_merge_retry(harvest_copy)
    harvest_copy.unlink(missing_ok=True)
    log(f"DONE in {time.time() - started:.0f}s — Actions will drain + hydrate the new seeds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
