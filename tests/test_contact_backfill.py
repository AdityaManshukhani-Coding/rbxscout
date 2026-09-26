"""Contact backfill + revival-memory tests.

The user-facing contract (2026-09-24):
  * every persisted verdict also lands in contact_archive (paid lookups are
    never thrown away),
  * pruned games keep their archived verdict — when a decayer/dead giant
    comes back alive and re-qualifies, the contact is restored for free,
  * contacts_backfill_due lists gate-qualified unchecked games first
    (oldest first), optionally extending to the decayed band,
  * the batched page-write keeps the archive in step.
"""

import sqlite3

import scout_core
from scout_core import RobloxPlatformScout


def _seed_game(scout: RobloxPlatformScout, uid: int, *, ccu=50, visits=50_000, title=None):
    scout.upsert_game({
        "universe_id": uid,
        "title": title or f"Game {uid}",
        "ccu": ccu,
        "visits": visits,
    })


def _verdict(uid: int, *, has=True, url="https://discord.gg/x"):
    return {
        "universe_id": uid,
        "has_discord": has,
        "discord_url": url if has else None,
        "status": "OK" if has else "No Contact Found",
        "found_via": "game_social_links" if has else None,
        "has_social_links": has,
        "contacts_checked_at": "2026-09-24 10:00:00",
    }


# --------------------------------------------------------------------------- #
# Archive write-through
# --------------------------------------------------------------------------- #


def test_single_verdict_is_archived(tmp_path):
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    _seed_game(scout, 1)
    scout._store_contact_cache(1, _verdict(1))
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT has_discord, discord_url, status FROM contact_archive WHERE universe_id=1"
        ).fetchone()
    assert row == (1, "https://discord.gg/x", "OK")


def test_batched_page_write_archives_too(tmp_path):
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    _seed_game(scout, 1)
    _seed_game(scout, 2)
    scout._store_contact_verdicts_batch({1: _verdict(1), 2: _verdict(2, has=False, url=None)})
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute(
            "SELECT universe_id, has_discord FROM contact_archive").fetchall())
    assert rows == {1: 1, 2: 0}


# --------------------------------------------------------------------------- #
# Revival memory
# --------------------------------------------------------------------------- #


def test_pruned_game_retains_verdict_and_revives_with_it(tmp_path):
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    _seed_game(scout, 7, ccu=0, visits=30_000, title="Phoenix")
    scout._store_contact_cache(7, _verdict(7))

    # Game observed dead 4 times -> pruned (strikes start at 0 on INSERT;
    # the +1 CASE in upsert_game only fires on re-hydrations).
    with sqlite3.connect(db) as conn:
        for _ in range(4):
            conn.execute(
                "UPDATE game_analytics SET zero_ccu_strikes = zero_ccu_strikes + 1 "
                "WHERE universe_id=7")
    removed = scout.prune_catalog()
    assert removed == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM game_analytics WHERE universe_id=7").fetchone()[0] == 0
        # The paid verdict survived the delete.
        assert conn.execute(
            "SELECT COUNT(*) FROM contact_archive WHERE universe_id=7").fetchone()[0] == 1

    # The game comes back alive and re-qualifies: fresh row, no contact state.
    _seed_game(scout, 7, ccu=120, visits=31_000, title="Phoenix")
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT contacts_checked_at FROM game_analytics WHERE universe_id=7"
        ).fetchone()[0] is None

    revived = scout._revive_archived_contacts([7])
    assert revived == 1
    with sqlite3.connect(db) as conn:
        has_discord, url, checked = conn.execute(
            "SELECT has_discord, discord_url, contacts_checked_at "
            "FROM game_analytics WHERE universe_id=7").fetchone()
    assert has_discord == 1
    assert url == "https://discord.gg/x"
    assert checked == "2026-09-24 10:00:00"


def test_resolve_serves_archived_verdict_without_network(tmp_path):
    """A revived game re-qualifying must not re-resolve: the archive answers."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    _seed_game(scout, 9, ccu=0, visits=25_000)
    scout._store_contact_cache(9, _verdict(9))
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM game_analytics WHERE universe_id=9")
    _seed_game(scout, 9, ccu=80, visits=26_000)

    class Boom(RobloxPlatformScout):
        def _get_json(self, url, retries=1):
            raise AssertionError("archived verdict must not trigger a live lookup")

    boom = Boom.__new__(Boom)
    boom.__dict__.update(scout.__dict__)
    rec = boom.resolve_game_contact({"universe_id": 9})
    assert rec["has_discord"] is True
    assert rec["discord_url"] == "https://discord.gg/x"


def test_stale_negative_archives_expire(tmp_path):
    """'No Contact Found' archives go stale on the recheck clock; hits don't."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    import time as _time

    stale_ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(_time.time() - 7 * 3600))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO contact_archive (universe_id, has_discord, status, "
            "contacts_checked_at, contact_schema_version) VALUES (11, 0, 'No Contact Found', ?, 2)",
            (stale_ts,))
    assert scout._load_archived_contact(11) is None  # stale negative -> re-check
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE contact_archive SET has_discord=1, status='OK', "
            "discord_url='https://discord.gg/live' WHERE universe_id=11")
    assert scout._load_archived_contact(11) is not None  # hits never expire


def test_drain_revives_archived_contacts_for_qualifiers(tmp_path):
    """The expansion drain's upsert path restores archived verdicts."""
    import tests.test_expansion as te

    db = str(tmp_path / "t.db")
    scout = te.ExpansionScout({
        "metrics:801": te._game(801, ccu=40, visits=250_000, title="Comeback"),
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO contact_archive (universe_id, has_discord, discord_url, "
            "status, contacts_checked_at, contact_schema_version) "
            "VALUES (801, 1, 'https://discord.gg/comeback', 'OK', "
            "'2026-09-24 10:00:00', 2)")
        conn.execute(
            "INSERT INTO discovery_queue (universe_id, source, priority, status) "
            "VALUES (801, 'atlas_dev', 1, 'pending')")

    stats = scout.drain_discovery_queue(batches=1)
    assert stats["qualified"] == 1
    with sqlite3.connect(db) as conn:
        has_discord, url = conn.execute(
            "SELECT has_discord, discord_url FROM game_analytics WHERE universe_id=801"
        ).fetchone()
    assert has_discord == 1 and url == "https://discord.gg/comeback"


# --------------------------------------------------------------------------- #
# Backfill pool selection
# --------------------------------------------------------------------------- #


def test_backfill_pool_orders_qualified_first_oldest_first(tmp_path):
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    # 1: qualified, checked (excluded). 2: decayed. 3+4: qualified unchecked.
    _seed_game(scout, 1, ccu=50, visits=50_000)
    scout._store_contact_cache(1, _verdict(1))
    _seed_game(scout, 2, ccu=3, visits=50_000)
    _seed_game(scout, 3, ccu=30, visits=25_000)
    _seed_game(scout, 4, ccu=26, visits=21_000)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE game_analytics SET first_seen='2026-09-01 00:00:00' WHERE universe_id=3")
        conn.execute("UPDATE game_analytics SET first_seen='2026-09-02 00:00:00' WHERE universe_id=4")

    assert scout.contacts_backfill_due() == [3, 4]  # decayed row excluded
    # The floor query reaches the decayed band too, qualified still first.
    assert scout.contacts_backfill_due(min_visits_floor=1)[:3] == [3, 4, 2]
    assert scout.contacts_backfill_due(limit=1) == [3]


def test_backfill_pool_skips_throttled_and_failed_rows(tmp_path):
    """Throttled lookups never persist contacts_checked_at -> resumable."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    _seed_game(scout, 5, ccu=40, visits=60_000)
    assert scout.contacts_backfill_due() == [5]
    # A throttled verdict stores nothing: the pool must still contain it.
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT contacts_checked_at FROM game_analytics WHERE universe_id=5"
        ).fetchone()[0] is None
    assert scout.contacts_backfill_due() == [5]


def test_backfill_lock_is_exclusive(tmp_path, monkeypatch):
    """Only ONE backfill may run at a time (incident 2026-09-24: a redundant
    launch pulled the store mid-run and replaced the DB under the worker)."""
    monkeypatch.setattr(scout_core.ContactBackfillLock, "PATH", tmp_path / "bf.lock")
    first = scout_core.ContactBackfillLock()
    assert first.acquire() is True
    # A second acquire must fail fast (non-blocking) while the first holds it.
    assert scout_core.ContactBackfillLock.is_busy() is True
    second = scout_core.ContactBackfillLock()
    assert second.acquire(wait_s=0) is False
    # The is_busy probe must NOT have disturbed the first holder.
    assert first._fh is not None
    first.release()
    assert scout_core.ContactBackfillLock.is_busy() is False
    # And a fresh acquire succeeds after release.
    third = scout_core.ContactBackfillLock()
    assert third.acquire() is True
    third.release()
