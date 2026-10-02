#!/usr/bin/env python3
"""Recover tonight's skipped contact sweep, then push the verdicts.

2026-10-02: the 20:18 atlas_home run harvested 141 new games but its final
push failed (GitHub upload verification flake), and the nightly contact
sweep's verdict writes collided with the push-retry cycle's DB re-pulls
("unable to open database file") — the sweep pointer was never set, so the
real sweep never completed. This script:

  1. waits for any running push_pending.py to finish (avoids the exact
     DB-file race that crashed the sweep last time),
  2. runs the same bounded contact sweep main() would have run,
  3. pushes the result with the merge-safe retry.

Safe to run any time: the sweep is budget-capped, idempotent for already-
checked rows, and sets its own pointer only on completion.
"""

import sqlite3
import subprocess
import time

import atlas_home as ah


def _other_push_running() -> bool:
    out = subprocess.run(
        ["pgrep", "-f", "push_pending.py"], capture_output=True, text=True
    )
    return bool(out.stdout.strip())


def main() -> int:
    local = ah.APP_DIR / "rbx_scout.db"
    # 1. wait out the concurrent push (up to 20 min, then proceed anyway —
    #    the sweep's own busy_timeout handles stragglers)
    waited = 0
    while _other_push_running() and waited < 1200:
        time.sleep(15)
        waited += 15
        ah.log(f"sweep_pending: waiting for the running push ({waited}s)")
    if _other_push_running():
        ah.log("sweep_pending: push still active after 20m — proceeding anyway")

    # 2. the sweep itself (same entry point as main(), pointer included)
    contact_stats: dict = {}
    checked = ah.contact_sweep(local, contact_stats)
    ah.log(
        "sweep_pending: sweep done — "
        f"{contact_stats.get('contacts_checked', checked)} checked · "
        f"{contact_stats.get('contacts_hits', 0)} with Discord"
    )
    if not checked:
        ah.log("sweep_pending: nothing swept (throttled or empty pool) — done")
        return 0

    # 3. carry the verdicts to the store
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
    ah.push_with_merge_retry(harvest_copy)
    harvest_copy.unlink(missing_ok=True)
    ah.log("sweep_pending: verdicts pushed to the store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
