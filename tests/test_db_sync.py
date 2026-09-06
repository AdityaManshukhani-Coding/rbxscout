"""Tests for db_sync.py — the release-backed catalog store.

These tests never touch GitHub. A threaded HTTP server impersonates
api.github.com (and the release-assets upload/download host), and
db_sync's repo/token resolution is pinned via environment variables.
The catalog-latest release semantics, marker round-trip, pull safety,
and clobber-push behaviour are all covered.
"""

from __future__ import annotations

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
        if name == db_sync.ASSET_DB:
            return self.assets.get("db", b"")
        return self.assets.get("state", b"")

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        self.requests.append(("GET", self.path))
        if self.path.startswith("/repos/x/y/releases/tags/"):
            tag = self.path.rsplit("/", 1)[-1]
            if tag in self.releases:
                return self._json(200, self.releases[tag])
            return self._json(404, {"message": "Not Found"})
        if self.path.startswith("/api/"):
            # asset download through the API URL; auth required like GitHub
            if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                return self._json(401, {"message": "Requires authentication"})
            name = self.path.rsplit("/", 1)[-1]
            body = self._asset_payload(name)
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
            length = int(self.headers.get("Content-Length", 0))
            name = self.path.split("name=")[-1]
            data = self.rfile.read(length)
            if name == db_sync.ASSET_DB:
                self.assets["db"] = data
            else:
                self.assets["state"] = data
            FakeGitHubHandler.next_id += 1
            asset = {
                "id": FakeGitHubHandler.next_id, "name": name, "size": len(data),
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
        FakeGitHubHandler.assets = {"db": b"", "state": b""}
        FakeGitHubHandler.requests = []
        FakeGitHubHandler.next_id = 100
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
        self.assertEqual(len(rel["assets"]), 2)
        self.assertEqual(FakeGitHubHandler.assets["db"], db_sync.DB_PATH.read_bytes())
        self.assertEqual(FakeGitHubHandler.assets["state"], b"41\n")
        # asset API urls must point at the fake host, not the real one
        self.assertIn(self.base, rel["upload_url"])

    def test_push_replaces_existing_assets_not_appends(self):
        self.make_local_db(games=2)
        db_sync.main(["db_sync.py", "push"])
        self.make_local_db(games=7)
        db_sync.STATE_PATH.write_text("42\n")
        db_sync.main(["db_sync.py", "push"])
        rel = self.release()
        self.assertEqual(len(rel["assets"]), 2)  # replaced, not duplicated
        blob = FakeGitHubHandler.assets["db"]
        self.assertEqual(len(blob), db_sync.DB_PATH.stat().st_size)

    def test_push_refuses_non_sqlite_file(self):
        db_sync.DB_PATH.write_bytes(b"definitely not sqlite")
        self.assertEqual(db_sync.main(["db_sync.py", "push"]), 1)

    def test_push_missing_db_errors(self):
        self.assertEqual(db_sync.main(["db_sync.py", "push"]), 1)


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
        FakeGitHubHandler.assets["db"] = b"<html>not a db</html>"
        with self.assertRaises(db_sync.SyncError):
            db_sync.cmd_pull()

    def test_status_reports_missing_release(self):
        rc = db_sync.main(["db_sync.py", "status"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
