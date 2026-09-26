"""Split-catalog tests: Discord links must never reach the public store.

Covers contacts_privacy end-to-end (encrypt/decrypt tamper rejection,
bundle round-trip, public-copy stripping) and the db_sync push/pull
integration on the fake GitHub: a push stores a link-free catalog asset
plus the encrypted bundle, and a pull restores the owner's links.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db_sync
import contacts_privacy

from test_db_sync import DBSyncTest, FakeGitHubHandler  # fake-GitHub fixtures


def make_contacts_db(path: Path) -> None:
    """A catalog shaped like the real one: game rows + revival archive."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE game_analytics ("
            "universe_id INTEGER PRIMARY KEY, title TEXT, has_discord BOOLEAN, "
            "discord_url TEXT, status TEXT, found_via TEXT, "
            "has_social_links BOOLEAN, contacts_checked_at TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE contact_archive ("
            "universe_id INTEGER PRIMARY KEY, has_discord BOOLEAN, "
            "discord_url TEXT, status TEXT, found_via TEXT, "
            "has_social_links BOOLEAN, contacts_checked_at TIMESTAMP, "
            "title TEXT)"
        )
        conn.executemany(
            "INSERT INTO game_analytics VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "rich game", 1, "https://discord.gg/rich", "OK",
                 "game_social_links", 1, "2026-09-25 10:00:00"),
                (2, "checked-empty", 0, None, "No Contact Found", None, 0,
                 "2026-09-25 10:00:00"),
            ],
        )
        conn.execute(
            "INSERT INTO contact_archive VALUES "
            "(999, 1, 'https://discord.gg/deadgame', 'OK', 'game_social_links', "
            "1, '2026-09-01 10:00:00', 'long dead')"
        )
        conn.commit()
    finally:
        conn.close()


class TestCrypto(unittest.TestCase):
    KEY = b"\x11" * 32

    def test_roundtrip(self):
        blob = contacts_privacy.encrypt_bytes(self.KEY, b"hello catalog")
        self.assertNotIn(b"hello", blob)
        self.assertEqual(contacts_privacy.decrypt_bytes(self.KEY, blob), b"hello catalog")

    def test_wrong_key_rejected(self):
        blob = contacts_privacy.encrypt_bytes(self.KEY, b"secret links")
        with self.assertRaises(ValueError):
            contacts_privacy.decrypt_bytes(b"\x22" * 32, blob)

    def test_tampered_ciphertext_rejected(self):
        blob = bytearray(contacts_privacy.encrypt_bytes(self.KEY, b"secret links"))
        blob[-1] ^= 0x01  # flip one bit
        with self.assertRaises(ValueError):
            contacts_privacy.decrypt_bytes(self.KEY, bytes(blob))

    def test_truncated_blob_rejected(self):
        with self.assertRaises(ValueError):
            contacts_privacy.decrypt_bytes(self.KEY, b"tiny")


class TestBundle(unittest.TestCase):
    KEY = b"\x33" * 32

    def _db(self) -> tuple[Path, tempfile.TemporaryDirectory]:
        td = tempfile.TemporaryDirectory()
        db = Path(td.name) / "rbx_scout.db"
        make_contacts_db(db)
        return db, td

    def test_build_contains_links_and_archive(self):
        db, td = self._db()
        try:
            payload = contacts_privacy.build_bundle_payload(db)
        finally:
            td.cleanup()
        self.assertEqual(payload["version"], contacts_privacy.BUNDLE_VERSION)
        self.assertEqual(payload["rows"]["1"]["discord_url"], "https://discord.gg/rich")
        self.assertIsNone(payload["rows"]["2"]["discord_url"])
        self.assertEqual(payload["archive"]["999"]["discord_url"], "https://discord.gg/deadgame")

    def test_bundle_roundtrip_is_link_free_and_restorable(self):
        db, td = self._db()
        try:
            blob = contacts_privacy.encrypt_bundle(
                self.KEY, contacts_privacy.build_bundle_payload(db)
            )
            # The encrypted blob must not leak the links in plaintext.
            self.assertNotIn(b"discord.gg/rich", blob)
            # Strip a public copy, then re-apply the bundle to it.
            public = Path(td.name) / "public.db"
            removed = contacts_privacy.strip_public_copy(db, public)
            self.assertEqual(removed, 1)  # only the game row held a link

            conn = sqlite3.connect(str(public))
            try:
                links = conn.execute(
                    "SELECT COUNT(*) FROM game_analytics WHERE discord_url IS NOT NULL"
                ).fetchone()[0]
                flags = conn.execute(
                    "SELECT COUNT(*) FROM game_analytics WHERE has_discord = 1"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(links, 0, "public copy must carry no links")
            self.assertEqual(flags, 1, "has_discord must survive the strip")

            rows, archive_rows = contacts_privacy.apply_bundle(
                public, contacts_privacy.decrypt_bundle(self.KEY, blob)
            )
            self.assertEqual((rows, archive_rows), (2, 1))
            conn = sqlite3.connect(str(public))
            try:
                link = conn.execute(
                    "SELECT discord_url FROM game_analytics WHERE universe_id = 1"
                ).fetchone()[0]
                dead = conn.execute(
                    "SELECT discord_url FROM contact_archive WHERE universe_id = 999"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(link, "https://discord.gg/rich")
            self.assertEqual(dead, "https://discord.gg/deadgame")
        finally:
            td.cleanup()

    def test_apply_bundle_preserves_other_columns(self):
        db, td = self._db()
        try:
            conn = sqlite3.connect(str(db))
            try:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN title2 TEXT")
                conn.execute("UPDATE game_analytics SET title2 = 'keep me' WHERE universe_id = 1")
                conn.commit()
            finally:
                conn.close()
            payload = contacts_privacy.build_bundle_payload(db)
            payload["rows"]["1"]["discord_url"] = "https://discord.gg/updated"
            contacts_privacy.apply_bundle(db, payload)
            conn = sqlite3.connect(str(db))
            try:
                title2, link = conn.execute(
                    "SELECT title2, discord_url FROM game_analytics WHERE universe_id = 1"
                ).fetchone()
            finally:
                conn.close()
        finally:
            td.cleanup()
        self.assertEqual(title2, "keep me", "non-contact columns must be untouched")
        self.assertEqual(link, "https://discord.gg/updated")

    def test_wrong_key_bundle_rejected(self):
        db, td = self._db()
        try:
            blob = contacts_privacy.encrypt_bundle(
                self.KEY, contacts_privacy.build_bundle_payload(db)
            )
        finally:
            td.cleanup()
        with self.assertRaises(ValueError):
            contacts_privacy.decrypt_bundle(b"\x44" * 32, blob)


class TestPushPullSplit(DBSyncTest):
    """db_sync integration on the fake GitHub: push strips, pull restores."""

    def _local_db_with_links(self) -> None:
        make_contacts_db(db_sync.DB_PATH)

    def test_push_uploads_stripped_catalog_plus_encrypted_bundle(self):
        self._local_db_with_links()
        contacts_privacy.write_key(contacts_privacy.KEY_PATH)
        db_sync.STATE_PATH.write_text("55\n")
        self.assertEqual(db_sync.main(["db_sync.py", "push"]), 0)
        rel = self.release()
        names = [a["name"] for a in rel["assets"]]
        self.assertIn(contacts_privacy.ASSET_BUNDLE, names)
        # Store: the catalog asset is link-free.
        public_blob = FakeGitHubHandler.assets[db_sync.ASSET_DB]
        self.assertNotIn(b"discord.gg/rich", public_blob)
        # ...and the bundle is encrypted (no plaintext links) yet decryptable.
        bundle_blob = FakeGitHubHandler.assets[contacts_privacy.ASSET_BUNDLE]
        self.assertNotIn(b"discord.gg", bundle_blob, "bundle must be encrypted")
        payload = contacts_privacy.decrypt_bundle(contacts_privacy.load_key(), bundle_blob)
        self.assertEqual(payload["rows"]["1"]["discord_url"], "https://discord.gg/rich")
        # Local DB untouched by the push (the strip happens on a copy).
        conn = sqlite3.connect(str(db_sync.DB_PATH))
        try:
            link = conn.execute(
                "SELECT discord_url FROM game_analytics WHERE universe_id = 1"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(link, "https://discord.gg/rich")

    def test_push_without_key_pushes_stripped_catalog_skips_bundle(self):
        """Keyless push degrades gracefully: the stripped catalog still lands
        (pipeline never stalls) but no bundle is uploaded and the old bundle,
        if links leak.
        """
        self._local_db_with_links()
        contacts_privacy.KEY_PATH.unlink(missing_ok=True)  # setUp installed one
        db_sync.STATE_PATH.write_text("58\n")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(db_sync.main(["db_sync.py", "push"]), 0)
        self.assertIn("skipped the contacts bundle", stdout.getvalue())
        names = [a["name"] for a in self.release()["assets"]]
        self.assertIn(db_sync.ASSET_DB, names)
        self.assertNotIn(contacts_privacy.ASSET_BUNDLE, names)
        self.assertNotIn(b"discord.gg/rich", FakeGitHubHandler.assets[db_sync.ASSET_DB])

    def test_pull_restores_links_from_bundle(self):
        self._local_db_with_links()
        contacts_privacy.write_key(contacts_privacy.KEY_PATH)
        db_sync.STATE_PATH.write_text("56\n")
        self.assertEqual(db_sync.main(["db_sync.py", "push"]), 0)
        # Simulate a fresh keyless consumer machine: wipe the local copy and
        # install a stub (what the public asset alone would give).
        db_sync.DB_PATH.unlink()
        stub = sqlite3.connect(str(db_sync.DB_PATH))
        try:
            stub.execute(
                "CREATE TABLE game_analytics (universe_id INTEGER PRIMARY KEY, title TEXT)"
            )
            stub.execute("INSERT INTO game_analytics VALUES (1, 'stripped world')")
            stub.commit()
        finally:
            stub.close()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(db_sync.main(["db_sync.py", "pull"]), 0)
        self.assertIn("contacts restored", stdout.getvalue())
        conn = sqlite3.connect(str(db_sync.DB_PATH))
        try:
            link = conn.execute(
                "SELECT discord_url FROM game_analytics WHERE universe_id = 1"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(link, "https://discord.gg/rich")

    def test_pull_without_key_leaves_public_copy(self):
        self._local_db_with_links()
        contacts_privacy.write_key(contacts_privacy.KEY_PATH)
        db_sync.STATE_PATH.write_text("57\n")
        self.assertEqual(db_sync.main(["db_sync.py", "push"]), 0)
        # Deployment without CONTACTS_KEY: key file gone, env unset.
        contacts_privacy.KEY_PATH.unlink()
        db_sync.DB_PATH.unlink()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(db_sync.main(["db_sync.py", "pull"]), 0)
        self.assertIn("no CONTACTS_KEY", stdout.getvalue())
        conn = sqlite3.connect(str(db_sync.DB_PATH))
        try:
            link = conn.execute(
                "SELECT discord_url FROM game_analytics WHERE universe_id = 1"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(link, "keyless pull must leave the public copy")


if __name__ == "__main__":
    unittest.main()
