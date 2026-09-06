#!/usr/bin/env python3
"""RbxScout catalog store — the SQLite catalog lives on a GitHub Release asset.

The repo used to carry rbx_scout.db as a committed blob (a fresh ~11 MB file
in every sync commit). Now the catalog is the **first asset** of a GitHub
Release tagged ``catalog-latest``:

    Release catalog-latest
    ├── rbx_scout.db           <- the catalog itself (asset 1)
    └── rbx_scout.db.sync_state  <- tiny marker: how many syncs wrote this asset

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
API = "https://api.github.com"


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
         expect_json: bool = True, accept: str | None = None):
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
        with urllib.request.urlopen(req, timeout=120) as resp:
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


def upload_asset(rel: dict, token: str, name: str, data: bytes) -> None:
    """Upload one asset, replacing any existing asset of the same name."""
    for asset in rel.get("assets", []):
        if asset.get("name") == name:
            _api("DELETE", f"/repos/{repo_slug()}/releases/assets/{asset['id']}", token)
    upload_url = rel["upload_url"].split("{")[0]
    sep = "&" if "?" in upload_url else "?"
    url = f"{upload_url}{sep}name={name}"
    _api("POST", url, token, raw=data, content_type="application/octet-stream", expect_json=False)


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
    upload_asset(rel, token, ASSET_DB, blob)
    upload_asset(rel, token, ASSET_STATE, (state + "\n").encode())
    print(f"push: {DB_PATH.name}  {len(blob)/1e6:.1f} MB  sync #{state}"
          + (f"  (replaces sync #{old_state or 'none'})" if old_state and old_state != state else ""))
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
