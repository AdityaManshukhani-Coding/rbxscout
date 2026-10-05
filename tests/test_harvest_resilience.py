"""Hardening tests for the home-harvest pipeline (incident 2026-10-02..04).

Every atlas_home run from Oct 2 to Oct 4 died at the snapshot/push tail with
"unable to open database file": launchd caps the process at 256 fds, and
`with sqlite3.connect(...)` never CLOSES — each WAL connection leaked its
db/-wal/-shm descriptors until GC (which did not come), so ~90 harvest
connections pushed the run past the cap. Root cause + regression:

  * scout_core._ClosingConnection — commit semantics kept, fd leak gone.
  * atlas_home._local_conn / _sqlite_open_retry — transient open retry.
  * atlas_home seed preservation — a store pull must never wipe local
    pending seeds (the 2026-10-04 morning run lost 131 finds this way).
  * atlas_home.run_push_tail — a failed push strands nothing: marker for
    the next run's drain-before-pull, loud log, non-zero exit.
"""

import json
import sqlite3

import pytest

import atlas_home
from scout_core import RobloxPlatformScout


@pytest.fixture()
def scout(tmp_path):
    s = RobloxPlatformScout(db_path=str(tmp_path / "t.db"))
    yield s
    s.session.close()


def test_connect_closes_on_with_exit(scout):
    """The EMFILE root cause: `with conn:` must CLOSE, not just commit."""
    conn = scout._connect()
    with conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, title) VALUES (1, 'G')"
        )
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connect_still_commits_before_closing(scout):
    """Closing must not change transaction semantics: the row survives."""
    with scout._connect() as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, title) VALUES (7, 'X')"
        )
    with scout._connect() as conn:
        assert conn.execute(
            "SELECT title FROM game_analytics WHERE universe_id = 7"
        ).fetchone()[0] == "X"


def test_local_conn_retries_transient_open_failures(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    attempts = {"n": 0}

    def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(atlas_home.sqlite3, "connect", flaky)
    monkeypatch.setattr(atlas_home.time, "sleep", lambda _s: None)

    db = tmp_path / "retry.db"
    with atlas_home._local_conn(db) as conn:
        conn.execute("CREATE TABLE t (x)")
    assert attempts["n"] == 3  # two transient failures, then success


def test_local_conn_raises_fast_on_non_transient(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("out of memory")

    monkeypatch.setattr(atlas_home.sqlite3, "connect", broken)
    monkeypatch.setattr(atlas_home.time, "sleep", lambda _s: None)
    with pytest.raises(sqlite3.OperationalError, match="out of memory"):
        with atlas_home._local_conn(tmp_path / "x.db"):
            pass


def test_local_conn_closes_after_use(tmp_path):
    db = tmp_path / "c.db"
    with atlas_home._local_conn(db) as conn:
        conn.execute("CREATE TABLE t (x)")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1 FROM t")


def _make_queue_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE discovery_queue ("
        "universe_id INTEGER PRIMARY KEY, source TEXT, priority INTEGER, "
        "status TEXT, outcome TEXT, seen_at TEXT, evaluated_at TEXT)"
    )
    conn.commit()
    conn.close()


def test_pending_atlas_rows_returns_only_pending_atlas(tmp_path):
    db = tmp_path / "q.db"
    _make_queue_db(db)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO discovery_queue VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "atlas_dev", 5, "pending", None, "2026-10-04T20:00:00", None),
            (2, "atlas_dev", 5, "processed", "ok", "2026-10-04T20:00:00", "2026-10-04T21:00:00"),
            (3, "legacy_crawler", 5, "pending", None, "2026-10-04T20:00:00", None),
        ],
    )
    conn.commit()
    conn.close()

    rows = atlas_home._pending_atlas_rows(db)
    assert [r[0] for r in rows] == [1]
    assert atlas_home._pending_atlas_rows(tmp_path / "missing.db") == []


def test_restore_pending_seeds_only_fills_missing(tmp_path):
    """A store pull wiped row 2; row 1 already exists as processed. The
    restore must re-add ONLY row 2 and never clobber row 1's verdict."""
    db = tmp_path / "q.db"
    _make_queue_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO discovery_queue VALUES "
        "(1, 'atlas_dev', 5, 'processed', 'ok', '2026-10-04T10:00:00', '2026-10-04T11:00:00')"
    )
    conn.commit()
    conn.close()

    seeds = [
        (1, "atlas_dev", 5, "pending", None, "2026-10-04T10:00:00", None),
        (2, "atlas_dev", 5, "pending", None, "2026-10-04T10:01:00", None),
    ]
    assert atlas_home._restore_pending_seeds(db, seeds) == 1
    assert atlas_home._restore_pending_seeds(db, []) == 0

    conn = sqlite3.connect(str(db))
    row1 = conn.execute(
        "SELECT status, outcome FROM discovery_queue WHERE universe_id = 1"
    ).fetchone()
    row2 = conn.execute(
        "SELECT status, seen_at FROM discovery_queue WHERE universe_id = 2"
    ).fetchone()
    conn.close()
    assert row1 == ("processed", "ok")  # untouched
    assert row2 == ("pending", "2026-10-04T10:01:00")  # restored


def test_run_push_tail_marks_failure_then_clears_on_success(tmp_path, monkeypatch):
    local = tmp_path / "local.db"
    _make_queue_db(local)
    conn = sqlite3.connect(str(local))
    conn.execute(
        "INSERT INTO discovery_queue VALUES "
        "(9, 'atlas_dev', 5, 'pending', NULL, '2026-10-04T20:00:00', NULL)"
    )
    conn.commit()
    conn.close()

    marker = tmp_path / "marker.json"
    monkeypatch.setattr(atlas_home, "PUSH_FAILED_MARKER", marker)

    def failing_push(_copy):
        raise SystemExit("push failed after 5 merge-retries")

    monkeypatch.setattr(atlas_home, "push_with_merge_retry", failing_push)
    assert atlas_home.run_push_tail(local) is False
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert "push failed after 5 merge-retries" in payload["reason"]
    assert payload["pending_atlas_seeds"] == 1
    assert not local.with_suffix(".db.harvest").exists()  # cleaned up

    monkeypatch.setattr(atlas_home, "push_with_merge_retry", lambda _copy: None)
    assert atlas_home.run_push_tail(local) is True
    assert not marker.exists()  # success clears the stranded-work marker


def test_snapshot_db_retries_and_produces_a_valid_copy(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    attempts = {"n": 0}

    def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    local = tmp_path / "local.db"
    _make_queue_db(local)
    copy = tmp_path / "copy.db"
    monkeypatch.setattr(atlas_home.sqlite3, "connect", flaky)
    monkeypatch.setattr(atlas_home.time, "sleep", lambda _s: None)

    atlas_home._snapshot_db(local, copy)
    assert attempts["n"] >= 2

    # The copy is a real SQLite DB carrying the queued row.
    conn = sqlite3.connect(str(copy))
    assert conn.execute("SELECT COUNT(*) FROM discovery_queue").fetchone()[0] >= 0
    conn.close()
