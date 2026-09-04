"""Resumable background sweep: Rolimons catalog -> place_map -> game_analytics.

Converts every qualifying Rolimons place to its universe ID, then hydrates the
newly resolved games in batched metrics passes and upserts them into
game_analytics. Safe to kill and re-run at any time: resolved places are
skipped on the next run, so it always makes forward progress.

v2 pacing model (replaces the v1 per-request retry loop that could livelock
when all workers were stuck re-hammering a saturated rate window):

- ONE shared token-bucket pacer (thread-safe) drives ALL requests. The emit
  interval adapts: +60% on every 429 (capped at 10 s), -2% on every 200
  (floored at 0.12 s). Slow to grow, quick to shrink — it tracks the real
  window instead of oscillating.
- A request 429s AT MOST 4 times, then is abandoned for this run. Abandoned
  places are simply not persisted, so the next run (or a later chunk of this
  run) retries them — the sweep is resumable, and progress never depends on a
  single stubborn request.
- Route order alternates per request between the RoProxy mirror and direct
  Roblox. They have separate IP pools and separate windows; alternating
  spreads load so neither is driven into its limit by us alone.

Usage:
    .venv/bin/python sweep_place_map.py [chunk_size] [max_chunks]
"""

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import scout_core

DB_PATH = "rbx_scout.db"
MIN_CCU = 25          # pre-gate: catalog entries below this CCU are skipped
CHUNK_SIZE = 100      # places resolved per commit chunk
PAUSE_BETWEEN_CHUNKS = 30.0  # seconds; lets both per-IP windows drain
ROUTE_HOSTS = ("apis.roproxy.com", "apis.roblox.com")
MAX_429_RETRIES = 4


class Pacer:
    """Thread-safe adaptive token bucket: one global emit interval."""

    def __init__(self, initial: float = 0.15):
        self._lock = threading.Lock()
        self._interval = initial
        self._next_ok = 0.0  # earliest monotonic time the next request may go

    def wait(self) -> float:
        """Block until this caller may emit one request. Returns the interval."""
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_ok - now)
            self._next_ok = now + delay + self._interval
            return self._interval

    def success(self) -> None:
        with self._lock:
            self._interval = max(0.12, self._interval * 0.98)

    def throttled(self, retry_after: float) -> None:
        with self._lock:
            self._interval = min(10.0, max(self._interval, 0.2) * 1.6)
            # Also push the next emit slot out so the window can drain.
            self._next_ok = max(self._next_ok, time.monotonic() + max(retry_after, 1.5))


def make_session() -> requests.Session:
    """Session WITHOUT any credential — the sweep must stay cookieless."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept": "application/json",
    })
    return s


def resolve_places(scout, session, pacer, place_ids):
    """Resolve places (4 workers, shared pacer). Returns (resolved, not_found).

    Abandoned (429-exhausted) places are silently left out — resumability
    covers them on a later pass.
    """
    resolved: dict[int, int] = {}
    not_found: list[int] = []
    throttled_out = 0

    def _json(res):
        try:
            return res.json()
        except ValueError:
            return None

    def work(idx_pid):
        idx, pid = idx_pid
        # Alternate route first per place: spreads load across both windows.
        order = (ROUTE_HOSTS[idx % len(ROUTE_HOSTS)], ROUTE_HOSTS[(idx + 1) % len(ROUTE_HOSTS)])
        for host in order:
            for attempt in range(MAX_429_RETRIES):
                pacer.wait()
                try:
                    res = session.get(
                        f"https://{host}/universes/v1/places/{pid}/universe", timeout=10
                    )
                except requests.RequestException:
                    time.sleep(1.0)
                    continue
                if res.status_code == 200:
                    pacer.success()
                    data = _json(res)
                    if data and data.get("universeId"):
                        return pid, int(data["universeId"])
                    break  # 200 without a universeId: nothing more to try
                if res.status_code == 429:
                    pacer.throttled(float(res.headers.get("Retry-After") or 0))
                    continue
                if res.status_code == 404:
                    pacer.success()
                    return pid, None  # deleted place — legitimate outcome
                break  # other status: give up on this route
        return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        for got in pool.map(work, list(enumerate(place_ids))):
            if got is None:
                continue
            pid, uid = got
            if uid is None:
                not_found.append(pid)
            else:
                resolved[pid] = uid

    return resolved, not_found


def persist_results(scout, resolved, not_found):
    if resolved:
        with scout._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO place_map (place_id, universe_id, resolved_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                list(resolved.items()),
            )
    if not_found:
        # Persist deletions as self-mapped so we never re-request them.
        with scout._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO place_map (place_id, universe_id, resolved_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                [(pid, pid) for pid in not_found],
            )


def hydrate_places(scout, place_ids):
    """Batch-hydrate resolved places (place->universe from place_map)."""
    pids = [int(p) for p in place_ids]
    if not pids:
        return 0
    with scout._connect() as conn:
        placeholders = ",".join("?" for _ in pids)
        rows = conn.execute(
            f"SELECT place_id, universe_id FROM place_map WHERE place_id IN ({placeholders})",
            tuple(pids),
        ).fetchall()
    # Deleted places are stored as place_id = universe_id; skip those pairs.
    pairs = [(int(p), int(u)) for p, u in rows if int(p) != int(u)]
    uids = [u for _, u in pairs]
    if not uids:
        return 0
    metrics = scout.fetch_game_metrics(uids)
    for meta in metrics.values():
        meta.setdefault("icon_url", None)
        root_place = next((p for p, u in pairs if u == meta["universe_id"]), None)
        scout.upsert_game({
            "universe_id": int(meta["universe_id"]),
            "root_place_id": root_place or meta.get("root_place_id"),
            "title": meta.get("title"),
            "ccu": meta.get("ccu"),
            "peak_ccu": meta.get("ccu"),
            "visits": meta.get("visits"),
            "favorites": meta.get("favorites"),
            "genre": meta.get("genre"),
            "creator_name": meta.get("creator_name"),
            "creator_type": meta.get("creator_type"),
            "creator_id": meta.get("creator_id"),
            "description": meta.get("description"),
            "icon_url": meta.get("icon_url"),
        })
    return len(metrics)


def progress_row(scout):
    with scout._connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM rolimons_catalog WHERE playing >= ?", (MIN_CCU,)
        ).fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*) FROM rolimons_catalog c JOIN place_map m ON c.place_id = m.place_id "
            "WHERE c.playing >= ?", (MIN_CCU,)
        ).fetchone()[0]
        games = conn.execute("SELECT COUNT(*) FROM game_analytics").fetchone()[0]
    return total, done, games


def main() -> None:
    chunk_size = int(sys.argv[1]) if len(sys.argv) > 1 else CHUNK_SIZE
    max_chunks = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    scout = scout_core.RobloxPlatformScout(db_path=DB_PATH)
    session = make_session()
    pacer = Pacer(initial=0.15)

    total, done, games = progress_row(scout)
    print(f"catalog (ccu>={MIN_CCU}): {total} | resolved: {done} | remaining: {total - done} "
          f"| game_analytics: {games}", flush=True)
    if total - done <= 0:
        print("Nothing to do — place_map already complete.", flush=True)
        return

    chunk_no = 0
    while True:
        with scout._connect() as conn:
            rows = conn.execute(
                "SELECT c.place_id FROM rolimons_catalog c "
                "LEFT JOIN place_map m ON c.place_id = m.place_id "
                "WHERE m.place_id IS NULL AND c.playing >= ? "
                "ORDER BY c.playing DESC LIMIT ?",
                (MIN_CCU, chunk_size),
            ).fetchall()
        pids = [r[0] for r in rows]
        if not pids:
            print("SWEEP COMPLETE — every qualifying catalog place is resolved.", flush=True)
            break

        chunk_no += 1
        t0 = time.time()
        resolved, not_found = resolve_places(scout, session, pacer, pids)
        persist_results(scout, resolved, not_found)
        hydrated = hydrate_places(scout, pids)
        total, done, games = progress_row(scout)

        skipped = len(pids) - len(resolved) - len(not_found)
        print(
            f"chunk {chunk_no}: {len(pids)} places -> {len(resolved)} resolved, "
            f"{len(not_found)} deleted, {skipped} deferred | {hydrated} hydrated | "
            f"progress {done}/{total} ({100 * done / max(total, 1):.0f}%) | "
            f"game_analytics {games} | interval {pacer._interval:.2f}s | {time.time() - t0:.0f}s",
            flush=True,
        )

        if max_chunks and chunk_no >= max_chunks:
            print(f"Stopping after {chunk_no} chunks (chunk limit).", flush=True)
            break
        if skipped >= len(pids):
            # Everything deferred: the windows are saturated. Longer cooldown.
            print("All places deferred this pass — cooling down 90s.", flush=True)
            time.sleep(90)
        else:
            time.sleep(PAUSE_BETWEEN_CHUNKS)


if __name__ == "__main__":
    main()
