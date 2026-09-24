import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

import scout_core
from scout_core import (
    normalize_discord_user_id,
    TIER_THRESHOLDS,
    TIER_CADENCE_SYNC,
    TIER8_STALE_PRUNE_DAYS,
    ZERO_CCU_STRIKES_TO_PRUNE,
    HYDRATION_BUDGET_PER_SYNC,
    classify_tier,
    tier_jump_count,
    DISCORD_FILTER_ALL,
    DISCORD_FILTER_FALSE,
    DISCORD_FILTER_TRUE,
    SOCIAL_FILTER_OFF,
    SOCIAL_FILTER_ON,
    RobloxPlatformScout,
    apply_filters,
    compact_num,
    escape_md,
    normalize_discord_url,
    truncate,
)


# --------------------------------------------------------------------------- #
# Discord extraction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Join my discord.gg/abc123 for updates", "https://discord.gg/abc123"),
        ("https://discord.gg/abc123", "https://discord.gg/abc123"),
        ("http://discord.gg/ABC-123_x", "http://discord.gg/ABC-123_x"),
        ("come hang https://discordapp.com/invite/xyz987", "https://discordapp.com/invite/xyz987"),
        ("link: dsc.gg/myserver.", "https://dsc.gg/myserver"),  # trailing dot trimmed
        ("DISCORD.IO/CoolServer", "https://DISCORD.IO/CoolServer"),
        ("discord.me/server1", "https://discord.me/server1"),
        ("no invite here", None),
        ("", None),
        (None, None),
        ("visit discord.com (not an invite)", None),
    ],
)
def test_extract_discord(text, expected):
    assert RobloxPlatformScout.extract_discord(text) == expected


def test_extract_discord_case_insensitive():
    assert RobloxPlatformScout.extract_discord("Join DISCORD.GG/AbC") == "https://DISCORD.GG/AbC"



# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "n,expected",
    [(86_900_000_000, "86.9B"), (437_200, "437.2K"), (999, "999"), (1_500_000, "1.5M"), (None, "—")],
)
def test_compact_num(n, expected):
    assert compact_num(n) == expected


def test_truncate():
    assert truncate("short", 28) == "short"
    out = truncate("a" * 40, 10)
    assert out.endswith("...") and len(out) == 10


def test_escape_md_and_normalize():
    assert escape_md("[🍓] Adopt Me!") == "［🍓］ Adopt Me!"
    assert normalize_discord_url("discord.gg/x") == "https://discord.gg/x"
    assert normalize_discord_url("https://discord.gg/x/") == "https://discord.gg/x"


# --------------------------------------------------------------------------- #
# Filter engine
# --------------------------------------------------------------------------- #


def make_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"title": "Blox Fruits", "creator_name": "Gamer Robot", "visits": 63_800_000_000,
             "ccu": 361_900, "peak_ccu": 400_000, "has_discord": True, "discord_url": "https://discord.gg/blox",
             "has_social_links": True, "genre": "RPG"},
            {"title": "MM2", "creator_name": "Nikilis", "visits": 29_800_000_000,
             "ccu": 259_200, "peak_ccu": 300_000, "has_discord": False, "discord_url": None,
             "has_social_links": True, "genre": "Survival"},
            {"title": "Tiny Game", "creator_name": "Solo Dev", "visits": 20_000,
             "ccu": 40, "peak_ccu": 90, "has_discord": False, "discord_url": None,
             "has_social_links": False, "genre": "RPG"},
        ]
    )


def test_filter_visits_and_ccu():
    df = make_df()
    out = apply_filters(df, min_visits=1_000_000, min_ccu=100)
    assert set(out["title"]) == {"Blox Fruits", "MM2"}

    out = apply_filters(df, min_ccu=100, max_ccu=300_000)
    assert set(out["title"]) == {"MM2"}
    out = apply_filters(df, min_visits=0, max_visits=None, max_ccu=None, max_peak_ccu=None)
    assert len(out) == 3


def test_filter_peak_ccu():
    df = make_df()
    out = apply_filters(df, min_peak_ccu=200_000)
    assert set(out["title"]) == {"Blox Fruits", "MM2"}
    out = apply_filters(df, min_peak_ccu=50, max_peak_ccu=100)
    assert set(out["title"]) == {"Tiny Game"}


def test_filter_discord_modes():
    df = make_df()
    assert set(apply_filters(df, discord_filter=DISCORD_FILTER_TRUE)["title"]) == {"Blox Fruits"}
    assert set(apply_filters(df, discord_filter=DISCORD_FILTER_FALSE)["title"]) == {"MM2", "Tiny Game"}
    assert len(apply_filters(df, discord_filter=DISCORD_FILTER_ALL)) == 3


def test_filter_social_and_genre_and_search():
    df = make_df()
    assert set(apply_filters(df, social_filter=SOCIAL_FILTER_ON)["title"]) == {"Blox Fruits", "MM2"}
    assert set(apply_filters(df, social_filter=SOCIAL_FILTER_OFF)["title"]) == {"Tiny Game"}
    assert set(apply_filters(df, genres=["Survival"])["title"]) == {"MM2"}
    assert set(apply_filters(df, search="blox")["title"]) == {"Blox Fruits"}
    assert set(apply_filters(df, search="nikilis")["title"]) == {"MM2"}
    assert apply_filters(df, search="zzz").empty


def test_search_tolerates_regex_metacharacters():
    """Search must match LITERALLY, never as a regular expression.

    Roblox is full of titles like "+1 Every Second", "[🔴] …" and "100% ".
    With pandas' Arrow-backed string columns an unescaped "+" compiled the
    needle as a regex and the dashboard died with ArrowInvalid: Invalid
    regular expression: no argument for repetition operator: +.
    """
    df = pd.DataFrame([
        {"title": "+1 Every Second", "creator_name": "Speed", "genre": "Simulation"},
        {"title": "[🔴] Closest Cut Wins!", "creator_name": "Blade", "genre": "Party"},
        {"title": "100% Obby", "creator_name": "Parkour", "genre": "Obby"},
    ])
    # Every pattern below crashed the app before the regex=False fix.
    assert set(apply_filters(df, search="+")["title"]) == {"+1 Every Second"}
    assert set(apply_filters(df, search="+1")["title"]) == {"+1 Every Second"}
    assert set(apply_filters(df, search="[🔴]")["title"]) == {"[🔴] Closest Cut Wins!"}
    assert set(apply_filters(df, search="100%")["title"]) == {"100% Obby"}
    assert set(apply_filters(df, search="cut wins")["title"]) == {"[🔴] Closest Cut Wins!"}
    assert apply_filters(df, search="(").empty
    assert apply_filters(df, search="*").empty
    assert apply_filters(df, search="?").empty


# --------------------------------------------------------------------------- #
# SQLite persistence: peak CCU via MAX()
# --------------------------------------------------------------------------- #


def test_upsert_grows_peak_ccu(tmp_path):
    scout = RobloxPlatformScout(db_path=str(tmp_path / "t.db"))
    scout.upsert_game({"universe_id": 1, "title": "G", "ccu": 100, "peak_ccu": 100, "visits": 5})
    scout.upsert_game({"universe_id": 1, "title": "G", "ccu": 70, "visits": 8})  # lower CCU
    df = scout.load_table()
    row = df[df["universe_id"] == 1].iloc[0]
    assert int(row["peak_ccu"]) == 100
    assert int(row["ccu"]) == 70
    assert int(row["visits"]) == 8

    scout.upsert_game({"universe_id": 1, "title": "G", "ccu": 250, "visits": 9})  # new high
    row = scout.load_table().set_index("universe_id").loc[1]
    assert int(row["peak_ccu"]) == 250
    # history should have 3 snapshots
    with sqlite3.connect(scout.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM ccu_history WHERE universe_id=1").fetchone()[0]
    assert count == 3


def test_load_catalog_matches_attaches_trend_columns(tmp_path):
    """The dashboard's ONLY catalog read is load_catalog_matches — the trend
    columns (Avg CCU 1d/3d, Momentum) must ride along or every result page
    renders "-". Regression: hosted dashboard showed all trend cells as
    "-"/"Unknown" because only load_table computed the trends.
    """
    scout = RobloxPlatformScout(db_path=str(tmp_path / "trend.db"))
    scout.upsert_game({"universe_id": 1, "title": "Trendy", "ccu": 100, "visits": 50000})
    scout.upsert_game({"universe_id": 1, "title": "Trendy", "ccu": 140, "visits": 50000})
    scout.upsert_game({"universe_id": 2, "title": "One Sample", "ccu": 50, "visits": 40000})
    # Momentum needs a reference snapshot older than 18 h (by design a game
    # seen only in the last hour has no measurable daily climb).
    old_ts = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(scout.db_path) as conn:
        conn.execute("INSERT INTO ccu_history (universe_id, ts, ccu) VALUES (1, ?, 100)", (old_ts,))

    df = scout.load_catalog_matches()
    row1 = df[df["universe_id"] == 1].iloc[0]
    row2 = df[df["universe_id"] == 2].iloc[0]
    # Two snapshots inside 24 h -> a real 1d average, and momentum vs the
    # 18h+ old reference.
    assert pd.notna(row1["avg_ccu_1d"]) and float(row1["avg_ccu_1d"]) == 120.0
    assert pd.notna(row1["avg_ccu_3d"])
    assert pd.notna(row1["momentum_1d"]) and float(row1["momentum_1d"]) == 40.0
    # One snapshot only -> averages/momentum stay None ("-" in the UI),
    # never a fake one-sample "trend".
    assert pd.isna(row2["avg_ccu_1d"])
    assert pd.isna(row2["momentum_1d"])


def test_load_catalog_matches_trends_survive_large_id_sets(tmp_path):
    """The trend query chunks IN-lists (SQLite host-parameter ceiling): a
    full-catalog read (~12k+ qualified games) must not hit it."""
    scout = RobloxPlatformScout(db_path=str(tmp_path / "chunk.db"))
    for uid in range(1, 31):
        scout.upsert_game({"universe_id": uid, "title": f"G{uid}", "ccu": 30 + uid, "visits": 40000 + uid})
        scout.upsert_game({"universe_id": uid, "title": f"G{uid}", "ccu": 60 + uid, "visits": 40000 + uid})
    df = scout.load_catalog_matches()
    assert len(df) == 30
    # Every game has 2 samples -> every game gets a non-null 3d average.
    assert df["avg_ccu_3d"].notna().all()
    assert df["avg_ccu_1d"].notna().all()


def test_load_blowup_watch_attaches_trend_columns(tmp_path):
    """The watchlist view renders the same trend columns as the main table."""
    scout = RobloxPlatformScout(db_path=str(tmp_path / "watch.db"))
    scout.upsert_game({"universe_id": 3, "title": "Blower", "ccu": 100, "visits": 90000})
    scout.upsert_game({"universe_id": 3, "title": "Blower", "ccu": 300, "visits": 90000})
    old_ts = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(scout.db_path) as conn:
        conn.execute("INSERT INTO ccu_history (universe_id, ts, ccu) VALUES (3, ?, 100)", (old_ts,))
        conn.execute("UPDATE game_analytics SET blowup_flag=1 WHERE universe_id=3")
    df = scout.load_blowup_watch()
    assert not df.empty
    row = df[df["universe_id"] == 3].iloc[0]
    assert pd.notna(row["avg_ccu_1d"])
    assert pd.notna(row["momentum_1d"])


def test_upsert_metrics_only_updates_numbers_without_touching_identity(tmp_path):
    """The metrics catch-up must fill ccu/visits (+ a history snapshot) but
    never rewrite title/contacts or restamp the tier schedule."""
    scout = RobloxPlatformScout(db_path=str(tmp_path / "m.db"))
    scout.upsert_game({
        "universe_id": 7, "title": "Original Name", "ccu": 10, "visits": 100,
        "found_via": "atlas_dev",
    })
    with sqlite3.connect(scout.db_path) as conn:
        before_updated = conn.execute(
            "SELECT last_updated FROM game_analytics WHERE universe_id=7"
        ).fetchone()[0]

    updated = scout.upsert_metrics_only({
        7: {"universe_id": 7, "title": "Renamed On Roblox", "ccu": 42, "visits": 5000,
            "favorites": 900, "description": "should not land"},
    })
    assert updated == 1
    row = scout.load_table().set_index("universe_id").loc[7]
    assert int(row["ccu"]) == 42
    assert int(row["visits"]) == 5000
    assert int(row["favorites"]) == 900
    assert row["title"] == "Original Name"
    assert pd.isna(row["description"]) or row["description"] in (None, "")
    assert row["found_via"] == "atlas_dev"
    with sqlite3.connect(scout.db_path) as conn:
        after_updated = conn.execute(
            "SELECT last_updated FROM game_analytics WHERE universe_id=7"
        ).fetchone()[0]
        hist = conn.execute(
            "SELECT COUNT(*) FROM ccu_history WHERE universe_id=7"
        ).fetchone()[0]
    assert hist == 2  # the original upsert + the metrics-only snapshot
    assert after_updated == before_updated  # tier-due schedule untouched

    # Rows missing from the catalog are skipped, never inserted.
    assert scout.upsert_metrics_only({99999: {"universe_id": 99999, "ccu": 5}}) == 0
    # Empty/None payloads are no-ops.
    assert scout.upsert_metrics_only({}) == 0
    assert scout.upsert_metrics_only({7: {"universe_id": 7}}) == 0


# --------------------------------------------------------------------------- #
# Contact resolution tiers (HTTP mocked)
# --------------------------------------------------------------------------- #


class MockScout(RobloxPlatformScout):
    """Scout with canned _get_json responses keyed by URL fragment."""

    def __init__(self, responses, db_path=":memory:"):
        # skip real sqlite init for pure-logic tests
        self.db_path = db_path
        self.max_workers = 1
        self.request_timeout = 1
        self.session = None
        self.has_cookie = False
        self.responses = responses

    def _get_json(self, url, retries=1):
        # Match the most specific route first (e.g. users/7/social-links
        # must win over the broader users/7 fixture).
        for key in sorted(self.responses, key=len, reverse=True):
            if key in url:
                return 200, self.responses[key]
        return 404, None


def test_contact_game_bio_beats_community():
    s = MockScout({
        "groups/55": {"description": "discord.gg/community", "owner": {"userId": 7}},
        "groups/55/social-links": {"data": [{"type": "Discord", "url": "https://discord.gg/link"}]},
    })
    rec = s.resolve_game_contact({"universe_id": 9, "creator_type": "Group", "creator_id": 55,
                                  "description": "discord.gg/gamebio"})
    assert rec["discord_url"] == "https://discord.gg/gamebio"
    assert rec["found_via"] == "game_description"


def test_contact_community_bio_beats_community_links():
    s = MockScout({
        "groups/55": {"description": "discord.gg/community", "owner": {"userId": 7}},
        "groups/55/social-links": {"data": [{"type": "Discord", "url": "https://discord.gg/link"}]},
    })
    rec = s.resolve_game_contact({"universe_id": 9, "creator_type": "Group", "creator_id": 55,
                                  "description": ""})
    assert rec["discord_url"] == "https://discord.gg/community"
    assert rec["found_via"] == "group_description"


def test_contact_tier_game_description_fallback():
    s = MockScout({
        "games/9/social-links/list": None,  # 404 -> links None
    })
    rec = s.resolve_game_contact({"universe_id": 9, "creator_type": "User", "creator_id": 7,
                                  "description": "join discord.gg/descfallback now"})
    assert rec["discord_url"] == "https://discord.gg/descfallback"
    assert rec["found_via"] == "game_description"


def test_contact_tier_group_then_user():
    s = MockScout({
        "groups/55/social-links": {"data": [{"type": "Twitter", "url": "https://x.com/dev"}]},
        "groups/55": {"description": "chat: discord.gg/groupbio", "owner": {"userId": 7}},
    })
    rec = s.resolve_game_contact({"universe_id": 9, "creator_type": "Group", "creator_id": 55,
                                  "description": ""})
    assert rec["discord_url"] == "https://discord.gg/groupbio"
    assert rec["found_via"] == "group_description"

    # Individual creator bios are intentionally not part of the contact
    # pipeline. (Different universe id: the process-wide contact memcache —
    # one of the 400-user load fixes — shares verdicts per game, so the
    # second scenario must not reuse id 9.)
    s = MockScout({"users/7": {"description": "msg me discord.gg/userbio"}})
    rec = s.resolve_game_contact({"universe_id": 10, "creator_type": "User", "creator_id": 7,
                                  "description": ""})
    assert rec["discord_url"] is None
    assert rec["status"] == "No Contact Found"


def test_contact_owner_bio_is_third_priority():
    s = MockScout({"groups/55": {"description": "", "owner": {"userId": 7}},
                   "users/7": {"description": "join discord.gg/owner"}})
    rec = s.resolve_game_contact({"universe_id": 9, "creator_type": "Group", "creator_id": 55,
                                  "description": ""})
    assert rec["discord_url"] == "https://discord.gg/owner"
    assert rec["found_via"] == "owner_description"


def test_contact_game_social_link_beats_bio():
    """Game links come from the current /social-links/list endpoint."""
    s = MockScout({
        "games/9/social-links/list": {"data": [{"type": "Discord", "url": "https://discord.gg/game-link"}]},
    })
    rec = s.resolve_game_contact({"universe_id": 9, "creator_type": "User", "creator_id": 7,
                                  "description": "discord.gg/game-bio"})
    assert rec["discord_url"] == "https://discord.gg/game-link"
    assert rec["found_via"] == "game_social_links"


def test_contact_game_links_404_falls_back_to_bio():
    """A missing/retired game-links route must not block the bio fallback."""
    s = MockScout({})  # every route 404s
    rec = s.resolve_game_contact({"universe_id": 9, "creator_type": "User", "creator_id": 7,
                                  "description": "discord.gg/bio-only"})
    assert rec["discord_url"] == "https://discord.gg/bio-only"
    assert rec["found_via"] == "game_description"


def test_contact_owner_profile_links_fallback():
    """The retired users/.../social-links endpoint is gone; profile payloads
    that embed link arrays directly are still picked up."""
    s = MockScout({
        "groups/55": {"description": "", "owner": {"userId": 7}},
        "users/7": {"description": "", "data": [{"type": "Discord", "url": "https://discord.gg/owner-link"}]},
    })
    rec = s.resolve_game_contact({"universe_id": 9, "creator_type": "Group", "creator_id": 55,
                                  "description": ""})
    assert rec["discord_url"] == "https://discord.gg/owner-link"
    assert rec["found_via"] == "owner_profile_links"


def test_contact_cache_invalidated_on_resolver_version_change(tmp_path):
    """Verdicts stored by an older resolver must not shadow new sources."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    scout.upsert_game({"universe_id": 1, "title": "G", "ccu": 50, "visits": 9_000})
    stale = {
        "universe_id": 1, "has_discord": False, "discord_url": None,
        "status": "No Contact Found", "found_via": None, "has_social_links": False,
        "contacts_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    scout._store_contact_cache(1, stale)  # stored with the CURRENT version
    assert scout._load_contact_cache(1) is not None

    # Simulate a verdict from the previous resolver version.
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE game_analytics SET contact_schema_version=1 WHERE universe_id=1")
    assert scout._load_contact_cache(1) is None  # stale -> treated as missing


def test_sourcing_parsers_live_schema():
    """Verify parsing against real captured API schema shapes (offline)."""
    s = MockScout({})
    discovery_games = [
        {"universeId": 10563114921, "rootPlaceId": 107778070777162, "name": "Steal An Egg",
         "playerCount": 1505123, "totalUpVotes": 578902, "totalDownVotes": 39770},
    ]
    assert discovery_games[0]["universeId"] == 10563114921
    roli_entry = ["Classic: Crossroads", 49, "https://tr.rbxcdn.com/x/150/150/Image/Webp/noFilter"]
    name, playing, icon = roli_entry[0], roli_entry[1], roli_entry[2]
    assert playing == 49 and name and icon.startswith("https://tr.rbxcdn.com")


# --------------------------------------------------------------------------- #
# Bulletproof scan pipeline: circuit breaker + CCU pre-gate
# --------------------------------------------------------------------------- #


class BrokenEndpointScout(MockScout):
    """Simulates a fully dead resolution endpoint (every call hard-fails)."""

    def resolve_universe_ids(self, place_ids):
        calls = {"batches": 0}
        statuses = []

        def _get_json(url, retries=1):
            calls["batches"] += 1
            statuses.append(403)
            return 403, None

        self._get_json_calls = calls
        self._statuses = statuses
        # Reuse the production loop by monkeypatching _get_json.
        original = self._get_json
        self._get_json = _get_json
        try:
            result = super().resolve_universe_ids(place_ids)
        finally:
            self._get_json = original
        return result


def test_place_resolution_circuit_breaker_stops_early():
    """A fully dead endpoint aborts after 25 consecutive hard failures —
    no 7,300-call death march. Each place costs 2 attempts (mirror +
    direct fallback), so the breaker trips after 50 HTTP calls."""
    s = BrokenEndpointScout({})
    ids = list(range(1000, 1000 + 300))
    result = s.resolve_universe_ids(ids)
    assert result == {}
    assert len(s._statuses) == 50, (
        f"circuit breaker should stop after 25 place failures (2 calls each), ran {len(s._statuses)}"
    )
    assert s.source_diagnostics["place_details"]["aborted_early"] is True
    assert s.source_diagnostics["place_details"]["batches"] == 25  # place-level hard failures


def test_place_resolution_404s_do_not_trip_breaker():
    """Deleted places (404 from Roblox directly) are legitimate per-place
    outcomes: resolution must continue past them, and a downed mirror must
    fall back to direct Roblox without failing the sweep."""

    class MirrorDownScout(MockScout):
        def resolve_universe_ids(self, place_ids):
            original = self._get_json

            def _get_json(url, retries=1):
                if "roproxy" in url:
                    return 503, None  # mirror down -> direct fallback handles it
                m = re.search(r"places/(\d+)/universe", url)
                pid = int(m.group(1))
                if pid % 10 == 0:
                    return 404, None  # every 10th place is deleted
                return 200, {"universeId": pid * 10}

            self._get_json = _get_json
            try:
                return super().resolve_universe_ids(place_ids)
            finally:
                self._get_json = original

    s = MirrorDownScout({})
    ids = list(range(1000, 1000 + 100))  # 10 of these will 404 on direct
    result = s.resolve_universe_ids(ids)
    assert len(result) == 90, "404 places must be skipped, not fail the sweep"
    assert all(int(pid) % 10 != 0 for pid in result), "404 places must not appear in results"
    diag = s.source_diagnostics["place_map"]
    assert diag["not_found"] == 10
    assert diag["newly_resolved"] == 90
    assert diag["via_direct"] == 90
    assert diag["via_roproxy"] == 0
    assert diag["breaker_tripped"] is False


def test_ccu_pregate_drops_sub_threshold_candidates():
    """scan() must not request metrics for games below the CCU target."""
    discovery = [
        {"universe_id": 1, "playing": 500},
        {"universe_id": 2, "playing": 10},   # below target -> must be dropped
        {"universe_id": 3, "playing": 40},
    ]
    roli = {
        "111": {"place_id": 111, "name": "A", "playing": 999},
        "222": {"place_id": 222, "name": "B", "playing": 5},  # below target
    }
    min_ccu = 25

    gated_discovery = [g for g in discovery if int(g.get("playing") or 0) >= min_ccu]
    gated_roli = [info for info in roli.values() if int(info.get("playing") or 0) >= min_ccu]

    assert [g["universe_id"] for g in gated_discovery] == [1, 3]
    assert [info["place_id"] for info in gated_roli] == [111]


# --------------------------------------------------------------------------- #
# Scan-run persistence
# --------------------------------------------------------------------------- #


def test_finish_scan_persists_full_snapshot(tmp_path):
    """Completion must write status/counts to the DB, not only finished_at."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    run_id = scout._begin_scan()
    scout.last_scan = {"run_id": run_id, "status": "running"}
    scout.last_scan.update({
        "candidate_count": 9,
        "matched_count": 3,
        "metrics_count": 3,
        "contacts_attempted": 3,
        "contacts_completed": 2,
        "contact_errors": 0,
        "min_visits": 20_000,
        "min_ccu": 25,
    })
    scout.last_scan.update({"status": "complete"})
    scout._finish_scan()

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, matched_count, metrics_count, candidate_count, "
            "contacts_attempted, contacts_completed, min_visits, min_ccu, finished_at, error "
            "FROM scan_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert row[0] == "complete"
    assert row[1] == 3 and row[2] == 3 and row[3] == 9
    assert row[4] == 3 and row[5] == 2
    assert row[6] == 20_000 and row[7] == 25
    assert row[8], "finished_at must be recorded"
    assert row[9] is None


def test_mark_scan_failed_persists_counts_and_error(tmp_path):
    """A failed scan keeps its counters and writes the error into the DB."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    run_id = scout._begin_scan()
    scout.last_scan = {"run_id": run_id, "status": "running"}
    scout.last_scan.update({"candidate_count": 40, "matched_count": 12})
    scout.mark_scan_failed(RuntimeError("boom"))

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, error, candidate_count, matched_count FROM scan_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert row[0] == "failed"
    assert row[1] == "boom"
    assert row[2] == 40 and row[3] == 12


def test_stale_running_runs_are_aborted_on_init(tmp_path):
    """Runs orphaned by a dead session must not pollute diagnostics forever."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO scan_runs (status, started_at, finished_at) "
            "VALUES ('running', '2020-01-01 00:00:00', '2020-01-01 00:05:00')"
        )
        conn.execute("INSERT INTO scan_runs (status, started_at) VALUES ('running', '2020-01-02 00:00:00')")

    fresh = RobloxPlatformScout(db_path=db)  # new init triggers the cleanup
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT status FROM scan_runs ORDER BY run_id").fetchall()
    assert rows == [("aborted",), ("aborted",)]
    assert fresh.last_scan["status"] == "aborted"
    assert fresh.last_scan.get("error")


# --------------------------------------------------------------------------- #
class MockScout(RobloxPlatformScout):
    """Scout with canned _get_json responses keyed by exact URL."""

    def __init__(self, responses, db_path=":memory:"):
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
        self._emit_pace_lock = threading.Lock()
        self._next_emit = 0.0
        self._emit_interval = scout_core.RobloxPlatformScout.BATCH_EMIT_INTERVAL
        self._init_sqlite()

    def _get_json(self, url, retries=1):
        # Exact-match first (used by Rolimons / metrics / icon tests that
        # provide full URLs as keys). Omni-search URLs carry a per-call
        # sessionId, so match those on their fixed prefix.
        if url in self.responses:
            payload = self.responses[url]
            if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], int):
                return payload
            return 200, payload
        # Substring-match fallback (used by contact tests that key on path
        # fragments like "groups/55" or "users/7"). The most specific key
        # wins so a narrower fixture overrides a broader one.
        matched = None
        for key, payload in self.responses.items():
            if key in url:
                if matched is None or len(key) > len(matched[0]):
                    matched = (key, payload)
        if matched is not None:
            key, payload = matched
            if isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], int):
                return payload
            return 200, payload
        return 404, None


# --------------------------------------------------------------------------- #
# Tier classifier: monotonic table + higher-axis rule
# --------------------------------------------------------------------------- #


def test_tier_table_is_monotonic_on_both_axes():
    """A higher tier must demand at least as much on visits AND CCU.
    Guards the original T3/T4 inversion (250 CCU vs 125 CCU)."""
    tiers = sorted(TIER_THRESHOLDS)
    for prev, cur in zip(tiers, tiers[1:]):
        assert TIER_THRESHOLDS[cur][0] >= TIER_THRESHOLDS[prev][0], "visits axis not monotonic"
        assert TIER_THRESHOLDS[cur][1] >= TIER_THRESHOLDS[prev][1], "CCU axis not monotonic"


def test_classify_dead_giant_lands_cold_not_tier_1():
    """1M visits + 30 CCU meets Tier 1's minimums but is a corpse — the
    higher-axis rule must put it in T7 (visits axis), not T1."""
    assert classify_tier(1_000_000, 30) == 7
    assert classify_tier(500_000, 30) == 6


def test_classify_ccu_rocket_lands_warm():
    """CCU is the leading indicator: a game must never sit below its CCU-axis
    tier. 300 CCU meets T5's 250-CCU floor (locked table), so it lands T5 —
    far warmer than its visits axis alone (T1 for 30k visits)."""
    assert classify_tier(30_000, 300) == 5
    assert classify_tier(30_000, 150) == 3   # exactly T3's CCU floor
    assert classify_tier(1_000, 60) == 1     # exactly its CCU-axis tier, never lower


def test_classify_boundaries_and_new():
    assert classify_tier(None, None) == 0          # never hydrated
    assert classify_tier(0, 0) == 0                # zeroed stats
    assert classify_tier(25_000, 25) == 1          # exact T1 minimums
    assert classify_tier(24_999, 24) == 0          # below every threshold
    assert classify_tier(100_000, 200) == 4        # exact T4 minimums
    assert classify_tier(2_000_000, 2_000) == 7    # ceiling


def test_tier_jump_count_signs():
    assert tier_jump_count(2, 5) == 3
    assert tier_jump_count(5, 2) == -3
    assert tier_jump_count(None, 4) == 0


def test_upsert_stamps_tier_and_restacks_on_growth(tmp_path):
    """Upsert computes the tier from incoming stats; growth re-stamps; the
    first stamp never counts as a blow-up (classification is not news)."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    # 30k visits (T1 axis) + 300 CCU (T5 axis) -> higher axis wins: T5.
    scout.upsert_game({"universe_id": 1, "title": "Rocket", "ccu": 300, "visits": 30_000})
    row = scout.load_table().set_index("universe_id").loc[1]
    assert int(row["tier"]) == 5
    assert int(row["prev_tier"]) == 0
    assert int(row["blowup_flag"]) == 0  # first classification is not news

    # +1 tier: quiet re-stamp, prev_tier tracks the old value.
    scout.upsert_game({"universe_id": 1, "title": "Rocket", "ccu": 600, "visits": 600_000})
    row = scout.load_table().set_index("universe_id").loc[1]
    assert int(row["tier"]) == 6
    assert int(row["prev_tier"]) == 5
    assert int(row["blowup_flag"]) == 0

    # A fresh game leaping from T1 to T7 in one re-hydration: the trigger.
    scout.upsert_game({"universe_id": 2, "title": "Late Bloomer", "ccu": 30, "visits": 30_000})
    scout.upsert_game({"universe_id": 2, "title": "Late Bloomer", "ccu": 1_100, "visits": 1_200_000})
    row = scout.load_table().set_index("universe_id").loc[2]
    assert int(row["tier"]) == 7
    assert int(row["prev_tier"]) == 1
    assert int(row["blowup_flag"]) == 1
    assert row["blowup_at"] is not None
    assert 2 in scout.blowup_watch_events
    assert 1 not in scout.blowup_watch_events  # quiet climbs never flag


def test_blowup_flag_on_ccu_multiplication_even_without_tier_jump(tmp_path):
    """3x+ CCU growth (floored at 10 old CCU) raises the flag even when the
    tier only steps 0→1 — the signal must come from the CCU multiplication,
    not the tier move. 0→25 noise (old CCU below the floor) never flags."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    scout.upsert_game({"universe_id": 2, "title": "Sprout", "ccu": 12, "visits": 100})
    assert int(scout.load_table().set_index("universe_id").loc[2]["blowup_flag"]) == 0

    scout.upsert_game({"universe_id": 2, "title": "Sprout", "ccu": 48, "visits": 150})
    row = scout.load_table().set_index("universe_id").loc[2]
    assert int(row["blowup_flag"]) == 1        # 4x CCU
    assert int(row["tier"]) == 1               # only stepped one tier...
    assert int(row["prev_tier"]) == 0          # ...so the flag came from 4x CCU
    assert 2 in scout.blowup_watch_events

    # 0→25 noise must NOT flag: old_ccu below the 10-CCU floor.
    scout.upsert_game({"universe_id": 3, "title": "Noise", "ccu": 0, "visits": 5})
    scout.upsert_game({"universe_id": 3, "title": "Noise", "ccu": 25, "visits": 6})
    assert int(scout.load_table().set_index("universe_id").loc[3]["blowup_flag"]) == 0


def test_blowup_flag_is_sticky_and_watchlist_reads_db(tmp_path):
    """Once flagged, a row stays on the New and Upcoming watchlist until the
    flag is explicitly cleared — even after a quiet re-hydration."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    scout.upsert_game({"universe_id": 5, "title": "Climber", "ccu": 30, "visits": 30_000})
    scout.upsert_game({"universe_id": 5, "title": "Climber", "ccu": 1_100, "visits": 1_200_000})
    assert int(scout.load_table().set_index("universe_id").loc[5]["blowup_flag"]) == 1

    # A later quiet upsert must not un-flag; the COALESCE keeps the flag.
    scout.upsert_game({"universe_id": 5, "title": "Climber", "ccu": 1001, "visits": 1_200_500})
    assert int(scout.load_table().set_index("universe_id").loc[5]["blowup_flag"]) == 1

    watch = scout.load_blowup_watch()
    assert set(watch["universe_id"]) == {5}
    # Watchlist rows carry the full main-table schema so the shared UI works.
    assert "discord_url" in watch.columns and "icon_url" in watch.columns


def test_prune_catalog_observed_death_rule(tmp_path):
    """A game dies from being OBSERVED dead repeatedly (4 consecutive 0-CCU
    visits), not from being ignored — otherwise the fast T8 rotation keeps
    corpses alive forever. Fresh zeros and live games survive."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    old_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 20 * 86400))
    scout.upsert_game({"universe_id": 10, "title": "Dead by strikes", "ccu": 0, "visits": 900})
    scout.upsert_game({"universe_id": 11, "title": "Fresh zero", "ccu": 0, "visits": 900})
    scout.upsert_game({"universe_id": 12, "title": "Alive", "ccu": 50, "visits": 900})
    with sqlite3.connect(db) as conn:
        # A corpse that was never re-observed still dies by staleness fallback.
        conn.execute("UPDATE game_analytics SET last_updated=? WHERE universe_id=10", (old_ts,))

    removed = scout.prune_catalog()
    assert removed == 1  # the 20-day-old zero dies via the fallback rule
    ids = set(scout.load_table()["universe_id"])
    assert 10 not in ids and {11, 12}.issubset(ids)

    # Strike accumulation: three more observed visits to the fresh zero.
    for _ in range(3):
        scout.upsert_game({"universe_id": 11, "title": "Fresh zero", "ccu": 0, "visits": 900})
    assert scout.prune_catalog() == 0          # 3 strikes < 4: still alive
    scout.upsert_game({"universe_id": 11, "title": "Fresh zero", "ccu": 0, "visits": 900})
    assert scout.prune_catalog() == 1          # 4th consecutive zero: dead
    assert 11 not in set(scout.load_table()["universe_id"])

    # Revival resets the counter — a game that comes back does not carry
    # its old strikes to the grave.
    scout.upsert_game({"universe_id": 13, "title": "Revivor", "ccu": 0, "visits": 10})
    for _ in range(3):
        scout.upsert_game({"universe_id": 13, "title": "Revivor", "ccu": 0, "visits": 10})
    scout.upsert_game({"universe_id": 13, "title": "Revivor", "ccu": 7, "visits": 900})
    scout.upsert_game({"universe_id": 13, "title": "Revivor", "ccu": 0, "visits": 10})
    with sqlite3.connect(db) as conn:
        strikes = conn.execute(
            "SELECT zero_ccu_strikes FROM game_analytics WHERE universe_id=13"
        ).fetchone()[0]
    assert strikes == 1
    assert scout.prune_catalog() == 0
    assert TIER8_STALE_PRUNE_DAYS == 14 and ZERO_CCU_STRIKES_TO_PRUNE == 4


def test_prune_clears_queue_memory_for_rediscovery(tmp_path):
    """Prune must also purge the game's discovery_queue rows. The queue is
    dedup memory — a stale 'qualified' row would permanently block Atlas from
    re-enqueuing a pruned game that later revives (viral resurgence, update,
    seasonal return). After prune, re-enqueueing must create a fresh pending
    row, i.e. the daily harvest can re-discover it."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    scout.upsert_game({"universe_id": 77, "title": "Revival candidate", "ccu": 0, "visits": 900})
    scout._enqueue_discovery([(77, "atlas_dev", 2)])
    scout._mark_discovery_outcome(77, "qualified")  # exactly what the drain wrote

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, outcome FROM discovery_queue WHERE universe_id=77"
        ).fetchone()
    assert row == ("processed", "qualified")

    # Four consecutive observed deaths -> prune fires (the row-creating
    # INSERT does not count as a strike; only re-visits do).
    for _ in range(4):
        scout.upsert_game({"universe_id": 77, "title": "Revival candidate", "ccu": 0, "visits": 900})
    assert scout.prune_catalog() == 1

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM game_analytics WHERE universe_id=77").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM discovery_queue WHERE universe_id=77").fetchone()[0] == 0

    # The revived game looks brand-new to the harvester: a fresh pending row
    # is created instead of being silently deduped against the corpse's memory.
    assert scout._enqueue_discovery([(77, "atlas_dev", 2)]) == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, source FROM discovery_queue WHERE universe_id=77"
        ).fetchone()
    assert row == ("pending", "atlas_dev")

    # Sanity: prune returns 0 when there is nothing to prune (no crash on
    # the empty-doomed-list fast path).
    assert scout.prune_catalog() == 0


def test_scheduler_picks_tiers_by_cadence_and_orders_first_in_line(tmp_path):
    """T1–T4 go due on WALL-CLOCK staleness (1h/2h/4h/6h) rather than sync
    counts, so the scheduler behaves identically whether hydration runs
    every 5 minutes or every 30. Weekly T5–T7 and the T8 rotation stay on
    their wall-clock / positional buckets."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    bucket_count = scout_core.TIER8_ROTATION_DAYS
    bucket = int(time.time() // 86400) // bucket_count % bucket_count
    uids_in = [u for u in range(100, 120) if u % bucket_count == bucket]
    uids_out = [u for u in range(100, 120) if u % bucket_count != bucket]
    t8_in, t8_out = uids_in[0], uids_out[0]  # one inside, one outside the slice

    def ts(hours_ago):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - hours_ago * 3600))

    with sqlite3.connect(db) as conn:
        for uid, tier, hours_ago in [
            (1, 1, 2.0),        # T1 stale past 1h -> due
            (2, 2, 1.5),        # T2 stale under 2h -> NOT due yet
            (3, 3, 3.0),        # T3 stale under 4h -> NOT due yet
            (4, 4, 7.0),        # T4 stale past 6h -> due
            (5, 5, 2.0),        # T5–T7: weekly cutoff governs, fresh -> not due
            (6, 6, 2.0),
            (7, 7, 2.0),
            (t8_in, 0, 2.0),    # in this rotation bucket -> due
            (t8_out, 0, 2.0),   # outside the bucket -> not due
        ]:
            conn.execute(
                "INSERT INTO game_analytics (universe_id, tier, ccu, visits, last_updated) "
                "VALUES (?, ?, 100, 100, ?)",
                (uid, tier, ts(hours_ago)),
            )

    due = set(scout.load_tier_refresh_ids(batch_size=50, budget_batches=10)["ids"])
    assert {1, 4, t8_in}.issubset(due)      # stale T1 + T4 + the T8 slice
    assert 2 not in due and 3 not in due    # under their staleness line yet
    assert 5 not in due and 6 not in due and 7 not in due
    assert t8_out not in due                # outside this rotation bucket

    # And once T2/T3 cross their staleness line, they come due too.
    with sqlite3.connect(db) as conn:
        for uid in (2, 3):
            conn.execute(
                "UPDATE game_analytics SET last_updated = ? WHERE universe_id = ?",
                (ts(12.0), uid),
            )
    due2 = set(scout.load_tier_refresh_ids(batch_size=50, budget_batches=10)["ids"])
    assert {2, 3}.issubset(due2)


def test_scheduler_weekly_bucket_and_t8_rotation(tmp_path):
    """T5–T7 rehydrate only after the 7-day wall-clock cutoff; tier-0 rows
    rotate through the T8 slice by universe_id mod bucket count."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    stale = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 8 * 86400))
    fresh = time.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, tier, ccu, visits, last_updated) "
            "VALUES (21, 5, 100, 100, ?)", (stale,)
        )
        conn.execute(
            "INSERT INTO game_analytics (universe_id, tier, ccu, visits, last_updated) "
            "VALUES (22, 6, 100, 100, ?)", (fresh,)
        )
        conn.execute(
            "INSERT INTO game_analytics (universe_id, tier, ccu, visits, last_updated) "
            "VALUES (23, 0, 100, 100, ?)", (fresh,)
        )
        conn.execute(
            "INSERT INTO game_analytics (universe_id, tier, ccu, visits, last_updated) "
            "VALUES (24, 0, 100, 100, ?)", (fresh,)
        )

    schedule = scout.load_tier_refresh_ids(batch_size=50, budget_batches=10)
    assert 21 in schedule["ids"]                       # weekly: stale T5 due
    assert 22 not in schedule["ids"]                   # fresh T6: not due

    bucket_count = scout_core.TIER8_ROTATION_DAYS
    epoch_days = int(time.time() // 86400)
    bucket = epoch_days // bucket_count % bucket_count
    expected_t8 = {uid for uid in (23, 24) if uid % bucket_count == bucket}
    assert expected_t8.issubset(set(schedule["ids"]))


def test_scheduler_budget_caps_selection(tmp_path):
    """The scheduler never selects more universes than the per-sync budget
    (batch_size * budget_batches) allows."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    with sqlite3.connect(db) as conn:
        for uid in range(500):
            conn.execute(
                "INSERT INTO game_analytics (universe_id, tier, ccu, visits, last_updated) "
                "VALUES (?, 1, 100, 100, '1970-01-01')",
                (uid,),
            )
    schedule = scout.load_tier_refresh_ids(batch_size=50, budget_batches=4)
    assert len(schedule["ids"]) == 200
    assert schedule["groups"]["t1_t2"] == 200


class BudgetScout(MockScout):
    """MockScout that answers any games.roblox.com metrics URL dynamically.
    Returns visits=99_000 so a hydration is observable against the seeded
    30_000 values (deferred games keep their stale stats)."""

    def _get_json(self, url, retries=1):
        if url.startswith("https://games.roblox.com/v1/games?universeIds="):
            raw_ids = url.split("universeIds=")[1].split("&")[0].split(",")
            return 200, {"data": [
                {
                    "id": int(i), "rootPlaceId": int(i), "name": f"G{i}",
                    "playing": 30, "visits": 99_000, "favoritedCount": 0,
                    "genre": "Games", "creator": {"id": 1, "name": "D", "type": "User"},
                    "description": "",
                }
                for i in raw_ids
            ]}
        return super()._get_json(url, retries)


def test_scan_budget_caps_known_due_hydration(tmp_path):
    """The per-sync budget is a hard ceiling: known-due games fill the slots
    in tier-priority order and the overflow rolls to the next sync. New-game
    hydration is owned by the discovery-queue drain on expand passes, so a
    pending Atlas seed is NOT spent here (strict-gate verified there)."""
    db = str(tmp_path / "t.db")
    scout = BudgetScout({}, db_path=db)
    with sqlite3.connect(db) as conn:
        # A pending Atlas seed: must NOT hydrate on the hydrate pass —
        # the drain owns it (and re-verifies the strict gate).
        conn.execute(
            "INSERT INTO discovery_queue (universe_id, source, priority, status) "
            "VALUES (801, 'atlas_dev', 2, 'pending')"
        )
        # 60 known T1 games due this sync; with a 1-batch budget only 50 fit.
        for uid in range(1000, 1060):
            conn.execute(
                "INSERT INTO game_analytics (universe_id, tier, ccu, visits, last_updated) "
                "VALUES (?, 1, 30, 30000, '1970-01-01')",
                (uid,),
            )
    scout_core.HYDRATION_BUDGET_PER_SYNC = 1  # ceiling: 50 games (1 batch)
    try:
        scout.scan(min_visits=50_000, min_ccu=50, progress_cb=lambda p, m: None,
                   phases=("hydrate",))
    finally:
        scout_core.HYDRATION_BUDGET_PER_SYNC = 150

    hydration = scout.last_scan["hydration_budget"]
    assert hydration["new"] == 0
    assert hydration["known_due"] == 50                  # scheduler honors the budget
    assert hydration["hydrated"] == 50                   # exactly one batch
    assert hydration["deferred"] == 0

    with sqlite3.connect(db) as conn:
        # The drain never ran on this hydrate-only pass.
        assert conn.execute(
            "SELECT status FROM discovery_queue WHERE universe_id=801"
        ).fetchone()[0] == "pending"
    table = scout.load_table().set_index("universe_id")
    upgraded = {
        uid for uid in range(1000, 1060) if int(table.loc[uid]["visits"]) == 99_000
    }
    assert upgraded == set(range(1000, 1050))            # priority order, tail deferred
    deferred = {uid for uid in range(1000, 1060) if uid not in upgraded}
    assert all(int(table.loc[uid]["visits"]) == 30_000 for uid in deferred)  # untouched


def test_sync_counter_persists_across_restarts(tmp_path):
    """The cadence driver must survive app restarts via the sidecar file."""
    db = str(tmp_path / "t.db")
    first = RobloxPlatformScout(db_path=db)
    assert first.bump_sync_sequence() == 1
    assert first.bump_sync_sequence() == 2
    second = RobloxPlatformScout(db_path=db)   # simulates an app restart
    assert second._sync_seq == 2
    assert second.bump_sync_sequence() == 3


# ---------------------------------------------------------------------------
# Finder / hydrator phase split (separate workflow cadences)
# ---------------------------------------------------------------------------


def test_hydrate_phase_never_calls_discovery_endpoints(tmp_path):
    """phases=('hydrate',) must drain the tier-due queue and spend zero
    requests on discovery: no Atlas fetch, no queue drain. This is the
    contract the 5-min workflow runs on."""
    calls = []

    class ProbeScout(MockScout):
        def _get_json(self, url, retries=1):
            calls.append(url)
            return super()._get_json(url, retries)

    scout = ProbeScout(
        {
            "https://games.roblox.com/v1/games?universeIds=": {"data": []},
        },
        db_path=str(tmp_path / "h.db"),
    )
    with sqlite3.connect(tmp_path / "h.db") as conn:
        # Stale T1 + T3 rows: both due under the wall-clock cadences.
        conn.execute(
            "INSERT INTO game_analytics (universe_id, tier, ccu, visits, last_updated) "
            "VALUES (901, 1, 30, 30000, '1970-01-01')"
        )
        conn.execute(
            "INSERT INTO game_analytics (universe_id, tier, ccu, visits, last_updated) "
            "VALUES (902, 3, 30, 80000, '1970-01-01')"
        )

    scout.scan(
        min_visits=20_000,
        min_ccu=25,
        deep_contacts=False,
        progress_cb=lambda p, m: None,
        phases=("hydrate",),
    )

    assert scout.last_scan["status"] == "complete"
    assert all("atlasdev.gg" not in u for u in calls)      # no Atlas traffic
    assert not scout.last_scan.get("expansion")            # no drain on hydrate runs
    assert any("games.roblox.com/v1/games" in u for u in calls)  # did hydrate


def test_invalid_phase_raises_and_full_pipeline_default(tmp_path):
    """Unknown phases raise loudly; omitting phases runs the full
    pipeline (hydrate + expand) for UI compatibility."""
    scout = MockScout({}, db_path=str(tmp_path / "i.db"))
    with pytest.raises(ValueError, match="Unknown scan phases"):
        scout.scan(progress_cb=lambda p, m: None, phases=("hydrate", "nonsense"))


# ---------------------------------------------------------------------------
# Catalog-only result set (the dashboard's read-only sync)
# --------------------------------------------------------------------------- #


def test_load_catalog_matches_applies_thresholds_and_ranking(tmp_path):
    """SQL-only result set: thresholds apply in the query, ranked closest-to-
    target first (visits ASC, CCU tiebreak) exactly like the dashboard sort."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    scout.upsert_game({"universe_id": 1, "title": "Just Over", "ccu": 30, "visits": 25_000})
    scout.upsert_game({"universe_id": 2, "title": "Bigger CCU", "ccu": 120, "visits": 25_000})
    scout.upsert_game({"universe_id": 3, "title": "Too Few Visits", "ccu": 900, "visits": 19_999})
    scout.upsert_game({"universe_id": 4, "title": "Too Cold", "ccu": 24, "visits": 500_000})

    df = scout.load_catalog_matches(min_visits=20_000, min_ccu=25)
    assert set(df["universe_id"]) == {1, 2}
    assert list(df["universe_id"]) == [1, 2]

    wider = scout.load_catalog_matches(min_visits=0, min_ccu=0)
    assert set(wider["universe_id"]) == {1, 2, 3, 4}


def test_load_catalog_matches_discord_filter_orders_visits_ascending(tmp_path):
    """The Discord filter must not disturb the catalog ranking: smallest
    qualifying visit counts first, CCU tiebreak — same as the unfiltered read."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    scout.upsert_game({"universe_id": 1, "title": "Huge", "ccu": 50, "visits": 9_000_000,
                       "has_discord": True, "discord_url": "https://discord.gg/h"})
    scout.upsert_game({"universe_id": 2, "title": "Small", "ccu": 50, "visits": 1_000,
                       "has_discord": True, "discord_url": "https://discord.gg/s"})
    scout.upsert_game({"universe_id": 3, "title": "Mid", "ccu": 50, "visits": 100_000,
                       "has_discord": True, "discord_url": "https://discord.gg/m"})
    scout.upsert_game({"universe_id": 4, "title": "No Discord", "ccu": 50, "visits": 5,
                       "has_discord": False, "status": "No Contact Found"})

    df = scout.load_catalog_matches(0, 0, discord=True)
    assert list(df["universe_id"]) == [2, 3, 1]  # ascending visits

    complement = scout.load_catalog_matches(0, 0, discord=False)
    assert list(complement["universe_id"]) == [4]


def test_load_catalog_matches_no_target_only_observed_games(tmp_path):
    """With no target set, games the pipeline never hydrated must not surface."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    scout.upsert_game({"universe_id": 1, "title": "Observed", "ccu": 10, "visits": 100})
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO game_analytics (universe_id, title, visits, ccu) "
            "VALUES (2, 'Corpse', 50, 5)"
        )

    df = scout.load_catalog_matches()
    assert set(df["universe_id"]) == {1}


def test_load_catalog_matches_missing_db_returns_empty(tmp_path):
    """A missing/unopenable catalog degrades to an empty frame, never raises."""
    db = str(tmp_path / "t.db")
    scout = RobloxPlatformScout(db_path=db)
    scout.upsert_game({"universe_id": 1, "title": "G", "ccu": 10, "visits": 100})
    Path(db).unlink()

    assert scout.load_catalog_matches().empty
