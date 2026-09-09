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


def release_asset_info(timeout: float = 15.0) -> dict:
    """Return {'size': int, 'updated_at': str} for the catalog DB asset."""
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
    raise CatalogFetchError(
        f"release {RELEASE_TAG} has no {ASSET_DB} asset yet — run "
        "`python db_sync.py push` once from your machine to seed it"
    )


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
