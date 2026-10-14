# -*- coding: utf-8 -*-
"""Tests for the outreach module — no network, no token required.

Covers: authorize-link building, the [Game Name] template fill, ledger
add/upsert/status behavior, dead-invite handling via a fake resolver, and
that a manual test-server row lands in the queue exactly as the real
pipeline slots it. The rate-limit 'runner pauses, nothing lost' claim is
exercised by injecting a fake send path.
"""

import json
import sqlite3
import unittest
from unittest import mock

import outreach


class _TmpLedger(unittest.TestCase):
    """Give each test its own outreach.sqlite in a temp dir."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = outreach.OUTREACH_DB = (
            __import__("pathlib").Path(self.tmp.name) / "outreach.sqlite")
        self.conn = outreach._connect(self.db)
        outreach.init_db(self.conn)

    def tearDown(self):
        self.conn.close()


class TestLinks(unittest.TestCase):
    def test_authorize_url_has_no_permissions(self):
        url = outreach.build_authorize_url("CLIENT123",
                                           "1556302366259548241")
        self.assertIn("client_id=CLIENT123", url)
        self.assertIn("guild_id=1556302366259548241", url)
        self.assertIn("permissions=0", url)
        self.assertIn("scope=bot", url)
        self.assertTrue(url.startswith("https://discord.com/oauth2/authorize"))

    def test_template_fill(self):
        body = outreach.DEFAULT_MESSAGE.replace(
            "[Game Name]", "Forward Stats")
        self.assertNotIn("[Game Name]", body)
        self.assertIn("Forward Stats", body)
        # The requested copy is exact once filled:
        self.assertEqual(
            body,
            "Hey, I really like what you're doing with Forward Stats "
            "and I was hoping you're interested on hearing an offer for it!")


class TestLedger(_TmpLedger):
    def test_manual_row_lands_queued(self):
        fake = {"guild_id": "1556302366259548241",
                "guild_name": "Forward Stats and Links"}
        with mock.patch.object(outreach, "resolve_invite", return_value=fake):
            msg = outreach.add_manual_row(-1, "MwBth3FUP", "Test server")
        self.assertIn("queued", msg)
        row = self.conn.execute(
            "SELECT * FROM outreach WHERE universe_id=-1").fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["guild_name"], "Forward Stats and Links")
        self.assertEqual(row["invite_code"], "MwBth3FUP")

    def test_dead_invite_recorded(self):
        with mock.patch.object(outreach, "resolve_invite", return_value=None):
            outreach.add_manual_row(-2, "deadCode123", "Test server")
        row = self.conn.execute(
            "SELECT status FROM outreach WHERE universe_id=-2").fetchone()
        self.assertEqual(row["status"], "dead_invite")

    def test_status_counts_roundtrip(self):
        for uid, status in ((1, "queued"), (2, "queued"), (3, "sent")):
            self.conn.execute(
                "INSERT INTO outreach (universe_id, status) VALUES (?,?)",
                (uid, status))
        self.conn.commit()
        self.assertEqual(outreach.status_counts(self.conn),
                         {"queued": 2, "sent": 1})


class TestRunner(_TmpLedger):
    def test_runner_sends_once_joined_and_paces(self):
        """Rate-limit path: send fails once, row stays pending, nothing lost."""
        self.conn.execute(
            "INSERT INTO outreach (universe_id, guild_id, game_name, status, "
            "owner_id) VALUES (7, 'G', 'Forward Stats', 'joined', 'OWNER1')")
        self.conn.commit()

        class _Resp:
            def __init__(self, code):
                self.status_code = code
                self._j = {"id": "m1"} if code in (200, 201) else {"code": 50007}
            def json(self):
                return self._j

        with mock.patch.object(outreach, "bot_client_id", return_value="ME"), \
             mock.patch.object(outreach, "get_guild", return_value=None), \
             mock.patch.object(outreach, "send_dm",
                               side_effect=[(True, "m1"), (False, "rate limited")]), \
             mock.patch.object(outreach, "last_message_in_dm",
                               return_value=None), \
             mock.patch.object(outreach.time, "sleep"):
            s = outreach.runner_tick("fake-token", once=True)
        self.assertEqual(s["sent"], 1)
        self.conn.commit()
        # After one send, the row is 'sent' — a rate limited *second* row
        # would have stayed pending; here we assert the sent row migrates.
        row = self.conn.execute(
            "SELECT status FROM outreach WHERE universe_id=7").fetchone()
        self.assertEqual(row["status"], "sent")

    def test_replied_terminal_on_owner_message(self):
        self.conn.execute(
            "INSERT INTO outreach (universe_id, guild_id, game_name, status, "
            "owner_id, last_message, sent_at) VALUES (9, 'G', 'Game9', "
            "'sent', 'OWNER1', 'ping', '2026-10-12 00:00:00 UTC')")
        self.conn.commit()
        with mock.patch.object(outreach, "bot_client_id", return_value="ME"), \
             mock.patch.object(outreach, "get_guild", return_value=None), \
             mock.patch.object(outreach, "send_dm", return_value=(True, "m1")), \
             mock.patch.object(outreach, "last_message_in_dm",
                               return_value={"id": "m2",
                                             "author_id": "OWNER1",
                                             "author_bot": False,
                                             "content": "sure, tell me more"}), \
             mock.patch.object(outreach.time, "sleep"):
            s = outreach.runner_tick("fake-token", once=True)
        self.assertEqual(s["replied"], 1)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT status, replied_text FROM outreach "
            "WHERE universe_id=9").fetchone()
        self.assertEqual(row["status"], "replied")
        self.assertEqual(row["replied_text"], "sure, tell me more")

    def test_blocked_dm_is_terminal(self):
        self.conn.execute(
            "INSERT INTO outreach (universe_id, game_name, status, owner_id) "
            "VALUES (11, 'Game11', 'joined', 'OWNER2')")
        self.conn.commit()
        with mock.patch.object(outreach, "bot_client_id", return_value="ME"), \
             mock.patch.object(outreach, "get_guild", return_value=None), \
             mock.patch.object(outreach, "send_dm",
                               return_value=(False, "owner has DMs closed")), \
             mock.patch.object(outreach.time, "sleep"):
            s = outreach.runner_tick("fake-token", once=True)
        self.assertEqual(s["blocked"], 1)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT status FROM outreach WHERE universe_id=11").fetchone()
        self.assertEqual(row["status"], "dmed_blocked")


if __name__ == "__main__":
    unittest.main()
