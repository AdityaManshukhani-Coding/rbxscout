"""Tests for the 📡 Live catalog section and the compact counter band."""

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
    return calls


def _app_with_view(view: str) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = "live-ref-1"
    at.session_state["onboarding_complete"] = True
    at.session_state["welcome_scan_started"] = True
    at.session_state["workspace_view"] = view
    at.run()
    return at


def test_live_view_is_a_dedicated_workspace(counts):
    at = _app_with_view("📡 Live catalog")
    assert not at.exception
    titles = [t.value for t in at.title]
    assert any("Live catalog" in t for t in titles)
    # The counter is the page: main-table headings stay away.
    assert not any("Games matching" in t for t in titles)


_FULL_MARKUP = 'class="ss-tracker ss-tracker-full"'  # the wrapper div, not the shared CSS rule


def test_live_counter_renders_big_number(counts):
    at = _app_with_view("📡 Live catalog")
    html = " ".join(m.value for m in at.markdown)
    assert "21,303" in _visible_text(at), "the catalog total is displayed"
    assert _FULL_MARKUP in html, "the big centered variant is used"
    assert "games in the catalog" in _visible_text(at)
    assert "discovered today" in _visible_text(at)


def test_live_counter_band_removed_from_main_table(counts):
    """The main view keeps a compact band; the huge full variant must not."""
    at = _app_with_view("🎮 Main scout")
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert _FULL_MARKUP not in html, "full-size counter belongs to the live section"
    assert 'class="ss-tracker"' in html, "compact band is present"
    assert "Games matching your target" in " ".join(t.value for t in at.title)
    assert "21,303" in _visible_text(at), "compact band still shows the total"


def test_upcoming_view_has_no_counter(counts):
    at = _app_with_view("🚀 New and Upcoming")
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert _FULL_MARKUP not in html
    assert 'class="ss-tracker"' not in html, "watch view stays counter-free"
    assert "21,303" not in _visible_text(at), "watch view stays counter-free"


def test_live_counter_counts_calls_stay_local(counts):
    """The counter reads the cached catalog copy — cheap enough to re-render."""
    import scout_core

    def _boom(self, *args, **kwargs):
        raise AssertionError("live view must never trigger a network scan")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scout_core.RobloxPlatformScout, "scan", _boom)
        mp.setattr(scout_core.RobloxPlatformScout, "scan_contacts", _boom)
        at = _app_with_view("📡 Live catalog")
    assert not at.exception
    assert counts, "counter read the catalog copy at least once"
    assert all(str(c).endswith(".db") for c in counts), "reads only the local catalog file"
