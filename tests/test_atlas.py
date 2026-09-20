"""Atlas Dev seed-ingestion tests (ATLAS_PLAN_REVIEW.md).

Covers the fourth discovery engine: polite index harvesting, the 24h
self-throttle, the deep-sweep page cursor, delta-dedup enqueue into
discovery_queue, the optional provisional first-paint rows (strict gate
respected, ccu_history NEVER written from Atlas data), the proxy flip,
and the kill switch. All HTTP is faked — the module's live behavior was
proven separately by the 2026-09-19 spike (3 pages, 150 IDs, 98 new).
"""

import sqlite3

import pytest

import scout_core
from scout_core import RobloxPlatformScout

INDEX_HTML = (
    '<html><a href="/analyze/9000000001">a</a>'
    '<a href="/analyze/9000000002">b</a>'
    '<a href="/analyze/9000000003">c</a></html>'
)


def _game_page(title, ccu, visits, with_meta=True):
    if not with_meta:
        return "<html><body>redesigned</body></html>"
    return (
        '<html><head><meta name="description" content="'
        f"{title} on Atlas - {ccu:,} playing now, {visits:,} total visits."
        '"></head><body></body></html>'
    )


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.headers = {}


@pytest.fixture()
def scout(tmp_path, monkeypatch):
    # Default-safe posture comes from constants; tests keep the deep sweep
    # off (its dedicated test passes explicit parameters) and delay at 0.
    monkeypatch.setenv("ATLAS_DEEP_EVERY_DAYS", "0")
    monkeypatch.setenv("ATLAS_REQUEST_DELAY", "0")
    s = RobloxPlatformScout(db_path=str(tmp_path / "atlas.db"))
    yield s
    s.session.close()


def _patch_http(monkeypatch, pages=None, games=None, calls=None):
    """Route scout_core.requests.get to fixtures keyed by URL fragment."""

    def fake_get(url, timeout=30, proxies=None, **kwargs):
        if calls is not None:
            calls.append({"url": url, "proxies": proxies, "headers": dict(kwargs.get("headers") or {})})
        if pages is not None and "/analyze?" in url:
            page = int(url.split("page=")[1]) if "page=" in url else 1
            return FakeResponse(pages.get(page, pages.get("*", "")))
        if games is not None and "/analyze/" in url:
            uid = int(url.rstrip("/").rsplit("/", 1)[1])
            return FakeResponse(games.get(uid, "<html></html>"))
        return FakeResponse("", status_code=404)

    monkeypatch.setattr(scout_core.requests, "get", fake_get)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def test_parse_atlas_stat_text_extracts_ccu_and_visits():
    text = (
        "The Black Bell [HORROR] on Atlas - 145 playing now, 29,664 total visits."
    )
    parsed = RobloxPlatformScout._parse_atlas_stat_text(text)
    assert parsed == ("The Black Bell [HORROR]", 145, 29664)


def test_parse_atlas_stat_text_rejects_garbage():
    assert RobloxPlatformScout._parse_atlas_stat_text("") is None
    assert RobloxPlatformScout._parse_atlas_stat_text("nothing here") is None
    assert RobloxPlatformScout._parse_atlas_stat_text(
        "Buy Roblox games - Atlas marketplace"
    ) is None


# --------------------------------------------------------------------------- #
# Harvest: fetch → parse → enqueue
# --------------------------------------------------------------------------- #


def test_harvest_enqueues_new_ids_with_source_and_priority(scout, monkeypatch):
    # One ID already queued: delta filter must drop it.
    with sqlite3.connect(scout.db_path) as conn:
        conn.execute(
            "INSERT INTO discovery_queue (universe_id, source, status) "
            "VALUES (9000000001, 'group_spiderweb', 'processed')"
        )
    _patch_http(monkeypatch, pages={"*": INDEX_HTML})
    stats = scout.harvest_atlas_seeds(throttle_hours=0, stat_pages=0)
    assert stats["throttled"] is False
    assert stats["fetched_ids"] == 3
    assert stats["enqueued"] == 2  # 9000000001 already known
    with sqlite3.connect(scout.db_path) as conn:
        row = conn.execute(
            "SELECT source, priority, status FROM discovery_queue "
            "WHERE universe_id = 9000000002"
        ).fetchone()
    assert row == ("atlas_dev", 2, "pending")
    # A completed sweep resets the cursor; the throttle stamp is set.
    assert scout._atlas_pointer("atlas_last_run") is not None
    assert scout._atlas_pointer("atlas_page") == 1.0


def test_harvest_sends_honest_user_agent(scout, monkeypatch):
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert calls, "expected at least one Atlas request"
    assert "RbxScout" in calls[0]["headers"]["User-Agent"]
    assert "atlasdev.gg/analyze?" in calls[0]["url"]


# --------------------------------------------------------------------------- #
# Throttle + deep-sweep cursor
# --------------------------------------------------------------------------- #


def test_throttle_blocks_second_harvest_within_window(scout, monkeypatch):
    _patch_http(monkeypatch, pages={"*": INDEX_HTML})
    first = scout.harvest_atlas_seeds(throttle_hours=24, stat_pages=0)
    assert first["throttled"] is False and first["enqueued"] == 3
    second = scout.harvest_atlas_seeds(throttle_hours=24, stat_pages=0)
    assert second["throttled"] is True
    assert second["enqueued"] == 0


def test_throttle_releases_after_window(scout, monkeypatch):
    _patch_http(monkeypatch, pages={"*": INDEX_HTML})
    import time as _time

    scout._set_atlas_pointer("atlas_last_run", _time.time() - 25 * 3600)
    stats = scout.harvest_atlas_seeds(throttle_hours=24, stat_pages=0)
    assert stats["throttled"] is False


def test_cursor_resumes_partial_sweep(scout, monkeypatch):
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    scout._set_atlas_pointer("atlas_page", 2)
    scout.harvest_atlas_seeds(pages=2, stat_pages=0, throttle_hours=0)
    fetched_pages = [
        int(c["url"].split("page=")[1]) if "page=" in c["url"] else 1 for c in calls
    ]
    assert fetched_pages == [2, 3]  # resumed, never re-fetched page 1


def test_deep_sweep_runs_full_index_when_due(scout, monkeypatch):
    # First-ever run (no cursor row) with deep cadence enabled: the one-off
    # catch-up sweeps the full index ceiling, then resets the cursor to 1.
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    stats = scout.harvest_atlas_seeds(
        deep_every_days=30, throttle_hours=0, stat_pages=0
    )
    assert stats["deep_sweep"] is True
    assert stats["sweep_pages"] == scout_core.ATLAS_DEEP_PAGES_DEFAULT
    assert scout._atlas_pointer("atlas_page") == 1.0  # completed -> head daily


# --------------------------------------------------------------------------- #
# Provisional first-paint rows
# --------------------------------------------------------------------------- #


def test_provisional_rows_respect_gate_and_skip_ccu_history(scout, monkeypatch):
    games = {
        9100000001: _game_page("Rocket Rush", 40, 25_000),   # passes 20k/25
        9100000002: _game_page("Tiny Game", 10, 19_000),     # below gate
        9100000003: _game_page("No Meta", 99, 99_999, with_meta=False),
    }
    _patch_http(
        monkeypatch,
        pages={"*": '<a href="/analyze/9100000001"></a><a href="/analyze/9100000002"></a>'
                     '<a href="/analyze/9100000003"></a>'},
        games=games,
    )
    stats = scout.harvest_atlas_seeds(pages=1, stat_pages=5, throttle_hours=0)
    assert stats["provisional_rows"] == 1
    with sqlite3.connect(scout.db_path) as conn:
        rows = conn.execute(
            "SELECT universe_id, ccu, visits, found_via FROM game_analytics"
        ).fetchall()
        history = conn.execute("SELECT COUNT(*) FROM ccu_history").fetchone()[0]
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        (9100000001, 40, 25_000, "atlas_dev")
    ]
    assert history == 0  # Atlas numbers NEVER enter ccu_history


def test_provisional_never_overwrites_real_catalog_rows(scout, monkeypatch):
    with sqlite3.connect(scout.db_path) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, ccu, visits, found_via, title) "
            "VALUES (9200000001, 55, 40_000, 'keyword', 'Real Row')"
        )
    _patch_http(
        monkeypatch,
        pages={"*": '<a href="/analyze/9200000001"></a>'},
        games={9200000001: _game_page("Real Row", 60, 45_000)},
    )
    stats = scout.harvest_atlas_seeds(pages=1, stat_pages=5, throttle_hours=0)
    assert stats["provisional_rows"] == 0
    with sqlite3.connect(scout.db_path) as conn:
        title, found_via = conn.execute(
            "SELECT title, found_via FROM game_analytics WHERE universe_id = 9200000001"
        ).fetchone()
    assert (title, found_via) == ("Real Row", "keyword")


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


def test_failed_fetch_aborts_without_stamping_pointers(scout, monkeypatch):
    def failing_get(url, timeout=30, proxies=None, **kwargs):
        raise scout_core.requests.ConnectionError("boom")

    monkeypatch.setattr(scout_core.requests, "get", failing_get)
    stats = scout.harvest_atlas_seeds(throttle_hours=0, stat_pages=0)
    assert stats["aborted"] is True
    assert stats["enqueued"] == 0
    assert scout._atlas_pointer("atlas_last_run") is None  # retry next run


def test_kill_switch_disables_everything(scout, monkeypatch):
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    stats = scout.harvest_atlas_seeds(pages=0, stat_pages=0, throttle_hours=0)
    assert stats == {
        "fetched_ids": 0,
        "pages_fetched": 0,
        "enqueued": 0,
        "provisional_rows": 0,
        "throttled": False,
        "aborted": False,
        "deep_sweep": False,
    }
    assert calls == []  # zero requests left the machine


# --------------------------------------------------------------------------- #
# Proxy flip
# --------------------------------------------------------------------------- #


def test_proxy_pool_direct_by_default(scout):
    assert scout._atlas_proxy_pool() == ["direct"]


def test_proxy_flip_routes_through_pool(scout, monkeypatch):
    monkeypatch.setenv("RBXSCOUT_SEARCH_PROXY_URLS", "http://p1.example, direct")
    assert scout._atlas_proxy_pool() == ["http://p1.example", "direct"]
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert calls[0]["proxies"] == {
        "http": "http://p1.example",
        "https": "http://p1.example",
    }


# --------------------------------------------------------------------------- #
# run_expansion integration
# --------------------------------------------------------------------------- #


def test_run_expansion_runs_atlas_and_keeps_drain(scout, monkeypatch):
    _patch_http(monkeypatch, pages={"*": INDEX_HTML})
    result = scout.run_expansion(queue_batches=0)  # drain budget off for speed
    assert result["atlas"]["enqueued"] == 3
    assert result["drain"]["claimed"] == 0
    assert "spiderweb" not in result
    assert "frontier" not in result
    assert "rec_mining" not in result


def test_removed_engines_stay_removed(scout, monkeypatch):
    """The legacy discovery engines are gone from the codebase entirely."""
    import pytest
    for attr in ("spiderweb_creators", "scan_frontier", "mine_recommendations"):
        assert not hasattr(scout, attr), attr
    for const in (
        "EXPAND_SPIDERWEB_CREATORS_DEFAULT", "EXPAND_FRONTIER_BATCHES_DEFAULT",
        "EXPAND_REC_SEEDS_DEFAULT", "REC_RECOMMENDATIONS_URL",
        "EXPAND_SPIDERWEB_CREATORS_RETIRED", "EXPAND_FRONTIER_BATCHES_RETIRED",
        "EXPAND_REC_SEEDS_RETIRED",
    ):
        assert not hasattr(scout_core, const), const
