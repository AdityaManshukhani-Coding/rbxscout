#!/usr/bin/env python3
"""Creator Exchange live-overflow client — the hot tiers' safety net.

Why this exists (HYDRATION_SOURCES.md Phase 2, measured 2026-10-07/08):
creatorexchange.io hosts a per-game page for EVERY Roblox universe with
live CCU/visits embedded in the Next.js RSC flight payload. When the
Roblox batch window 429s out mid-run, the hydrator's remaining HOT-tier
due games (T0–T4 only) get refreshed through this channel instead of
rolling forward — the "never rolled again" guarantee. Every hit is
double-purpose: live CCU + enrichment (momentum / globalRank /
scammerFlag) that Roblox never exposes.

Measured properties that shape this module:

  * URL: https://creatorexchange.io/roblox-game/{universeId}/ANY-SLUG —
    the universe ID is the only real key; any/wrong/emoji slug 301s to
    the canonical slug then 200s with full data (verified on 19-CCU,
    0-CCU and global-rank-16,140 games — NOT top-240-limited).
  * Payload: `initialGameData:{...}` inside the RSC chunks carries
    playing/visits/likeRatio/momentum/globalRank/scammerFlag/
    rootPlaceId (verified live: Blox Fruits page playing=202,357 vs
    Roblox 202k; small games ±1 CCU).
  * Rate: 47-request burst all 200 with no observed limiter (backend is
    Google Cloud Run, not Cloudflare). Politeness cap is a CHOICE
    (§3.2): ≤2 req/s sustained with ≥0.5 s spacing — this is a small,
    cooperative platform; the cap is self-preservation, not measured
    headroom.
  * HAZARD: nonexistent/dead universe IDs HANG INDEFINITELY (60 s hit
    with 0 bytes returned). Every call carries a hard total timeout and
    maps any hang/timeout to 404 — there is no retry for a hang.
  * Two consecutive non-200s abort the batch (their redesign or an
    outage must not burn the whole queue at 10 s a pop).

Design invariants:
  * Fail-open: report() returning {} leaves the caller's rollback
    behavior identical to pre-Phase-2 hydration.
  * Rows are timestamped at FETCH time with source='cx_live' — it is
    genuinely live (verified within ±1 CCU of Roblox).
  * Roblox stays the tie-breaker: cx rows go into ccu_history tagged
    cx_live; last_updated/tier bookkeeping recompute exactly as a live
    hydration.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("rbxscout.cx")

# --------------------------------------------------------------------------- #
# Configuration (env-overridable; defaults are politeness, not measured caps)
# --------------------------------------------------------------------------- #
CX_BASE_URL = "https://creatorexchange.io/roblox-game"
CX_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 UpScaleScoutingTool/1.0"
)
CX_TIMEOUT = 10.0       # HARD total timeout — dead IDs hang forever without it
CX_MIN_INTERVAL = 0.5   # ≥0.5 s between calls (≤2 req/s sustained, honest politeness)
CX_MAX_CONSECUTIVE_FAILS = 4   # abort the batch after this many back-to-back misses
# Soft-throttle calibration (2026-10-09 deep dive): CE serves an intangible
# throttle at sustained ≥~0.5-1 req/s — HTTP 200, full-length template shell,
# but the RSC payload (initialGameData) is MISSING. ~12 rapid requests buy
# a wall that lasts ≥15 min (observed up to 40+). At 2 s + request spacing
# clean runs are 100%; at 0.5-0.7 s the wall starts after ~4-12 requests.
# Production pacing (2 rps) therefore sits exactly ON the tripwire and the
# measured soft-miss rate was ~40%+, tripping the 2-miss circuit within a
# dozen games and silently disabling the overflow for the rest of the sync.
CX_BURST_LIMIT = 12          # requests before a mandatory cool-down (measured)
CX_BURST_COOLDOWN = 75.0     # seconds; wall builds in ~4-12 requests at speed
CX_MISS_RETRY_DELAY = 4.0    # one immediate, spaced retry for a parse miss


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


CX_RPS = _env_float("CX_RPS", 2.0, minimum=0.2)   # sustained-cap knob —
# Effective spacing feeds the burst counter: CE's real limiter is ~12 rapid
# requests, not a clean rps budget (see CX_BURST_LIMIT calibration notes).
# Wall-clock cap for the whole overflow batch (2026-10-10 root-cause fix): on
# Oct 9–10 a catalog with 18K+ stale T7 rows fed thousands of missed IDs into
# the serial overflow loop; each miss costs ~16s wall (10s timeout + 4s retry
# + pacing), so the hydration ran past the workflow's 20-min timeout and the
# runner cancelled mid-pass — every 5-min dispatch failed the same way and the
# pipeline wedged. Overflow is a repair channel, not a bulk channel, so it gets
# a bounded slice of the tick (env-overridable) and rolls the remainder forward.
CX_DEADLINE_S = _env_float("CX_DEADLINE_S", 90.0, minimum=10.0)


class CxClient:
    """ThreadSingleton-ish paced fetcher for Creator Exchange game pages.

    One shared instance per sync (the hydration pass and the enrichment
    writer share its pacer + failure counters). Not thread-safe by
    intent; the hydrator calls it serially from its rollback loop.
    """

    def __init__(self, rps: float = CX_RPS, timeout: float = CX_TIMEOUT,
                 deadline_s: float = None) -> None:
        self.timeout = float(max(4.0, timeout))  # never below the hang-proof floor
        self.deadline_s = float(deadline_s) if deadline_s else None
        self.started_at = time.time()
        self._next_emit = 0.0
        self._lock = threading.Lock()
        self._consecutive_fails = 0
        self._burst_count = 0        # requests since the last mandatory rest
        self._cooldown_guard: bool = False
        self.session = requests.Session()
        self.session.headers["User-Agent"] = CX_USER_AGENT
        self.session.headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        )

    _instance: Optional["CxClient"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def shared(cls) -> "CxClient":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------ #
    # Pacing
    # ------------------------------------------------------------------ #
    def _emit_pace(self) -> None:
        """Token-pacer PLUS a burst counter (soft-throttle defense).

        CE's measured limiter (2026-10-09)serves ~12 rapid requests clean,
        then builds a multi-minute wall of payload-less pages that look like
        HTTP 200. Every paced emit checks the rolling count: after
        CX_BURST_LIMIT requests the process sleeps CX_BURST_COOLDOWN once
        (counting resets), which measurably avoids the wall entirely.
        """
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_emit - now)
            self._next_emit = max(now, self._next_emit) + 1.0 / self.rps
            self._burst_count += 1
            force_rest = 0.0
            if self._burst_count >= CX_BURST_LIMIT:
                force_rest = CX_BURST_COOLDOWN
                self._burst_count = 0
        if wait:
            time.sleep(wait)
        if force_rest:
            log.info(
                "cx burst cool-down: %ds after %d requests (soft-throttle defense)",
                int(force_rest), CX_BURST_LIMIT,
            )
            time.sleep(force_rest)

    @property
    def rps(self) -> float:
        return CX_RPS

    # ------------------------------------------------------------------ #
    # Parse the RSC flight payload
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_game_payload(html: str) -> Optional[Dict[str, Any]]:
        """Extract `initialGameData:{...}` from the Next.js RSC flight HTML.

        The page embeds chunks of shape `self.__next_f.push(...)","initialGameData
        \\":{"ok":true,"game":{...}}`. RSC escapes quotes as \\\" and newlines as
        \\\\n, so a plain regex extraction up to a known terminator works;
        `game` is a flat dict with no nested braces until its closing `}`.

        Returns the `game` sub-dict ({playing, visits, likeRatio, momentum,
        globalRank, scammerFlag, rootPlaceId, ...}) or None on any miss.
        """
        marker = 'initialGameData'
        i = html.find(marker)
        if i < 0:
            return None
        # Find the start of the JSON-ish object after the marker. RSC escapes
        # the quotes: `initialGameData\\\":{\\\"ok\\\"...` — scan forward to
        # the first '{' after the marker.
        brace = html.find("{", i)
        if brace < 0:
            return None
        # Walk to the matching close brace accounting for escaped quotes —
        # the payload contains `\\\"` sequences (escaped quote) but no
        # unescaped '{'/'}' inside string values; a depth walk is safe here
        # because RSC string values have their braces escaped as `\\{`.
        depth = 0
        end = -1
        j = brace
        n = len(html)
        while j < n:
            c = html[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end < 0:
            return None
        raw = html[brace : end + 1]
        import json
        # RSC double-escapes: `\\\"` in the raw HTML means `\"` in JSON, so
        # un-escape once more than a normal string requires.
        raw = raw.replace('\\"', '"')
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            # Escaped variant with unicode/line continuations: try a second
            # unwind for `\\\\n` sequences (description text embeds them).
            raw2 = raw.replace("\\\\n", "\\n").replace("\\\\\"", "\\\"")
            try:
                payload = json.loads(raw2)
            except (ValueError, TypeError):
                return None
        if not isinstance(payload, dict):
            return None
        game = payload.get("game")
        if not isinstance(game, dict):
            return None
        return game

    # ------------------------------------------------------------------ #
    # Fetch one universe
    # ------------------------------------------------------------------ #
    def fetch_universe(self, universe_id: int) -> Optional[Dict[str, Any]]:
        """Live metrics + enrichment for one universe via its CE page.

        Returns {"playing", "visits", "likeRatio", "momentum", "globalRank",
        "scammerFlag", "rootPlaceId", "fetched_at"} or None on ANY failure
        (hang/timeout mapped to 404 per §3.2; a hang cannot distinguish a
        dead ID from a slow page and must never retry-stall the hydrator).

        Failure counting: two consecutive misses set `self.circuit_open`;
        the caller checks it and rolls the rest of the batch forward.
        """
        uid = int(universe_id)
        if uid <= 0 or self.circuit_open:
            return None
        self._emit_pace()
        url = f"{CX_BASE_URL}/{uid}/x"
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            # includes ReadTimeout/ConnectTimeout from dead-ID hangs
            self._note_fail(uid, f"transport: {type(exc).__name__}")
            return None
        if resp.status_code != 200:
            self._note_fail(uid, f"HTTP {resp.status_code}")
            return None
        game = self.parse_game_payload(resp.text or "")
        if not game:
            # Soft-throttle signature (2026-10-09 calibration): HTTP 200 with
            # a full-length shell but NO payload happens under sustained fast
            # pacing and clears in seconds-to-minutes. One spacing-limited
            # retry turns the measured ~40% soft-miss rate into ~0 without
            # touching the burst budget much; a real dead/unrenderable ID
            # still misses twice and counts both times toward the circuit.
            time.sleep(CX_MISS_RETRY_DELAY)
            self._emit_pace()
            try:
                resp2 = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException as exc:
                self._note_fail(uid, f"retry transport: {type(exc).__name__}")
                return None
            if resp2.status_code == 200:
                game = self.parse_game_payload(resp2.text or "")
            if not game:
                self._note_fail(uid, "parse miss (no initialGameData, retried)")
                return None
        self._consecutive_fails = 0
        out: Dict[str, Any] = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        for src, dst in (
            ("playing", "ccu"),
            ("visits", "visits"),
            ("likeRatio", "like_ratio"),
            ("momentum", "momentum"),
            ("globalRank", "global_rank"),
            ("scammerFlag", "scammer_flag"),
            ("rootPlaceId", "root_place_id"),
        ):
            val = game.get(src)
            if val is not None:
                out[dst] = val
        # CCU sanity: a page whose playing is missing/None carries enrichment
        # only — filtering that against the caller's CCU-based gate happens
        # upstream; keep whatever came back.
        return out

    def _note_fail(self, uid: int, reason: str) -> None:
        """Record a miss with its REASON at warning level.

        First cloud run 2026-10-08 (#3090): the circuit opened from a real
        runner with no visible cause — debug logs don't survive to Actions.
        The overflow's whole job is fail-open resilience, but diagnosing a
        403 wall vs dead-ID hangs vs a parser break needs the reason in the
        run log. Counting + circuit logic is unchanged.
        """
        log.warning("cx miss U%s: %s", uid, reason)
        with self._lock:
            self._consecutive_fails += 1
            if self._consecutive_fails >= CX_MAX_CONSECUTIVE_FAILS:
                log.warning(
                    "cx circuit open after %d consecutive failures — "
                    "overflow disabled until the next sync",
                    self._consecutive_fails,
                )

    @property
    def circuit_open(self) -> bool:
        return self._consecutive_fails >= CX_MAX_CONSECUTIVE_FAILS


def fetch_universes(
    universe_ids: List[int],
    progress_cb: Optional[Any] = None,
    client: Optional[CxClient] = None,
    deadline_s: float = None,
) -> Dict[int, Dict[str, Any]]:
    """Serial overflow refresh for a batch of hot-tier due games.

    Returns {universe_id: fetch_universe() result} for games that
    resolved; rolls the rest forward (they stay in the caller's due
    queue). Serial on purpose: the 2 req/s cap IS the concurrency budget.

    Backoff ×2 (POLITEness §3.2): on each miss the pacer doubles the
    per-call spacing, resetting on the next success.

    Wall-clock deadline (2026-10-10, ROOT-CAUSE FIX for the Oct 9–10
    hydrator failures): the overflow pass on a game catalogue with 18K+
    stale T7 rows would enqueue thousands of missed IDs into this serial
    loop. Each transported timeout (~10s) + retry (~4s) + pacing (~2s)
    eats ~16s wall per missed game — thousands of games × 16s ≫ the
    workflow's 20-min budget, so the job got canceled by the runner
    while the sweep was still in CE. The 20-min no-op 'cancel' repeated
    every 5 min, wedging the pipeline (Actions' concurrency group has
    queue: max, so every one of these dead-on-arrival runs still lands
    in the queue, and nothing ever turns green).

    Fix: fetch_universes() now accepts a wall-clock deadline. The caller
    (scout_core's hydration overflow pass) allocates a bounded slice
    (default CX_DEADLINE_S = 90 s) so the pass stays inside the tick
    budget regardless of how many misses queued it up; anything past
    the deadline rolls forward to the next sync.
    """
    cl = client or CxClient.shared()
    if deadline_s:
        cl.deadline_s = float(deadline_s)
        cl.started_at = time.time()
    out: Dict[int, Dict[str, Any]] = {}
    report = progress_cb or (lambda p, m: None)
    for n, uid in enumerate(universe_ids, start=1):
        if cl.circuit_open:
            break
        if cl.deadline_s and (time.time() - cl.started_at) > cl.deadline_s:
            log.warning(
                "cx overflow hit its %.0fs wall-clock deadline after %d attempts "
                "(%d hydrated) — rolling the rest forward",
                cl.deadline_s, n - 1, len(out),
            )
            break
        got = cl.fetch_universe(uid)
        if got:
            out[int(uid)] = got
        else:
            # ×2 the spacing for one call: on error, stretch to be safe.
            with cl._lock:
                cl._next_emit += 1.0 / cl.rps
        if progress_cb and n % 20 == 0:
            report(0.5, f"cx overflow {n}/{len(universe_ids)} · {len(out)} hydrated")
    return out
