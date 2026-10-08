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
CX_MAX_CONSECUTIVE_FAILS = 2   # abort the batch after this many back-to-back misses


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


CX_RPS = _env_float("CX_RPS", 2.0, minimum=0.2)   # sustained-cap knob


class CxClient:
    """ThreadSingleton-ish paced fetcher for Creator Exchange game pages.

    One shared instance per sync (the hydration pass and the enrichment
    writer share its pacer + failure counters). Not thread-safe by
    intent; the hydrator calls it serially from its rollback loop.
    """

    _instance: Optional["CxClient"] = None
    _instance_lock = threading.Lock()

    def __init__(self, rps: float = CX_RPS, timeout: float = CX_TIMEOUT) -> None:
        self.timeout = float(max(4.0, timeout))  # never below the hang-proof floor
        self._next_emit = 0.0
        self._lock = threading.Lock()
        self._consecutive_fails = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = CX_USER_AGENT
        self.session.headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        )

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
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_emit - now)
            self._next_emit = max(now, self._next_emit) + 1.0 / self.rps
        if wait:
            time.sleep(wait)

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
            self._note_fail(uid, "parse miss (no initialGameData)")
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
) -> Dict[int, Dict[str, Any]]:
    """Serial overflow refresh for a batch of hot-tier due games.

    Returns {universe_id: fetch_universe() result} for games that
    resolved; rolls the rest forward (they stay in the caller's due
    queue). Serial on purpose: the 2 req/s cap IS the concurrency budget.

    Backoff ×2 (POLITEness §3.2): on each miss the pacer doubles the
    per-call spacing, resetting on the next success.
    """
    cl = client or CxClient.shared()
    out: Dict[int, Dict[str, Any]] = {}
    report = progress_cb or (lambda p, m: None)
    for n, uid in enumerate(universe_ids, start=1):
        if cl.circuit_open:
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
