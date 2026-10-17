"""Take-down removal sweep tests (2026-10-17, owner request).

Covers the soft-delete chain end to end with all HTTP faked:
migration (columns + atlas_last_seen backfill), Atlas-harvest stamping +
instant re-add of tombstoned rows, the sweep's suspicion trigger
(off Atlas for REMOVAL_STALE_HOURS), direct Roblox verification
(absent = soft-delete, present = keep), the fail-closed contract (an API
outage writes NOTHING), the self-throttle, the kill switch, and the
catalog loaders hiding tombstoned rows.
"""

import os
import sqlite3

import pandas as pd
import pytest

from scout_core import RobloxPlatformScout

# Realistic universe IDs — the harvest's index regex only accepts 6–15 digit
# IDs, so the fixtures below must speak Atlas's actual format.
UID_DOOMED = 111000001
UID_ALIVE = 222000001
UID_FRESH = 999000001


@pytest.fixture()
def scout(tmp_path, monkeypatch):
    # Pin every removal/Atlas knob to default: the Actions environment
    # exports REMOVAL_* into CI runs and must not skew the sweep contract.
    for name in ("REMOVAL_STALE_HOURS", "REMOVAL_SWEEP_MAX_GAMES",
                 "REMOVAL_SWEEP_HOURS", "ATLAS_ROLL_PAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ATLAS_DEEP_EVERY_DAYS", "0")
    monkeypatch.setenv("ATLAS_REQUEST_DELAY", "0")
    s = RobloxPlatformScout(db_path=str(tmp_path / "removal.db"))
    s.upsert_game({"universe_id": UID_DOOMED, "title": "Doomed Game", "ccu": 500,
                   "peak_ccu": 500, "visits": 500_000, "found_via": "atlas_dev"})
    s.upsert_game({"universe_id": UID_ALIVE, "title": "Healthy Game", "ccu": 800,
                   "peak_ccu": 800, "visits": 800_000, "found_via": "atlas_dev"})
    yield s
    s.session.close()


@pytest.fixture()
def db(scout):
    conn = sqlite3.connect(scout.db_path)
    yield conn
    conn.close()


def _age_atlas_last_seen(db, universe_id, hours):
    """Force a row Atlas has NOT listed for `hours`."""
    with db:
        db.execute(
            f"UPDATE game_analytics SET atlas_last_seen = "
            f"datetime('now', '-{int(hours)} hours') WHERE universe_id = ?",
            (universe_id,),
        )


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #

def test_migration_writes_takedown_columns_and_backfills_atlas_seen(scout, db):
    cols = {row[1] for row in db.execute("PRAGMA table_info(game_analytics)").fetchall()}
    for name in ("atlas_last_seen", "removed", "removed_suspect", "removed_at",
                 "removed_checked_at"):
        assert name in cols, f"migration must add game_analytics.{name}"
    null_stamps = db.execute(
        "SELECT COUNT(*) FROM game_analytics WHERE atlas_last_seen IS NULL"
    ).fetchone()[0]
    assert null_stamps == 0, (
        "atlas_last_seen is backfilled on migration — a NULL would suspect the "
        "ENTIRE existing catalog the day the migration lands"
    )
    rows = scout.load_table([UID_DOOMED, UID_ALIVE])
    assert set(rows["removed"]) == {0} and set(rows["removed_suspect"]) == {0}


# --------------------------------------------------------------------------- #
# Harvest stamping + quick re-add
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200
        self.headers = {}


INDEX_PAGE = (
    '<html>'
    f'<a href="/analyze/{UID_DOOMED}">revived</a>'   # still listed → re-add + stamp
    f'<a href="/analyze/{UID_ALIVE}">healthy</a>'
    f'<a href="/analyze/{UID_FRESH}">fresh seed</a>'
    '</html>'
)


def test_harvest_stamps_atlas_seen_and_instantly_readds_removed(monkeypatch, tmp_path):
    for name in ("REMOVAL_STALE_HOURS", "REMOVAL_SWEEP_MAX_GAMES",
                 "REMOVAL_SWEEP_HOURS", "ATLAS_ROLL_PAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ATLAS_DEEP_EVERY_DAYS", "0")
    monkeypatch.setenv("ATLAS_REQUEST_DELAY", "0")
    s = RobloxPlatformScout(db_path=str(tmp_path / "readd.db"))
    s.upsert_game({"universe_id": UID_DOOMED, "title": "Removed Then Back", "ccu": 300,
                   "peak_ccu": 300, "visits": 300_000})
    s.upsert_game({"universe_id": UID_ALIVE, "title": "Suspect But Alive", "ccu": 400,
                   "peak_ccu": 400, "visits": 400_000})
    with sqlite3.connect(s.db_path) as conn:
        # UID_DOOMED: tombstoned yesterday by a WRONG verdict — Roblox relisted.
        conn.execute(
            "UPDATE game_analytics SET removed = 1, removed_suspect = 0, "
            f"removed_at = datetime('now', '-1 day') WHERE universe_id = {UID_DOOMED}"
        )
        # UID_ALIVE: still merely suspicious.
        conn.execute(
            "UPDATE game_analytics SET removed_suspect = 1 "
            f"WHERE universe_id = {UID_ALIVE}"
        )
    monkeypatch.setattr(
        type(s), "_atlas_fetch", lambda self, url, delay: _FakeResp(INDEX_PAGE)
    )
    stats = s.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0,
                                  deep_every_days=0)
    assert stats["fetched_ids"] == 3
    assert stats["readded"] == 2, "both the tombstone and the suspect row revive"
    assert stats["enqueued"] >= 1, "re-listed IDs also re-enter discovery as usual"

    revived = s.load_table([UID_DOOMED]).iloc[0]
    assert int(revived["removed"]) == 0 and int(revived["removed_suspect"]) == 0
    assert pd.isna(revived["removed_at"]), "the tombstone is wiped, not just masked"
    suspect = s.load_table([UID_ALIVE]).iloc[0]
    assert int(suspect["removed_suspect"]) == 0
    s.session.close()


# --------------------------------------------------------------------------- #
# Removal sweep
# --------------------------------------------------------------------------- #

def _patch_verify(monkeypatch, scout, present_ids: set, status=200):
    """Fake the live games API: only `present_ids` come back; other requested
    IDs are OMITTED (Roblox's real behavior: a removed universe vanishes from
    the batch response)."""
    def fake_get_json(self, url, *a, **k):
        if "universeIds=" not in url:
            raise AssertionError(f"unexpected URL in removal sweep: {url}")
        requested = [int(x) for x in url.split("universeIds=")[-1].split(",")]
        return status, {
            "data": [
                {"id": uid, "name": f"game {uid}", "playing": 10}
                for uid in sorted(set(requested) & present_ids)
            ]
        }
    monkeypatch.setattr(type(scout), "_get_json", fake_get_json)


def test_removal_sweep_soft_deletes_confirmed_removals_and_keeps_alive(scout, db, monkeypatch):
    _age_atlas_last_seen(db, UID_DOOMED, 80)  # off Atlas 80 h → suspect
    _age_atlas_last_seen(db, UID_ALIVE, 80)   # suspect too, but Roblox alive
    _patch_verify(monkeypatch, scout, present_ids={UID_ALIVE})
    stats = scout.run_removal_sweep(stale_hours=72, throttle_hours=0)
    assert stats["candidates"] == 2
    assert stats["removed"] == 1
    assert stats["verified_present"] == 1

    dead = scout.load_table([UID_DOOMED]).iloc[0]
    assert int(dead["removed"]) == 1, "confirmed absent → soft-deleted"
    assert int(dead["removed_suspect"]) == 0
    alive = scout.load_table([UID_ALIVE]).iloc[0]
    assert int(alive["removed"]) == 0, "Roblox says alive → stays in the catalog"
    assert int(alive["removed_suspect"]) == 0, "suspicion cleared on proof of life"
    tombstones = db.execute(
        "SELECT removed_at IS NOT NULL, removed_checked_at IS NOT NULL "            f"FROM game_analytics WHERE universe_id = {UID_DOOMED}"
    ).fetchone()
    assert all(tombstones), "tombstone timestamps written"
    first_seen_kept = db.execute(
        f"SELECT first_seen IS NOT NULL FROM game_analytics WHERE universe_id = {UID_DOOMED}"
    ).fetchone()[0]
    assert first_seen_kept, "first_seen (scouting history) is NEVER touched"


def test_removed_rows_leave_the_catalog_but_keep_their_data(scout, db, monkeypatch):
    _age_atlas_last_seen(db, UID_DOOMED, 80)
    _patch_verify(monkeypatch, scout, present_ids=set())  # Roblox confirmed gone
    scout.run_removal_sweep(stale_hours=72, throttle_hours=0)
    catalog = scout.load_catalog_matches(min_visits=0, min_ccu=0)
    assert UID_DOOMED not in set(catalog["universe_id"]), "tombstoned rows never surface"
    assert UID_ALIVE in set(catalog["universe_id"]), "healthy rows still list"
    watch = scout.load_blowup_watch()
    assert UID_DOOMED not in set(watch["universe_id"])
    still_there = db.execute(
        f"SELECT COUNT(*) FROM game_analytics WHERE universe_id = {UID_DOOMED}"
    ).fetchone()[0]
    assert still_there == 1, "SOFT delete only — the row (and history) stays"


def test_removal_sweep_fail_closed_on_api_outage(scout, db, monkeypatch):
    _age_atlas_last_seen(db, UID_DOOMED, 80)

    def dead_api(self, url, *a, **k):
        return 503, None

    monkeypatch.setattr(type(scout), "_get_json", dead_api)
    stats = scout.run_removal_sweep(stale_hours=72, throttle_hours=0)
    assert stats["aborted"] is True
    assert stats["removed"] == 0
    df = scout.load_catalog_matches(min_visits=0, min_ccu=0)
    assert UID_DOOMED in set(df["universe_id"]), (
        "an outage must NEVER masquerade as a take-down"
    )


def test_removal_sweep_throttles_after_a_run(scout):
    scout.run_removal_sweep(stale_hours=72, throttle_hours=0)
    stats = scout.run_removal_sweep(stale_hours=72)  # default 6 h window
    assert stats["throttled"] is True
    assert stats["removed"] == 0


def test_removal_sweep_kill_switch(scout, monkeypatch):
    stats = scout.run_removal_sweep(stale_hours=72, throttle_hours=0, max_games=0)
    assert stats == {"candidates": 0, "verified_present": 0, "removed": 0,
                     "batches": 0, "failed_batches": 0, "throttled": False,
                     "aborted": False}, "kill switch returns the empty map"
