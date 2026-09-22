"""Tests for the compact live-counter band in the main-view sidebar.

The dedicated 📡 Live catalog workspace section was removed (the band in the
sidebar is the only remaining surface), so these tests now pin that band's
presence on the main view and its absence on the watch view.
"""

import re

import pytest
from streamlit.testing.v1 import AppTest

import catalog_fetch

from test_app_flow import APP_PATH


def _visible_text(at) -> str:
    """All markdown-rendered text, tags stripped (the counter splits digits
    into per-digit spans, so the raw HTML never contains '21,303')."""
    html = " ".join(m.value for m in at.markdown)
    return re.sub(r"<[^>]+>", "", html)


@pytest.fixture()
def counts(monkeypatch):
    """Deterministic catalog counters, recorded on every call."""
    calls = []

    def fake_counts(db_path):
        calls.append(db_path)
        return {
            "games": 21303,
            "target": 4321,
            "found_today": 57,
            "last_sync": "2026-09-13 12:00:00",
        }

    monkeypatch.setattr(catalog_fetch, "catalog_counts", fake_counts)
    # Bypass the new process-wide 60 s counter cache (an earlier test in the
    # run may have warmed it with real DB values).
    monkeypatch.setattr(catalog_fetch, "catalog_counts_cached", catalog_fetch.catalog_counts)
    return calls


def _app_with_view(view: str) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = "live-ref-1"
    at.session_state["onboarding_complete"] = True
    at.session_state["welcome_scan_started"] = True
    at.session_state["workspace_view"] = view
    at.run()
    return at


def test_workspace_radio_has_no_live_catalog_entry(counts):
    """The 📡 Live catalog workspace is removed; the radio offers exactly the
    two remaining views."""
    at = _app_with_view("🎮 Main scout")
    assert not at.exception
    radio = at.sidebar.radio(key="workspace_view")
    assert "📡 Live catalog" not in radio.options
    assert set(radio.options) == {"🎮 Main scout", "🚀 New and Upcoming"}


def test_counter_band_on_main_view(counts):
    """The compact band lives in the sidebar of the main view only."""
    at = _app_with_view("🎮 Main scout")
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert "21,303" in _visible_text(at), "compact band still shows the total"
    assert "games in the catalog" in _visible_text(at)
    assert 'class="ss-tracker"' in html, "compact band is present"
    # The full-size centered variant was part of the removed Live section.
    assert "ss-tracker-full" not in html
    assert "Games matching your target" in " ".join(t.value for t in at.title)


def test_upcoming_view_has_no_counter(counts):
    at = _app_with_view("🚀 New and Upcoming")
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert 'class="ss-tracker"' not in html, "watch view stays counter-free"
    assert "21,303" not in _visible_text(at), "watch view stays counter-free"


def test_counter_counts_calls_stay_local(counts):
    """The counter reads the cached catalog copy — cheap enough to re-render."""
    import scout_core

    def _boom(self, *args, **kwargs):
        raise AssertionError("live view must never trigger a network scan")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scout_core.RobloxPlatformScout, "scan", _boom)
        mp.setattr(scout_core.RobloxPlatformScout, "scan_contacts", _boom)
        at = _app_with_view("🎮 Main scout")
    assert not at.exception
    assert counts, "counter read the catalog copy at least once"
    assert all(str(c).endswith(".db") for c in counts), "reads only the local catalog file"
