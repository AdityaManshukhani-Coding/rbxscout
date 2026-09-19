"""Catalog expansion pilot tests (EXPANSION_PILOT.md).

Covers the three new engines in scout_core — creator spiderwebbing, the
priority discovery-queue drain, and the frontier scan — with the strict
20k-visits / 25-CCU gate as the central invariant: non-qualifying games
NEVER reach game_analytics.
"""

import sqlite3

import scout_core
from scout_core import RobloxPlatformScout


class ExpansionScout(RobloxPlatformScout):
    """Scout with canned _get_json responses keyed by URL fragment.

    Portfolio fixtures answer /v2/groups/{id}/games and /v2/users/{id}/games
    with the exact shape the Phase-0 spike observed (id, rootPlace, name,
    placeVisits, nextPageCursor). Metrics batches answer dynamically for any
    universeIds list so both small and large ID sets work.
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

        # Recommendations mining: fixture key "recs" -> {seed_uid: [games]}.
        # Missing seed -> 404 (models an endpoint failure for that seed).
        if url.startswith(scout_core.REC_RECOMMENDATIONS_URL.replace("{universe_id}", "")):
            uid = int(url.rstrip("/").rsplit("/", 1)[1])
            recs = self.responses.get("recs") or {}
            if uid in recs:
                return 200, {"games": recs[uid], "nextPaginationKey": None}
            return 404, None

        # Portfolio endpoints (cursor-aware). Fixture key: ("group", id) or
        # ("user", id) -> list of pages, each page a dict like the live API.
        for (kind, cid), pages in self.responses.get("portfolios", {}).items():
            base = (
                f"https://games.roblox.com/v2/groups/{cid}/games"
                if kind == "group"
                else f"https://games.roblox.com/v2/users/{cid}/games"
            )
            if not url.startswith(base):
                continue
            cursor = None
            if "cursor=" in url:
                cursor = url.split("cursor=")[1].split("&")[0]
            page_index = int(cursor) if cursor and cursor.isdigit() else 0
            if page_index >= len(pages):
                return 200, {"data": [], "nextPageCursor": None}
            return 200, pages[page_index]

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
    scout = ExpansionScout({}, str(tmp_path / "t.db"))
    metas = {
        1: _game(1, ccu=30, visits=25_000),   # qualifies
        2: _game(2, ccu=25, visits=19_999),   # visits below -> discard
        3: _game(3, ccu=24, visits=99_000),   # ccu below -> discard
        4: _game(4, ccu=300, visits=20_000),  # qualifies (exact boundary)
    }
    assert set(scout._qualified_only(metas)) == {1, 4}


# --------------------------------------------------------------------------- #
# Spiderwebbing
# --------------------------------------------------------------------------- #


def test_spiderweb_pregates_and_enqueues(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "portfolios": {
            ("group", 55): [{
                "data": [
                    {"id": 801, "name": "Hit", "rootPlace": {"id": 9001, "type": "Place"}, "placeVisits": 250_000},
                    {"id": 802, "name": "Tiny", "rootPlace": {"id": 9002, "type": "Place"}, "placeVisits": 500},
                ],
                "nextPageCursor": None,
            }],
            ("user", 7): [{
                "data": [
                    {"id": 803, "name": "Also Hit", "rootPlace": {"id": 9003, "type": "Place"}, "placeVisits": 20_000},
                ],
                "nextPageCursor": None,
            }],
        },
        # Hydration verdicts when the queue drains: 801/803 qualify.
        "metrics:801": _game(801, ccu=40, visits=250_000, creator_id=55, creator_type="Group"),
        "metrics:803": _game(803, ccu=30, visits=20_000, creator_id=7),
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, creator_id, creator_type, ccu, visits) "
            "VALUES (1, 55, 'Group', 30, 30000), (2, 7, 'User', 30, 30000)"
        )

    stats = scout.spiderweb_creators(limit_creators=10)

    assert stats["crawled"] == 2
    assert stats["games_found"] == 3
    assert stats["pregate_passed"] == 2          # 802 pre-gated out, free
    assert stats["enqueued"] == 2
    with sqlite3.connect(db) as conn:
        queued = dict(conn.execute(
            "SELECT universe_id, priority FROM discovery_queue").fetchall())
        logged = dict(conn.execute(
            "SELECT creator_id || creator_type, game_count FROM creator_spiderweb_log"
        ).fetchall())
    assert queued == {801: 1, 803: 1}            # priority 1 (spiderweb)
    assert logged == {"55Group": 2, "7User": 1}


def test_spiderweb_dedups_against_queue_and_relogs_after_ttl(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "portfolios": {
            ("group", 55): [{
                "data": [{"id": 801, "name": "Hit", "rootPlace": {"id": 1}, "placeVisits": 100_000}],
                "nextPageCursor": None,
            }],
        },
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, creator_id, creator_type, ccu, visits) "
            "VALUES (1, 55, 'Group', 30, 30000)"
        )
        conn.execute(
            "INSERT INTO discovery_queue (universe_id, source, priority, status) "
            "VALUES (801, 'sequential_scan', 3, 'pending')"
        )

    stats = scout.spiderweb_creators(limit_creators=5)
    assert stats["enqueued"] == 0                # already queued: not duplicated
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT source, priority FROM discovery_queue WHERE universe_id=801"
        ).fetchone()
    assert row == ("sequential_scan", 3)         # original claim preserved

    # A fresh crawl within the 14-day TTL is skipped entirely.
    again = scout.spiderweb_creators(limit_creators=5)
    assert again["crawled"] == 0 and again["games_found"] == 0


def test_spiderweb_failed_fetch_not_logged(tmp_path):
    """A failing portfolio fetch returns None -> creator NOT logged, retried next run."""
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({}, db)  # no fixtures -> 404 -> None
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, creator_id, creator_type, ccu, visits) "
            "VALUES (1, 55, 'Group', 30, 30000)"
        )
    stats = scout.spiderweb_creators(limit_creators=5)
    assert stats["failed"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM creator_spiderweb_log").fetchone()[0] == 0
    # But an EMPTY portfolio logs with 0 games (no wasted retry next run).
    scout2 = ExpansionScout({
        "portfolios": {("user", 7): [{"data": [], "nextPageCursor": None}]},
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, creator_id, creator_type, ccu, visits) "
            "VALUES (2, 7, 'User', 30, 30000)"
        )
    stats2 = scout2.spiderweb_creators(limit_creators=5)
    assert stats2["empty"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT game_count FROM creator_spiderweb_log WHERE creator_id=7"
        ).fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Queue drain: the strict gate is the invariant
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
                f"VALUES ({uid}, 'group_spiderweb', {pri}, 'pending')"
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
            "VALUES (801, 'seed_harvest', 2, 'pending')"
        )
    stats = scout.drain_discovery_queue(batches=1)
    assert stats["duplicates"] == 1 and stats["hydrated"] == 0
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


# --------------------------------------------------------------------------- #
# Frontier scan
# --------------------------------------------------------------------------- #


def test_frontier_scan_advances_pointer_and_stores_qualifiers_only(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({}, db)
    start = scout.load_frontier_pointer()
    assert start >= 10_765_584_604               # seeded at the max known ID
    # Fixtures INSIDE the scanned range [start+1, start+50].
    hit, miss = start + 46, start + 47
    scout.responses["metrics:%d" % hit] = _game(hit, ccu=55, visits=90_000, title="Fresh hit")
    scout.responses["metrics:%d" % miss] = _game(miss, ccu=2, visits=30, title="Fresh corpse")

    stats = scout.scan_frontier(batches=1)       # 50 IDs from start+1

    assert stats["scanned"] == 50
    assert stats["qualified"] == 1
    assert stats["below_gate"] == 1
    assert scout.load_frontier_pointer() == stats["end_id"] == start + 50
    with sqlite3.connect(db) as conn:
        catalog = {r[0] for r in conn.execute("SELECT universe_id FROM game_analytics").fetchall()}
        queue = conn.execute(
            "SELECT status, outcome FROM discovery_queue WHERE universe_id=?", (miss,)
        ).fetchone()
    assert catalog == {hit}
    assert queue == ("processed", "below_gate")  # discard recorded, not stored


def test_frontier_pointer_seeded_and_persists(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({}, db)
    seed = scout.load_frontier_pointer()
    scout._advance_frontier_pointer(seed + 500)
    scout2 = ExpansionScout({}, db)              # "restart"
    assert scout2.load_frontier_pointer() == seed + 500


def test_env_knobs_bound_the_pilot(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({}, db)
    monkeypatch.setenv("EXPAND_FRONTIER_BATCHES", "0")   # kill-switch value
    monkeypatch.setenv("EXPAND_QUEUE_BATCHES", "0")
    result = scout.run_expansion()
    assert result["frontier"]["scanned"] == 0
    assert result["drain"]["claimed"] == 0
    # Malformed env values fall back to defaults instead of crashing a run.
    monkeypatch.setenv("EXPAND_FRONTIER_BATCHES", "garbage")
    scout2 = ExpansionScout({}, db)
    default = scout_core._env_int("EXPAND_FRONTIER_BATCHES", 10)
    assert default == 10
    assert scout2.load_frontier_pointer() >= 10_765_584_604


# --------------------------------------------------------------------------- #
# scan() integration: the expand phase
# --------------------------------------------------------------------------- #


def test_scan_expand_phase_runs_full_pass(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "portfolios": {
            ("group", 55): [{
                "data": [{"id": 801, "name": "Hit", "rootPlace": {"id": 1}, "placeVisits": 200_000}],
                "nextPageCursor": None,
            }],
        },
        "metrics:801": _game(801, ccu=60, visits=200_000, creator_id=55, creator_type="Group"),
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, creator_id, creator_type, ccu, visits) "
            "VALUES (1, 55, 'Group', 30, 30000)"
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


# --------------------------------------------------------------------------- #
# Recommendations mining (third seed source, added 2026-09-19)
# --------------------------------------------------------------------------- #


def _rec(uid):
    """One recommendations row, exactly the live shape (spike 2026-09-19)."""
    return {
        "universeId": uid,
        "name": f"Rec {uid}",
        "placeId": uid + 1,
        "creatorId": 42,
        "creatorType": "Group",
        "creatorName": "Somebody",
        "totalUpVotes": 3000,
        "totalDownVotes": 100,
    }


def test_rec_mining_enqueues_new_ids_at_priority_2(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "recs": {
            500: [_rec(9001), _rec(9002), _rec(500)],   # self-rec must be skipped
            501: [_rec(9001), _rec(9003)],              # 9001 dup across seeds
        },
    }, db)
    # Seeds = small-band rows (500, 501). Pool ordered ascending -> cursor 0.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, ccu, visits) VALUES (500, 30, 50000), (501, 30, 50000)"
        )
    stats = scout.mine_recommendations(seed_count=10)
    assert stats["seeds"] == 2
    assert stats["recs_seen"] == 5
    assert stats["enqueued"] == 3                # 9001, 9002, 9003
    assert stats["known"] == 2                   # self-rec + 9001 seen twice
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute(
            "SELECT universe_id, priority FROM discovery_queue").fetchall())
        sources = dict(conn.execute(
            "SELECT universe_id, source FROM discovery_queue").fetchall())
        cursor = conn.execute(
            "SELECT last_universe_id FROM scan_pointers WHERE id='rec_seed'").fetchone()[0]
    assert rows == {9001: 2, 9002: 2, 9003: 2}   # priority 2 (rec mining)
    assert all(v == "rec_mining" for v in sources.values())
    assert cursor == 0                            # full sweep wraps (mod pool size)


def test_rec_mining_skips_ids_already_in_queue(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "recs": {500: [_rec(9001), _rec(9002)]},
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, ccu, visits) VALUES (500, 30, 50000)")
        conn.execute(
            "INSERT INTO discovery_queue (universe_id, source, priority, status) "
            "VALUES (9001, 'sequential_scan', 3, 'pending')")
    stats = scout.mine_recommendations(seed_count=10)
    # Existing queue rows keep their original source/priority (_enqueue_discovery
    # contract) and are not counted as new.
    assert stats["enqueued"] == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT source, priority FROM discovery_queue WHERE universe_id=9001").fetchone()
    assert row == ("sequential_scan", 3)


def test_rec_mining_rotation_does_not_repeat_seeds(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "recs": {
            500: [_rec(9001)], 501: [_rec(9002)],
            502: [_rec(9003)], 503: [_rec(9004)],
        },
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, ccu, visits) "
            "VALUES (500, 30, 50000), (501, 30, 50000), (502, 30, 50000), (503, 30, 50000)")
    first = scout.mine_recommendations(seed_count=2)
    second = scout.mine_recommendations(seed_count=2)
    assert first["seeds"] == 2 and second["seeds"] == 2   # fresh slice each run
    with sqlite3.connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM discovery_queue").fetchone()[0]
    assert n == 4                                            # no seed repeated


def test_rec_mining_seed_failure_does_not_break_run(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "recs": {501: [_rec(9009)]},                 # seed 500 -> 404 in the mock
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, ccu, visits) VALUES (500, 30, 50000), (501, 30, 50000)")
    stats = scout.mine_recommendations(seed_count=10)
    assert stats["failed"] == 1
    assert stats["enqueued"] == 1                    # healthy seed still harvested


def test_rec_mining_giants_and_below_band_excluded_from_pool(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({}, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, ccu, visits, found_via) "
            "VALUES (10, 900, 5_000_000, 'keyword'), (20, 30, 19_000, 'keyword')")
    stats = scout.mine_recommendations(seed_count=10)
    assert stats["seeds"] == 0   # giant + below-band rows are not valid seeds


def test_run_expansion_includes_rec_mining(tmp_path):
    db = str(tmp_path / "t.db")
    scout = ExpansionScout({
        "recs": {500: [_rec(9001)]},
        "metrics:9001": _game(9001, ccu=30, visits=25_000),
    }, db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, ccu, visits) VALUES (500, 30, 50000)")
    result = scout.run_expansion(
        spiderweb_creators=0, queue_batches=1, frontier_batches=0, rec_seeds=5,
    )
    assert result["rec_mining"]["enqueued"] == 1
    assert result["drain"]["qualified"] == 1         # 9001 crossed the gate
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT found_via FROM game_analytics WHERE universe_id=9001").fetchone()
    assert row[0] == "expansion"
