#!/usr/bin/env python3
"""Expansion pilot — observe-only telemetry for the catalog-expansion engines.

Every sync records one row of health telemetry into the ``sync_health_log``
table; every EXPANDER run prints the expansion-pilot block into the Actions
log with the KEEP / BORDERLINE / REVERT verdict for the catalog-expansion
pilot (EXPANSION_PILOT.md):

  * yield — did hydrated candidates qualify (20k visits / 25 CCU) at a real
    rate across the window?
  * health — did metric batches fail (429s) at a sustainable rate?

Everything derives from data already in the DB (sync_health_log) plus the
expansion stats attached to each run — the pilot adds zero network calls and
cannot affect sync behaviour.

Usage:
    from expansion_pilot import record_run, report  # from live_sync.py
    python expansion_pilot.py report [--db PATH]    # CLI: verdict from the DB
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

# Expansion-pilot verdict thresholds (EXPANSION_PILOT.md). Same observe ->
# verdict -> scale pattern as the old keyword trial. Yield = qualified /
# evaluated across the window; metrics-failure rate proxies the 429 health.
EXP_TRIAL_MIN_RUNS = 72       # ~36h of expander runs at the 30-min cadence
EXP_YIELD_GREEN = 0.002       # >= 0.2% of evaluated IDs qualify -> real signal
EXP_YELLOW_FAIL = 0.10        # metrics-failure band between green and revert
EXP_FAIL_REVERT = 0.30        # > 30% failed metric batches -> REVERT


# ---------------------------------------------------------------------------
# Telemetry capture
# ---------------------------------------------------------------------------

def record_run(db_path: str, mode: str, run_id: int, scan: dict, diag: dict,
               tier_schedule: dict | None = None) -> None:
    """Store one health row per sync. Called at the end of live_sync.main().

    The sync_health_log schema predates the expansion pilot (keyword-trial
    columns are retained, defaulting to 0) — only the expansion-relevant
    fields are populated: metrics health + the ``extra`` payload carrying
    ``scan['expansion']``.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_health_log
                    (run_id, mode, ts, kw_total, kw_ok, kw_benched, kw_breaker,
                     metrics_failed, metrics_total, known_due, hydrated,
                     deferred, utilization_pct, recommendations, extra)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(run_id),
                    mode,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    0, 0, 0, 0,
                    (diag.get("metrics") or {}).get("failed_batches", 0),
                    (diag.get("metrics") or {}).get("batches", 0),
                    int(((scan.get("hydration_budget") or {}).get("known_due")) or 0),
                    int(((scan.get("hydration_budget") or {}).get("hydrated")) or 0),
                    int(((scan.get("hydration_budget") or {}).get("deferred")) or 0),
                    0.0,
                    None,
                    json.dumps({
                        "catalog": (scan.get("catalog_count")),
                        "tier_counts": scan.get("tier_counts") or {},
                        "expansion": scan.get("expansion") or None,
                    }),
                ),
            )
    except sqlite3.Error as exc:  # NEVER break a sync over telemetry
        print(f"[pilot] health-log write skipped: {exc}")


# ---------------------------------------------------------------------------
# Verdict & reporting
# ---------------------------------------------------------------------------

def expansion_trial_verdict(db_path: str, window: int = EXP_TRIAL_MIN_RUNS) -> dict:
    """KEEP / BORDERLINE / REVERT for the catalog-expansion pilot.

    Reads the recorded expansion stats out of sync_health_log.extra and asks
    two questions over the window:
      1. yield — did evaluated candidates qualify at a real rate?
      2. health — did metric batches fail (429s) at a sustainable rate?
    Returns {"state": "warming", ...} until EXP_TRIAL_MIN_RUNS expander runs
    have accumulated.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT extra, metrics_failed, metrics_total
                FROM sync_health_log
                WHERE mode='expander'
                ORDER BY run_id DESC LIMIT ?
                """,
                (window,),
            ).fetchall()
    except sqlite3.Error:
        return {}
    # Keep only runs that actually carried an expansion block.
    runs: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["extra"] or "{}").get("expansion")
        except (TypeError, ValueError):
            payload = None
        if payload:
            runs.append(payload)
    if len(runs) < EXP_TRIAL_MIN_RUNS:
        return {"state": "warming", "runs": len(runs), "need": EXP_TRIAL_MIN_RUNS}

    drained = sum(int((r.get("drain") or {}).get("claimed") or 0) for r in runs)
    scanned = sum(int((r.get("atlas") or {}).get("fetched_ids") or 0) for r in runs)
    qualified = (
        sum(int((r.get("drain") or {}).get("qualified") or 0) for r in runs)
    )
    failed_batches = sum(int(r.get("metrics_failed") or 0) for r in runs)
    checked = drained + scanned
    yield_rate = qualified / checked if checked else 0.0
    fail_rate = failed_batches / checked if checked else 0.0

    if checked == 0 or yield_rate < EXP_YIELD_GREEN / 10:
        state, why = "REVERT", f"no real yield: {qualified} qualified / {checked} evaluated"
    elif fail_rate > EXP_FAIL_REVERT:
        state, why = "REVERT", f"metric batches failing: {fail_rate:.0%} of evaluated IDs"
    elif yield_rate >= EXP_YIELD_GREEN and fail_rate <= EXP_YELLOW_FAIL:
        state, why = "KEEP", (
            f"yield {yield_rate:.2%}, failures {fail_rate:.1%} "
            f"({qualified} qualified / {checked} evaluated)"
        )
    else:
        state, why = "BORDERLINE", (
            f"yield {yield_rate:.2%}, failures {fail_rate:.1%} "
            f"({qualified} qualified / {checked} evaluated)"
        )
    return {
        "state": state, "why": why, "runs": len(runs),
        "yield_rate": yield_rate, "fail_rate": fail_rate,
        "qualified": qualified, "evaluated": checked,
    }


def report(db_path: str) -> str:
    """The expansion-pilot block printed at the end of expander syncs."""
    lines: list[str] = ["", "=" * 62, "EXPANSION PILOT (observe only)", "=" * 62]

    exp = expansion_trial_verdict(db_path)
    if exp.get("state") == "warming":
        lines.append(f"expansion pilot : warming up — {exp['runs']}/{exp['need']} expander runs logged")
    elif exp:
        mark = {"KEEP": "✅ KEEP pilot rates", "BORDERLINE": "⚠️ BORDERLINE", "REVERT": "⛔ REVERT"}[exp["state"]]
        lines.append(f"expansion pilot : {mark}  [{exp['why']}]")
        if exp["state"] == "REVERT":
            lines.append("                  -> set EXPAND_FRONTIER_BATCHES=0 and EXPAND_QUEUE_BATCHES=5 "
                         "(repo variables) or revert the expansion commit")
    else:
        lines.append("expansion pilot : no expander data yet")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Expansion pilot report")
    ap.add_argument("command", nargs="?", default="report", help="report (default)")
    ap.add_argument("--db", default=str(APP_DIR / "rbx_scout.db"))
    args = ap.parse_args()
    if args.command != "report":
        ap.error(f"unknown command {args.command!r} (expected 'report')")
    print(report(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
