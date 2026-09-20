"""Catalog expansion tests (Atlas Dev harvest + discovery-queue drain).

The legacy discovery engines (creator spiderwebbing, frontier scan,
recommendations mining) were removed 2026-09-20 — Atlas Dev seed ingestion
is the sole discovery source. These tests cover the machinery that remains:
the priority discovery-queue drain with the strict 20k-visits / 25-CCU gate
as the central invariant (non-qualifying games NEVER reach game_analytics),
plus the scan(expand) integration path.
"""

import sqlite3

import scout_core
from scout_core import RobloxPlatformScout


class ExpansionScout(RobloxPlatformScout):
    """Scout with canned _get_json responses keyed by URL fragment.

    Metrics batches answer dynamically for any universeIds list so both
    small and large ID sets work.
    """

    def __init__(self, responses, db_path):
        self.db_path = db_path
        self.max_workers = 1
        self.request_timeout = 1
        self.session = None
        self.has_cookie = False
        self.responses = responses
        self._lock = None
        self.last_contact_diagnostics = {}
        self.last_scan = {}
        self.last_metrics = {}
        self.source_diagnostics = {}
        self.blowup_watch_events = {}
        self._sync_counter_path = scout_core.Path(db_path + ".sync_state")
        self._sync_seq = self._load_sync_sequence()
        self._emit_pace_lock = scout_core.threading.Lock()
        self._next_emit = 0.0
        self._emit_interval = scout_core.RobloxPlatformScout.BATCH_EMIT_INTERVAL
        self._init_sqlite()

    def _get_json(self, url, retries=1):
        # Metrics batches (any ID set) — dynamic answer.
        if url.startswith("https://games.roblox.com/v1/games?universeIds="):
            raw_ids = url.split("universeIds=")[1].split("&")[0].split(",")
            data = []
            for i in raw_ids:
                uid = int(i)
                fixture = self.responses.get(f"metrics:{uid}")
                if fixture is None:
                    continue
                data.append({
                    "id": uid,
                    "rootPlaceId": fixture.get("root_place_id", uid),
                    "name": fixture.get("title", f"G{uid}"),
                    "playing": fixture.get("ccu", 0),
                    "visits": fixture.get("visits", 0),
                    "favoritedCount": fixture.get("favorites", 0),
                    "genre_l1": {"name": fixture.get("genre", "Games")},
                    "creator": {
                        "id": fixture.get("creator_id", 1),
                        "name": fixture.get("creator_name", "Dev"),
                        "type": fixture.get("creator_type", "User"),
                    },
                    "description": fixture.get("description", ""),
                })
            return 200, {"data": data}

        return 404, None


def _game(uid, *, ccu=0, visits=0, creator_id=1, creator_type="User", title=None):
    return {
        "ccu": ccu,
        "visits": visits,
        "creator_id": creator_id,
        "creator_type": creator_type,
        "title": title or f"Game {uid}",
    }


# --------------------------------------------------------------------------- #
# Strict gate
# --------------------------------------------------------------------------- #


def test_qualified_only_enforces_both_axes(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({}, db)
    metas = {
        1: {"visits": 25_000, "ccu": 30},    # both axes pass
        2: {"visits": 15_000, "ccu": 30},    # visits below target
        3: {"visits": 25_000, "ccu": 5},     # CCU below target
        4: {"visits": 0, "ccu": 0},          # dead
    }
    assert scout._qualified_only(metas).keys() == {1}


# --------------------------------------------------------------------------- #
# Discovery-queue drain (hydrates Atlas seeds through the strict gate)
# --------------------------------------------------------------------------- #


def test_drain_stores_only_qualifiers_and_marks_outcomes(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "metrics:801": _game(801, ccu=40, visits=250_000, creator_id=55, creator_type="Group", title="Qualifier"),
        "metrics:802": _game(802, ccu=5, visits=500, title="Small"),
        "metrics:803": _game(803, ccu=24, visits=100_000, title="Near miss"),
        # 804: metrics endpoint returns nothing (deleted game)
    }, db)
    with sqlite3.connect(db) as conn:
        for uid, pri in ((801, 1), (802, 1), (803, 1), (804, 3)):
            conn.execute(
                "INSERT INTO discovery_queue (universe_id, source, priority, status) "
                f"VALUES ({uid}, 'atlas_dev', {pri}, 'pending')"
            )

    stats = scout.drain_discovery_queue(batches=1)

    assert stats["claimed"] == 4
    assert stats["qualified"] == 1
    assert stats["below_gate"] == 2
    assert stats["metrics_failed"] == 1
    with sqlite3.connect(db) as conn:
        catalog = {r[0] for r in conn.execute("SELECT universe_id FROM game_analytics").fetchall()}
        outcomes = dict(conn.execute(
            "SELECT universe_id, outcome FROM discovery_queue").fetchall())
    assert catalog == {801}                      # THE strict gate
    assert outcomes == {
        801: "qualified", 802: "below_gate", 803: "below_gate", 804: "metrics_failed",
    }
    # The qualifier carries expansion provenance and full tier stamping.
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT found_via, tier, ccu FROM game_analytics WHERE universe_id=801"
        ).fetchone()
    assert row[0] == "expansion" and row[1] >= 1 and row[2] == 40


def test_drain_skips_ids_already_in_catalog(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({}, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, ccu, visits) VALUES (801, 40, 100000)"
        )
        conn.execute(
            "INSERT INTO discovery_queue (universe_id, source, priority, status) "
            "VALUES (801, 'atlas_dev', 2, 'pending')"
        )
    stats = scout.drain_discovery_queue(batches=1)
    assert stats["claimed"] == 1
    assert stats["duplicates"] == 1              # no hydration request spent
    assert stats["qualified"] == 0
    with sqlite3.connect(db) as conn:
        outcome = conn.execute(
            "SELECT outcome FROM discovery_queue WHERE universe_id=801").fetchone()[0]
    assert outcome == "already_in_catalog"


def test_drain_respects_priority_and_claims_atomically(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({}, db)
    with sqlite3.connect(db) as conn:
        for uid, pri in ((10, 3), (11, 1), (12, 2)):
            conn.execute(
                "INSERT INTO discovery_queue (universe_id, source, priority, status) "
                f"VALUES ({uid}, 'x', {pri}, 'pending')"
            )
    claimed = scout._claim_discovery_batch(2)
    assert [uid for uid, _ in claimed] == [11, 12]   # priority 1 then 2 (then 3)
    with sqlite3.connect(db) as conn:
        states = dict(conn.execute(
            "SELECT universe_id, status FROM discovery_queue").fetchall())
    assert states == {11: "processing", 12: "processing", 10: "pending"}
    # A crashed run's claims (processing with an old claim stamp) self-heal:
    # seen_at is refreshed at CLAIM time, so we backdate both claimed rows.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE discovery_queue SET seen_at='1970-01-01' WHERE universe_id IN (11, 12)"
        )
    scout._reset_stale_queue_claims()
    with sqlite3.connect(db) as conn:
        states = dict(conn.execute(
            "SELECT universe_id, status FROM discovery_queue").fetchall())
    assert states == {11: "pending", 10: "pending", 12: "pending"}


def test_drain_budget_zero_claims_nothing(tmp_path):
    # Budget-0 must claim and hydrate nothing (the old max(1, limit) clamp
    # silently hydrated one game per run even with the budget off).
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({}, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO discovery_queue (universe_id, source, priority, status) "
            "VALUES (801, 'atlas_dev', 1, 'pending')"
        )
    stats = scout.drain_discovery_queue(batches=0)
    assert stats["claimed"] == 0
    with sqlite3.connect(db) as conn:
        state = conn.execute(
            "SELECT status FROM discovery_queue WHERE universe_id=801").fetchone()[0]
    assert state == "pending"


def test_env_knob_bounds_the_drain(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    monkeypatch.setenv("EXPAND_QUEUE_BATCHES", "0")
    scout = ExpansionScout({}, db)
    result = scout.run_expansion()
    assert result["drain"]["claimed"] == 0
    # Malformed env values fall back to defaults instead of crashing a run.
    monkeypatch.setenv("EXPAND_QUEUE_BATCHES", "garbage")
    assert scout_core._env_int("EXPAND_QUEUE_BATCHES", 10) == 10


# --------------------------------------------------------------------------- #
# scan() integration: the expand phase
# --------------------------------------------------------------------------- #


def test_scan_expand_phase_runs_full_pass(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "metrics:801": _game(801, ccu=60, visits=200_000, creator_id=55, creator_type="Group"),
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, creator_id, creator_type, ccu, visits) "
            "VALUES (1, 55, 'Group', 30, 30000)"
        )
        conn.execute(
            "INSERT INTO discovery_queue (universe_id, source, priority, status) "
            "VALUES (801, 'atlas_dev', 2, 'pending')"
        )
    # The Atlas harvest itself is covered by tests/test_atlas.py; stub it here
    # so this test exercises scan() → run_expansion() → drain in isolation.
    monkeypatch.setattr(
        scout, "harvest_atlas_seeds",
        lambda progress_cb=None: {"enqueued": 0, "pages_fetched": 0},
    )
    df = scout.scan(
        min_visits=20_000, min_ccu=25,
        deep_contacts=False, progress_cb=lambda p, m: None,
        phases=("expand",),
    )
    assert scout.last_scan["status"] == "complete"
    assert scout.last_scan["expansion"]["drain"]["qualified"] == 1
    assert 801 in set(df["universe_id"])
    with sqlite3.connect(db) as conn:
        # The expand run lands in scan_runs with the standard lifecycle.
        row = conn.execute(
            "SELECT status, matched_count FROM scan_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    assert row[0] == "complete" and row[1] == 1


def test_scan_rejects_unknown_phase():
    scout = ExpansionScout({}, ":memory:")
    import pytest
    with pytest.raises(ValueError):
        scout.scan(phases=("explode",))
