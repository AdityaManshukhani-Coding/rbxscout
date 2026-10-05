#!/usr/bin/env python3
"""Atlas Dev prufer metric-series client — the free stats-refresh accelerator.

Why this exists (ATLAS_PRUFER_HYDRATOR_PLAN.md, validated 2026-10-04):
atlasdev.gg exposes an unadvertised JSON API their own dashboard uses:

    GET https://atlasdev.gg/api/prufer/universe-metric-series/{universeId}
        ?days=N&bucket=1d|1h|30m

It returns an hourly-grain series of playing/visits/favorited/ratingPercent
going back to 2026-08-19 (their tracker start; days=90 is the hard cap and
days>90 silently degrades). Measured properties that shape this module:

  * 96% of the catalog has series data, but enrollment is LUMPY: Atlas's
    sampler drops games mid-history and never resumes them. The last point
    can be weeks old (Steal An Egg: dropped 2026-08-24 right before its
    blowup). So data must pass a FRESHNESS GATE before it is trusted.
  * atlasdev.gg 403s GitHub Actions runner IPs at Cloudflare's edge
    (verified 7/7 attempts across 2 runners) — but a free public proxy
    pool passes at a 9.0% yield (195/2164, verified full-pool from a real
    runner in 206s). The pool must be re-validated EVERY run: the working
    set churns within hours.
  * Free proxies are untrusted middlemen: responses are validated against
    a strict schema + sanity rules and a single malformed byte discards the
    game's payload (the game simply stays due for the Roblox hydrator).

Design invariants (same as the whole pipeline):
  * Fail-open: any prufer failure degrades to today's behavior. Nothing in
    the Roblox hydrator path depends on this module.
  * Never send credentials through public proxies (this API needs none).
  * Roblox remains the source of truth; prufer stats only pre-refresh rows.

Usage (inside the hydrator pass):
    from prufer import PruferClient
    client = PruferClient()
    if client.ensure_pool(min_size=10):
        stats = client.fresh_stats(universe_id)   # dict or None
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration (env-overridable; defaults match the plan's measured values)
# --------------------------------------------------------------------------- #
PRUFER_BASE_URL = "https://atlasdev.gg/api/prufer/universe-metric-series"
PRUFER_USER_AGENT = "UpScaleScoutingTool/1.0 (Roblox game discovery; contact via repo)"

DEFAULT_PROXY_LISTS = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]

def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default

def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _env_list(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    return [p.strip() for p in re.split(r"[,;\n]+", raw) if p.strip()]


PRUFER_ENABLED = os.environ.get("PRUFER_ENABLED", "1") not in ("0", "false", "no")
PRUFER_REQUESTS_PER_TICK = _env_int("PRUFER_REQUESTS_PER_TICK", 300, minimum=0)
PRUFER_MAX_HOURS = _env_float("PRUFER_MAX_HOURS", 2.0)          # freshness gate
PRUFER_POOL_VALIDATION_SAMPLE = _env_int("PRUFER_POOL_VALIDATION_SAMPLE", 1400, minimum=100)
PRUFER_POOL_MIN_SIZE = _env_int("PRUFER_POOL_MIN_SIZE", 10, minimum=1)
PRUFER_VALIDATION_THREADS = _env_int("PRUFER_VALIDATION_THREADS", 60, minimum=1)
PRUFER_FETCH_THREADS = _env_int("PRUFER_FETCH_THREADS", 50, minimum=1)
PRUFER_TIMEOUT = _env_int("PRUFER_TIMEOUT", 8, minimum=2)
PRUFER_HISTORY_DAYS = min(_env_int("PRUFER_HISTORY_DAYS", 2, minimum=1), 90)
PRUFER_PROXY_LISTS = _env_list("PRUFER_PROXY_LISTS", DEFAULT_PROXY_LISTS)

# Warmup proxy used to validate the pool against the real endpoint (any
# well-known universe works; Blox Fruits is stable and always tracked).
_VALIDATION_CANARY_UID = 994732206

# Series sanity bounds (tamper detection for untrusted proxy paths).
_MAX_REASONABLE_CCU = 12_000_000          # Roblox all-time record headroom
_MAX_REASONABLE_VISITS = 5_000_000_000_000  # 5T — ~77x Roblox's biggest game


class PruferClient:
    """Validated fetcher for the Atlas Dev prufer metric-series API.

    One client per sync. The proxy pool is process-local and validated
    lazily on the first refresh batch (never at import — tests import this
    module without touching the network).
    """

    def __init__(self) -> None:
        self._pool: List[str] = []
        self._pool_lock = threading.Lock()
        self._pool_cursor = 0
        self._pool_attempted = False

    # ------------------------------------------------------------------ #
    # Proxy pool
    # ------------------------------------------------------------------ #
    @property
    def pool_size(self) -> int:
        with self._pool_lock:
            return len(self._pool)

    def _proxy_dicts(self, proxy: Optional[str]) -> Dict[str, str]:
        if not proxy:
            return {}
        return {"http": f"http://{proxy}", "https": f"http://{proxy}"}

    def _validate_one(self, proxy: str) -> Optional[str]:
        """True only if this proxy returns a 200 + plausible series payload."""
        try:
            resp = requests.get(
                f"{PRUFER_BASE_URL}/{_VALIDATION_CANARY_UID}",
                params={"days": 1, "bucket": "1d"},
                headers={"User-Agent": PRUFER_USER_AGENT},
                proxies=self._proxy_dicts(proxy),
                timeout=PRUFER_TIMEOUT,
            )
            if resp.status_code == 200 and '"series"' in resp.text:
                return proxy
        except requests.RequestException:
            pass
        return None

    def ensure_pool(self, min_size: Optional[int] = None) -> bool:
        """Validate a fresh proxy pool (parallel). Safe to call repeatedly:
        revalidation happens at most once per client unless force=True.

        Every hydrator tick builds a NEW client, so the plan's rule — the
        working set is never cached between runs — is enforced by design.
        """
        if not PRUFER_ENABLED:
            return False
        want = int(min_size if min_size is not None else PRUFER_POOL_MIN_SIZE)
        with self._pool_lock:
            if self._pool_attempted:
                return len(self._pool) >= want
            self._pool_attempted = True

        proxies: List[str] = []
        for list_url in PRUFER_PROXY_LISTS:
            try:
                resp = requests.get(list_url, timeout=15)
                if resp.status_code == 200:
                    proxies += [p.strip() for p in resp.text.splitlines() if p.strip()]
            except requests.RequestException as exc:
                log.warning("prufer proxy list fetch failed (%s): %s", list_url, exc)
        proxies = list(dict.fromkeys(proxies))
        random.shuffle(proxies)
        sample = proxies[: PRUFER_POOL_VALIDATION_SAMPLE]
        if not sample:
            log.warning("prufer: no proxies available; pass will fall back to Roblox")
            return False

        good: List[str] = []
        with ThreadPoolExecutor(PRUFER_VALIDATION_THREADS) as ex:
            for result in ex.map(self._validate_one, sample):
                if result:
                    good.append(result)
        with self._pool_lock:
            self._pool = good
        log.info("prufer pool validated: %d/%d working", len(good), len(sample))
        return len(good) >= want

    def _next_proxy(self) -> Optional[str]:
        with self._pool_lock:
            if not self._pool:
                return None
            proxy = self._pool[self._pool_cursor % len(self._pool)]
            self._pool_cursor += 1
            return proxy

    def _retire_proxy(self, proxy: Optional[str]) -> None:
        """Drop a proxy that just misbehaved so later requests don't reuse it."""
        if not proxy:
            return
        with self._pool_lock:
            try:
                self._pool.remove(proxy)
            except ValueError:
                pass

    # ------------------------------------------------------------------ #
    # Series fetch + strict validation
    # ------------------------------------------------------------------ #
    def fetch_series(
        self, universe_id: int, days: int = 2, bucket: str = "1h"
    ) -> Optional[List[Dict[str, Any]]]:
        """One validated series fetch through the pool. None on any failure.

        Retry-once semantics: a failed proxy is retired and one more attempt
        is made through a different proxy, then the game is given up on (it
        stays due for the Roblox hydrator — fail-open).
        """
        if not PRUFER_ENABLED:
            return None
        uid = int(universe_id)
        if uid <= 0:
            return None
        days = max(1, min(int(days), 90))
        if bucket not in ("1d", "1h", "30m"):
            bucket = "1h"
        for _attempt in range(2):
            proxy = self._next_proxy()
            if proxy is None:
                return None
            try:
                resp = requests.get(
                    f"{PRUFER_BASE_URL}/{uid}",
                    params={"days": days, "bucket": bucket},
                    headers={"User-Agent": PRUFER_USER_AGENT},
                    proxies=self._proxy_dicts(proxy),
                    timeout=PRUFER_TIMEOUT + 4,
                )
                if resp.status_code != 200:
                    if resp.status_code in (403, 429):
                        # Proxy IP burned or rate-limited: stop using it.
                        self._retire_proxy(proxy)
                    continue
                series = self._validate_series_payload(resp)
                if series is not None:
                    return series
                # Body tampered/garbage: the proxy is either broken or hostile.
                self._retire_proxy(proxy)
                continue
            except (requests.RequestException, ValueError):
                self._retire_proxy(proxy)
                continue
        return None

    @staticmethod
    def _validate_series_payload(resp: requests.Response) -> Optional[List[Dict[str, Any]]]:
        """Strict schema + sanity validation for an UNTRUSTED response path.

        Returns the series list, or None when anything is off. Every point
        must have an ISO-ish bucket and integer metrics inside plausible
        bounds; None metric values (observed in stale series) discard the
        whole payload — a partially-valid series is worse than no series
        because it invites silent corruption into ccu_history.
        """
        try:
            payload = resp.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        series = payload.get("series")
        if not isinstance(series, list):
            return None
        bucket_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        cleaned: List[Dict[str, Any]] = []
        for point in series:
            if not isinstance(point, dict):
                return None
            bucket = point.get("bucket")
            if not isinstance(bucket, str) or not bucket_re.match(bucket):
                return None
            playing = point.get("playing")
            visits = point.get("visits")
            if not isinstance(playing, int) or not isinstance(visits, int):
                return None  # nulls/floats from stale or tampered series
            if playing < 0 or playing > _MAX_REASONABLE_CCU:
                return None
            if visits < 0 or visits > _MAX_REASONABLE_VISITS:
                return None
            cleaned.append(
                {
                    "bucket": bucket,
                    "playing": playing,
                    "visits": visits,
                    "favorited": point.get("favorited")
                    if isinstance(point.get("favorited"), int)
                    else None,
                    "ratingPercent": point.get("ratingPercent")
                    if isinstance(point.get("ratingPercent"), (int, float))
                    else None,
                }
            )
        return cleaned or None

    @staticmethod
    def _parse_bucket_ts(bucket: str) -> Optional[float]:
        # Buckets are UTC ("...Z"); timegm parses them as UTC. mktime would
        # silently reinterpret as local time and skew the freshness gate by
        # the machine's UTC offset.
        try:
            return timegm(time.strptime(bucket[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            return None

    def fresh_stats(
        self, universe_id: int, max_hours: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch + gate: stats dict ONLY if the last series point is fresh.

        The gate is the whole point of this module: Atlas drops games
        mid-history and the last point may be weeks old. Returns
            {"ccu", "visits", "favorites", "bucket_ts"}
        or None (game stays due for Roblox).
        """
        gate = float(max_hours if max_hours is not None else PRUFER_MAX_HOURS)
        series = self.fetch_series(universe_id)
        if not series:
            return None
        last = series[-1]
        ts = self._parse_bucket_ts(last["bucket"])
        if ts is None:
            return None
        age_hours = (time.time() - ts) / 3600.0
        if age_hours < 0 or age_hours > gate:
            return None
        return {
            "ccu": last["playing"],
            "visits": last["visits"],
            "favorites": last.get("favorited"),
            "bucket_ts": ts,
        }

    def refresh_batch(
        self,
        universe_ids: List[int],
        max_hours: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """Threaded fresh-stats refresh for a batch of due games.

        Returns {universe_id: stats} for games that passed the gate — the
        caller upserts those and requeues the rest for Roblox. Never raises;
        network/API problems surface as a smaller (or empty) dict.
        """
        ids = [int(u) for u in universe_ids if int(u) > 0]
        cap = int(limit) if limit and limit > 0 else PRUFER_REQUESTS_PER_TICK
        if cap <= 0:
            return {}
        ids = ids[:cap]
        if not ids:
            return {}

        # Ensure the pool before spending the batch: one validation sweep
        # (~3-4 min measured) covers all fetches in this pass.
        if not self.ensure_pool():
            return {}

        results: Dict[int, Dict[str, Any]] = {}
        lock = threading.Lock()

        def _fetch(uid: int) -> None:
            stats = self.fresh_stats(uid, max_hours=max_hours)
            if stats is not None:
                with lock:
                    results[uid] = stats

        with ThreadPoolExecutor(PRUFER_FETCH_THREADS) as ex:
            list(ex.map(_fetch, ids))
        return results


def backfill_series_to_rows(series: List[Dict[str, Any]]) -> List[Tuple[str, int]]:
    """Convert a validated series into (timestamp_str, ccu) rows ready for
    ccu_history. Caller filters by which points are new."""
    rows: List[Tuple[str, int]] = []
    for point in series:
        ts = PruferClient._parse_bucket_ts(point["bucket"])
        if ts is not None:
            rows.append(
                (
                    time.strftime("%Y-%m-%d %H:%M:%S.%f", time.gmtime(ts)),
                    point["playing"],
                )
            )
    return rows
