"""Welcome-flow and outreach-message tests.

``AppTest`` runs the whole Streamlit script headlessly. Live Roblox scans are
forced to fail offline by breaking ``requests``, so the dashboard falls back
to demo/DB data and still renders the results table with its copy buttons.
"""

import html as html_module
import json
import re
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import catalog_fetch
import profile_store
import scout_core
from scout_core import (
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_MESSAGE_TEMPLATE,
    normalize_discord_user_id,
    render_outreach_message,
)

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Fail every scan instantly so the dashboard falls back to demo/DB data.

    Patching the scan methods (instead of the HTTP layer) avoids the real
    network *and* scout_core's retry backoff, keeping runs fast and offline.
    """

    def _boom(self, *args, **kwargs):
        raise RuntimeError("offline test scan")

    monkeypatch.setattr(scout_core.RobloxPlatformScout, "scan", _boom)
    monkeypatch.setattr(scout_core.RobloxPlatformScout, "scan_contacts", _boom)


def _fresh_app() -> AppTest:
    return AppTest.from_file(APP_PATH, default_timeout=45)


def _demo_frame() -> pd.DataFrame:
    """One game row shaped like the dashboard's demo/DB frame.

    Seeding ``data`` directly keeps table tests off ``load_table``, which
    takes ~25s against the full 27MB repository database.
    """
    return pd.DataFrame([
        {
            "universe_id": 1, "root_place_id": 4924922222, "title": "Blox Fruits",
            "ccu": 1000, "peak_ccu": 1200, "visits": 50_000_000, "favorites": 100_000,
            "genre": "RPG", "creator_name": "Creator", "creator_type": "Group",
            "creator_id": None, "description": "", "icon_url": "",
            "has_discord": True, "discord_url": "https://discord.gg/test",
            "status": "OK", "found_via": "test", "has_social_links": True,
            "avg_ccu_1d": 900.0, "avg_ccu_3d": 880.0, "momentum_1d": 100,
            "upvotes": 90_000, "downvotes": 10_000, "first_seen": "2026-09-05 12:00:00",
            "contacts_checked_at": None,
        }
    ])


def _render_dashboard(name: str = "dev_razor10") -> AppTest:
    """Skip onboarding by seeding its outcome, then render the dashboard."""
    at = _fresh_app()
    at.run()
    at.session_state["onboarding_complete"] = True
    at.session_state["pending_initial_scan"] = False
    at.session_state["welcome_scan_started"] = True
    at.session_state["discord_name"] = name
    at.session_state["data"] = _demo_frame()
    at.session_state["source"] = "demo"
    at.run()
    return at


# --------------------------------------------------------------------------- #
# Message rendering (pure function)
# --------------------------------------------------------------------------- #


def test_render_fills_name_and_game_tags():
    message = render_outreach_message(DEFAULT_MESSAGE_TEMPLATE, "dev_razor10", "Blox Fruits")
    assert "dev_razor10" in message
    assert "Blox Fruits" in message
    assert "[Your Name]" not in message
    assert "[Game Name]" not in message
    assert "I'm [Your Name] from UpScale" not in message
    assert "caught our eye" in message and "Blox Fruits" in message
    assert "temporary percentage of revenue" in message


def test_render_keeps_tag_when_name_missing():
    message = render_outreach_message(DEFAULT_MESSAGE_TEMPLATE, "", "Blox Fruits")
    assert "[Your Name]" in message
    assert "Blox Fruits" in message


def test_render_case_insensitive_tags():
    assert render_outreach_message("Hi [name], I like [Game].", "ace", "Speed Run") == (
        "Hi ace, I like Speed Run."
    )


def test_render_empty_template_falls_back_to_default():
    message = render_outreach_message("   ", "ace", "Speed Run")
    assert "I'm ace from UpScale" in message


def test_default_templates_open_with_plain_greeting():
    """(2026-09-28) The [User] placeholder is gone: every starter opens with
    a plain greeting, so nothing is ever copied with an unfilled tag."""
    for template in DEFAULT_MESSAGE_TEMPLATES:
        assert "[User]" not in template
        assert template.splitlines()[0] in ("Hey,", "Hi,"), template.splitlines()[0]


# --------------------------------------------------------------------------- #
# Optional Discord User ID -> real <@ID> mention in the copied message
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("53908099506183680", "53908099506183680"),
        (" 53908099506183680 ", "53908099506183680"),
        ("<@53908099506183680>", "53908099506183680"),  # full token pasted
        ("5390-8099-5061-83680", "53908099506183680"),  # dashed grouping
        ("dev_razor10", ""),  # username, not an ID
        ("12345", ""),  # too short
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_discord_user_id(raw, expected):
    assert normalize_discord_user_id(raw) == expected


def test_render_with_user_id_makes_real_mention_token():
    message = render_outreach_message(
        DEFAULT_MESSAGE_TEMPLATE, "dev_razor10", "Blox Fruits",
        discord_user_id="53908099506183680",
    )
    assert "<@53908099506183680>" in message
    assert "dev_razor10" not in message
    assert "[Your Name]" not in message


def test_render_without_user_id_keeps_plain_name():
    message = render_outreach_message(
        DEFAULT_MESSAGE_TEMPLATE, "dev_razor10", "Blox Fruits", discord_user_id=""
    )
    assert "dev_razor10" in message
    assert "<@" not in message


def test_render_invalid_user_id_falls_back_to_plain_name():
    message = render_outreach_message(
        DEFAULT_MESSAGE_TEMPLATE, "dev_razor10", "Blox Fruits",
        discord_user_id="not-an-id",
    )
    assert "dev_razor10" in message
    assert "<@" not in message


def test_render_missing_name_leaves_tag_even_with_id():
    """No username + an ID: the token still fills — the mention IS the name."""
    message = render_outreach_message(
        DEFAULT_MESSAGE_TEMPLATE, "", "Blox Fruits",
        discord_user_id="53908099506183680",
    )
    assert "<@53908099506183680>" in message
    assert "[Your Name]" not in message


# --------------------------------------------------------------------------- #
# Welcome flow (Streamlit AppTest)
# --------------------------------------------------------------------------- #


def test_onboarding_asks_discord_name_then_template():
    at = _fresh_app()
    at.run()
    assert not at.exception

    at.button(key="onb0_next").click().run()  # welcome -> targets
    at.button(key="onb1_next").click().run()  # targets -> Discord name

    # Discord username step comes right after the targets step.
    titles = [element.value for element in at.title]
    assert any("Discord username" in value for value in titles)

    at.text_input(key="discord_name").set_value("dev_razor10").run()
    at.button(key="onb3_next").click().run()  # Discord -> message template

    titles = [element.value for element in at.title]
    assert any("outreach message" in value for value in titles)

    # The template starts as the default and the guidance keeps the tags.
    template_area = at.text_area(key="message_template")
    assert template_area.value == DEFAULT_MESSAGE_TEMPLATE
    captions = [element.value for element in at.caption]
    assert any("[Your Name]" in value and "[Game Name]" in value for value in captions)

    # Five starter templates are offered; each loads into the editable area.
    radio = at.radio(key="message_variant")
    assert len(radio.options) == 5
    assert radio.value == radio.options[0]
    radio.set_value(radio.options[2]).run()
    assert not at.exception
    assert at.text_area(key="message_template").value == DEFAULT_MESSAGE_TEMPLATES[2]
    # Switching back restores Starter 1 (the original default).
    radio.set_value(radio.options[0]).run()
    assert at.text_area(key="message_template").value == DEFAULT_MESSAGE_TEMPLATE


def test_sidebar_variant_switcher_swaps_template(monkeypatch, tmp_path):
    """The sidebar expander offers the same 5 starters after onboarding."""
    monkeypatch.setenv("SS_PROFILE_DIR", str(tmp_path / "profiles"))
    profile_store.save_profile(
        "variant-ref-1",
        {
            "discord_name": "Var Scout",
            "discord_user_id": "",
            "message_template": DEFAULT_MESSAGE_TEMPLATE,
            "target_min_visits": 20_000,
            "target_min_ccu": 25,
            "onboarding_cookie": "",
            "onboarding_step": 4,
            "guide_step": 4,
            "onboarding_complete": True,
        },
    )
    _patch_catalog_reader(monkeypatch, _demo_frame())
    at = _fresh_app()
    at.session_state["_device_ref"] = "variant-ref-1"
    at.run()
    assert not at.exception

    radio = at.sidebar.radio(key="message_variant")
    assert len(radio.options) == 5
    radio.set_value(radio.options[4]).run()
    assert not at.exception
    assert at.sidebar.text_area(key="message_template").value == DEFAULT_MESSAGE_TEMPLATES[4]


def test_user_id_optional_field_flows_into_copied_message(monkeypatch):
    """Step 3 shows the optional User ID field with helper captions; a saved
    ID makes the copied message carry a real <@ID> mention token; leaving it
    empty keeps the plain username."""
    _patch_catalog_reader(monkeypatch, _demo_frame())

    at = _fresh_app()
    at.run()
    at.button(key="onb0_next").click().run()
    at.button(key="onb1_next").click().run()

    # Step 3: the optional field + the 3-step "how to find your ID" helper.
    captions = [element.value for element in at.caption]
    assert any("Developer Mode" in value and "Copy User ID" in value for value in captions)
    assert any("optional" in value.lower() for value in captions)

    at.text_input(key="discord_name").set_value("dev_razor10").run()
    at.text_input(key="discord_user_id").set_value("53908099506183680").run()
    at.button(key="onb3_next").click().run()  # -> message template step
    at.button(key="onb4_start").click().run()  # first scan from the catalog
    at.run()

    assert not at.exception
    match = re.search(r'data-msg="([^"]+)"', _table_html(at))
    message = json.loads(html_module.unescape(match.group(1)))
    assert "<@53908099506183680>" in message
    assert "dev_razor10" not in message
    assert "[Your Name]" not in message


def _table_html(at: AppTest) -> str:
    """Return the results-table HTML from the app's st.html element."""
    html_blocks = at.main.get("html")
    joined = "".join(el.proto.body for el in html_blocks)
    assert "ss-table" in joined, "results table should render after onboarding"
    return joined


# --------------------------------------------------------------------------- #
# Brand identity (BRAND.md): the lockup, the favicon, the palette
# --------------------------------------------------------------------------- #


def test_sidebar_renders_brand_lockup():
    """The sidebar shows the radar badge + name treatment (no emoji title):
    the brand HTML carries the lockup classes and the UpScale Orange accent."""
    at = _render_dashboard()
    assert not at.exception
    sidebar_html = " ".join(el.proto.body for el in at.sidebar.markdown)
    assert "ss-brand" in sidebar_html
    assert "Up<span class=\"ss-accent\">Scale</span> Scouting Tool" in sidebar_html
    assert "FF6E01" in sidebar_html


def test_gate_screen_uses_brand_mark(monkeypatch):
    """The lock screen is the first thing a visitor sees; it must render the
    radar badge (or the emoji fallback when the asset is missing) and the
    accent treatment — never the old plain-text title."""
    monkeypatch.delenv("SS_TEST_BYPASS_GATE")  # render the real gate
    at = _fresh_app()
    at.run()
    assert not at.exception
    markdown_html = " ".join(el.proto.body for el in at.markdown)
    assert "gate-brand" in markdown_html
    assert "Up<span class=\"ss-accent\">Scale</span> Scouting Tool" in markdown_html


def test_browser_favicon_injected_as_data_uri():
    """The radar favicon ships to the browser via the favicon script, so the
    tab shows the brand mark instead of Streamlit's default emoji icon."""
    at = _render_dashboard()
    html_blocks = list(at.main.get("html")) + list(at.sidebar.markdown)
    joined = "".join(el.proto.body for el in html_blocks)
    assert "link[rel*='icon']" in joined, "favicon injection script must ship"
    assert "data:image/png;base64," in joined, "favicon must be embedded as a data URI"


# --------------------------------------------------------------------------- #
# Catalog-store outage (the 2026-09-09 red-traceback crash)
# --------------------------------------------------------------------------- #


def test_store_outage_shows_banner_instead_of_crashing(monkeypatch):
    """When the catalog store is unreachable (e.g. the rbx_scout.db asset is
    missing during a pipeline outage), the hosted dashboard must render with
    a friendly banner + demo data — never the red CatalogFetchError page."""
    import catalog_fetch
    import streamlit as st

    def _unreachable(*args, **kwargs):
        raise catalog_fetch.CatalogFetchError(
            "release catalog-latest currently has no rbx_scout.db asset"
        )

    monkeypatch.setattr(catalog_fetch, "is_hosted", lambda: True)
    monkeypatch.setattr(catalog_fetch, "ensure_catalog", _unreachable)
    monkeypatch.setattr(catalog_fetch, "stale_cache_fallback", lambda exc: None)

    # st.cache_resource is process-global: earlier tests in this file already
    # resolved the catalog in local mode. Clear before and after so this test
    # exercises the outage path and later tests recompute normally.
    st.cache_resource.clear()
    try:
        at = AppTest.from_file(APP_PATH, default_timeout=30)
        at.run()
    finally:
        st.cache_resource.clear()

    assert not at.exception, "an outage must not crash the dashboard"
    warnings = [w.value for w in at.warning]
    assert any("temporarily unavailable" in w for w in warnings)
    # The welcome flow still renders on top of the banner.
    assert any("UpScale" in el.value for el in at.header) or at.title


def test_table_column_order_discord_before_averages():
    """Column order (user request 2026-09-23): Game, Genre, Total visits,
    CCU, Discord, Message, Avg CCU (1d), Avg CCU (3d), Momentum, Rating."""
    at = _render_dashboard()
    assert not at.exception

    table_html = _table_html(at)
    header_match = re.search(r"<thead><tr>(.*?)</tr></thead>", table_html, re.S)
    assert header_match, "results table header should render"
    headers = re.findall(r"<th[^>]*>(.*?)</th>", header_match.group(1))
    assert headers == [
        "Game", "Genre", "Total visits", "CCU", "Discord", "Message",
        "Avg CCU (1d)", "Avg CCU (3d)", "Momentum (1d)", "Rating",
    ], headers


def test_missing_discord_says_no_discord_server():
    """Games without a resolved invite must read 'No Discord Server' —
    a bare dash looked like a cell still loading."""
    frame = _demo_frame()
    frame.loc[0, "discord_url"] = None
    frame.loc[0, "has_discord"] = False
    at = _fresh_app()
    at.run()
    at.session_state["onboarding_complete"] = True
    at.session_state["pending_initial_scan"] = False
    at.session_state["welcome_scan_started"] = True
    at.session_state["data"] = frame
    at.session_state["source"] = "demo"
    at.run()

    assert not at.exception
    table_html = _table_html(at)
    assert "No Discord Server" in table_html


def test_game_url_fallback_uses_discover_not_dead_search_route():
    """roblox.com/search?keyword= 404s for EVERY keyword (verified live
    2026-09-23) — emoji titles were never the problem. The keyword fallback
    must use /discover, which serves search results. Rows with a place id
    keep deep-linking to the game page."""
    frame = _demo_frame()
    no_place = frame.copy()
    no_place["root_place_id"] = None

    at = _fresh_app()
    at.run()
    at.session_state["onboarding_complete"] = True
    at.session_state["pending_initial_scan"] = False
    at.session_state["welcome_scan_started"] = True
    at.session_state["data"] = no_place
    at.session_state["source"] = "demo"
    at.run()

    assert not at.exception
    table_html = _table_html(at)
    assert "https://www.roblox.com/discover/?Keyword=Blox%20Fruits" in table_html
    assert "roblox.com/search?keyword=" not in table_html

    # A row with a place id still deep-links to the game page.
    at.session_state["data"] = frame
    at.run()
    assert not at.exception
    assert "https://www.roblox.com/games/4924922222/" in _table_html(at)


def test_genre_x_click_degrades_gracefully_not_grey_screen(monkeypatch):
    """Removing a picked genre whose options vanished with a data reload
    (demo↔live swap, new sync) must NOT grey out the whole app: Streamlit
    raises StreamlitAPIException for the stale label, which used to abort
    the script run. The stale genre is dropped before the widget renders
    and the dashboard comes back with the visits/CCU targets intact."""
    frame = _demo_frame()
    at = _fresh_app()
    at.run()
    at.session_state["onboarding_complete"] = True
    at.session_state["pending_initial_scan"] = False
    at.session_state["welcome_scan_started"] = True
    at.session_state["data"] = frame
    at.session_state["source"] = "demo"
    at.session_state["genre_multiselect"] = ["Party"]  # not in demo genres
    at.run()

    assert not at.exception, "a stale genre must never crash the dashboard"
    assert "Blox Fruits" in _table_html(at)

    # The same protection covers a genre the frame genuinely had before a
    # reload replaced it: the stale label is discarded, targets stay applied.
    reloaded = frame.copy()
    reloaded["genre"] = "Shooter"
    at.session_state["genre_multiselect"] = ["RPG"]  # vanished in the reload
    at.session_state["data"] = reloaded
    at.run()
    assert not at.exception, "a vanished genre must never crash the dashboard"
    assert "Blox Fruits" in _table_html(at)


def test_results_table_has_copy_message_column():
    at = _render_dashboard()
    assert not at.exception

    table_html = _table_html(at)

    assert "Copy message" in table_html
    match = re.search(r'data-msg="([^"]+)"', table_html)
    assert match, "copy button should embed the rendered message"
    message = json.loads(html_module.unescape(match.group(1)))

    # The Discord name from the welcome flow and the row's game title are filled in.
    assert "dev_razor10" in message
    assert "Blox Fruits" in message
    assert "[Your Name]" not in message
    assert "[Game Name]" not in message


def test_copy_button_rotates_all_variants_never_repeating():
    """Every copy button carries all 5 starters (plus a customized template)
    in data-alts, and the click script picks one at random, excluding the
    previously copied variant — so two consecutive copies can never be the
    same starter, regardless of which game each copy was for."""
    at = _render_dashboard()
    assert not at.exception

    table_html = _table_html(at)
    match = re.search(r'data-alts="([^"]+)"', table_html)
    assert match, "copy button should embed the rotation pool"
    pool = json.loads(html_module.unescape(match.group(1)))

    # 5 starters are in the pool, each keyed so repeats can be excluded.
    keys = [entry["v"] for entry in pool]
    assert keys == [f"s{i}" for i in range(5)], "one pool entry per starter"
    texts = [entry["t"] for entry in pool]
    assert len(set(texts)) == 5, "all starters differ"
    # Every pool entry is rendered for this row's game but keeps [Your Name]
    # for the click-time identity fill.
    for text in texts:
        assert "Blox Fruits" in text
        assert "[Game Name]" not in text
        assert "[Your Name]" in text

    # The client script does the no-repeat pick in localStorage.
    scripts = re.findall(r"<script>(.*?)</script>", table_html, re.S)
    js = next((s for s in scripts if "ss-copy" in s and "ss_last_variant" in s), "")
    assert js, "rotation logic ships with the table script"
    assert "dataset.alts" in js, "the pool is read from the button"
    assert "filter" in js, "previous variant is excluded from the pick"


def test_standard_session_rotation_pool_has_no_increates_messages():
    """A regular (friend-key or gate-bypassed) session never rotates the
    master-only Increates set: the pool stays exactly the 5 UpScale starters."""
    at = _render_dashboard()
    assert not at.exception

    table_html = _table_html(at)
    match = re.search(r'data-alts="([^"]+)"', table_html)
    assert match, "copy button should embed the rotation pool"
    pool = json.loads(html_module.unescape(match.group(1)))
    texts = [entry["t"] for entry in pool]
    for template in scout_core.INCREASES_MESSAGE_TEMPLATES:
        rendered = render_outreach_message(template, "", "Blox Fruits")
        assert rendered not in texts, "Increates copy leaked into a standard session"


def test_copy_script_rewrites_identity_from_live_sidebar_inputs():
    """The freshness override must cover BOTH identity widgets.

    Streamlit text inputs only commit on blur/rerun, so a User ID or name
    typed just before clicking Copy is not yet in data-msg. The client-side
    script re-reads the live sidebar inputs — the User ID must win over
    both unfilled tags and any stale mention token from an older ID.
    """
    import re as _re

    at = _render_dashboard()
    assert not at.exception

    table_html = _table_html(at)
    # Target the copy script specifically (the input-guard script also ships
    # inside an st.html element but never touches .ss-copy buttons).
    scripts = _re.findall(r"<script>(.*?)</script>", table_html, _re.S)
    js = next((s for s in scripts if "aria-label" in s and "ss-copy" in s), "")
    assert js, "copy freshness script should ship with the table"

    # Both identity inputs are re-read at click time.
    assert "Discord username" in js
    assert "Discord User ID" in js

    # The stored message was rendered server-side (name filled); the JS
    # must carry the ID-validity gate and the live mention rewrite so a
    # just-typed ID still wins at click time.
    match = _re.search(r"data-msg=\"([^\"]+)\"", _table_html(at))
    assert match
    stored = json.loads(html_module.unescape(match.group(1)))
    assert "[Your Name]" not in stored
    assert "digits.length >= 15" in js and "digits.length <= 21" in js
    assert "'<@' + digits + '>'" in js


def test_sidebar_edits_name_and_template():
    at = _render_dashboard(name="dev_razor10")
    assert not at.exception

    at.sidebar.text_area(key="message_template").set_value(
        "Yo [Your Name] here — loving [Game Name]!"
    ).run()
    at.sidebar.text_input(key="discord_name").set_value("rip_indra").run()
    assert not at.exception

    match = re.search(r'data-msg="([^"]+)"', _table_html(at))
    message = json.loads(html_module.unescape(match.group(1)))
    assert message.startswith("Yo rip_indra here")
    assert "Blox Fruits" in message


# --------------------------------------------------------------------------- #
# First scan / Sync live data: read the catalog, never run the finder
# --------------------------------------------------------------------------- #


def _patch_catalog_reader(monkeypatch, frame: pd.DataFrame) -> list[dict]:
    """Replace the catalog reader with a stub; return the calls it saw."""
    calls: list[dict] = []

    def _fake(self, min_visits=0, min_ccu=0):
        calls.append({"min_visits": min_visits, "min_ccu": min_ccu})
        return frame.copy()

    monkeypatch.setattr(scout_core.RobloxPlatformScout, "load_catalog_matches", _fake)
    return calls


def _walk_onboarding_to_template(at: AppTest) -> None:
    at.button(key="onb0_next").click().run()  # welcome -> targets
    at.button(key="onb1_next").click().run()  # targets -> Discord name
    at.button(key="onb3_next").click().run()  # Discord -> message template


def test_first_scan_pulls_from_catalog_without_discovery(monkeypatch):
    """'Start my first scan' must read the DB catalog with the chosen targets
    and must never launch the finder (deep charts / keyword slices)."""
    calls = _patch_catalog_reader(monkeypatch, _demo_frame())

    def _no_finder(self, *args, **kwargs):
        raise AssertionError("finder scan() must not run for the first scan")

    monkeypatch.setattr(scout_core.RobloxPlatformScout, "scan", _no_finder)

    at = _fresh_app()
    at.run()
    _walk_onboarding_to_template(at)
    at.button(key="onb4_start").click().run()
    at.run()

    assert not at.exception
    assert calls == [{"min_visits": 20000, "min_ccu": 25}]
    assert "Blox Fruits" in _table_html(at)
    captions = [element.value for element in at.caption]
    assert any("Page 1 of" in value for value in captions)


def test_sync_button_reads_catalog_instantly(monkeypatch):
    """'🔄 Sync live data' re-reads the catalog with the sidebar targets."""
    at = _render_dashboard()
    calls = _patch_catalog_reader(monkeypatch, _demo_frame())

    at.sidebar.button(key="sync_live_data").click().run()

    assert not at.exception
    assert len(calls) == 1
    assert calls[0]["min_visits"] == 20000 and calls[0]["min_ccu"] == 25
    assert "Blox Fruits" in _table_html(at)


def test_shared_catalog_cache_shares_and_isolates(monkeypatch):
    """The process-wide catalog cache: same targets = one computation shared
    by all sessions (each gets an independent copy); different targets or a
    forced refresh = a real recompute; empty results are never cached."""
    import app as app_module

    calls: list[tuple] = []
    frame = _demo_frame()

    def _fake_read(min_visits, min_ccu, discord=None):
        calls.append((min_visits, min_ccu, discord))
        return frame.copy()

    monkeypatch.setattr(app_module, "_read_catalog", _fake_read)
    monkeypatch.setattr(app_module, "_CATALOG_CACHE", {})

    first = app_module._shared_catalog_frame(20000, 25, None)
    second = app_module._shared_catalog_frame(20000, 25, None)
    assert calls == [(20000, 25, None)], "second same-target call must reuse the cache"
    assert len(first) == len(second) == len(frame)

    # Session isolation: mutating one caller's copy must not leak into the
    # shared frame (or the next caller's copy).
    first.loc[first.index[0], "title"] = "MUTATED"
    third = app_module._shared_catalog_frame(20000, 25, None)
    assert not (third["title"] == "MUTATED").any()

    # Different targets = a different cache entry = a real computation.
    app_module._shared_catalog_frame(50000, 75, None)
    assert calls[-1] == (50000, 75, None)

    # Forced refresh recomputes even on a warm entry.
    app_module._shared_catalog_frame(20000, 25, None, force=True)
    assert calls.count((20000, 25, None)) == 2

    # Empty results are never cached (a mid-push blank must not stick).
    monkeypatch.setattr(app_module, "_read_catalog", lambda *a, **k: frame.iloc[:0].copy())
    assert app_module._shared_catalog_frame(999, 999, None).empty
    assert (999, 999, None) not in {k for k, _ in [(k, v) for k, v in app_module._CATALOG_CACHE.items()]}


def test_downcast_frame_shrinks_numerics_and_preserves_values():
    import app as app_module

    fat = pd.DataFrame({
        "universe_id": pd.array([1, 2, 3], dtype="int64"),
        "visits": pd.array([50_000_000, 1, 2], dtype="int64"),
        "ccu": pd.array([25, 1, 2], dtype="int64"),
        "momentum_1d": pd.array([1.5, 2.5, 3.5], dtype="float64"),
        "title": ["a", "b", "c"],
    })
    slim = app_module._downcast_frame(fat)
    assert slim["visits"].dtype == pd.Int32Dtype() or str(slim["visits"].dtype) in ("int32", "int16")
    assert str(slim["momentum_1d"].dtype) == "float32"
    assert slim["visits"].tolist() == [50_000_000, 1, 2], "values unchanged"
    assert slim["title"].tolist() == ["a", "b", "c"], "strings untouched"


def test_sidebar_has_discord_filter_radio_without_coverage_chip(monkeypatch):
    """The Discord contact filter (2026-09-24): a sidebar radio
    (All / Discord available / No Discord). The coverage chip was removed
    (2026-09-27, user request) — it must not render anywhere."""
    at = _render_dashboard()
    assert not at.exception
    radios = [r for r in at.sidebar.radio if (r.key or "") == "discord_filter_radio"]
    assert len(radios) == 1, at.sidebar.radio
    options = radios[0].options
    assert len(options) == 3
    assert any("Discord" in str(o) for o in options)
    # The coverage chip is gone from every surface.
    sidebar_all = " ".join(
        str(el.value) for el in at.sidebar.caption
    ) + " ".join(el.proto.body for el in at.sidebar.markdown)
    assert "games with Discord ·" not in sidebar_all
    assert "checked" not in sidebar_all
    # The old removal-era banner must stay gone.
    infos = [w.value for w in at.info]
    assert not any("known contact state" in v for v in infos)


# --------------------------------------------------------------------------- #
# Discord availability filter narrows BEFORE the page slice (fixed
# 2026-09-28): the filter used to run after paging, so a 20-row page
# collapsed to the 2–5 rows on that page that happened to have Discord.
# --------------------------------------------------------------------------- #


def test_discord_filter_shows_full_page_of_matching_games(monkeypatch):
    """With 'Discord available' selected, a 20-game page shows all 20 Discord
    games — not the handful that survived a filter applied after paging."""
    at = _fresh_app()
    at.run()
    at.session_state["onboarding_complete"] = True
    at.session_state["pending_initial_scan"] = False
    at.session_state["welcome_scan_started"] = True
    at.session_state["discord_name"] = "dev_razor10"
    # 30 games: 25 with Discord, 5 without — filtering 25 rows to a
    # 20-per-page slice must still render a full 20-row page.
    base = _demo_frame()
    import copy
    rows = []
    for i in range(30):
        row = base.iloc[0].copy()
        row["universe_id"] = i + 1
        row["root_place_id"] = 1000 + i
        row["title"] = f"Game {i}"
        row["visits"] = 50_000_000 + i
        row["ccu"] = 1000 + i
        has_discord = i < 25
        row["has_discord"] = has_discord
        row["discord_url"] = "https://discord.gg/test" if has_discord else None
        rows.append(row)
    at.session_state["data"] = pd.DataFrame(rows)
    at.session_state["source"] = "demo"
    at.session_state["discord_filter_radio"] = "Discord Available (True)"
    at.run()
    assert not at.exception
    table_html = _table_html(at)
    count = table_html.count("<tr>") - 1  # minus the header row
    assert count == 20, f"expected a full 20-row page, got {count}"
    assert "Game 0" in table_html and "Game 19" in table_html, "first 20 Discord games"
    assert "Game 25" not in table_html, "no-Discord games are filtered out"


# --------------------------------------------------------------------------- #
# Device profile: refreshes and back-navigation remember you
# --------------------------------------------------------------------------- #


def test_refresh_restores_completed_scout_and_reloads_results(
    monkeypatch, tmp_path
):
    """A returning scout (refresh / new tab) lands on their results with
    identity restored — never back at the start of the welcome flow."""
    monkeypatch.setenv("SS_PROFILE_DIR", str(tmp_path / "profiles"))
    profile_store.save_profile(
        "test-ref-1",
        {
            "discord_name": "Saved Scout",
            "discord_user_id": "53908099506183680",
            "message_template": DEFAULT_MESSAGE_TEMPLATE,
            "target_min_visits": 50_000,
            "target_min_ccu": 75,
            "onboarding_cookie": "",
            "onboarding_step": 4,
            "guide_step": 4,
            "onboarding_complete": True,
        },
    )
    frame = _demo_frame()
    monkeypatch.setattr(
        scout_core.RobloxPlatformScout,
        "load_catalog_matches",
        lambda self, min_visits=0, min_ccu=0: frame.copy(),
    )

    at = _fresh_app()
    at.session_state["_device_ref"] = "test-ref-1"
    at.run()

    assert not at.exception
    titles = [el.value for el in at.title]
    assert any("Games matching" in t for t in titles), "dashboard, not welcome flow"
    # Identity came back for the copy-message cells: with a saved User ID the
    # message carries the real mention token (which takes precedence), so the
    # restored identity is proven by the token itself.
    match = re.search(r'data-msg="([^"]+)"', _table_html(at))
    assert match, "results table with copy buttons rendered"
    message = json.loads(html_module.unescape(match.group(1)))
    assert "<@53908099506183680>" in message
    assert "[Your Name]" not in message


def test_mid_onboarding_refresh_resumes_the_saved_step(monkeypatch, tmp_path):
    """Refreshing mid-welcome-flow resumes at the saved step with fields filled."""
    monkeypatch.setenv("SS_PROFILE_DIR", str(tmp_path / "profiles"))
    profile_store.save_profile(
        "test-ref-2",
        {
            "discord_name": "Part Scout",
            "discord_user_id": "",
            "message_template": DEFAULT_MESSAGE_TEMPLATE,
            "target_min_visits": 20_000,
            "target_min_ccu": 25,
            "onboarding_cookie": "",
            "onboarding_step": 3,
            "guide_step": 4,
            "onboarding_complete": False,
        },
    )

    at = _fresh_app()
    at.session_state["_device_ref"] = "test-ref-2"
    at.run()

    assert not at.exception
    titles = [el.value for el in at.title]
    assert any("Discord username" in t for t in titles), "resumes at step 3"
    assert at.text_input(key="discord_name").value == "Part Scout"


def test_onboarding_progress_is_saved_for_the_next_refresh(monkeypatch, tmp_path):
    """Advancing the welcome flow snapshots the profile immediately."""
    monkeypatch.setenv("SS_PROFILE_DIR", str(tmp_path / "profiles"))

    at = _fresh_app()
    at.session_state["_device_ref"] = "test-ref-3"
    at.run()
    at.button(key="onb0_next").click().run()
    at.button(key="onb1_next").click().run()  # targets -> Discord name
    at.run()

    saved = profile_store.load_profile("test-ref-3")
    assert saved.get("onboarding_step") == 3  # Discord name step (cookie guide removed)
    assert saved.get("target_min_visits")


def test_forget_this_device_clears_profile_and_returns_to_welcome(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SS_PROFILE_DIR", str(tmp_path / "profiles"))
    profile_store.save_profile(
        "test-ref-4",
        {
            "discord_name": "Doomed Scout",
            "target_min_visits": 1,
            "target_min_ccu": 1,
            "onboarding_cookie": "",
            "onboarding_step": 4,
            "guide_step": 4,
            "onboarding_complete": True,
        },
    )
    at = _fresh_app()
    at.session_state["_device_ref"] = "test-ref-4"
    at.session_state["data"] = _demo_frame()
    at.session_state["source"] = "demo"
    at.run()
    assert not at.exception

    at.sidebar.button(key="forget_device").click().run()

    assert not at.exception
    assert profile_store.load_profile("test-ref-4") == {}
    titles = [el.value for el in at.title]
    assert any("Welcome" in t for t in titles), "back to a fresh welcome flow"


