"""Tests for the removed live-counter band.

The sidebar catalog counter (and the older 📡 Live catalog workspace before
it) was removed; these tests pin its absence on every workspace view.
"""

import re

import pytest
from streamlit.testing.v1 import AppTest

import catalog_fetch

from test_app_flow import APP_PATH


def _visible_text(at) -> str:
    """All markdown-rendered text, tags stripped (a leftover counter would
    split digits into per-digit spans, so the raw HTML never contains
    '21,303')."""
    html = " ".join(m.value for m in at.markdown)
    return re.sub(r"<[^>]+>", "", html)


@pytest.fixture()
def counts(monkeypatch):
    """Deterministic catalog counters — the app must not need the DB either
    way now that the counter is gone."""
    def fake_counts(db_path):
        return {
            "games": 21303,
            "target": 4321,
            "found_today": 57,
            "last_sync": "2026-09-13 12:00:00",
        }

    monkeypatch.setattr(catalog_fetch, "catalog_counts", fake_counts)
    # Bypass the process-wide 60 s counter cache (an earlier test in the
    # run may have warmed it with real DB values).
    monkeypatch.setattr(catalog_fetch, "catalog_counts_cached", catalog_fetch.catalog_counts)


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


def test_no_counter_on_main_view(counts):
    """The sidebar catalog counter was removed; the main view renders no
    tracker band even though the catalog copy is available."""
    at = _app_with_view("🎮 Main scout")
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert "21,303" not in _visible_text(at), "counter must stay removed"
    assert 'class="ss-tracker"' not in html, "counter must stay removed"
    assert "Games matching your target" in " ".join(t.value for t in at.title)


def test_no_counter_on_watch_view(counts):
    at = _app_with_view("🚀 New and Upcoming")
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert 'class="ss-tracker"' not in html, "watch view stays counter-free"
    assert "21,303" not in _visible_text(at), "watch view stays counter-free"
