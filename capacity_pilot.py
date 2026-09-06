#!/usr/bin/env python3
"""Capacity pilot — Phase 1 of the cadence auto-scaler + keyword-trial monitor.

PHASE 1 = observe and recommend, NEVER act. Every sync records one row of
health telemetry into the ``sync_health_log`` table; every run prints a short
pilot block into the Actions log with:

  * keyword-trial verdict (200/run trial): KEEP 200 / BORDERLINE / REVERT,
    based on chronic proxy benching, keyword success rate, breaker trips;
  * hydrator utilization: known-due queue vs the 7,500/run cap, and the
    86,400-refreshes/day budget;
  * cadence recommendations per tier (tighten/stretch), plus how far each
    tier is from the Phase-2 floors (T1 <= 1h, T2 <= 2h).

Everything derives from data already in the DB (scan_runs, sync_health_log,
game_analytics) — the pilot adds zero network calls and cannot affect sync
behaviour. Phase 2 later promotes these exact rules into an actor, gated by
the same floors/hysteresis described in PILOT_PLAN.md.

Usage:
    from capacity_pilot import record_run, report  # from live_sync.py
    python capacity_pilot.py report [--runs 72]    # CLI: last 12h of runs
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import sqlite3

# --- constants shared with scout_core (imported lazily to avoid cycles) ----
def _core():
    import scout_core
    return scout_core


CAP_PER_RUN = 150 * 50          # budget_batches x batch_size = 7,500 games/run
RUNS_PER_DAY_HYDRATOR = 288     # every 5 minutes
# Theoretical ceiling if every run used the full 7,500 cap. Observed runs are
# far smaller (~300-800), so this denominator reads as a floor on headroom.
DAILY_BUDGET = CAP_PER_RUN * RUNS_PER_DAY_HYDRATOR   # 2,160,000 refreshes/day

# Phase-2 guardrail preview: cadence floors the actor must never cross.
FLOORS_H = {1: 1.0, 2: 2.0}
# Weekly tiers refresh on a 7-day bucket; T8 on a 2-day rotation.
STATIC_CADENCE_H = {5: 168.0, 6: 168.0, 7: 168.0, 8: 48.0}

# Keyword-trial thresholds (3-day monitored trial at 200/run).
KW_TRIAL_MIN_RUNS = 72        # ~12h of finder runs before a verdict is issued
KW_SUCCESS_GREEN = 0.97
KW_SUCCESS_YELLOW = 0.95      # below this -> REVERT territory
BENCH_RATE_GREEN = 0.05       # fraction of runs with any benching
BENCH_RATE_YELLOW = 0.30      # above this -> chronic benching
BREAKER_ALLOWED = 2           # breaker trips tolerated across the window

RUNS_WINDOW = 72              # finder runs = 12h at 10-min cadence
HYD_WINDOW = 288              # hydrator runs = 24h at 5-min cadence


# ---------------------------------------------------------------------------
# Telemetry capture
# ---------------------------------------------------------------------------

def record_run(db_path: str, mode: str, run_id: int, scan: dict, diag: dict,
               tier_schedule: dict | None = None) -> None:
    """Store one health row per sync. Called at the end of live_sync.main()."""
    kw = diag.get("keyword_crawl") or {}
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
                    _kw(diag, "keywords"),
                    _kw(diag, "successful"),
                    len(kw.get("benched") or []),
                    1 if kw.get("breaker_tripped") else 0,
                    (diag.get("metrics") or {}).get("failed_batches", 0),
                    (diag.get("metrics") or {}).get("batches", 0),
                    int(((scan.get("hydration_budget") or {}).get("known_due")) or 0),
                    int(((scan.get("hydration_budget") or {}).get("hydrated")) or 0),
                    int(((scan.get("hydration_budget") or {}).get("deferred")) or 0),
                    round(_utilization(conn), 2),
                    json.dumps(_recommendations(conn, tier_schedule or {})),
                    json.dumps({
                        "catalog": (scan.get("catalog_count")),
                        "tier_counts": scan.get("tier_counts") or {},
                        "keyword_discovered": scan.get("keyword_discovered", 0),
                    }),
                ),
            )
    except sqlite3.Error as exc:  # NEVER break a sync over telemetry
        print(f"[pilot] health-log write skipped: {exc}")


def _kw(diag: dict, key: str) -> int:
    kw = diag.get("keyword_crawl") or {}
    return int(kw.get(key, kw.get(f"{key}_keywords", 0)) or 0)


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

def _utilization(conn: sqlite3.Connection) -> float:
    """Current per-day refresh demand / DAILY_BUDGET, from live tier counts."""
    core = _core()
    tiers = dict(conn.execute(
        "SELECT tier, COUNT(*) FROM game_analytics GROUP BY tier"
    ).fetchall())
    demand = 0.0
    for tier, n in tiers.items():
        if tier <= 0:
            continue
        cad = core.TIER_CADENCE_WALL_HOURS.get(tier, STATIC_CADENCE_H.get(tier, 336.0))
        demand += n * (24.0 / cad)
    return demand / DAILY_BUDGET * 100.0 if DAILY_BUDGET else 0.0


def _recommendations(conn: sqlite3.Connection, tier_schedule: dict) -> list[str]:
    """Phase-1 rule engine: what the Phase-2 actor WOULD do. Pure function of
    DB state; identical inputs always yield identical recommendations."""
    core = _core()
    tiers = dict(conn.execute(
        "SELECT tier, COUNT(*) FROM game_analytics GROUP BY tier"
    ).fetchall())
    recs: list[str] = []
    for tier in sorted(tiers):
        if tier not in core.TIER_CADENCE_WALL_HOURS:
            continue
        cad = core.TIER_CADENCE_WALL_HOURS[tier]
        n = tiers[tier]
        due_now = int(tier_schedule.get({
            1: "t1_t2", 2: "t2", 3: "t3", 4: "t4"}.get(tier, ""), 0) or 0)
        # Rule (Phase 1): only flag real pressure. Tightening recommendations
        # need observed served-lateness data and arrive with Phase 2.
        if due_now > CAP_PER_RUN * 0.4:
            recs.append(f"stretch T{tier}: due {due_now} > 40% of run cap")
    if not recs:
        recs.append("hold: all tiers inside band")
    return recs


# ---------------------------------------------------------------------------
# Verdicts & reporting
# ---------------------------------------------------------------------------

def keyword_trial_verdict(db_path: str, window: int = RUNS_WINDOW) -> dict:
    """KEEP / BORDERLINE / REVERT for the 200-keywords-per-run trial.

    Decision rule (agreed): chronic benching or success < ~95% -> REVERT;
    clean metrics across the window -> KEEP; anything between -> BORDERLINE.
    Returns {} until KW_TRIAL_MIN_RUNS finder runs have accumulated.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT kw_total, kw_ok, kw_benched, kw_breaker
                FROM sync_health_log
                WHERE mode='finder' AND kw_total > 0
                ORDER BY run_id DESC LIMIT ?
                """,
                (window,),
            ).fetchall()
    except sqlite3.Error:
        return {}
    if len(rows) < KW_TRIAL_MIN_RUNS:
        return {"state": "warming", "runs": len(rows), "need": KW_TRIAL_MIN_RUNS}

    total = sum(r["kw_total"] for r in rows)
    ok = sum(r["kw_ok"] for r in rows)
    bench_runs = sum(1 for r in rows if r["kw_benched"] > 0)
    breakers = sum(r["kw_breaker"] for r in rows)
    success = ok / total if total else 0.0
    bench_rate = bench_runs / len(rows)

    if success < KW_SUCCESS_YELLOW or bench_rate > BENCH_RATE_YELLOW or breakers > BREAKER_ALLOWED:
        state, why = "REVERT", (
            f"success {success:.1%} / bench {bench_rate:.0%} of runs / "
            f"{breakers} breaker trips")
    elif success >= KW_SUCCESS_GREEN and bench_rate <= BENCH_RATE_GREEN and breakers == 0:
        state, why = "KEEP", f"success {success:.1%}, bench {bench_rate:.0%} of runs, 0 breakers"
    else:
        state, why = "BORDERLINE", (
            f"success {success:.1%}, bench {bench_rate:.0%} of runs, {breakers} breakers")
    return {
        "state": state, "why": why, "runs": len(rows), "success": success,
        "bench_rate": bench_rate, "breakers": breakers,
    }


def hydrator_report(db_path: str, window: int = HYD_WINDOW) -> dict:
    """Utilization + served-lateness proxy for the hydrator over ~24h."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT known_due, hydrated, deferred, utilization_pct
                FROM sync_health_log WHERE mode='hydrator'
                ORDER BY run_id DESC LIMIT ?
                """,
                (window,),
            ).fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        return {"runs": 0}
    due = [r["known_due"] for r in rows]
    return {
        "runs": len(rows),
        "due_avg": sum(due) / len(due),
        "due_max": max(due),
        "cap_pct_avg": sum(due) / len(due) / CAP_PER_RUN * 100.0,
        "cap_pct_max": max(due) / CAP_PER_RUN * 100.0,
        "deferred_total": sum(r["deferred"] for r in rows),
        "utilization_avg": sum(r["utilization_pct"] for r in rows) / len(rows),
    }


def report(db_path: str) -> str:
    """The pilot block printed at the end of every sync (and the CLI)."""
    lines: list[str] = ["", "=" * 62, "CAPACITY PILOT (Phase 1 — observe only)", "=" * 62]

    trial = keyword_trial_verdict(db_path)
    if trial.get("state") == "warming":
        lines.append(f"keyword trial   : warming up — {trial['runs']}/{trial['need']} finder runs logged")
    elif trial:
        mark = {"KEEP": "✅ KEEP 200/run", "BORDERLINE": "⚠️ BORDERLINE", "REVERT": "⛔ REVERT"}[trial["state"]]
        lines.append(f"keyword trial   : {mark}  [{trial['why']}]")
        if trial["state"] == "REVERT":
            lines.append("                  -> set KEYWORDS_PER_SYNC = 100 (scout_core.py) or add a second proxy mirror")
    else:
        lines.append("keyword trial   : no finder data yet")

    hyd = hydrator_report(db_path)
    if hyd.get("runs"):
        lines.append(
            f"hydrator 24h    : due avg {hyd['due_avg']:.0f} (max {hyd['due_max']:.0f}) "
            f"= {hyd['cap_pct_avg']:.1f}% of the 7,500/run cap "
            f"(peak {hyd['cap_pct_max']:.1f}%) · deferred {hyd['deferred_total']:,}"
        )
        lines.append(f"budget utilized : {hyd['utilization_avg']:.1f}% of the 2.16M/day theoretical cap "
                     f"(observed due {hyd['due_avg']:.0f}/run) — "
                     f"{'OK, headroom' if hyd['cap_pct_avg'] < 40 else 'PRESSURE: begin Phase-2 scaler'}")
    else:
        lines.append("hydrator 24h    : no hydrator data yet")

    lines.append("Phase 2         : locked — pilot observes only (see PILOT_PLAN.md)")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Capacity pilot report")
    ap.add_argument("--db", default=str(APP_DIR / "rbx_scout.db"))
    args = ap.parse_args()
    print(report(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
