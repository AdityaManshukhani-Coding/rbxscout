#!/usr/bin/env python3
"""Rotrends daily sweep — the cold tail's dedicated stats channel.

Why this exists (HYDRATION_SOURCES.md Phase 1, measured 2026-10-07/08):
api.rotrends.com serves pre-built DAILY CCU series per universe ID — the
single thing games.roblox.com never provides. The Roblox hydrator spends
~60% of every run's batch budget re-touching weekly/cold games (T5–T7 =
93% of the store) while hot games run hours past their cadence. Moving the
cold tail to this channel frees the Roblox budget ring-fence (Phase 0) to
serve hot tiers first.

Measured properties that shape this module:

  * Endpoint: GET /explore/games/{universeId}/metrics/1d
    ?fields=playing,like_ratio,earning_rank&start=...&end=...
    Keys by UNIVERSE ID (place IDs return empty metrics). 10/10 random
    catalog sample tracked — including 0-CCU games.
  * Daily granularity only: the newest process_date is YESTERDAY (~1-day
    lag). Rotrends is a trends/history channel, never a hot-game channel.
  * Free tier: custom date ranges ≤ 90 days (365-day windows are
    rejected: {"success": false, "message": "Free users cannot use
    custom date ranges"}). Unknown/undocumented fields are silently
    ignored, so field enumeration stays {playing, like_ratio,
    earning_rank} — everything verified populated.
  * Limiter (stress-tested §2.5): a token bucket with a ~90 s penalty box.
    Hammered at ~75 req/s it hard-429s after ~1,200 requests; the box
    self-clears in ~91 s. Sustained ~11.5 req/s × 90 s = 1,039/1,039 ×
    200 clean; ~20 req/s trips it within ~30 s. Production runs at 8
    req/s (safely inside the bucket) and honors a 95-second pause on the
    FIRST 429, then resumes at half rate — measured worst case, zero
    requests lost. No Retry-After header exists.
  * Nightly full sweep: T5–T7 ≈ 23k games at 8 req/s ≈ 48 min.

Design invariants (same as atlas_home/catchup_metrics):
  * Fail-open: a dead Rotrends costs nothing — the Roblox hydrator keeps
    doing today's exact job on cold games whenever the sweep is absent
    (with 4x more slack once hot tiers stop competing for its budget).
  * One sample per source per day (pk (universe_id, ts) + INSERT OR
    IGNORE — dedupe is automatic, no read-before-write).
  * ts = snapshot process_date (midnight UTC), NEVER fetch time.
  * Roblox remains the tie-breaker: rotrends rows never overwrite the
    game's live ccu in ccu_history (a different source value keeps rows
    distinguishable; dashboard trend filters live vs daily).

Usage:
    .venv/bin/python rotrends_sweep.py                  # nightly sweep
    .venv/bin/python rotrends_sweep.py --backfill 90    # 90d series for shallow games
    .venv/bin/python rotrends_sweep.py --dispatch       # cold unknowns → discovery_queue

Environment knobs:
    ROTRENDS_RPS                 target requests/sec (default 8)
    ROTRENDS_MAX_HOURS           staleness cutoff for "due" games (default 20)
    ROTRENDS_SWEEP_MAX_GAMES     hard cap on games per sweep pass (0 = all due)
    ROTRENDS_BACKFILL_DAYS       window for the shallow backfill (default 90)
    ROTRENDS_BACKFILL_MAX_GAMES  per-pass cap on the shallow backlog drain
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

log = logging.getLogger("rbxscout.rotrends")

# --------------------------------------------------------------------------- #
# Configuration (env-overridable; defaults are the plan's measured values)
# --------------------------------------------------------------------------- #
ROTRENDS_BASE_URL = "https://api.rotrends.com/explore/games"
ROTRENDS_USER_AGENT = (
    "UpScaleScoutingTool/1.0 (Roblox game discovery; contact via repo)"
)
ROTRENDS_FIELDS = "playing,like_ratio,earning_rank"

ROTRENDS_RPS_DEFAULT = 8.0           # measured clean ceiling ~11.5 req/s; production cap 8
PENALTY_BOX_SECONDS = 95.0           # first 429: measured box ≈ 90–91 s, pause once then halve rate
ROTRENDS_TIMEOUT = 10.0              # small JSON payload; 10 s is generous
BACKFILL_WINDOW_DAYS = 90            # free-tier custom-range ceiling

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from scout_core import RobloxPlatformScout  # noqa: E402  (DB writer)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


ROTRENDS_RPS = _env_float("ROTRENDS_RPS", ROTRENDS_RPS_DEFAULT, minimum=0.5)
ROTRENDS_MAX_HOURS = _env_float("ROTRENDS_MAX_HOURS", 20.0, minimum=1.0)
ROTRENDS_SWEEP_MAX_GAMES = _env_int("ROTRENDS_SWEEP_MAX_GAMES", 0, minimum=0)
ROTRENDS_BACKFILL_DAYS = min(
    _env_int("ROTRENDS_BACKFILL_DAYS", BACKFILL_WINDOW_DAYS, minimum=1), 90
)
ROTRENDS_BACKFILL_MAX_GAMES = _env_int("ROTRENDS_BACKFILL_MAX_GAMES", 500, minimum=0)
ROTRENDS_THREADS = _env_int("ROTRENDS_THREADS", 4, minimum=1)


# --------------------------------------------------------------------------- #
# Paced client
# --------------------------------------------------------------------------- #
class RotrendsClient:
    """Thread-safe paced fetcher for the api.rotrends.com metrics endpoint.

    One client per pass; the pacing state (rps, penalty box) is shared by
    every worker thread so the aggregate rate across the pool is what the
    limiter sees — never per-thread rate.
    """

    def __init__(self, rps: float = ROTRENDS_RPS, timeout: float = ROTRENDS_TIMEOUT) -> None:
        self.timeout = float(timeout)
        self._rps_lock = threading.Lock()
        self._rps = max(0.5, float(rps))
        self._next_emit = 0.0  # monotonic
        self._429_lock = threading.Lock()
        self._count_429 = 0
        self._docs_fetched = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = ROTRENDS_USER_AGENT
        self.session.headers["Accept"] = "application/json"

    # -- pacing ---------------------------------------------------------- #
    def _emit_pace(self) -> None:
        with self._rps_lock:
            now = time.monotonic()
            wait = max(0.0, self._next_emit - now)
            self._next_emit = max(now, self._next_emit) + 1.0 / self._rps
        if wait:
            time.sleep(wait)

    def _on_success(self) -> None:
        self._docs_fetched += 1

    def _on_429(self) -> None:
        """First 429: sleep the measured penalty box once, then half rate.

        Measured behavior (HYDRATION_SOURCES.md §2.5): the token bucket's
        penalty box is ~90 s and self-clears; there is no Retry-After
        header. A 95 s pause rides it out; half-rate keeps the second box
        from tripping. Every caller pays the pause exactly once per box
        (guard flag), so concurrent threads don't stack pauses.
        """
        with self._429_lock:
            if self._count_429 == 0:
                self._count_429 += 1
                log.warning(
                    "rotrends 429 — entering the measured %.0f s penalty box, "
                    "then halving the rate to %.1f req/s",
                    PENALTY_BOX_SECONDS, self._rps / 2,
                )
                time.sleep(PENALTY_BOX_SECONDS)
                with self._rps_lock:
                    self._rps = max(0.5, self._rps / 2)

    @property
    def stats(self) -> Dict[str, Any]:
        with self._rps_lock:
            rps = self._rps
        return {
            "fetched": self._docs_fetched,
            "client_429s": self._count_429,
            "final_rps": round(rps, 2),
        }

    # -- fetch ------------------------------------------------------------ #
    def fetch_metrics_window(
        self,
        universe_id: int,
        start: str,
        end: str,
    ) -> List[Dict[str, Any]]:
        """Daily metrics rows [{process_date, playing, like_ratio, earning_rank}].

        `start`/`end` are YYYY-MM-DD strings (UTC); end is inclusive-latest
        (the API's window semantics: end date's row MAY be absent — the
        newest process_date is typically 'yesterday').

        Raises on non-200 (caller decides retry policy); returns [] when
        the universe has no data (e.g. unmatched place IDs, brand-new).
        """
        uid = int(universe_id)
        if uid <= 0:
            return []
        params = urlencode({
            "fields": ROTRENDS_FIELDS,
            "start": f"{start}T00:00:00.000Z",
            "end": f"{end}T00:00:00.000Z",
            "include_previous_period": "false",
        })
        url = f"{ROTRENDS_BASE_URL}/{uid}/metrics/1d?{params}"
        # 429 retry-once: the request that trips the penalty box is retried
        # AFTER the measured box clears (first cloud run 2026-10-08: the
        # un-retried in-flight request plus box-time trickle losses accounted
        # for 225/6,000 errored games). _on_429 sleeps the box inside the
        # first thread that hits it; this attempt loop re-emits once after.
        for attempt in range(2):
            self._emit_pace()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                raise RotrendsFetchError(f"U{uid}: transport error {exc}") from exc
            if resp.status_code == 429:
                self._on_429()
                if attempt == 0:
                    continue  # box handled; re-emit once at the (halved) rate
                raise RotrendsFetchError(f"U{uid}: 429 persists after box")
            break
        if resp.status_code != 200:
            raise RotrendsFetchError(f"U{uid}: HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RotrendsFetchError(f"U{uid}: non-JSON body") from exc
        if payload.get("success") is False:
            # Free-tier window violations come back as success=false with a
            # human message; do not retry — the caller shortens the window.
            raise RotrendsFetchError(f"U{uid}: rejected: {payload.get('message', '?')[:120]}")
        metrics = ((payload.get("data") or {}).get("metrics")) or []
        self._on_success()
        rows: List[Dict[str, Any]] = []
        for m in metrics:
            if not isinstance(m, dict):
                continue
            pd_ = m.get("process_date")
            playing = m.get("playing")
            if not pd_ or playing is None:
                continue
            try:
                rows.append({
                    "universe_id": uid,
                    "process_date": str(pd_)[:10],  # midnight-UTC snapshot date
                    "ccu": int(playing),
                    "like_ratio": (
                        float(m["like_ratio"])
                        if m.get("like_ratio") is not None else None
                    ),
                    "earning_rank": (
                        int(m["earning_rank"])
                        if m.get("earning_rank") is not None else None
                    ),
                })
            except (TypeError, ValueError):
                continue
        return rows


class RotrendsFetchError(RuntimeError):
    """Non-200 / unparsable fetch — surfaced for retry loops."""


# --------------------------------------------------------------------------- #
# DB selection helpers
# --------------------------------------------------------------------------- #
def select_due_universe_ids(
    scout: RobloxPlatformScout,
    max_hours: float = ROTRENDS_MAX_HOURS,
    tiers: Optional[Tuple[int, ...]] = (5, 6, 7),
    limit: int = 0,
) -> List[int]:
    """Tier-t5..7 game IDs whose LAST ccu_history sample is > max_hours old.

    Staleness is read from ccu_history (ANY source — a rotrends_daily row
    from today already satisfies the daily promise). T0–T4 are excluded:
    their cadence is measured in HOURS and belongs to the live channels.
    """
    cutoff = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - max_hours * 3600)
    )
    tier_marks = ",".join(str(t) for t in tiers or ())
    sql = f"""
        SELECT ga.universe_id
        FROM game_analytics ga
        WHERE ga.tier IN ({tier_marks})
          AND COALESCE(
                (SELECT MAX(h.ts) FROM ccu_history h
                  WHERE h.universe_id = ga.universe_id),
              '1970-01-01'
            ) <= ?
        ORDER BY COALESCE(
            (SELECT MAX(h.ts) FROM ccu_history h
              WHERE h.universe_id = ga.universe_id),
            '1970-01-01'
        ) ASC, ga.universe_id ASC
    """
    if limit and int(limit) > 0:
        sql += " LIMIT ?"
    try:
        with scout._connect() as conn:
            params: List[Any] = [cutoff]
            if limit and int(limit) > 0:
                params.append(int(limit))
            return [int(r[0]) for r in conn.execute(sql, params)]
    except sqlite3.Error as exc:
        log.warning("rotrends due-selection failed: %s", exc)
        return []


def select_shallow_universe_ids(
    scout: RobloxPlatformScout,
    max_samples: int = 5,
    limit: int = 500,
    exclude_recent_days: float = 7.0,
) -> List[int]:
    """Games with ≤ max_samples history rows, oldest-stale-first.

    Backfill scope for Phase 1's slow catch-up: the 8,930 currently
    shallow games each get a 90-day window (a handful of games per pass),
    so a shallow game steadily accrues trend depth. `exclude_recent_days`
    skips games whose freshest sample is recent — no point re-fetching a
    90-day window for something already being touched daily.
    """
    recent = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.gmtime(time.time() - exclude_recent_days * 86400),
    )
    try:
        with scout._connect() as conn:
            return [
                int(r[0]) for r in conn.execute(
                    """
                    SELECT universe_id FROM game_analytics
                    WHERE
                      (SELECT COUNT(*) FROM ccu_history h
                        WHERE h.universe_id = game_analytics.universe_id) <= ?
                      AND COALESCE(
                            (SELECT MAX(ts) FROM ccu_history h
                              WHERE h.universe_id = game_analytics.universe_id),
                          '1970-01-01'
                        ) < ?
                    ORDER BY
                      (SELECT COUNT(*) FROM ccu_history h
                        WHERE h.universe_id = game_analytics.universe_id) ASC,
                      universe_id ASC
                    LIMIT ?
                    """,
                    (int(max_samples), recent, int(limit)),
                )
            ]
    except sqlite3.Error as exc:
        log.warning("rotrends shallow-selection failed: %s", exc)
        return []


def select_unknown_cold_ids(
    scout: RobloxPlatformScout, tier: int = 8, limit: int = 200
) -> List[int]:
    """Cold/never-hydrated IDs that ccu_history has NEVER touched.

    The universe does not exist in game_analytics yet (the queue drain owns
    catalog insertion). The sweep's --dispatch mode re-enqueues these into
    discovery as a gentle re-touch — the strict gate still verifies them
    through Roblox first.
    """
    try:
        with scout._connect() as conn:
            return [
                int(r[0]) for r in conn.execute(
                    """
                    SELECT universe_id FROM game_analytics
                    WHERE COALESCE(tier, 0) = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM ccu_history h
                          WHERE h.universe_id = game_analytics.universe_id
                      )
                    ORDER BY universe_id ASC LIMIT ?
                    """,
                    (int(tier), int(limit)),
                )
            ]
    except sqlite3.Error as exc:
        log.warning("rotrends unknown-selection failed: %s", exc)
        return []


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #
def store_day_snapshots(
    scout: RobloxPlatformScout,
    day_rows: Dict[str, Dict[str, Any]],
) -> Dict[str, int]:
    """Persist fetched daily rows via the scout's writer (source-tagged).

    ``day_rows`` — one entry per universe: {
      universe_id: {"ts": "YYYY-MM-DD", "ccu": int,
                     "like_ratio": float|None, "earning_rank": int|None}
    } keyed by universe (latest row per universe wins — one day per pass).
    Calls RobloxPlatformScout.upsert_rotrends_snapshot (introduced for this
    module), which handles history inserts, enrichment writes and the
    unknown-IDs dispatch path without inserting catalog rows itself.
    """
    if not day_rows:
        return {"history_rows": 0, "enriched": 0, "dispatched": 0}
    try:
        result = scout.upsert_rotrends_snapshot(day_rows)
        _note_store_totals(result)
        return result
    except Exception as exc:  # never fail the sweep on storage
        log.warning("rotrends store failed: %s", exc)
        return {"history_rows": 0, "enriched": 0, "dispatched": 0}


# --------------------------------------------------------------------------- #
# Sweeps
# --------------------------------------------------------------------------- #
def sweep_tiers(
    scout: RobloxPlatformScout,
    tiers: Tuple[int, ...] = (5, 6, 7),
    day: Optional[str] = None,
    limit: int = 0,
    client: Optional[RotrendsClient] = None,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """One daily sweep: fetch the RECENT window for every stale T5–T7 game.

    Window = the 3 days ending `day` (default: yesterday). A 3-day window
    is robust against days where the snapshot lags an extra day for some
    games, at zero extra request cost. Only rows the DB doesn't already
    have survive dedupe (INSERT OR IGNORE on (universe_id, ts) + source).

    Returns the summary dict live_sync prints; the keys match Phase 5's
    per-source summary contract.
    """
    client = client or RotrendsClient()
    report = progress_cb or (lambda p, m: None)
    global _sweep_store_totals
    _sweep_store_totals = {}   # fresh accumulator per pass
    today = time.strftime("%Y-%m-%d", time.gmtime())
    day = day or today
    # 3-day trailing window ending `day`: snapshots lag ~1 day, and some
    # games lag 2 — one window covers all of them.
    start = time.strftime(
        "%Y-%m-%d", time.gmtime(time.mktime(time.strptime(day, "%Y-%m-%d")) - 2 * 86400)
    )
    due = select_due_universe_ids(scout, tiers=tiers, limit=limit)
    if ROTRENDS_SWEEP_MAX_GAMES and len(due) > ROTRENDS_SWEEP_MAX_GAMES:
        due = due[: ROTRENDS_SWEEP_MAX_GAMES]
    if not due:
        return {
            "due": 0, "fetched": 0, "ok": 0, "errored": 0,
            "history_rows": 0, "enriched": 0, "dispatched": 0,
            "window": f"{start}..{day}", "tiers": tiers, **client.stats,
        }
    report(0.05, f"Rotrends sweep: {len(due):,} due games, window {start}..{day}")
    stats_counter = {"ok": 0, "errored": 0, "fetched_rows": 0}
    counter_lock = threading.Lock()

    def _fetch_one(uid: int) -> Tuple[int, List[Dict[str, Any]], Optional[str]]:
        try:
            rows = client.fetch_metrics_window(uid, start, day)
            with counter_lock:
                stats_counter["ok"] += 1
                stats_counter["fetched_rows"] += len(rows)
            return uid, rows, None
        except RotrendsFetchError as exc:
            with counter_lock:
                stats_counter["errored"] += 1
            log.debug("rotrends fetch failed: %s", exc)
            return uid, [], str(exc)

    # Store in batches as futures complete (a 23k-game sweep must not hold
    # the whole payload in RAM); batched by DONE order via a little list.
    pending: Dict[str, Dict[str, Any]] = {}
    pending_lock = threading.Lock()

    def _flush(min_size: int = 200, force: bool = False) -> None:
        with pending_lock:
            if force or len(pending) >= min_size:
                batch = dict(pending)
                pending.clear()
            else:
                return
        if batch:
            store_day_snapshots(scout, batch)

    with ThreadPoolExecutor(max_workers=ROTRENDS_THREADS) as pool:
        futures = [pool.submit(_fetch_one, uid) for uid in due]
        for n, fut in enumerate(as_completed(futures), start=1):
            uid, rows, err = fut.result()
            if not rows:
                _flush()
                if progress_cb and n % 500 == 0:
                    report(
                        min(0.95, n / max(1, len(futures))),
                        f"rotrends sweep {n:,}/{len(futures):,} · "
                        f"{stats_counter['ok']:,} ok · {stats_counter['errored']:,} err",
                    )
                continue
            # Keep the freshest row per universe (the sweep's target day):
            freshest = max(rows, key=lambda r: r["process_date"])
            with pending_lock:
                pending[str(uid)] = {
                    "universe_id": uid,
                    "ts": freshest["process_date"],
                    "ccu": freshest["ccu"],
                    "like_ratio": freshest.get("like_ratio"),
                    "earning_rank": freshest.get("earning_rank"),
                }
            if n % 200 == 0 or n == len(futures):
                _flush()
            if progress_cb and n % 500 == 0:
                report(
                    min(0.95, n / max(1, len(futures))),
                    f"rotrends sweep {n:,}/{len(futures):,} · "
                    f"{stats_counter['ok']:,} ok · {stats_counter['errored']:,} err",
                )
    _flush(force=True)
    result: Dict[str, Any] = {
        "due": len(due),
        "fetched": stats_counter["fetched_rows"],
        "ok": stats_counter["ok"],
        "errored": stats_counter["errored"],
        "window": f"{start}..{day}",
        "tiers": list(tiers),
        **client.stats,
    }
    result["history_rows"] = _sweep_store_totals.get("history_rows", 0)
    result["enriched"] = _sweep_store_totals.get("enriched", 0)
    result["dispatched"] = _sweep_store_totals.get("dispatched", 0)
    report(1.0, "Rotrends sweep done: "
                f"{result['ok']:,}/{result['due']:,} games · "
                f"{result['history_rows']:,} history rows")
    return result


def _note_store_totals(totals: Dict[str, int]) -> None:
    global _sweep_store_totals
    _sweep_store_totals = {
        k: _sweep_store_totals.get(k, 0) + int(totals.get(k, 0))
        for k in ("history_rows", "enriched", "dispatched")
    }


# Module-level accumulator so batched storage totals flow into the sweep's
# summary without threading a mutable dict through the executor.
_sweep_store_totals: Dict[str, int] = {}


def backfill_shallow(
    scout: RobloxPlatformScout,
    days: int = ROTRENDS_BACKFILL_DAYS,
    max_games: int = ROTRENDS_BACKFILL_MAX_GAMES,
    max_samples: int = 5,
    client: Optional[RotrendsClient] = None,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """90-day window for shallow games (≤ max_samples history rows).

    One pass = max_games universes (default 500 ≈ 1.5 min at 8 req/s) —
    the caller cadences this until the 8,930-shallow backlog drains.
    Backfilled rows carry source='rotrends_daily' at each snapshot's
    process_date; the pk dedupe makes reruns exact no-ops.
    """
    client = client or RotrendsClient()
    report = progress_cb or (lambda p, m: None)
    global _sweep_store_totals
    _sweep_store_totals = {}   # fresh accumulator per pass
    end_date = time.strftime("%Y-%m-%d", time.gmtime())
    start_date = time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() - int(days) * 86400)
    )
    targets = select_shallow_universe_ids(
        scout, max_samples=max_samples, limit=max_games
    )
    if not targets:
        return {"due": 0, "ok": 0, "errored": 0, "history_rows": 0,
                "enriched": 0, "dispatched": 0, "window": f"{start_date}..{end_date}",
                **client.stats}
    report(0.05, f"rotrends backfill: {len(targets):,} shallow games, {days}d window")
    ok = err = rows_n = 0
    lock = threading.Lock()
    results: Dict[str, Dict[str, Any]] = {}

    def _fetch(uid: int) -> List[Dict[str, Any]]:
        try:
            rows = client.fetch_metrics_window(uid, start_date, end_date)
            with lock:
                ok += 1; rows_n += len(rows)
            return rows
        except RotrendsFetchError:
            with lock:
                err += 1
            return []

    with ThreadPoolExecutor(max_workers=ROTRENDS_THREADS) as pool:
        futures = [pool.submit(_fetch, uid) for uid in targets]
        for i, fut in enumerate(as_completed(futures), start=1):
            rows = fut.result()
            for r in rows:
                results[str(r["universe_id"])] = {
                    "universe_id": r["universe_id"],
                    "ts": r["process_date"],
                    "ccu": r["ccu"],
                    "like_ratio": r.get("like_ratio"),
                    "earning_rank": r.get("earning_rank"),
                }
            if i % 200 == 0 or i == len(futures):
                totals = store_day_snapshots(scout, dict(results))
                _note_store_totals(totals)
                results.clear()
    result = {
        "due": len(targets), "fetched": rows_n, "ok": ok, "errored": err,
        "window": f"{start_date}..{end_date}", **client.stats,
    }
    result.update(_sweep_store_totals)
    return result


def backfill_new_discoveries(
    scout: RobloxPlatformScout,
    universe_ids: List[int],
    days: int = ROTRENDS_BACKFILL_DAYS,
    client: Optional[RotrendsClient] = None,
) -> Dict[str, Any]:
    """90-day trend backfill for JUST-discovered games (expander hook).

    Called from the discovery-queue drain's qualified path: a game that
    qualified into the catalog gets its full trend chart the same tick —
    no waiting for the daily sweep to reach it. Fail-open per game.
    """
    if not universe_ids:
        return {"due": 0, "ok": 0, "errored": 0, "history_rows": 0,
                "enriched": 0, "dispatched": 0}
    client = client or RotrendsClient(rps=max(2.0, ROTRENDS_RPS / 4))
    end_date = time.strftime("%Y-%m-%d", time.gmtime())
    start_date = time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() - int(days) * 86400)
    )
    results: Dict[str, Dict[str, Any]] = {}
    ok = err = 0
    for uid in universe_ids[:50]:  # safety bound: only freshly qualified games
        try:
            rows = client.fetch_metrics_window(int(uid), start_date, end_date)
            ok += 1
        except RotrendsFetchError:
            err += 1
            continue
        for r in rows:
            results[str(r["universe_id"])] = {
                "universe_id": r["universe_id"],
                "ts": r["process_date"],
                "ccu": r["ccu"],
                "like_ratio": r.get("like_ratio"),
                "earning_rank": r.get("earning_rank"),
            }
    totals = store_day_snapshots(scout, results)
    return {
        "due": len(universe_ids[:50]), "ok": ok, "errored": err,
        "window": f"{start_date}..{end_date}",
        "history_rows": totals.get("history_rows", 0),
        "enriched": totals.get("enriched", 0),
        "dispatched": totals.get("dispatched", 0),
        **client.stats,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Rotrends daily sweep for the cold tail (T5–T7)."
    )
    parser.add_argument(
        "--backfill", type=int, default=0,
        help="Also run the shallow-game 90-day backfill (arg = max games, 0 = off)."
    )
    parser.add_argument(
        "--backfill-only", action="store_true",
        help="Skip the daily sweep; only run the shallow backfill."
    )
    parser.add_argument(
        "--dispatch", action="store_true",
        help="Enqueue never-hydrated cold games into discovery_queue."
    )
    parser.add_argument(
        "--day", type=str, default=None, help="Sweep target day YYYY-MM-DD (default: today)."
    )
    parser.add_argument(
        "--db", type=str, default=None, help="Optional DB path override (default: repo catalog)."
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db_path = args.db or os.path.join(APP_DIR, "rbx_scout.db")
    scout = RobloxPlatformScout(db_path=db_path)
    started = time.time()
    result: Dict[str, Any] = {}
    if not args.backfill_only:
        result["sweep"] = sweep_tiers(scout, day=args.day)
        print(
            f"sweep  : {result['sweep']['ok']:,}/{result['sweep']['due']:,} games · "
            f"{result['sweep'].get('history_rows', 0):,} history rows · "
            f"{result['sweep'].get('errored', 0):,} errored · "
            f"{result['sweep'].get('client_429s', 0)} penalty boxes · "
            f"({time.time() - started:.0f}s)"
        )
    if args.backfill or args.backfill_only:
        n = args.backfill or ROTRENDS_BACKFILL_MAX_GAMES
        result["backfill"] = backfill_shallow(scout, max_games=n)
        print(
            f"backfill: {result['backfill']['ok']:,}/{result['backfill']['due']:,} games · "
            f"{result['backfill'].get('history_rows', 0):,} history rows "
            f"({time.time() - started:.0f}s total)"
        )
    if args.dispatch:
        ids = select_unknown_cold_ids(scout)
        n = scout._enqueue_discovery((uid, "rotrends_sweep", 4) for uid in ids)
        print(f"dispatch: {n:,} cold IDs re-enqueued into discovery_queue")

    if not result:
        print("nothing to do: pass --backfill <N> or run the default sweep")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
