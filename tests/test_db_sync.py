"""Tests for db_sync.py — the release-backed catalog store.

These tests never touch GitHub. A threaded HTTP server impersonates
api.github.com (and the release-assets upload/download host), and
db_sync's repo/token resolution is pinned via environment variables.
The catalog-latest release semantics, marker round-trip, pull safety,
and clobber-push behaviour are all covered.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(APP_DIR))
import db_sync  # noqa: E402

SQLITE_MAGIC = b"SQLite format 3\x00"


def make_db(path: Path, games: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS game_analytics (universe_id INTEGER PRIMARY KEY, title TEXT)"
        )
        conn.execute("DELETE FROM game_analytics")
        conn.executemany(
            "INSERT INTO game_analytics VALUES (?, ?)",
            [(i, f"game {i}") for i in range(games)],
        )
        conn.commit()
    finally:
        conn.close()


class FakeGitHubHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for the GitHub REST endpoints db_sync.py uses."""

    server_version = "FakeGitHub/1.0"

    # populated by setUpClass
    releases: dict = {}
    assets: dict = {}
    next_id = 100
    requests: list = []
    # Failure injection for testing the safe replace path.
    fail_uploads = False       # POST /api/* -> 500
    lie_about_size = False     # upload response reports a wrong size

    def log_message(self, *args):  # silence the test log
        pass

    # -- helpers ----------------------------------------------------------
    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _asset_payload(self, name: str) -> bytes:
        return self.assets.get(name, b"")

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        self.requests.append(("GET", self.path))
        if self.path.startswith("/repos/x/y/releases/tags/"):
            tag = self.path.rsplit("/", 1)[-1]
            if tag in self.releases:
                return self._json(200, self.releases[tag])
            return self._json(404, {"message": "Not Found"})
        if self.path.startswith("/api/"):
            # asset download through the API URL; auth required like GitHub.
            # The URL carries an asset id; resolve the asset's *current* name
            # (a rename PATCH moves the blob, as on real GitHub where the id
            # is the stable handle).
            if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                return self._json(401, {"message": "Requires authentication"})
            parts = self.path.split("/")
            asset_name: str | None = None
            try:
                aid = int(parts[3])  # /api/assets/<id>/<name>
            except (IndexError, ValueError):
                aid = None
            if aid is not None:
                for rel in self.releases.values():
                    for a in rel.get("assets", []):
                        if a["id"] == aid:
                            asset_name = a["name"]
            if asset_name is None:
                asset_name = parts[-1] if len(parts) > 3 else ""
            body = self._asset_payload(asset_name)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"message": "Not Found"})

    def do_POST(self):
        self.requests.append(("POST", self.path))
        if self.path.startswith("/api/"):  # asset upload
            if self.fail_uploads:
                return self._json(500, {"message": "upload failed (injected)"})
            length = int(self.headers.get("Content-Length", 0))
            name = self.path.split("name=")[-1]
            data = self.rfile.read(length)
            self.assets[name] = data
            FakeGitHubHandler.next_id += 1
            asset = {
                "id": FakeGitHubHandler.next_id, "name": name,
                "size": len(data) + (1 if self.lie_about_size else 0),
                "url": f"http://{self.headers.get('Host')}/api/assets/{FakeGitHubHandler.next_id}/{name}",
                "updated_at": "2026-09-06T00:00:00Z",
            }
            for rel in self.releases.values():
                rel["assets"] = [a for a in rel["assets"] if a["name"] != name]
                rel["assets"].append(asset)
            return self._json(201, asset)
        if self.path == "/repos/x/y/releases":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            tag = body.get("tag_name", "unknown")
            FakeGitHubHandler.next_id += 1
            rel = {
                "id": FakeGitHubHandler.next_id,
                "tag_name": tag,
                "html_url": f"https://example.test/releases/{tag}",
                "upload_url": f"http://{self.headers.get('Host')}/api/upload",
                "assets_url": f"https://api.github.test/assets",
                "assets": [],
            }
            self.releases[tag] = rel
            return self._json(201, rel)
        self._json(404, {"message": "Not Found"})

    def do_PATCH(self):
        self.requests.append(("PATCH", self.path))
        if self.path.startswith("/repos/x/y/releases/assets/"):
            # Rename an existing asset (PATCH {"name": ...}). Mirrors GitHub:
            # the blob stays the same, only the name moves.
            aid = int(self.path.rstrip("/").rsplit("/", 1)[-1])
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            for rel in self.releases.values():
                for asset in rel["assets"]:
                    if asset["id"] == aid:
                        old_name = asset["name"]
                        asset.update(body)
                        new_name = asset["name"]
                        if new_name != old_name and old_name in self.assets:
                            self.assets[new_name] = self.assets.pop(old_name)
                        return self._json(200, asset)
            return self._json(404, {"message": "Not Found"})
        if self.path.startswith("/repos/x/y/releases/"):
            rid = int(self.path.rstrip("/").rsplit("/", 1)[-1])
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            for rel in self.releases.values():
                if rel["id"] == rid:
                    rel.update(body)
                    return self._json(200, rel)
            return self._json(404, {"message": "Not Found"})
        self._json(404, {"message": "Not Found"})

    def do_DELETE(self):
        self.requests.append(("DELETE", self.path))
        if self.path.startswith("/repos/x/y/releases/assets/"):
            aid = int(self.path.rstrip("/").rsplit("/", 1)[-1])
            for rel in self.releases.values():
                rel["assets"] = [a for a in rel["assets"] if a["id"] != aid]
            return self._json(204, None)
        self._json(404, {"message": "Not Found"})


class FakeGitHub:
    """Lifecycle wrapper so each test class gets a pristine fake server."""

    @classmethod
    def start(cls):
        cls.handler = FakeGitHubHandler
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        return f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def stop(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()


class DBSyncTest(unittest.TestCase):
    """Shared fixtures: fake server, temp dir as APP_DIR, env pinned."""

    @classmethod
    def setUpClass(cls):
        cls.base = FakeGitHub.start()
        # Point db_sync's module-level constants at the fake server and a
        # temp workspace so the real repo files are never touched.
        cls._orig = {k: getattr(db_sync, k) for k in ("API", "APP_DIR", "DB_PATH", "STATE_PATH", "repo_slug")}
        db_sync.API = cls.base
        cls.tmp = tempfile.TemporaryDirectory()
        db_sync.APP_DIR = Path(cls.tmp.name)
        db_sync.DB_PATH = Path(cls.tmp.name) / "rbx_scout.db"
        db_sync.STATE_PATH = Path(cls.tmp.name) / "rbx_scout.db.sync_state"

    @classmethod
    def tearDownClass(cls):
        db_sync.API, db_sync.APP_DIR = cls._orig["API"], cls._orig["APP_DIR"]
        db_sync.DB_PATH, db_sync.STATE_PATH = cls._orig["DB_PATH"], cls._orig["STATE_PATH"]
        FakeGitHub.stop()
        cls.tmp.cleanup()

    def setUp(self):
        FakeGitHubHandler.releases = {}
        FakeGitHubHandler.assets = {db_sync.ASSET_DB: b"", db_sync.ASSET_STATE: b""}
        FakeGitHubHandler.requests = []
        FakeGitHubHandler.next_id = 100
        FakeGitHubHandler.fail_uploads = False
        FakeGitHubHandler.lie_about_size = False
        self._env = {k: os.environ.pop(k, None) for k in
                     ("RBXSCOUT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "RBXSCOUT_GITHUB_REPO")}
        os.environ["RBXSCOUT_GITHUB_TOKEN"] = "test-token"
        os.environ["RBXSCOUT_GITHUB_REPO"] = "x/y"
        # per-test isolation: scrub local db artifacts from earlier tests
        for p in (db_sync.DB_PATH, db_sync.STATE_PATH,
                  db_sync.DB_PATH.with_name(db_sync.DB_PATH.name + "-wal"),
                  db_sync.DB_PATH.with_name(db_sync.DB_PATH.name + "-shm"),
                  db_sync.DB_PATH.with_suffix(".db.tmp")):
            p.unlink(missing_ok=True)

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    # -- helpers ----------------------------------------------------------
    def release(self) -> dict:
        return FakeGitHubHandler.releases[db_sync.RELEASE_TAG]

    def make_local_db(self, games: int = 3) -> None:
        make_db(db_sync.DB_PATH, games=games)


class TestPush(DBSyncTest):
    def test_first_push_creates_release_and_uploads_both_assets(self):
        self.make_local_db(games=5)
        db_sync.STATE_PATH.write_text("41\n")
        rc = db_sync.main(["db_sync.py", "push"])
        self.assertEqual(rc, 0)
        rel = self.release()
        self.assertEqual(rel["tag_name"], "catalog-latest")
        # db + sync_state + stats.json (stats_target.json is skipped: the
        # minimal test DB has no visits/ccu columns to count against).
        self.assertEqual(len(rel["assets"]), 3)
        self.assertEqual(FakeGitHubHandler.assets[db_sync.ASSET_DB], db_sync.DB_PATH.read_bytes())
        self.assertEqual(FakeGitHubHandler.assets[db_sync.ASSET_STATE], b"41\n")
        stats = json.loads(FakeGitHubHandler.assets[db_sync.ASSET_STATS].decode())
        self.assertEqual(stats["games"], 5)
        self.assertTrue(stats["message"].startswith("5"))
        # the release page's 📊 counts line was refreshed via PATCH
        patch_calls = [r for r in FakeGitHubHandler.requests if r[0] == "PATCH"]
        self.assertTrue(patch_calls)
        self.assertTrue(rel["body"].startswith("📊 **5 games**"))
        # asset API urls must point at the fake host, not the real one
        self.assertIn(self.base, rel["upload_url"])

    def test_push_replaces_existing_assets_not_appends(self):
        self.make_local_db(games=2)
        db_sync.main(["db_sync.py", "push"])
        self.make_local_db(games=7)
        db_sync.STATE_PATH.write_text("42\n")
        db_sync.main(["db_sync.py", "push"])
        rel = self.release()
        self.assertEqual(len(rel["assets"]), 3)  # replaced, not duplicated
        blob = FakeGitHubHandler.assets[db_sync.ASSET_DB]
        self.assertEqual(len(blob), db_sync.DB_PATH.stat().st_size)

    def test_push_refuses_non_sqlite_file(self):
        db_sync.DB_PATH.write_bytes(b"definitely not sqlite")
        self.assertEqual(db_sync.main(["db_sync.py", "push"]), 1)

    def test_push_missing_db_errors(self):
        self.assertEqual(db_sync.main(["db_sync.py", "push"]), 1)

    def test_failed_upload_leaves_old_catalog_intact(self):
        """The 2026-09-09 outage: an upload dying mid-push must never empty
        the store. The safe replace uploads first, so a failed upload leaves
        the previous catalog asset exactly as it was."""
        self.make_local_db(games=3)
        db_sync.main(["db_sync.py", "push"])
        original = FakeGitHubHandler.assets[db_sync.ASSET_DB]
        self.make_local_db(games=9)
        FakeGitHubHandler.fail_uploads = True
        self.assertEqual(db_sync.main(["db_sync.py", "push"]), 1)
        # The store still serves the OLD catalog, byte for byte.
        self.assertEqual(FakeGitHubHandler.assets[db_sync.ASSET_DB], original)
        rel = self.release()
        db_assets = [a for a in rel["assets"] if a["name"] == db_sync.ASSET_DB]
        self.assertEqual(len(db_assets), 1, "old catalog asset must survive")

    def test_size_mismatch_aborts_before_old_asset_is_touched(self):
        """A truncated/corrupted upload (GitHub reports a different size than
        what we sent) is detected while the old asset is still in place."""
        self.make_local_db(games=3)
        db_sync.main(["db_sync.py", "push"])
        original = FakeGitHubHandler.assets[db_sync.ASSET_DB]
        self.make_local_db(games=8)
        FakeGitHubHandler.lie_about_size = True
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(db_sync.main(["db_sync.py", "push"]), 1)
        self.assertIn("verification failed", stderr.getvalue())
        self.assertEqual(FakeGitHubHandler.assets[db_sync.ASSET_DB], original)

    def test_replace_uses_incoming_and_leaves_no_leftover(self):
        """The catalog is uploaded as <name>.incoming, then swapped; after a
        successful push no .incoming asset remains."""
        self.make_local_db(games=2)
        db_sync.main(["db_sync.py", "push"])
        self.make_local_db(games=6)
        db_sync.main(["db_sync.py", "push"])
        names = [a["name"] for a in self.release()["assets"]]
        self.assertNotIn(db_sync.ASSET_DB + db_sync.INCOMING_SUFFIX, names)
        self.assertIn(db_sync.ASSET_DB, names)
        # And the served catalog is the newest blob.
        blob = FakeGitHubHandler.assets[db_sync.ASSET_DB]
        self.assertEqual(len(blob), db_sync.DB_PATH.stat().st_size)


class TestPull(DBSyncTest):
    def test_roundtrip_push_then_pull(self):
        self.make_local_db(games=9)
        db_sync.STATE_PATH.write_text("77\n")
        db_sync.main(["db_sync.py", "push"])
        # mutate the local copy so pull has something to restore
        db_sync.DB_PATH.unlink()
        make_db(db_sync.DB_PATH, games=1)
        db_sync.STATE_PATH.write_text("0\n")
        rc = db_sync.main(["db_sync.py", "pull"])
        self.assertEqual(rc, 0)
        conn = sqlite3.connect(str(db_sync.DB_PATH))
        try:
            n = conn.execute("SELECT COUNT(*) FROM game_analytics").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 9)
        self.assertEqual(db_sync.STATE_PATH.read_text().strip(), "77")

    def test_pull_before_any_push_errors(self):
        self.assertEqual(db_sync.main(["db_sync.py", "pull"]), 1)

    def test_pull_refuses_non_sqlite_asset(self):
        # Hand-craft a release whose db asset is not a SQLite file.
        FakeGitHubHandler.next_id += 1
        FakeGitHubHandler.releases[db_sync.RELEASE_TAG] = {
            "id": FakeGitHubHandler.next_id, "tag_name": db_sync.RELEASE_TAG,
            "html_url": "https://example.test/r", "upload_url": f"{self.base}/api/upload",
            "assets": [{"id": 1, "name": db_sync.ASSET_DB, "size": 21,
                        "url": f"{self.base}/api/assets/1/{db_sync.ASSET_DB}",
                        "updated_at": "2026-09-06T00:00:00Z"}],
        }
        FakeGitHubHandler.assets[db_sync.ASSET_DB] = b"<html>not a db</html>"
        with self.assertRaises(db_sync.SyncError):
            db_sync.cmd_pull()

    def test_status_reports_missing_release(self):
        rc = db_sync.main(["db_sync.py", "status"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
