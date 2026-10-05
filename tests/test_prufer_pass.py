"""prufer pre-pass integration tests (ATLAS_PRUFER_HYDRATOR_PLAN.md Phase 2).

Verifies the scan()-level contract: games the prufer gate accepts are served
WITHOUT Roblox requests and leave the tier-due queue; games the gate rejects
(stale, missing series, no pool) stay in hydration_ids and are hydrated by
Roblox exactly as before. All HTTP is faked.
"""

import sqlite3

import pytest

import prufer
import scout_core
from scout_core import RobloxPlatformScout


def _seed_game(db_path, universe_id, tier=1, ccu=100, visits=100_000):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT OR IGNORE INTO game_analytics
            (universe_id, title, ccu, visits, tier, last_updated)
        VALUES (?, ?, ?, ?, ?, '1970-01-01 00:00:00')
        """,
        (universe_id, f"Game {universe_id}", ccu, visits, tier),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def scout(tmp_path):
    s = RobloxPlatformScout(db_path=str(tmp_path / "t.db"))
    yield s
    s.session.close()


def _patch_prufer(monkeypatch, stats_by_uid, pool_size=25):
    """Force the pre-pass to 'accept' exactly stats_by_uid."""
    captured = {}

    class FakeClient:
        def __init__(self):
            self.pool_size = pool_size

        def refresh_batch(self, ids, **kw):
            captured["ids"] = list(ids)
            return dict(stats_by_uid)

    monkeypatch.setattr(prufer, "PruferClient", FakeClient)
    monkeypatch.setattr(prufer, "PRUFER_ENABLED", True)
    return captured


def _patch_roblox_metrics(monkeypatch, scout, calls):
    def fake_metrics(ids):
        calls.extend(ids)
        return {
            int(uid): {
                "universe_id": int(uid),
                "root_place_id": None,
                "title": f"Game {uid}",
                "ccu": 111,
                "peak_ccu": 111,
                "visits": 222_000,
                "favorites": 3_000,
                "genre": None,
                "creator_name": None,
                "creator_type": None,
                "creator_id": None,
                "description": None,
                "icon_url": None,
            }
            for uid in ids
        }

    monkeypatch.setattr(scout, "fetch_game_metrics", fake_metrics)


def test_prufer_accepted_games_skip_roblox(tmp_path, scout, monkeypatch):
    _seed_game(scout.db_path if hasattr(scout, "db_path") else str(tmp_path / "t.db"), 9001)
    db = str(tmp_path / "t.db")
    _seed_game(db, 9002, tier=1)
    _seed_game(db, 9003, tier=1)
    # prufer accepts only 9002; 9001/9003 must go to Roblox
    captured = _patch_prufer(monkeypatch, {9002: {"ccu": 555, "visits": 777_000, "favorites": None}})
    roblox_calls = []
    _patch_roblox_metrics(monkeypatch, scout, roblox_calls)

    scout.scan(phases=("hydrate",))

    assert captured["ids"] and sorted(captured["ids"]) == [9001, 9002, 9003]
    assert sorted(roblox_calls) == [9001, 9003]          # 9002 never hit Roblox
    pru = scout.last_scan["prufer"]
    assert pru["accepted"] == 1 and pru["gate_rejected"] == 2
    assert scout.last_scan["hydration_budget"]["prufer_served"] == 1
    # accepted stats actually landed in the catalog
    conn = sqlite3.connect(db)
    ccu_9002 = conn.execute("SELECT ccu FROM game_analytics WHERE universe_id=9002").fetchone()[0]
    conn.close()
    assert ccu_9002 == 555


def test_all_accepted_short_circuits_roblox(tmp_path, scout, monkeypatch):
    db = str(tmp_path / "t.db")
    _seed_game(db, 8001, tier=2)
    _patch_prufer(monkeypatch, {8001: {"ccu": 42, "visits": 99_000, "favorites": 10}})
    roblox_calls = []
    _patch_roblox_metrics(monkeypatch, scout, roblox_calls)

    scout.scan(phases=("hydrate",))
    assert roblox_calls == []                            # budget untouched
    assert scout.last_scan["status"] == "complete"


def test_gate_rejections_all_fall_back(tmp_path, scout, monkeypatch):
    """No game passes the gate -> behavior is IDENTICAL to the old pipeline."""
    db = str(tmp_path / "t.db")
    _seed_game(db, 7001, tier=3)
    _patch_prufer(monkeypatch, {})                       # nothing accepted
    roblox_calls = []
    _patch_roblox_metrics(monkeypatch, scout, roblox_calls)

    scout.scan(phases=("hydrate",))
    assert roblox_calls == [7001]
    assert scout.last_scan["prufer"]["accepted"] == 0


def test_prufer_disabled_is_pure_passthrough(tmp_path, scout, monkeypatch):
    monkeypatch.setattr(prufer, "PRUFER_ENABLED", False)
    db = str(tmp_path / "t.db")
    _seed_game(db, 6001, tier=1)
    roblox_calls = []
    _patch_roblox_metrics(monkeypatch, scout, roblox_calls)

    scout.scan(phases=("hydrate",))
    assert roblox_calls == [6001]
    # disabled = pre-pass never attempted: no prufer counters recorded
    assert "prufer" not in scout.last_scan
    assert scout.last_scan["hydration_budget"].get("prufer_served") is None


def test_prufer_exception_fails_open(tmp_path, scout, monkeypatch):
    """A crashing prufer client must NEVER break the hydrator."""
    db = str(tmp_path / "t.db")
    _seed_game(db, 5001, tier=1)

    class ExplodingClient:
        def __init__(self):
            self.pool_size = 0

        def refresh_batch(self, ids, **kw):
            raise RuntimeError("proxy pool exploded")

    monkeypatch.setattr(prufer, "PruferClient", ExplodingClient)
    monkeypatch.setattr(prufer, "PRUFER_ENABLED", True)
    roblox_calls = []
    _patch_roblox_metrics(monkeypatch, scout, roblox_calls)

    scout.scan(phases=("hydrate",))
    assert roblox_calls == [5001]
    assert scout.last_scan["status"] == "complete"
