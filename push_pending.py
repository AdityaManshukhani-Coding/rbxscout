#!/usr/bin/env python3
"""Re-push the local catalog after an interrupted atlas_home run.

2026-09-27: the nightly contact sweep crashed mid-run (duplicate-instance
file race) and the recovery process was killed during its push, leaving the
sweep's 54 verdicts stranded in the local DB while the store lacked them.
This re-runs the same snapshot + merge-safe push tail as main() so nothing
is stranded. Safe to run any time: the push counter guard refuses stale
writes, and push_with_merge_retry re-pulls when Actions raced ahead.
"""

import sqlite3

import atlas_home as ah


def main() -> int:
    local = ah.APP_DIR / "rbx_scout.db"
    harvest_copy = local.with_suffix(".db.harvest")
    # SQLite backup API, NOT a byte copy (WAL commits live in -wal; the
    # byte-copy bug class is documented in atlas_home.main).
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
    ah.log("push_pending: local catalog pushed to the store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
