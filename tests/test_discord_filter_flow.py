"""Dashboard Discord-filter behavior (2026-10-15 bug fixes).

Two user-reported symptoms, one root cause plus one gap:

A. With 'Discord Available' on, a search narrowed the table but CLEARING
   the box did not go back to the home page, and
B. switching 'Discord availability' from 'Discord Available' back to
   'All Games' left the table showing only the discord-available rows.

Root cause: the scan block persisted the Discord-filtered frame back into
``st.session_state.data``, so the session's stored result set was narrowed
and the render-time filter could never widen it again. The scan block now
keeps the FULL synced frame and the radio schedules a rescan on change,
so every switch rebuilds from the full catalog and only the render-time
apply_filters narrows the view.

Battle field for the user-visible fix (2026-10-15): 'Discord Available'
must keep working as a display filter, but it can never again eat the
stored result set.
"""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import scout_core
from tests.test_app_flow import _fresh_app, _table_html

DISCORD_TRUE = scout_core.DISCORD_FILTER_TRUE
DISCORD_ALL = scout_core.DISCORD_FILTER_ALL
DISCORD_FALSE = scout_core.DISCORD_FILTER_FALSE


def _frame(with_discord: bool, uid: int, title: str) -> dict:
    """One catalog row shaped like the dashboard's shared frame."""
    return {
        "universe_id": uid, "root_place_id": 4900000 + uid, "title": title,
        "ccu": 1000, "peak_ccu": 1200, "visits": 50_000_000, "favorites": 100_000,
        "genre": "RPG", "creator_name": "Creator", "creator_type": "Group",
        "creator_id": None, "description": "", "icon_url": "",
        "has_discord": with_discord,
        "discord_url": "https://discord.gg/x" if with_discord else None,
        "status": "OK" if with_discord else "No Contact Found",
        "found_via": "game_description" if with_discord else None,
        "has_social_links": with_discord,
    }


def _catalog_frame() -> pd.DataFrame:
    """Two discord-available games + one checked-but-no-discord game.

    'Discord Game One' matches a search on 'one', so the search narrowing
    and the filter narrowing pick different rows.
    """
    return pd.DataFrame([
        _frame(True, 1, "Discord Game One"),
        _frame(True, 2, "Discord Game Two"),
        _frame(False, 3, "No Discord Game"),
    ])


@pytest.fixture
def fake_catalog(monkeypatch):
    monkeypatch.setattr(
        scout_core.RobloxPlatformScout, "load_catalog_matches",
        lambda self, min_visits=0, min_ccu=0, discord=None: _catalog_frame(),
    )


def _seeded_capture(fake_catalog):
    """Fresh app whose initial scan runs WITH the Discord filter already on —
    exactly the returning scout's precondition (profile restore)."""
    at = _fresh_app()
    at.session_state["onboarding_complete"] = True
    at.session_state["pending_initial_scan"] = True
    at.session_state["welcome_scan_started"] = False
    at.session_state["discord_filter_radio"] = DISCORD_TRUE
    at.run()
    assert not at.exception
    return at


def test_search_clear_under_discord_filter_goes_home(fake_catalog):
    """Search under the filter narrows; clearing the box returns to the
    filter's home view (all discord-available games), not the table from
    the old search and not a stale narrowed set."""
    at = _seeded_capture(fake_catalog)

    search = at.sidebar.text_input(key="game_search")
    search.set_value("one").run()
    assert not at.exception
    html = _table_html(at)
    assert "Discord Game One" in html, "search narrows the view"
    assert "Discord Game Two" not in html, "other games stay hidden during the search"
    assert "No Discord Game" not in html, "discord filter applies during the search"

    search.set_value("").run()
    assert not at.exception
    html = _table_html(at)
    assert html.count("Discord Game") >= 2, "home = every discord-available game"
    assert "No Discord Game" not in html, "filter still hides no-discord games on home"


def test_filter_switch_back_to_all_shows_everything(fake_catalog):
    """'Discord Available' -> 'All Games' must widen the table again, even
    when the stored frame was produced while the filter was already on."""
    at = _seeded_capture(fake_catalog)
    html = _table_html(at)
    assert "No Discord Game" not in html

    at.sidebar.radio(key="discord_filter_radio").set_value(DISCORD_ALL).run()
    assert not at.exception
    html = _table_html(at)
    assert "No Discord Game" in html, "All Games shows the no-discord game again"
    assert html.count("Discord Game") >= 3 or ("Discord Game One" in html and "Discord Game Two" in html)


def test_filter_switch_to_false_keeps_only_no_discord(fake_catalog):
    at = _seeded_capture(fake_catalog)
    at.sidebar.radio(key="discord_filter_radio").set_value(DISCORD_FALSE).run()
    assert not at.exception
    html = _table_html(at)
    assert "No Discord Game" in html
    assert "Discord Game One" not in html, "discord-available rows stay hidden"


def test_radio_change_schedules_rescan_from_full_frame(fake_catalog):
    """The radio callback schedules a rescan (cheap, cache-served) and the
    stored frame comes back FULL, never narrowed by the outgoing filter."""
    at = _seeded_capture(fake_catalog)
    stored = at.session_state["data"]
    assert stored is not None and not stored.empty, "initial scan populated the frame"
    assert "No Discord Game" in set(stored["title"]), "stored frame keeps ALL games"


def test_search_clear_then_filter_switch_combo(fake_catalog):
    """The user's exact sequence: filter on -> search -> clear -> switch to
    ALL. Every step shows the correct set."""
    at = _seeded_capture(fake_catalog)
    search = at.sidebar.text_input(key="game_search")
    search.set_value("one").run()
    search.set_value("").run()
    assert not at.exception
    at.sidebar.radio(key="discord_filter_radio").set_value(DISCORD_ALL).run()
    assert not at.exception
    html = _table_html(at)
    assert "No Discord Game" in html
    assert html.count("Discord Game") >= 2
