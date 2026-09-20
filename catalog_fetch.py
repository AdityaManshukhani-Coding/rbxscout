#!/usr/bin/env python3
"""RbxScout hosted catalog loader — for the Streamlit Community Cloud app.

The 24/7 pipeline (Cloudflare Worker → GitHub Actions → release asset) is the
single writer of the catalog. Readers — your laptop via ``db_sync.py pull``,
and the hosted dashboard via this module — just download the current
``rbx_scout.db`` from the rolling release ``catalog-latest``.

Hosted mode (no local DB present) is for the Streamlit Community Cloud
deployment, which gets a read-only container filesystem: the repo checkout
cannot be used as a cache. Therefore:

* the DB is downloaded once per app process into a cache directory
  (``~/.cache/rbxscout/rbx_scout.db``), then read locally from there;
* every 5 minutes a tiny metadata request (the release JSON) checks whether
  the remote asset is newer; only then is the ~30 MB file re-downloaded;
* the ``st.cache_resource`` wrapper in app.py means the check runs once per
  app process per 5-minute window — not per user or per rerun. 2–3 users
  cause ~1 metadata request / 5 min, far under GitHub's 60 req/h anonymous
  limit for api.github.com.

Local mode is untouched: if ``rbx_scout.db`` exists in the repo (or
``RBXSCOUT_LOCAL_DB=1``), the app reads the local file exactly as before and
this module is never used.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# Anonymous downloads need no token: the repo and the catalog release are
# public. Override only if the repo ever goes private (a PAT with repo read
# access would go in RBXSCOUT_GITHUB_TOKEN, picked up below).
GITHUB_OWNER = os.environ.get("RBXSCOUT_GITHUB_OWNER", "AdityaManshukhani-Coding")
GITHUB_REPO = os.environ.get("RBXSCOUT_GITHUB_REPO", "rbxscout")
RELEASE_TAG = "catalog-latest"
ASSET_DB = "rbx_scout.db"

# ~30 MB DB: do not re-download unless the asset actually changed. The check
# itself is a few hundred bytes of release JSON.
FRESHNESS_CHECK_INTERVAL = 300  # seconds

# One source of truth with db_sync.py for the “meets target” bar used below.
from db_sync import TARGET_MIN_CCU, TARGET_MIN_VISITS

def _default_cache_dir() -> Path:
    """Cache dir: env override, else ~/.cache/rbxscout, else /tmp fallback.

    Streamlit Community Cloud containers only allow writes under /tmp, so the
    home-directory path is only a preference — if it cannot be created we
    silently fall back to the temp dir rather than failing the app.
    """
    preferred = Path(
        os.environ.get("RBXSCOUT_CATALOG_CACHE_DIR", "~/.cache/rbxscout")
    ).expanduser()
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        import tempfile

        fallback = Path(tempfile.gettempdir()) / "rbxscout-catalog"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


CACHE_DIR = _default_cache_dir()
CACHE_DB_PATH = CACHE_DIR / ASSET_DB
CACHE_STATE_PATH = CACHE_DIR / "catalog_state.json"


class CatalogFetchError(RuntimeError):
    """Raised when the hosted catalog cannot be fetched; message is user-facing."""


# ---------------------------------------------------------------------------
# Contact overlay (UI-resolved Discord state that must survive asset swaps)
# ---------------------------------------------------------------------------

# The dashboard's page-by-page Discord lookups write contact state into the
# cache copy of the catalog — but the pipeline replaces that whole file on
# every sync. Without protection, every resolved invite (and every checked
# "no Discord" verdict) would silently vanish from the filter minutes after
# the user finds it. The overlay is a small sidecar JSON keyed by universe_id
# holding the contact columns; it is replayed onto each freshly downloaded
# catalog copy so user-visible progress accumulates instead of evaporating.
CONTACT_COLUMNS = (
    "has_discord",
    "discord_url",
    "status",
    "found_via",
    "has_social_links",
    "contacts_checked_at",
)
OVERLAY_MAX_ROWS = 50_000  # hard cap so the file can never grow unbounded


def _overlay_path() -> Path:
    return CACHE_DIR / "contact_overlay.json"


def load_contact_overlay() -> dict:
    """Return the overlay as ``{universe_id_str: {column: value}}``. Never raises."""
    try:
        data = json.loads(_overlay_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# Serializes overlay read-modify-write cycles across all user sessions (see
# record_contacts: last-writer-wins here silently dropped other sessions'
# freshly resolved verdicts).
_OVERLAY_LOCK = threading.Lock()


def _jsonable(value):
    """Coerce pandas/numpy cell values (np.bool_, np.int64, Timestamp) to JSON types."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    return str(value)


def record_contacts(records: dict) -> None:
    """Merge UI-resolved contact rows into the overlay (best effort).

    ``records`` maps universe_id -> dict with the CONTACT_COLUMNS keys.
    A failed write must never break the caller: the authoritative write
    already happened in the catalog DB; the overlay only protects it from
    the next asset replacement.

    Concurrency-safe: a process-wide lock serializes the read-modify-write
    (two sessions merging at once used to lose one side's rows), and the
    file is written to a temp file + os.replace so a reader can never see a
    half-written JSON (a torn file used to parse as {} and the next write
    then persisted ONLY the new records — silently erasing every verdict
    accumulated so far).
    """
    if not records:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _OVERLAY_LOCK:
            overlay = load_contact_overlay()
            for uid, rec in records.items():
                try:
                    overlay[str(int(uid))] = {
                        col: _jsonable(rec.get(col)) for col in CONTACT_COLUMNS
                    }
                except (TypeError, ValueError, AttributeError):
                    continue
            if len(overlay) > OVERLAY_MAX_ROWS:
                # Keep the newest rows by checked timestamp; oldest fall off.
                keep = sorted(
                    overlay.items(),
                    key=lambda kv: str((kv[1] or {}).get("contacts_checked_at") or ""),
                    reverse=True,
                )[:OVERLAY_MAX_ROWS]
                overlay = dict(keep)
            fd, tmp_name = tempfile.mkstemp(dir=str(CACHE_DIR), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(overlay, handle)
                os.replace(tmp_name, _overlay_path())
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
    except Exception:
        pass  # overlay is an optimization, never a dependency


def _replay_overlay(db_path: Path) -> None:
    """Re-apply overlay rows onto a freshly installed catalog copy.

    Runs right after the atomic swap in _download_and_install, so the new
    file starts with every contact verdict the UI ever resolved. Best
    effort: any error leaves the fresh download as-is.
    """
    overlay = load_contact_overlay()
    if not overlay:
        return
    try:
        import sqlite3

        rows = [
            (
                bool((rec or {}).get("has_discord")),
                (rec or {}).get("discord_url"),
                (rec or {}).get("status"),
                (rec or {}).get("found_via"),
                bool((rec or {}).get("has_social_links")),
                (rec or {}).get("contacts_checked_at"),
                int(uid),
                str((rec or {}).get("contacts_checked_at") or ""),
            )
            for uid, rec in overlay.items()
        ]
        conn = sqlite3.connect(str(db_path))
        try:
            with conn:
                conn.executemany(
                    "UPDATE game_analytics SET has_discord=?, discord_url=?, "
                    "status=?, found_via=?, has_social_links=?, "
                    "contacts_checked_at=? "
                    "WHERE universe_id=? AND (contacts_checked_at IS NULL "
                    "OR contacts_checked_at < ?)",
                    rows,
                )
        finally:
            conn.close()
    except Exception:
        pass  # a schema drift or corrupt overlay must never block downloads


def is_hosted() -> bool:
    """Hosted mode = no local DB and not explicitly forced local.

    Order of precedence:
      RBXSCOUT_LOCAL_DB=1  -> local mode, even if the file is missing
      repo DB exists       -> local mode (unchanged behavior for your laptop)
      otherwise            -> hosted mode (Streamlit Cloud container)
    """
    if os.environ.get("RBXSCOUT_LOCAL_DB") == "1":
        return False
    return not (APP_DIR / ASSET_DB).exists()


def local_catalog_path() -> str:
    """Path the dashboard should read: the repo DB in local mode, the cache
    copy in hosted mode (downloaded on first call to ensure_catalog)."""
    if not is_hosted():
        return str(APP_DIR / ASSET_DB)
    return str(CACHE_DB_PATH)


# ---------------------------------------------------------------------------
# Release metadata (anonymous, public repo)
# ---------------------------------------------------------------------------

def _headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "rbxscout-catalog-fetch",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Optional token (only needed if the repo ever goes private). Anonymous
    # requests work today and avoid any secret management on Streamlit Cloud.
    token = os.environ.get("RBXSCOUT_GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise CatalogFetchError(
            f"GitHub API {url} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CatalogFetchError(f"GitHub API unreachable: {exc}") from exc


def _get_bytes(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise CatalogFetchError(
            f"asset download -> HTTP {exc.code} ({url.rsplit('/', 1)[-1]})"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CatalogFetchError(f"asset download failed: {exc}") from exc


def release_asset_info(
    timeout: float = 15.0, attempts: int = 2, retry_delay: float = 3.0
) -> dict:
    """Return {'size': int, 'updated_at': str} for the catalog DB asset.

    Retries once after a short delay: the asset is legitimately absent only
    for the second or two a healthy push takes to swap it in, and a single
    retry rides out both that window and one transient GitHub hiccup.
    """
    last_error: CatalogFetchError | None = None
    for attempt in range(max(1, attempts)):
        try:
            rel = _get_json(
                f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}",
                timeout,
            )
            for asset in rel.get("assets", []):
                if asset.get("name") == ASSET_DB:
                    return {
                        "size": int(asset.get("size") or 0),
                        "updated_at": str(asset.get("updated_at") or ""),
                    }
            last_error = CatalogFetchError(
                f"release {RELEASE_TAG} currently has no {ASSET_DB} asset — "
                "the catalog store is empty. This is usually a brief pipeline "
                "outage; it self-repairs on the next successful db_sync push."
            )
        except CatalogFetchError as exc:
            last_error = exc
        if attempt + 1 < max(1, attempts):
            time.sleep(retry_delay)
    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# Cache state (what the local cache currently holds)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(CACHE_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        CACHE_STATE_PATH.write_text(json.dumps(state))
    except Exception:
        pass  # unwritable cache dir: re-download each process, still works


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------

def ensure_catalog(force_refresh: bool = False, timeout: float = 120.0) -> Path:
    """Make sure CACHE_DB_PATH holds the current release asset; return its path.

    Behavior:
      * cache missing or corrupt        -> full download
      * force_refresh=True              -> full download (Refresh button)
      * metadata checked < 5 min ago    -> trust the cached check, no request
      * otherwise                       -> metadata request; re-download only
                                           if the remote asset is newer
    Raises CatalogFetchError with a user-facing message on failure; the caller
    decides whether to fall back to a stale cache.
    """
    state = _load_state()
    now = time.time()
    last_check = float(state.get("checked_at") or 0)
    remote = state.get("remote") or {}

    cache_ok = _cache_is_valid()

    if force_refresh or not cache_ok:
        return _download_and_install(timeout, state)

    if now - last_check < FRESHNESS_CHECK_INTERVAL:
        # Recently verified fresh enough; skip the metadata request entirely.
        return CACHE_DB_PATH

    info = release_asset_info(timeout=min(timeout, 20.0))
    state["checked_at"] = now
    state["remote"] = info
    _save_state(state)

    if info.get("updated_at") and info == remote:
        return CACHE_DB_PATH  # nothing changed upstream
    return _download_and_install(timeout, state)


def _cache_is_valid() -> bool:
    """A cache is valid when it is a real SQLite file (or at least non-empty).
    A truncated/interrupted download must never be served."""
    if not CACHE_DB_PATH.exists() or CACHE_DB_PATH.stat().st_size < 100_000:
        return False
    try:
        with CACHE_DB_PATH.open("rb") as fh:
            return fh.read(16).startswith(b"SQLite format 3\x00")
    except OSError:
        return False


def _download_and_install(timeout: float, state: dict) -> Path:
    info = release_asset_info(timeout=min(timeout, 20.0))
    asset = _asset_download_url()
    blob = _get_bytes(asset, timeout)
    if not blob[:16].startswith(b"SQLite format 3\x00"):
        raise CatalogFetchError(
            "downloaded catalog is not a SQLite database — refusing to install it"
        )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)  # no-op; dir is resolved at import
    tmp = CACHE_DIR / (ASSET_DB + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(CACHE_DB_PATH)  # atomic swap: readers never see a partial file
    _replay_overlay(CACHE_DB_PATH)  # restore UI-resolved contacts the swap erased
    state.update({
        "checked_at": time.time(),
        "remote": info,
        "installed_at": time.time(),
        "installed_size": len(blob),
    })
    _save_state(state)
    return CACHE_DB_PATH


def _asset_download_url() -> str:
    # Browser-style URL: GitHub redirects it to the signed S3 object. Works
    # anonymously for a public repo and follows redirects via urllib.
    return (
        f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/"
        f"releases/download/{RELEASE_TAG}/{ASSET_DB}"
    )


def stale_cache_fallback(exc: CatalogFetchError) -> Path | None:
    """Return the cached path if it is usable despite the fetch error."""
    if _cache_is_valid():
        return CACHE_DB_PATH
    return None


# ---------------------------------------------------------------------------
# Counters for the dashboard tracker band
# ---------------------------------------------------------------------------

# Process-wide 60 s cache for catalog_counts. The dashboard renders the
# tracker band on every rerun and the live view refreshes it every 60 s per
# open tab; without the cache that is one heavy GROUP BY over the full
# ccu_history table PER TAB PER MINUTE on a container that must also serve
# real users. With it, the aggregate runs at most once per minute for the
# whole process no matter how many tabs are open.
COUNTS_CACHE_TTL = 60  # seconds
_COUNTS_CACHE: dict = {}
_COUNTS_CACHE_LOCK = threading.Lock()


def catalog_counts_cached(db_path: str | Path) -> dict:
    """60-second cached wrapper around catalog_counts (see note above)."""
    key = str(Path(db_path))
    now = time.monotonic()
    with _COUNTS_CACHE_LOCK:
        entry = _COUNTS_CACHE.get(key)
        if entry and now - entry[0] < COUNTS_CACHE_TTL:
            return dict(entry[1])
    value = catalog_counts(db_path)
    with _COUNTS_CACHE_LOCK:
        # Keep only the current path's entry: the cached copy is at most one
        # TTL stale for a counter band, never a correctness surface.
        _COUNTS_CACHE.clear()
        _COUNTS_CACHE[key] = (now, value)
    return dict(value)


def _reset_counts_cache() -> None:
    """Test/ops hook: drop the cached counters (fresh read on next call)."""
    with _COUNTS_CACHE_LOCK:
        _COUNTS_CACHE.clear()


def catalog_counts(db_path: str | Path) -> dict:
    """Read-only counters for the dashboard's live tracker band.

    Defensive by design: any missing table/column degrades that field to
    None so a schema surprise can never take the dashboard down.
    """
    import sqlite3

    counts = {"games": None, "target": None, "found_today": None, "last_sync": None}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            counts["games"] = int(
                conn.execute("SELECT COUNT(*) FROM game_analytics").fetchone()[0]
            )
            try:
                counts["target"] = int(conn.execute(
                    "SELECT COUNT(*) FROM game_analytics WHERE visits >= ? AND ccu >= ?",
                    (TARGET_MIN_VISITS, TARGET_MIN_CCU),
                ).fetchone()[0])
            except sqlite3.Error:
                pass
            try:
                # “Discovered today” = games whose earliest ccu_history sample
                # falls on the current UTC day — a true first-seen date, not a
                # refresh stamp. (last_updated changes on every hydration.)
                # Both sides are UTC: SQLite date('now') and the pipeline's
                # naive-but-UTC timestamps, so the window is a clean UTC day.
                counts["found_today"] = int(conn.execute(
                    "SELECT COUNT(*) FROM ("
                    "  SELECT universe_id, MIN(ts) AS first_seen"
                    "  FROM ccu_history GROUP BY universe_id"
                    "  HAVING date(first_seen) = date('now')"
                    ")"
                ).fetchone()[0])
            except sqlite3.Error:
                pass
            try:
                counts["last_sync"] = conn.execute(
                    "SELECT MAX(finished_at) FROM scan_runs WHERE status = 'complete'"
                ).fetchone()[0]
            except sqlite3.Error:
                pass
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return counts


if __name__ == "__main__":
    # Manual smoke test: python catalog_fetch.py [--force]
    import sys

    try:
        path = ensure_catalog(force_refresh="--force" in sys.argv)
        size_mb = path.stat().st_size / 1e6
        info = _load_state().get("remote", {})
        print(f"catalog ready: {path} ({size_mb:.1f} MB, updated {info.get('updated_at')})")
    except CatalogFetchError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
