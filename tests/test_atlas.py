"""Atlas Dev seed-ingestion tests (ATLAS_PLAN_REVIEW.md).

Covers the fourth discovery engine: polite index harvesting, the 24h
self-throttle, the deep-sweep page cursor, delta-dedup enqueue into
discovery_queue, the optional provisional first-paint rows (strict gate
respected, ccu_history NEVER written from Atlas data), the proxy flip,
and the kill switch. All HTTP is faked — the module's live behavior was
proven separately by the 2026-09-19 spike (3 pages, 150 IDs, 98 new).
"""

import os
import sqlite3

import pytest

import atlas_home
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
    # The expander workflow exports the ATLAS_* ops knobs into the SAME
    # process that runs these tests (ATLAS_ROLL_PAGES=8 at the 2026-10-15
    # 10-min cadence) — the cursor assertions below verify the HARVEST
    # contract against the module default, so pin the env back to default
    # here. (2026-10-15 run #1058: the exported knob made cursor == 10.0
    # while the tests expected ATLAS_TAIL_START + DEFAULT(25) = 27.)
    monkeypatch.delenv("ATLAS_ROLL_PAGES", raising=False)
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
# Merge: harvest-side columns must survive the union merge
# --------------------------------------------------------------------------- #


def test_merge_carries_new_catalog_columns(tmp_path):
    """Harvest-side columns added after 2026-09-21 (upvotes, downvotes,
    first_seen) must survive the union merge into the pulled store."""
    db = tmp_path / "t.db"
    harvest = tmp_path / "h.db"
    for target in (db, harvest):
        with sqlite3.connect(str(target)) as conn:
            conn.execute(
                "CREATE TABLE game_analytics (universe_id INTEGER PRIMARY KEY, "
                "title TEXT, upvotes INTEGER, downvotes INTEGER, first_seen TIMESTAMP)"
            )
            conn.execute(
                "CREATE TABLE ccu_history (universe_id INTEGER, ts TIMESTAMP, "
                "ccu INTEGER, PRIMARY KEY (universe_id, ts))"
            )
            conn.execute(
                "CREATE TABLE discovery_queue (universe_id INTEGER PRIMARY KEY, "
                "source TEXT, priority INTEGER DEFAULT 1, status TEXT DEFAULT 'pending')"
            )
            conn.execute("CREATE TABLE place_map (place_id INTEGER PRIMARY KEY, universe_id INTEGER)")
            conn.execute(
                "CREATE TABLE scan_pointers (id TEXT PRIMARY KEY, "
                "last_universe_id INTEGER, updated_at TIMESTAMP)"
            )
    with sqlite3.connect(str(harvest)) as conn:
        conn.execute(
            "INSERT INTO game_analytics VALUES (900, 'Fresh', 500, 20, '2026-09-21 10:00:00')"
        )
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO game_analytics VALUES (800, 'Old', 100, 5, '2026-09-02 10:00:00')"
        )
    merged = atlas_home.merge_harvest_into(db, harvest)
    assert merged["game_analytics"] == 1
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT title, upvotes, downvotes, first_seen FROM game_analytics "
            "WHERE universe_id = 900"
        ).fetchone()
    assert row == ("Fresh", 500, 20, "2026-09-21 10:00:00")


# --------------------------------------------------------------------------- #
# Nightly bounded contact sweep (frontier Discord checks, home IP only)
# --------------------------------------------------------------------------- #


class _SweepScout(RobloxPlatformScout):
    """Records the pool scan_contacts was called with; verdicts hit."""

    last_pool = None
    all_pools: list = []

    def __init__(self, db_path=None, **_ignored):
        self.db_path = db_path
        self.max_workers = 1
        self.request_timeout = 1
        self.session = None
        self.has_cookie = False
        self.last_contact_diagnostics = {}
        self.last_scan = {"run_id": None}
        self.last_metrics = {}
        self.source_diagnostics = {}
        self.blowup_watch_events = {}
        self._own_run_ids = set()
        self._init_sqlite()

    def set_cookie(self, roblox_cookie):
        self.has_cookie = bool(roblox_cookie)

    def scan_contacts(self, universe_ids, force=False, run_id=None, progress_cb=None):
        type(self).last_pool = list(universe_ids)
        type(self).all_pools.append(list(universe_ids))
        import pandas as pd

        rows = [{
            "universe_id": uid,
            "has_discord": uid % 2 == 0,
            "discord_url": "https://discord.gg/x" if uid % 2 == 0 else None,
            "status": "OK" if uid % 2 == 0 else "No Contact Found",
            "found_via": None,
            "has_social_links": uid % 2 == 0,
            "contacts_checked_at": "2026-09-24 20:00:00",
        } for uid in universe_ids]
        return pd.DataFrame(rows)


def test_contact_sweep_budgets_frontier_then_stragglers(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("RBXSCOUT_COOKIE", ".ROBLOSECURITY=test-cookie")
    monkeypatch.setattr(atlas_home, "CONTACT_SWEEP_BUDGET", 3)
    # Build the real schema first: contact_sweep constructs a scout whose
    # _init_sqlite migrates in a fixed column order and cannot run against a
    # hand-rolled minimal table.
    _SweepScout(str(db))
    with sqlite3.connect(str(db)) as conn:
        # Frontier: two Atlas discoveries from today. Timestamps are relative
        # to now — hardcoded dates silently age out of the sweep's rolling
        # 24h window (this test rotted at 12:00 PDT on 2026-09-25).
        conn.execute("INSERT INTO discovery_queue (universe_id, source, status, seen_at) "
                     "VALUES (2, 'atlas_dev', 'pending', datetime('now', '-2 hours'))")
        conn.execute("INSERT INTO discovery_queue (universe_id, source, status, seen_at) "
                     "VALUES (4, 'atlas_dev', 'pending', datetime('now', '-3 hours'))")
        # Straggler: qualified but never checked (older than the budget).
        conn.execute(
            "INSERT INTO game_analytics (universe_id, visits, ccu, first_seen) "
            "VALUES (6, 30000, 30, datetime('now', '-20 days'))"
        )
    import scout_core as sc
    monkeypatch.setattr(sc, "RobloxPlatformScout", _SweepScout)

    stats = {}
    checked = atlas_home.contact_sweep(db, stats)

    assert checked == 3
    assert _SweepScout.last_pool == [2, 4, 6]  # frontier first, then straggler
    assert stats["contacts_checked"] == 3
    with sqlite3.connect(str(db)) as conn:
        stamp = conn.execute(
            "SELECT last_universe_id FROM scan_pointers WHERE id='home_contact_sweep_at'"
        ).fetchone()
    assert stamp is not None  # 24h self-throttle stamped


def test_contact_sweep_two_cookies_split_pool(tmp_path, monkeypatch):
    """Two cookies = two parallel lanes over disjoint halves of the pool."""
    db = tmp_path / "t.db"
    monkeypatch.setenv("RBXSCOUT_COOKIE",
                       ".ROBLOSECURITY=cookie-A||.ROBLOSECURITY=cookie-B")
    monkeypatch.setattr(atlas_home, "CONTACT_SWEEP_BUDGET", 4)
    _SweepScout(str(db))
    with sqlite3.connect(str(db)) as conn:
        for uid, hours in ((1, 1), (2, 2), (3, 3), (4, 4)):
            conn.execute(
                "INSERT INTO discovery_queue (universe_id, source, status, seen_at) "
                "VALUES (?, 'atlas_dev', 'pending', datetime('now', '-' || ? || ' hours'))",
                (uid, hours))
    import scout_core as sc
    monkeypatch.setattr(sc, "RobloxPlatformScout", _SweepScout)

    stats = {}
    _SweepScout.all_pools = []  # class-level accumulator: reset per test
    checked = atlas_home.contact_sweep(db, stats)

    assert checked == 4
    assert stats["contacts_checked"] == 4
    # Two lanes ran, each with its own cookie; together they cover the pool
    # exactly once (disjoint halves — no game requested twice). Which lane
    # records last_pool is scheduling-dependent, so assert on all lanes.
    assert len(_SweepScout.all_pools) == 2
    flat = sorted(uid for p in _SweepScout.all_pools for uid in p)
    assert flat == [1, 2, 3, 4]
    assert not set(_SweepScout.all_pools[0]) & set(_SweepScout.all_pools[1])


def test_contact_sweep_throttled_when_already_run_today(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("RBXSCOUT_COOKIE", ".ROBLOSECURITY=test-cookie")
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE scan_pointers (id TEXT PRIMARY KEY, "
            "last_universe_id INTEGER, updated_at TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO scan_pointers VALUES ('home_contact_sweep_at', 0, CURRENT_TIMESTAMP)"
        )
    stats = {}
    assert atlas_home.contact_sweep(db, stats) == 0
    assert stats == {}  # nothing ran


def test_contact_sweep_skips_without_cookie(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.delenv("RBXSCOUT_COOKIE", raising=False)
    # Isolate APP_DIR so real local_cookie*.txt files on this machine can't
    # satisfy the sweep — the test must pass on a cookie-less checkout.
    monkeypatch.setattr(atlas_home, "APP_DIR", tmp_path)
    stats = {}
    assert atlas_home.contact_sweep(db, stats) == 0
    assert stats == {}


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
    # A completed steady harvest advances the tail cursor (it must NEVER
    # reset to the head — that reset made pages 4+ unreachable for 30 days).
    assert scout._atlas_pointer("atlas_last_run") is not None
    assert scout._atlas_pointer("atlas_page") == (
        scout_core.ATLAS_TAIL_START + scout_core.ATLAS_ROLL_PAGES_DEFAULT
    )  # tail ran [2..26] -> next [27..51]


def test_harvest_sends_honest_user_agent(scout, monkeypatch):
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert calls, "expected at least one Atlas request"
    assert "UpScaleScoutingTool" in calls[0]["headers"]["User-Agent"]
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
    """Deep-sweep partial retry: 50 pages fetched, then a network blip —
    the pointer must resume at 1 + fetched so the remaining pages are
    harvested on the next run."""
    calls = []
    limit = 50

    def fake_get(url, timeout=30, proxies=None, **kwargs):
        if calls is not None:
            calls.append(url)
        if "/analyze?" in url:
            page = int(url.split("page=")[1]) if "page=" in url else 1
            if page > limit:
                raise scout_core.requests.ConnectionError("boom")
            return FakeResponse(INDEX_HTML)
        return FakeResponse("", status_code=404)

    monkeypatch.setattr(scout_core.requests, "get", fake_get)
    stats = scout.harvest_atlas_seeds(
        deep_every_days=30, throttle_hours=0, stat_pages=0
    )
    assert stats["aborted"] is True
    assert stats["pages_fetched"] == limit
    assert scout._atlas_pointer("atlas_page") == 1 + limit  # retry from page 51
    assert scout._atlas_pointer("atlas_deep") is None  # never completes ⇒ never stamps


def test_tail_circuit_advances_and_wraps(scout, monkeypatch):
    """2026-10-14 coverage fix: the steady harvest must walk the WHOLE gated
    index via a rolling tail cursor that wraps at the ceiling — the old
    reset-to-page-1 behavior left pages 4..242 unreachable for 30 days."""
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    scout.harvest_atlas_seeds(throttle_hours=0, stat_pages=0)  # tail 2..26
    assert scout._atlas_pointer("atlas_page") == (
        scout_core.ATLAS_TAIL_START + scout_core.ATLAS_ROLL_PAGES_DEFAULT
    )
    calls.clear()
    scout.harvest_atlas_seeds(throttle_hours=0, stat_pages=0)  # tail 27..51
    fetched_tails = [
        int(c["url"].split("page=")[1]) for c in calls if "page=" in c["url"]
    ]
    # Head pages 2-3 are re-checked first; then the circuit runs [27..51].
    assert fetched_tails == [2, 3] + list(range(
        scout_core.ATLAS_TAIL_START + scout_core.ATLAS_ROLL_PAGES_DEFAULT,
        scout_core.ATLAS_TAIL_START + 2 * scout_core.ATLAS_ROLL_PAGES_DEFAULT,
    ))
    # Head pages are re-checked BEFORE every tail window (fresh listings).
    assert calls[0]["url"].endswith("sort=totalVisits&dir=asc&totalVisitsMin=20000&ccuMin=25")
    calls.clear()
    scout._set_atlas_pointer("atlas_page", scout_core.ATLAS_DEEP_PAGES_DEFAULT)
    scout.harvest_atlas_seeds(throttle_hours=0, stat_pages=0)  # wraps at the ceiling
    assert scout._atlas_pointer("atlas_page") == scout_core.ATLAS_TAIL_START


def test_tail_walk_survives_pages_beyond_index_end(scout, monkeypatch):
    """Pages past the live listing count return 404 — the cursor must keep
    walking them (parse empty, advance) instead of aborting and pinning the
    circuit outside the index forever."""
    # _patch_http returns FakeResponse('', 404) for pages missing from the
    # dict, so pages beyond 2 are 'gone'; page 2 exists.
    _patch_http(monkeypatch, pages={2: INDEX_HTML})
    stats = scout.harvest_atlas_seeds(throttle_hours=0, stat_pages=0)
    assert stats["aborted"] is False
    assert stats["fetched_ids"] == 3
    assert scout._atlas_pointer("atlas_page") == (
        scout_core.ATLAS_TAIL_START + scout_core.ATLAS_ROLL_PAGES_DEFAULT
    )


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


def test_deep_sweep_env_zero_disables_catchup(scout, monkeypatch):
    """The workflow exports ATLAS_DEEP_EVERY_DAYS as an ops knob — with no
    explicit deep_every_days, 0 must keep the sweep at the page budget
    (regression guard: the env var used to be silently ignored)."""
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    monkeypatch.setenv("ATLAS_DEEP_EVERY_DAYS", "0")
    stats = scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert stats["deep_sweep"] is False
    assert stats["sweep_pages"] == (
        1 + scout_core.ATLAS_ROLL_PAGES_DEFAULT  # 1 head page + the tail window
    )
    assert stats["pages_fetched"] == 1 + scout_core.ATLAS_ROLL_PAGES_DEFAULT


def test_deep_sweep_defaults_on_when_env_unset(scout, monkeypatch):
    """Env unset (the workflow default) keeps today's behavior: a fresh
    cursor plus the 30-day default triggers the full-index catch-up."""
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    monkeypatch.delenv("ATLAS_DEEP_EVERY_DAYS", raising=False)
    stats = scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert stats["deep_sweep"] is True
    assert stats["pages_fetched"] == scout_core.ATLAS_DEEP_PAGES_DEFAULT


def test_deep_sweep_clock_does_not_reset_on_steady_harvests(scout, monkeypatch):
    """Regression for the 2026-10-13 coverage bug: the deep-due clock was
    reading atlas_page.updated_at, which every 3-hourly steady harvest
    refreshes — so the 30-day full-index catch-up could never fire and the
    sweep stayed pinned to the first pages forever. The clock must live on
    its own pointer stamped only by COMPLETED deep sweeps."""
    import time as _time

    _patch_http(monkeypatch, pages={"*": INDEX_HTML})
    # A steady harvest that completes well inside the deep window must NOT
    # be a deep sweep, and must not push the deep clock forward.
    steady = scout.harvest_atlas_seeds(deep_every_days=30, throttle_hours=0, stat_pages=0)
    assert steady["deep_sweep"] is True  # first-ever run: catch-up fires
    deep_stamp = scout._atlas_pointer("atlas_deep")
    assert deep_stamp is not None  # completed deep sweep stamps the clock

    # Immediate second run: window closed -> steady, and atlas_deep untouched.
    followup = scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert followup["deep_sweep"] is False
    assert scout._atlas_pointer("atlas_deep") == deep_stamp

    # Simulate steady harvests churning atlas_page for weeks: the deep clock
    # must not care. Back date the deep stamp past the window -> deep fires.
    scout._set_atlas_pointer("atlas_page", 1)  # what steady harvests keep doing
    scout._set_atlas_pointer("atlas_deep", _time.time() - 31 * 86400)
    due = scout.harvest_atlas_seeds(deep_every_days=30, pages=1, stat_pages=0, throttle_hours=0)
    assert due["deep_sweep"] is True
    assert due["pages_fetched"] == scout_core.ATLAS_DEEP_PAGES_DEFAULT


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
            "VALUES (9200000001, 55, 40000, 'keyword', 'Real Row')"
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


def test_proxy_pool_direct_by_default(scout, monkeypatch):
    # CI exports repo Variables (e.g. RBXSCOUT_SEARCH_PROXY_URLS) into the
    # environment; the default-direct contract must hold without them.
    monkeypatch.delenv("RBXSCOUT_SEARCH_PROXY_URLS", raising=False)
    assert scout._atlas_proxy_pool() == ["direct"]


def test_proxy_flip_routes_through_pool(scout, monkeypatch):
    monkeypatch.setenv("RBXSCOUT_SEARCH_PROXY_URLS", "http://p1.example, direct")
    assert scout._atlas_proxy_pool() == ["http://p1.example", "direct"]
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    # Pool entries are path-mirrors: the fetch URL is <mirror>/<target-url>.
    assert calls[0]["url"].startswith("http://p1.example/https://atlasdev.gg/")
    assert calls[0]["proxies"] is None


def test_host_port_pool_entry_routes_as_http_proxy(scout, monkeypatch):
    """A host:port entry is an HTTP proxy (the prufer-validated free pool's
    ip:port shape): the target URL stays direct and requests gets proxies=."""
    monkeypatch.setenv("RBXSCOUT_SEARCH_PROXY_URLS", "http://10.0.0.1:8080, direct")
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert calls[0]["url"].startswith("https://atlasdev.gg/")
    assert calls[0]["proxies"] == {
        "http": "http://10.0.0.1:8080",
        "https": "http://10.0.0.1:8080",
    }


def test_bare_ip_port_pool_entry_accepted_and_routes_as_proxy(scout, monkeypatch):
    """2026-10-12 regression fix: prufer.py exports the validated free pool
    as bare ``ip:port`` (no scheme) — run #722's harvest discarded every
    one of the 191 working proxies at parse time as 'malformed' and then
    burned its Scrape.do/direct fallbacks before aborting. The parser must
    accept the exported shape and the router must send it via proxies=."""
    monkeypatch.setenv(
        "RBXSCOUT_SEARCH_PROXY_URLS", "10.0.0.1:8080, 2.3.4.5:999, direct"
    )
    pool = scout._atlas_proxy_pool()
    assert pool == ["http://10.0.0.1:8080", "http://2.3.4.5:999", "direct"]
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert calls[0]["url"].startswith("https://atlasdev.gg/")
    assert calls[0]["proxies"] == {
        "http": "http://10.0.0.1:8080",
        "https": "http://10.0.0.1:8080",
    }


def test_scrape_do_token_alone_now_becomes_primary(scout, monkeypatch):
    """A ``…url={url}`` entry is a scraping-API template (paid free tiers):
    the target URL is percent-encoded into the query string, no proxies=."""
    monkeypatch.setenv(
        "RBXSCOUT_SEARCH_PROXY_URLS",
        "https://api.scrape.do?token=K&url={url}, direct",
    )
    calls = []
    _patch_http(monkeypatch, pages={"*": INDEX_HTML}, calls=calls)
    scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert calls[0]["url"].startswith(
        "https://api.scrape.do?token=K&url=https%3A%2F%2Fatlasdev.gg"
    )
    assert calls[0]["proxies"] is None
    # The template 404s in this fixture; direct must still complete the sweep.
    assert calls[-1]["url"].startswith("https://atlasdev.gg/")


def test_scrape_do_token_inserted_after_pool_before_direct(scout, monkeypatch):
    """RBXSCOUT_SCRAPE_DO_TOKEN appends the Scrape.do template as a failover
    slice: after the operator/free pool, immediately before the direct
    terminal — a credit only burns once every free proxy has failed."""
    monkeypatch.setenv("RBXSCOUT_SEARCH_PROXY_URLS", "http://10.0.0.1:8080, direct")
    monkeypatch.setenv("RBXSCOUT_SCRAPE_DO_TOKEN", "tok-123")
    assert scout._atlas_proxy_pool() == [
        "http://10.0.0.1:8080",
        "https://api.scrape.do?token=tok-123&url={url}",
        "direct",
    ]


def test_scrape_do_token_alone_now_becomes_primary(scout, monkeypatch):
    """2026-10-11 always-on request: a token with NO free pool now puts the
    template FIRST in the pool and keeps ``direct`` as the terminal
    fallback — an exhausted token degrades to direct (403s, aborts) rather
    than silently skipping the harvest. The laptop opts out
    (RBXSCOUT_SCRAPE_DO_LOCAL_DIRECT=1) so the home IP stays free."""
    monkeypatch.delenv("RBXSCOUT_SEARCH_PROXY_URLS", raising=False)
    monkeypatch.setenv("RBXSCOUT_SCRAPE_DO_TOKEN", "tok-123")
    assert scout._atlas_proxy_pool() == [
        "https://api.scrape.do?token=tok-123&url={url}",
        "direct",
    ]


def test_scrape_do_token_is_percent_encoded(scout, monkeypatch):
    """The token lands in a query value — characters like / and space must
    be encoded, not spliced raw into the URL."""
    monkeypatch.setenv("RBXSCOUT_SEARCH_PROXY_URLS", "http://10.0.0.1:8080")
    monkeypatch.setenv("RBXSCOUT_SCRAPE_DO_TOKEN", "a/b c+d")
    pool = scout._atlas_proxy_pool()
    assert pool[1] == "https://api.scrape.do?token=a%2Fb%20c%2Bd&url={url}"


def test_scrape_do_laptop_opt_out_keeps_direct_only(scout, monkeypatch):
    """RBXSCOUT_SCRAPE_DO_LOCAL_DIRECT=1 (the laptop): a token with no pool
    must NOT promote the template — the home IP keeps its free behavior."""
    monkeypatch.delenv("RBXSCOUT_SEARCH_PROXY_URLS", raising=False)
    monkeypatch.setenv("RBXSCOUT_SCRAPE_DO_TOKEN", "tok-123")
    monkeypatch.setenv("RBXSCOUT_SCRAPE_DO_LOCAL_DIRECT", "1")
    assert scout._atlas_proxy_pool() == ["direct"]


def _workflow_policy_test(env, *, expect_routes="", expect_fail=False):
    """Run expander.yml's REAL route-decision block from the actual YAML.

    The run: block is extracted from .github/workflows/expander.yml so a
    workflow edit can never silently rot the policy tests. The bash script
    prints the final RBXSCOUT_SEARCH_PROXY_URLS instead of running the
    live pipeline; pool fixtures are written into a temp cwd as
    atlas_proxies.txt exactly as the validate step produces them.
    """
    import re
    import subprocess
    import tempfile
    from pathlib import Path

    pool, token = env
    workflow_path = Path(__file__).resolve().parent.parent / ".github/workflows/expander.yml"
    text = workflow_path.read_text(encoding="utf-8")
    # Extract the "Run expander" step's run: block WITHOUT PyYAML — CI's
    # requirements.txt deliberately doesn't carry it, and these tests must
    # run anywhere the repo's own env does.
    match = re.search(
        r"name: Run expander \(Atlas harvest \+ queue drain\)\n"
        r"(?:.*\n)*?        run: \|\n"
        r"((?:          .*\n)+)",
        text,
    )
    assert match, "expander.yml: 'Run expander' step run-block not found"
    block = "\n".join(line[10:] for line in match.group(1).splitlines())

    lines = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("export RBXSCOUT_SEARCH_PROXY_URLS="):
            # Keep the assignment, drop the export; the value is still
            # built exactly as the workflow does it.
            indent = line[: len(line) - len(line.lstrip())]
            lines.append(f"{indent}RBXSCOUT_SEARCH_PROXY_URLS={stripped.split('=', 1)[1]}")
        elif "python live_sync.py" in stripped:
            lines.append(":")  # no-op — never run the real pipeline
        else:
            lines.append(line)
    lines.append('printf \'%s\\n\' "$RBXSCOUT_SEARCH_PROXY_URLS"')
    script = "\n".join(lines)

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "atlas_proxies.txt").write_text("\n".join(f"p{i}" for i in range(pool)))
        child_env = dict(os.environ)
        child_env["RBXSCOUT_SCRAPE_DO_TOKEN"] = token
        child_env["RBXSCOUT_SEARCH_PROXY_URLS"] = ""
        child_env.pop("ATLAS_MIN_PROXY_POOL", None)
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            cwd=tmp,
            timeout=10,
            env=child_env,
        )
    if expect_fail:
        assert result.returncode != 0, (pool, token, result.stdout)
    else:
        assert result.returncode == 0, (pool, token, result.stderr)
        assert expect_routes in result.stdout, (pool, token, result.stdout)


def test_workflow_big_pool_harvests_through_proxies():
    """Pool ≥ 20: the full validated pool routes the harvest; the Scrape.do
    token (when set) rides as a trailing last-resort failover entry."""
    _workflow_policy_test(
        (200, "tok-123"), expect_routes="api.scrape.do?token=tok-123&url={url}"
    )


def test_workflow_tiny_pool_routes_scrape_do():
    """Pool < 20 with a token: the WHOLE harvest runs through Scrape.do,
    direct as terminal fallback — no guaranteed-403 direct wave."""
    _workflow_policy_test(
        (5, "tok-123"), expect_routes="https://api.scrape.do?token=tok-123&url={url},direct"
    )


def test_workflow_tiny_pool_no_token_fails_loud():
    """2026-10-11 fail-loud policy: a small pool with NO token refuses to
    run the harvest (exit non-zero) instead of wasting ~100+ guaranteed-403
    direct requests and possibly a whole discovery day."""
    _workflow_policy_test((5, ""), expect_fail=True)



def test_scrape_do_failover_fires_only_after_proxies_fail(scout, monkeypatch):
    """Order proof: dead free proxy → scrape.do template (target URL
    percent-encoded) → direct still completes the sweep when the template
    403s too. The pool can never be worse than direct."""
    monkeypatch.setenv("RBXSCOUT_SEARCH_PROXY_URLS", "http://10.0.0.1:8080")
    monkeypatch.setenv("RBXSCOUT_SCRAPE_DO_TOKEN", "tok-123")
    calls = []
    real_get = scout_core.requests.get

    def fake_get(url, timeout=30, proxies=None, **kwargs):
        calls.append({"url": url, "proxies": proxies})
        if url.startswith("https://api.scrape.do?"):
            return FakeResponse("out of credits", status_code=403)
        if proxies is not None:
            raise scout_core.requests.exceptions.ConnectionError("dead proxy")
        if "/analyze?" in url:
            return FakeResponse(INDEX_HTML)
        return FakeResponse("", status_code=404)

    monkeypatch.setattr(scout_core.requests, "get", fake_get)
    assert real_get is not None  # keep linters quiet about the unused capture
    # deep_every_days=0 bounds the sweep to pages=1 — otherwise the deep
    # catch-up (default 30d, 275-page ceiling) multiplies the dead-proxy
    # 1s backoff per page into a minutes-long test.
    scout.harvest_atlas_seeds(
        pages=1, stat_pages=0, throttle_hours=0, deep_every_days=0
    )
    assert calls[0]["proxies"] is not None  # free proxy tried first
    assert any(
        c["url"].startswith(
            "https://api.scrape.do?token=tok-123&url=https%3A%2F%2Fatlasdev.gg"
        )
        for c in calls
    )  # then the template, target URL-encoded
    assert calls[-1]["url"].startswith("https://atlasdev.gg/")  # direct finishes
    assert calls[-1]["proxies"] is None


def test_scrape_do_token_never_leaks_into_logs(scout, monkeypatch, caplog):
    """A failing template logs the pool entry (and requests exceptions embed
    the full URL) — the token must be masked in every log line."""
    import logging

    monkeypatch.setenv("RBXSCOUT_SEARCH_PROXY_URLS", "http://10.0.0.1:8080")
    monkeypatch.setenv("RBXSCOUT_SCRAPE_DO_TOKEN", "SECRETTOKEN")
    real_get = scout_core.requests.get

    def fake_get(url, timeout=30, proxies=None, **kwargs):
        if url.startswith("https://api.scrape.do?"):
            return FakeResponse("nope", status_code=403)
        if proxies is not None:
            raise scout_core.requests.exceptions.ConnectionError(
                "https://api.scrape.do?token=SECRETTOKEN&url=x"
            )
        if "/analyze?" in url:
            return FakeResponse(INDEX_HTML)
        return FakeResponse("", status_code=404)

    monkeypatch.setattr(scout_core.requests, "get", fake_get)
    assert real_get is not None
    with caplog.at_level(logging.WARNING, logger="upscalescouts"):
        scout.harvest_atlas_seeds(
            pages=1, stat_pages=0, throttle_hours=0, deep_every_days=0
        )
    assert "SECRETTOKEN" not in caplog.text
    assert "token=***" in caplog.text


def test_pool_failover_reaches_direct_after_mirror_error(scout, monkeypatch):
    """A mirror answering 403 must never abort a sweep: direct is tried next."""
    monkeypatch.setenv("RBXSCOUT_SEARCH_PROXY_URLS", "http://p1.example, direct")
    calls = []

    real_get = scout_core.requests.get

    def fake_get(url, timeout=30, **kwargs):
        calls.append(url)
        if url.startswith("http://p1.example/"):
            return FakeResponse("forbidden", status_code=403)
        if "/analyze?" in url:
            return FakeResponse(INDEX_HTML)
        return FakeResponse("", status_code=404)

    monkeypatch.setattr(scout_core.requests, "get", fake_get)
    assert real_get is not None  # keep linters quiet about the unused capture
    scout.harvest_atlas_seeds(pages=1, stat_pages=0, throttle_hours=0)
    assert calls[0].startswith("http://p1.example/")
    assert any(call.startswith("https://atlasdev.gg/") for call in calls)
    assert scout._atlas_pointer("atlas_last_run") is not None


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
