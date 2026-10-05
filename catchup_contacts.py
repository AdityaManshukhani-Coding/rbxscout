#!/usr/bin/env python3
"""One-off Discord contact backfill — clear the unchecked qualified backlog.

Why this exists (2026-09-24): ~9.5k games that meet the dashboard's entry bar
(20k visits / 25 CCU) have never had their Discord contact resolved — the
dashboard only checks the page a user is looking at. This script walks the
whole backlog through the exact same resolve_game_contact pipeline (game
social links -> description -> group -> owner, 6h cache) and writes the
verdicts into game_analytics / contact_archive, so the "With Discord" filter
and the outreach column fill in for everything at once.

Runs from the HOME machine only (like atlas_home.py / catchup_metrics.py):
the /social-links/list endpoint requires a .ROBLOSECURITY cookie and the
whole run is paced through scout_core's shared token bucket + throttle
window. Verdicts persist on the catalog rows, so re-running after a crash
(or a throttle window) simply resumes where it stopped.

Credential (required for the primary source):
    export RBXSCOUT_COOKIE='.ROBLOSECURITY=_|WARNING:...|_.ABC...'
  or put the cookie value in the gitignored local_cookie.txt file
  (same value you paste into the dashboard's Connect-your-Roblox-session
  step). The script strips an optional leading '.ROBLOSECURITY='.

Usage:
    .venv/bin/python catchup_contacts.py                 # qualified backlog
    .venv/bin/python catchup_contacts.py --limit 2000    # first slice only
    .venv/bin/python catchup_contacts.py --all           # + decayed band
    .venv/bin/python catchup_contacts.py --no-push       # local only
"""

from __future__ import annotations

import argparse
import os
import signal
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
LOCAL_COOKIE_FILE = APP_DIR / "local_cookie.txt"

GROUP = 100           # rows per drain group — small groups keep request
                      # bursts short (a full group is ~300 single GETs)
EMPTY_STREAK_STOP = 3  # consecutive throttle-hit groups -> abort cleanly
# Mid-run checkpoint pushes (incident 2026-09-24: the end-of-run push means a
# crash or reboot costs the WHOLE night's verdicts — a checkpoint every N
# games bounds any loss to roughly an hour of work).
CHECKPOINT_EVERY = 2_000
# Polite overnight pacing: ~1.2 s per game between groups (~2.5 req/s
# average). The first un-paced run tripped Roblox's 429s within ~750 games
# (~19 req/s) — sustainable for a 23k-game sweep needs single-digit req/s.
# Override with SS_CONTACT_BACKFILL_DELAY (seconds per game).
PER_GAME_DELAY = max(0.2, float(os.environ.get("SS_CONTACT_BACKFILL_DELAY", "1.2")))
# Sentinel: the last run wrote verdicts it never pushed (crash / throttle
# abort / kill). While it exists, a restart RESUMES — skipping the pull,
# because a pull replaces the local catalog and unpushed verdicts exist
# only locally. Cleared by every successful push.
CHECKPOINT_FILE = APP_DIR / "logs" / "contact_backfill.unpushed"
# Pause protocol (incident 2026-09-25 ~11:00): when the primary's push
# races Actions, its recovery PULL atomically replaces the catalog file —
# any shard writes between the pre-push snapshot and that pull are
# stranded in the orphaned inode. The primary now raises this flag and
# waits out the shard's current group before snapshotting; the shard
# checks the flag between groups and holds until the pull+merge is done.
PAUSE_FILE = APP_DIR / "logs" / "contact_backfill.pause"
PAUSE_SETTLE_S = 200  # > worst observed group time (~172s)

# Contact columns carried by the push-race merge (see merge_contact_verdicts).
CONTACT_COLS = (
    "has_discord", "discord_url", "status", "found_via",
    "has_social_links", "contacts_checked_at", "contact_schema_version",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def _install_signal_trace() -> None:
    """Log fatal signals before dying (incident 2026-09-24 ~21:10: the
    worker vanished with NO traceback — undiagnosable). SIGTERM handlers
    run on graceful kills; SIGKILL can never be caught, but the supervisor
    (launch_backfill.py) now relaunches the resumable sweep either way."""

    def _die(signum, _frame):
        log(f"FATAL SIGNAL {signum} received — verdicts stored so far are safe; "
            "re-run launch_backfill.py to resume")
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(sig, _die)
        except (ValueError, OSError):
            pass


def load_cookie() -> str:
    """Credential from RBXSCOUT_COOKIE or a gitignored local cookie file.

    SS_CONTACT_COOKIE_FILE overrides the file path so a second account can
    run as a shard worker off local_cookie2.txt without touching the
    primary's credential.
    """
    import os

    value = (os.environ.get("RBXSCOUT_COOKIE") or "").strip()
    if value:
        return value
    cookie_file = os.environ.get("SS_CONTACT_COOKIE_FILE", "").strip()
    cookie_path = (
        APP_DIR / cookie_file if cookie_file else LOCAL_COOKIE_FILE
    )
    if cookie_path.exists():
        value = cookie_path.read_text(encoding="utf-8").strip()
        if value:
            log(f"cookie: read from {cookie_path.name} (gitignored)")
            return value
    log(
        "NO COOKIE — the primary source (game /social-links/list) answers 401 "
        "without .ROBLOSECURITY, so only the description/owner fallbacks would "
        "run. export RBXSCOUT_COOKIE='...' or write it into local_cookie.txt."
    )
    return ""


def db_sync_out(*args: str) -> tuple[int, str]:
    """Run db_sync.py; returns (returncode, combined output). Never raises."""
    res = subprocess.run(
        [PY, str(APP_DIR / "db_sync.py"), *args],
        capture_output=True, text=True, cwd=str(APP_DIR),
    )
    return res.returncode, (res.stdout + res.stderr).strip()


def contact_coverage(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """(qualified_total, qualified_checked, hits) for the progress log."""
    q_total = conn.execute(
        "SELECT COUNT(*) FROM game_analytics "
        "WHERE COALESCE(visits, 0) >= 20000 AND COALESCE(ccu, 0) >= 25"
    ).fetchone()[0]
    q_checked = conn.execute(
        "SELECT COUNT(*) FROM game_analytics "
        "WHERE COALESCE(visits, 0) >= 20000 AND COALESCE(ccu, 0) >= 25 "
        "AND contacts_checked_at IS NOT NULL"
    ).fetchone()[0]
    hits = conn.execute(
        "SELECT COUNT(*) FROM game_analytics WHERE has_discord = 1"
    ).fetchone()[0]
    return int(q_total), int(q_checked), int(hits)


def snapshot_catalog(target: Path) -> None:
    """Consistent snapshot of the live catalog via SQLite's backup API.

    A raw file copy of a WAL-mode database misses everything still sitting
    in the -wal file (the first run's push-race merge died exactly because
    its file-copy snapshot predated the run's own writes). backup() captures
    a committed, WAL-inclusive state even while writers are active.
    """
    src = sqlite3.connect(str(DB))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def merge_contact_verdicts(target: Path, snapshot: Path) -> int:
    """Re-apply snapshot verdicts onto a freshly pulled catalog (push race).

    A push that races with Actions re-pulls the store — which would REPLACE
    the local catalog and silently discard every verdict this run just wrote
    (the contact overlay only carries UI-resolved state, not this script's).
    Before the retry, fill the pulled copy's NULL verdicts from the
    pre-push snapshot (NULL-only: never clobbers fresher store data), and
    restore the archive rows verbatim. Classic correlated-subquery UPDATE:
    no SQLite-version assumptions. Returns rows updated.

    A store copy pushed BEFORE contact_archive existed (the table was added
    2026-09-24) lacks the table entirely — create it here so the merge can
    never die on a fresh pull. Keep the column list in sync with scout_core.
    """
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("ATTACH DATABASE ? AS snap", (str(snapshot),))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS contact_archive ("
            "universe_id INTEGER PRIMARY KEY, has_discord BOOLEAN, discord_url TEXT, "
            "status TEXT, found_via TEXT, has_social_links BOOLEAN, "
            "contacts_checked_at TIMESTAMP, contact_schema_version INTEGER DEFAULT 0, "
            "title TEXT, archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        sets = ",\n".join(
            f"{col} = (SELECT s.{col} FROM snap.game_analytics s "
            "WHERE s.universe_id = game_analytics.universe_id)"
            for col in CONTACT_COLS
        )
        cur = conn.execute(
            "UPDATE game_analytics SET " + sets +
            " WHERE contacts_checked_at IS NULL AND EXISTS ("
            "SELECT 1 FROM snap.game_analytics s WHERE s.universe_id = game_analytics.universe_id "
            "AND s.contacts_checked_at IS NOT NULL)"
        )
        updated = cur.rowcount or 0
        conn.execute(
            "INSERT OR REPLACE INTO contact_archive ("
            "universe_id, has_discord, discord_url, status, found_via, "
            "has_social_links, contacts_checked_at, contact_schema_version, "
            "title, archived_at) "
            "SELECT universe_id, has_discord, discord_url, status, found_via, "
            "has_social_links, contacts_checked_at, contact_schema_version, "
            "title, CURRENT_TIMESTAMP FROM snap.contact_archive"
        )
        conn.commit()
        conn.execute("DETACH DATABASE snap")
        return updated
    finally:
        conn.close()


def push_with_retry(label: str = "") -> int:
    """Push the local catalog to the store, recovering from Actions races.

    Returns 0 on success, 2 on unrecoverable failure. Snapshot BEFORE the
    first attempt: if the store moved ahead mid-run, the pull in the
    merge-retry must not discard the run's verdicts — they are re-applied
    from the snapshot before re-pushing. The snapshot is deleted ONLY by a
    clean push; on any failure it is the only copy of the verdicts the
    store has not yet received. A successful push also clears the resume
    sentinel (the store now holds everything the run has produced).
    """
    snapshot = DB.with_suffix(".db.contacts")
    snapshot_catalog(snapshot)
    for attempt in range(1, 4):
        rc, out = db_sync_out("push")
        if out:
            log("  " + out.replace("\n", "\n  "))
        if rc == 0:
            snapshot.unlink(missing_ok=True)
            CHECKPOINT_FILE.unlink(missing_ok=True)
            log(f"push ok ({label or 'end-of-run'}) — resume sentinel cleared")
            return 0
        if "refusing to push" in out and attempt < 3:
            log(f"push raced with Actions (attempt {attempt}) — re-pulling + re-applying verdicts…")
            # Quiesce the shard BEFORE snapshot+pull: it finishes its
            # current group, sees the flag, and holds — so every verdict
            # it writes is inside the fresh snapshot when the pull
            # replaces the file. Flag is cleared no matter how this ends.
            PAUSE_FILE.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            try:
                time.sleep(PAUSE_SETTLE_S)
                snapshot_catalog(snapshot)  # fresh: includes the settle window
                db_sync_out("pull")
                try:
                    merged = merge_contact_verdicts(DB, snapshot)
                    log(f"re-applied {merged:,} verdicts from the pre-push snapshot")
                except Exception as exc:
                    log(f"CRITICAL: merge failed ({exc}) — snapshot kept at "
                        f"{snapshot.name}; re-run the backfill to retry the push "
                        "(nothing is lost: the snapshot holds every verdict).")
                    return 2
            finally:
                PAUSE_FILE.unlink(missing_ok=True)
            continue
        # Store busy but not refusing (network/API blip): wait it out —
        # Actions pushes on a ~30-60 min cadence, so patience beats loss.
        log(f"push failed (attempt {attempt}) — waiting 5 min before retrying")
        time.sleep(300)
    log("push still failing after 3 attempts — verdicts are safe in the "
        f"snapshot ({snapshot.name}); re-run the backfill later")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fill Discord contact verdicts for the qualified catalog."
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="cap how many games to check this run (0 = all)")
    parser.add_argument("--all", action="store_true",
                        help="also sweep the decayed band (visits >= 20k, "
                             "any CCU > 0) after the qualified backlog")
    parser.add_argument("--no-push", action="store_true",
                        help="keep the filled catalog local (no release push)")
    parser.add_argument("--no-pull", action="store_true",
                        help="skip the pre-pull: the local copy is already "
                             "fresher than the store (unpushed verdicts would "
                             "be discarded by a pull, since they only exist "
                             "locally until the push)")
    parser.add_argument("--pool", choices=("all", "qualified", "decayed"),
                        default="all",
                        help="which pool to sweep: 'all' (default) does the "
                             "qualified backlog then the decayed band; a shard "
                             "worker takes a single pool so two accounts can "
                             "split the catalog")
    parser.add_argument("--reverse", action="store_true",
                        help="walk the pool(s) from the end — lets a shard "
                             "start where the primary will finish, so the two "
                             "lanes meet in the middle (shared 6h verdict "
                             "cache makes overlap nearly free)")
    args = parser.parse_args()

    # Shard worker (SS_CONTACT_BACKFILL_ROLE=shard): coordinated BY the
    # primary backfill — never pulls (would replace the DB under the
    # primary), never pushes (the primary's checkpoint pushes carry the
    # shard's verdicts — they share the local catalog), and never takes
    # the exclusive lock (the primary owns the sweep).
    helper_mode = (
        os.environ.get("SS_CONTACT_BACKFILL_ROLE", "").strip().lower() == "shard"
    )
    if helper_mode:
        args.no_push = True
        args.no_pull = True

    _install_signal_trace()
    lock = None
    if not helper_mode:
        # Incident 2026-09-24 20:00: a redundant second launch pulled the
        # store mid-run, atomically REPLACING the DB file under the live
        # worker and orphaning ~50 minutes of verdicts (they lived in the
        # unlinked inode). The lock makes concurrent PULL/PUSH-capable
        # backfills impossible: the second launch exits instead of
        # duplicating ~40k Roblox requests and racing the catalog file.
        lock = scout_core.ContactBackfillLock()
        if not lock.acquire(wait_s=60):
            log("ANOTHER CONTACT BACKFILL IS ALREADY RUNNING — exiting.")
            log("The running job owns the sweep and is fully resumable; this "
                "launch would only duplicate requests and race the catalog file.")
            return 3
        log("backfill lock acquired (logs/contact_backfill.lock)")
    else:
        log("shard mode: coordinated by the primary backfill "
            "(no pull, no push, no lock; verdicts ride the primary's pushes)")
    try:
        # A leftover sentinel means the previous run died with unpushed
        # verdicts — the local catalog is fresher than the store, so this
        # run resumes (no pull) instead of discarding that paid work.
        resumed = helper_mode or CHECKPOINT_FILE.exists()
        return _run_backfill(args, resumed=resumed)
    finally:
        if lock is not None:
            lock.release()


def _run_backfill(args: argparse.Namespace, *, resumed: bool = False) -> int:
    helper_mode = (
        os.environ.get("SS_CONTACT_BACKFILL_ROLE", "").strip().lower() == "shard"
    )
    log("=" * 62)
    log("CONTACT BACKFILL — start"
        + (" (resume: local catalog is fresher than the store — skipping the pull)" if resumed else ""))
    started = time.time()

    # A pull atomically REPLACES the local catalog with the store copy — and
    # unpushed verdicts only exist locally until a push. On a resume the
    # local copy is by definition fresher (this run wrote them since its own
    # pull), so pulling would silently discard hours of paid lookups.
    if not resumed and not args.no_push and not args.no_pull:
        rc, out = db_sync_out("pull")
        if out:
            log("  " + out.replace("\n", "\n  "))
        if rc != 0:
            log("pull failed — continuing with the local copy (it may be stale)")

    scout = scout_core.RobloxPlatformScout(db_path=str(DB), max_workers=1)
    # A pull (above) can install a store copy predating contact_archive —
    # recreate it up front so archive writes never START the run failing.
    scout._ensure_contact_archive()
    cookie = load_cookie()
    if cookie:
        scout.set_cookie(cookie)
        log(f"cookie: attached (.ROBLOSECURITY, {len(cookie):,} chars)")

    pool = args.pool
    with sqlite3.connect(str(DB), factory=scout_core._ClosingConnection) as conn:
        qualified_ids = scout.contacts_backfill_due(limit=args.limit or 10 ** 9)
        # --pool fully determines the sweep; --all is the legacy "also do
        # the decayed band" switch (incident 2026-09-25: a shard launched
        # with --pool decayed but without --all selected an empty pool and
        # exited without checking anything).
        include_decayed = args.all or pool in ("all", "decayed")
        decayed_ids = (
            scout.contacts_backfill_due(min_visits_floor=1, limit=args.limit or 10 ** 9)
            if include_decayed else []
        )
    # The floor query re-lists the qualified backlog first; skip what the
    # qualified pass already covers so --all only ADDS the decayed band.
    decayed_ids = [uid for uid in decayed_ids if uid not in set(qualified_ids)]
    if pool == "decayed":
        qualified_ids = []  # computed above for the exclusion only
    if args.reverse:
        qualified_ids = qualified_ids[::-1]
        decayed_ids = decayed_ids[::-1]

    with sqlite3.connect(str(DB), factory=scout_core._ClosingConnection) as conn:
        q_total, q_checked, hits = contact_coverage(conn)
    log(f"coverage now: {q_checked:,}/{q_total:,} qualified checked · {hits:,} with Discord")
    log(f"qualified backlog: {len(qualified_ids):,} games"
        + (f" · decayed band: +{len(decayed_ids):,}" if args.all else ""))

    # Sentinel ON from the first verdict: any death after this point leaves
    # the next run in resume mode (no pull -> no verdict loss). Cleared only
    # by a successful push.
    if not args.no_push:
        CHECKPOINT_FILE.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))

    total_checked = 0
    total_hits = 0
    groups_since_push = 0
    for label, ids in (("qualified", qualified_ids), ("decayed", decayed_ids)):
        if not ids:
            continue
        log(f"--- {label} pool: {len(ids):,} games ---")
        empty_streak = 0
        for start in range(0, len(ids), GROUP):
            # Shard pause protocol: the primary raises PAUSE_FILE during a
            # push-race recovery (snapshot + pull + merge) — writing to the
            # catalog during that window would orphan the writes when the
            # pull replaces the file. Hold here until the flag clears.
            if helper_mode and PAUSE_FILE.exists():
                log("primary is recovering a push race — pausing until the "
                    "catalog swap completes")
                while PAUSE_FILE.exists():
                    time.sleep(20)
            chunk = ids[start:start + GROUP]
            t0 = time.time()
            try:
                result = scout.scan_contacts(chunk)
                hits_here = int((result.get("has_discord") == True).sum()) if not result.empty else 0  # noqa: E712
            except Exception as exc:
                log(f"group at {start}: FAILED ({exc}) — continuing")
                hits_here = 0
                empty_streak += 1
            else:
                if scout.throttle_window_active():
                    empty_streak += 1
                else:
                    empty_streak = 0
            total_checked += len(chunk)
            total_hits += hits_here
            with sqlite3.connect(str(DB), factory=scout_core._ClosingConnection) as conn:
                _, q_checked_now, hits_now = contact_coverage(conn)
            log(
                f"[{label}] {start + len(chunk):,}/{len(ids):,} queued · "
                f"{hits_now:,} total hits (+{hits_here}) · "
                f"{q_checked_now:,}/{q_total:,} qualified checked · "
                f"{time.time() - t0:.0f}s"
            )
            if empty_streak >= EMPTY_STREAK_STOP:
                log("Roblox is throttling repeatedly — stopping cleanly. "
                    "Re-run later; completed verdicts are already stored.")
                break
            # Overnight pacing: enforce a MINIMUM average of PER_GAME_DELAY
            # seconds per game across group+pause combined. Request-aware:
            # a slow group sleeps little (or not at all), a fast one sleeps
            # the remainder — the average request rate never exceeds the
            # politeness budget, but no idle time is wasted on top of slow
            # groups (blind sleeps made slow groups cost double).
            time.sleep(max(0.0, len(chunk) * PER_GAME_DELAY - (time.time() - t0)))
            # Checkpoint: bounds the cost of ANY death (kill, crash, reboot)
            # to roughly one hour of work instead of the whole night. The
            # sentinel is rewritten per group so even a mid-group death
            # leaves the next run in no-pull resume mode.
            if not args.no_push:
                CHECKPOINT_FILE.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            groups_since_push += 1
            if not args.no_push and groups_since_push >= CHECKPOINT_EVERY // GROUP:
                if push_with_retry("checkpoint") != 0:
                    return 2
                groups_since_push = 0
        else:
            continue
        break  # only reached via the throttle break

    with sqlite3.connect(str(DB), factory=scout_core._ClosingConnection) as conn:
        q_total, q_checked, hits = contact_coverage(conn)
    log(
        f"coverage now: {q_checked:,}/{q_total:,} qualified checked · "
        f"{hits:,} with Discord ({hits / max(1, q_total) * 100:.1f}% hit rate)"
    )
    log(f"checked this run: ~{total_checked:,} games · {total_hits:,} new hits")

    if args.no_push:
        log("--no-push: catalog stays local")
    elif push_with_retry("final") != 0:
        return 2

    log(f"CONTACT BACKFILL — done in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
