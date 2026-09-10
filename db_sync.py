#!/usr/bin/env python3
"""RbxScout catalog store — the SQLite catalog lives on a GitHub Release asset.

The repo used to carry rbx_scout.db as a committed blob (a fresh ~11 MB file
in every sync commit). Now the catalog is the **first asset** of a GitHub
Release tagged ``catalog-latest``:

    Release catalog-latest
    ├── rbx_scout.db             <- the catalog itself (asset 1)
    ├── rbx_scout.db.sync_state  <- tiny marker: how many syncs wrote this asset
    └── stats.json               <- tiny badge feed: live catalog counts
                                   (drives the README shields.io badges +
                                    the counts at the top of the release page)

Why a Release asset instead of a commit?
  * 2 GiB per asset, no bandwidth limit, no LFS quota, no credit card.
  * Both GitHub Actions workflows authenticate with the workflow's own
    GITHUB_TOKEN; locally you use any PAT with repo contents access.
  * The old "repo as database" git history (a new 11 MB blob per 5 minutes)
    disappears; the code and the dashboard stay in git as normal.

Concurrency is unchanged: both workflows still share the ``rbxscout-sync``
Actions concurrency group, so only one pull/push cycle runs at a time.
The marker file makes the release self-describing (mirrors the old
rbx_scout.db.sync_state counter) and lets `db_sync.py status` tell you
which sync produced the stored catalog.

Usage:
    python db_sync.py pull     # release asset -> local rbx_scout.db
    python db_sync.py push     # local rbx_scout.db -> release asset (clobber)
    python db_sync.py status   # compare local vs release (size + marker)

Local environment (one time):
    export RBXSCOUT_GITHUB_REPO=AdityaManshukhani-Coding/rbxscout
    export RBXSCOUT_GITHUB_TOKEN=ghp_...   # or export GH_TOKEN=...
In GitHub Actions both come from the runner environment automatically.

Public repos *can* read the asset anonymously, but this catalog is a private
working dataset by default — everything authenticates. No third-party
service, no signup beyond the GitHub account you already have.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "rbx_scout.db"
STATE_PATH = APP_DIR / "rbx_scout.db.sync_state"

RELEASE_TAG = "catalog-latest"
RELEASE_NAME = "RbxScout catalog (rolling)"
ASSET_DB = "rbx_scout.db"
ASSET_STATE = "rbx_scout.db.sync_state"
ASSET_STATS = "stats.json"
API = "https://api.github.com"

# The “meets the target” line uses the pipeline's standard entry bar
# (live_sync.py defaults). Kept in one place so the badge and the release
# page always agree with what the finder actually enforces.
TARGET_MIN_VISITS = 20_000
TARGET_MIN_CCU = 25


class SyncError(RuntimeError):
    """Raised for any auth/API/asset problem; message is user-facing."""


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

def repo_slug() -> str:
    """Target repo: RBXSCOUT_GITHUB_REPO, else derived from 'origin'."""
    explicit = os.environ.get("RBXSCOUT_GITHUB_REPO", "").strip()
    if explicit:
        return explicit.strip("/")
    try:
        import subprocess
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True, cwd=str(APP_DIR),
        ).stdout.strip()
    except Exception as exc:
        raise SyncError(f"cannot read git remote 'origin' ({exc}); set RBXSCOUT_GITHUB_REPO")
    slug = url.split("github.com")[-1].lstrip("/:").removesuffix(".git")
    if "/" not in slug:
        raise SyncError(f"unrecognised origin URL '{url}'; set RBXSCOUT_GITHUB_REPO")
    return slug


def gh_token() -> str:
    """Auth token: RBXSCOUT_GITHUB_TOKEN, GH_TOKEN, GITHUB_TOKEN, or the local
    git credential store (macOS Keychain / wincred / libsecret)."""
    for var in ("RBXSCOUT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        tok = os.environ.get(var, "").strip()
        if tok:
            return tok
    try:
        import subprocess
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, check=True, cwd=str(APP_DIR),
        ).stdout
        for line in out.splitlines():
            if line.startswith("password="):
                return line[len("password="):].strip()
    except Exception:
        pass
    raise SyncError(
        "No GitHub credentials found. Set RBXSCOUT_GITHUB_TOKEN (a PAT with "
        "repo access) or run 'git push' once so the credential store has a token."
    )


# --------------------------------------------------------------------------
# GitHub REST plumbing (stdlib only — requirements.txt stays clean)
# --------------------------------------------------------------------------

def _headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rbxscout-db-sync",
    }


def _api(method: str, path: str, token: str, *, body: dict | None = None,
         raw: bytes | None = None, content_type: str | None = None,
         expect_json: bool = True, accept: str | None = None,
         timeout: float = 120.0):
    url = path if path.startswith("http") else f"{API}{path}"
    headers = _headers(token, accept or "application/vnd.github+json")
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = content_type or "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 404:
            return None if expect_json else b""
        raise SyncError(f"GitHub API {method} {path} -> HTTP {exc.code}: {detail}")
    if expect_json:
        text = payload.decode("utf-8", "replace")
        return json.loads(text) if text.strip() else None
    return payload


def get_release(token: str) -> dict | None:
    return _api("GET", f"/repos/{repo_slug()}/releases/tags/{RELEASE_TAG}", token)


def release_body() -> str:
    return (
        "Rolling catalog storage for RbxScout — written by the Hydrator/Finder "
        "workflows, read by db_sync.py. The 'rbx_scout.db' asset is the current "
        "catalog and 'rbx_scout.db.sync_state' counts the syncs that wrote it. "
        "The 📊 line at the top shows the live catalog counts (stats.json is "
        "the machine-readable feed behind the README badges). "
        "Do not edit this release manually; each push replaces the assets."
    )


def create_release(token: str) -> dict:
    body = {
        "tag_name": RELEASE_TAG,
        "target_commitish": "main",
        "name": RELEASE_NAME,
        "body": release_body(),
        "draft": False,
        "prerelease": False,
    }
    rel = _api("POST", f"/repos/{repo_slug()}/releases", token, body=body)
    if not rel:
        raise SyncError(f"could not create release {RELEASE_TAG}")
    return rel


def get_or_create_release(token: str) -> dict:
    rel = get_release(token)
    if rel:
        return rel
    try:
        return create_release(token)
    except SyncError:
        # Lost a create race (or the tag already exists on a commit): re-read.
        rel = get_release(token)
        if rel:
            return rel
        raise


# Asset uploads can be slow (a ~30 MB catalog from a CI runner); the default
# 120 s timeout once killed an upload mid-push and left the store empty.
UPLOAD_TIMEOUT = 600.0

# Suffix used by replace_catalog_asset for the upload-first swap below.
INCOMING_SUFFIX = ".incoming"


def upload_asset(rel: dict, token: str, name: str, data: bytes) -> None:
    """Upload one asset, replacing any existing asset of the same name.

    Only used for the small, rebuilt-every-push assets (sync_state, stats).
    The catalog DB itself must go through replace_catalog_asset, which can
    never leave the release without a catalog.
    """
    for asset in rel.get("assets", []):
        if asset.get("name") == name:
            _api("DELETE", f"/repos/{repo_slug()}/releases/assets/{asset['id']}", token)
    upload_url = rel["upload_url"].split("{")[0]
    sep = "&" if "?" in upload_url else "?"
    url = f"{upload_url}{sep}name={name}"
    _api("POST", url, token, raw=data, content_type="application/octet-stream",
         expect_json=False, timeout=UPLOAD_TIMEOUT)


def replace_catalog_asset(rel: dict, token: str, name: str, data: bytes) -> None:
    """Replace the catalog DB asset without ever leaving the store empty.

    Order of operations (this exact sequence fixed the 2026-09-09 outage,
    where the old delete-then-upload code lost the catalog when the ~30 MB
    upload timed out after the delete had already gone through):

      1. upload the new blob under ``<name>.incoming`` — the old asset stays
         untouched while this (slow) transfer runs;
      2. verify GitHub reports the uploaded size byte-for-byte;
      3. only then delete the old asset and rename the incoming one into
         place — the unprotected window shrinks from a multi-minute upload
         to a single tiny rename call.

    A stale ``<name>.incoming`` from an earlier crashed push is removed
    before uploading, so the swap self-heals after any incident.
    """
    incoming_name = name + INCOMING_SUFFIX
    for asset in rel.get("assets", []):
        if asset.get("name") == incoming_name:
            _api("DELETE", f"/repos/{repo_slug()}/releases/assets/{asset['id']}", token)
    upload_url = rel["upload_url"].split("{")[0]
    sep = "&" if "?" in upload_url else "?"
    url = f"{upload_url}{sep}name={incoming_name}"
    uploaded = _api("POST", url, token, raw=data,
                    content_type="application/octet-stream",
                    expect_json=True, timeout=UPLOAD_TIMEOUT)
    reported = int((uploaded or {}).get("size") or 0)
    if reported != len(data):
        # The old asset is still in place — nothing was lost. Fail loudly so
        # the workflow run is marked failed and the next push retries.
        raise SyncError(
            f"upload verification failed for {name}: GitHub reports "
            f"{reported} bytes, expected {len(data)} — old catalog left intact"
        )
    old = next((a for a in rel.get("assets", []) if a.get("name") == name), None)
    if old is not None:
        _api("DELETE", f"/repos/{repo_slug()}/releases/assets/{old['id']}", token)
    # The rename is the only step after which the store could briefly lack
    # the catalog (old deleted, incoming not yet renamed). Retry it a few
    # times so a single transient 5xx cannot strand the swap half-done.
    rename_path = f"/repos/{repo_slug()}/releases/assets/{uploaded['id']}"
    for attempt in range(3):
        try:
            _api("PATCH", rename_path, token, body={"name": name},
                 timeout=UPLOAD_TIMEOUT)
            return
        except SyncError:
            if attempt == 2:
                raise
            time.sleep(2.0)


def _asset_state(rel: dict, token: str) -> str | None:
    """Read the marker text asset; None when absent. Authenticated because
    the catalog repo may be private (anonymous API needs a public repo)."""
    for asset in rel.get("assets", []):
        if asset.get("name") == ASSET_STATE:
            data = _api("GET", asset["url"], token, expect_json=False,
                        accept="application/octet-stream")
            return data.decode("utf-8", "replace").strip() or None
    return None


# --------------------------------------------------------------------------
# Live counts (stats.json badge feed + release-page line)
# --------------------------------------------------------------------------

def stats_payloads(db: Path) -> dict[str, dict]:
    """Build the badge-feed JSON files from the local catalog.

    ``stats.json``      -> shields.io endpoint badge: total games.
    ``stats_target.json`` -> badge: games meeting the 20k visits / 25 CCU bar.

    Read-only and defensive: a missing table/column degrades that field, and
    an unreadable DB returns {} so a stats hiccup can NEVER block the
    catalog push itself.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            games = conn.execute("SELECT COUNT(*) FROM game_analytics").fetchone()[0]
            passing = last = None
            try:
                passing = conn.execute(
                    "SELECT COUNT(*) FROM game_analytics "
                    "WHERE visits >= ? AND ccu >= ?",
                    (TARGET_MIN_VISITS, TARGET_MIN_CCU),
                ).fetchone()[0]
            except sqlite3.Error:
                pass
            try:
                last = conn.execute(
                    "SELECT MAX(finished_at) FROM scan_runs WHERE status = 'complete'"
                ).fetchone()[0]
            except sqlite3.Error:
                pass
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    payloads: dict[str, dict] = {
        ASSET_STATS: {
            # shields.io endpoint schema…
            "schemaVersion": 1,
            "label": "catalog",
            "message": f"{int(games):,} games",
            "color": "green",
            # …plus badgen.net schema (the README uses badgen because
            # shields.io blocks github.com release URLs). Unknown keys are
            # ignored by either renderer, so one file feeds both.
            "subject": "catalog",
            "status": f"{int(games):,} games",
            # Self-describing extras for humans and other consumers.
            "games": int(games),
            "last_sync_utc": last or "",
        },
    }
    if passing is not None:
        payloads["stats_target.json"] = {
            "schemaVersion": 1,
            "label": f"match {TARGET_MIN_VISITS // 1000}k visits / {TARGET_MIN_CCU} CCU",
            "message": f"{int(passing):,} games",
            "color": "blue",
            "subject": f"match {TARGET_MIN_VISITS // 1000}k visits / {TARGET_MIN_CCU} CCU",
            "status": f"{int(passing):,} games",
            "games": int(passing),
            "last_sync_utc": last or "",
        }
    return payloads


def _update_release_stats_line(rel: dict, token: str, payloads: dict[str, dict]) -> None:
    """Best-effort: show the live counts at the top of the release page.

    Cosmetic only — any failure here is reported and skipped, never allowed
    to fail the catalog push.
    """
    try:
        stats = payloads.get(ASSET_STATS) or {}
        target = payloads.get("stats_target.json") or {}
        games = int(stats.get("games") or 0)
        passing = int(target.get("games") or 0)
        last = str(stats.get("last_sync_utc") or "unknown")
        lines = [l for l in (rel.get("body") or "").splitlines()
                 if not l.startswith("📊")]
        body = (
            f"📊 **{games:,} games** · **{passing:,}** meet the "
            f"{TARGET_MIN_VISITS:,} visits / {TARGET_MIN_CCU} CCU target · "
            f"last sync {last} UTC\n\n" + "\n".join(lines).strip()
        )
        _api("PATCH", f"/repos/{repo_slug()}/releases/{rel['id']}", token,
             body={"body": body})
    except Exception as exc:  # cosmetic: never block the push
        print(f"stats line on release page skipped: {exc}")


# --------------------------------------------------------------------------
# Local side
# --------------------------------------------------------------------------

def _checkpoint_wal(db: Path) -> None:
    import sqlite3
    try:
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # a corrupt/partial DB is upload's problem, not pull's


def local_state() -> str:
    return STATE_PATH.read_text().strip() if STATE_PATH.exists() else "-"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_pull() -> int:
    token = gh_token()
    rel = get_release(token)
    if not rel:
        raise SyncError(
            f"No {RELEASE_TAG} release yet. Seed it once with: python db_sync.py push"
        )
    asset = next((a for a in rel["assets"] if a.get("name") == ASSET_DB), None)
    if not asset:
        raise SyncError(f"Release {RELEASE_TAG} has no {ASSET_DB} asset yet.")
    print(f"pull: {asset['name']}  {asset['size']/1e6:.1f} MB  (updated {asset['updated_at']})")
    blob = _api("GET", asset["url"], token, expect_json=False,
                accept="application/octet-stream")
    if not blob:
        raise SyncError(f"asset download returned nothing (asset id {asset['id']})")
    tmp = DB_PATH.with_suffix(".db.tmp")
    tmp.write_bytes(blob)
    if not tmp.read_bytes().startswith(b"SQLite format 3\x00"):
        tmp.unlink(missing_ok=True)
        raise SyncError("downloaded asset is not a SQLite database — refusing to install it")
    _checkpoint_wal(DB_PATH)
    tmp.replace(DB_PATH)
    state = _asset_state(rel, token)
    if state:
        STATE_PATH.write_text(state + "\n")
    print(f"installed: {DB_PATH.name}  {len(blob)/1e6:.1f} MB  (sync #{state or '?'})")
    return 0


def cmd_push() -> int:
    token = gh_token()
    if not DB_PATH.exists():
        raise SyncError(f"{DB_PATH} does not exist — nothing to push")
    blob = DB_PATH.read_bytes()
    if not blob[:16].startswith(b"SQLite format 3\x00"):
        raise SyncError(f"{DB_PATH} is not a SQLite database — refusing to upload")
    _checkpoint_wal(DB_PATH)
    state = local_state()
    rel = get_or_create_release(token)
    old_state = _asset_state(rel, token)
    replace_catalog_asset(rel, token, ASSET_DB, blob)
    upload_asset(rel, token, ASSET_STATE, (state + "\n").encode())
    payloads = stats_payloads(DB_PATH)
    for name, payload in payloads.items():
        upload_asset(rel, token, name, (json.dumps(payload) + "\n").encode())
    if payloads:
        _update_release_stats_line(rel, token, payloads)
    print(f"push: {DB_PATH.name}  {len(blob)/1e6:.1f} MB  sync #{state}"
          + (f"  (replaces sync #{old_state or 'none'})" if old_state and old_state != state else ""))
    for name, payload in payloads.items():
        print(f"push: {name}  games={payload.get('games'):,}")
    return 0


def cmd_status() -> int:
    token = gh_token()
    rel = get_release(token)
    if not rel:
        print(f"release {RELEASE_TAG}: does not exist yet")
        return 1
    asset = next((a for a in rel["assets"] if a.get("name") == ASSET_DB), None)
    remote = f"{asset['size']/1e6:.1f} MB, sync #{_asset_state(rel, token) or '?'}" if asset else "missing"
    local = f"{DB_PATH.stat().st_size/1e6:.1f} MB, sync #{local_state()}" if DB_PATH.exists() else "missing"
    print(f"release {RELEASE_TAG} ({rel['html_url']})")
    print(f"  remote : {remote}")
    print(f"  local  : {local}")
    if asset and DB_PATH.exists() and asset["size"] == DB_PATH.stat().st_size:
        print("  sizes match — local copy is current")
    return 0


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "").lower()
    try:
        if cmd == "pull":
            return cmd_pull()
        if cmd == "push":
            return cmd_push()
        if cmd == "status":
            return cmd_status()
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
