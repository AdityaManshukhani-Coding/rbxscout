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
    DEFAULT_MESSAGE_TEMPLATE,
    DISCORD_FILTER_ALL,
    DISCORD_FILTER_TRUE,
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
            "avg_ccu_1d": 900.0, "momentum_1d": 100, "contacts_checked_at": None,
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
    assert "I'm dev_razor10 from Studio Scouts" in message
    assert "impressed by **Blox Fruits**" in message


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
    assert "I'm ace from Studio Scouts" in message


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
    at.button(key="onb1_next").click().run()  # targets -> cookie guide
    for _ in range(4):
        at.button(key="onb2_next").click().run()  # guide steps 1..4 -> Discord

    # Discord username step is shown right after the cookie step.
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


def test_user_id_optional_field_flows_into_copied_message(monkeypatch):
    """Step 3 shows the optional User ID field with helper captions; a saved
    ID makes the copied message carry a real <@ID> mention token; leaving it
    empty keeps the plain username."""
    _patch_catalog_reader(monkeypatch, _demo_frame())

    at = _fresh_app()
    at.run()
    at.button(key="onb0_next").click().run()
    at.button(key="onb1_next").click().run()
    for _ in range(4):
        at.button(key="onb2_next").click().run()

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
    assert any("Studio Scouts" in el.value for el in at.header) or at.title


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
    at.button(key="onb1_next").click().run()  # targets -> cookie guide
    for _ in range(4):
        at.button(key="onb2_next").click().run()  # guide steps 1..4
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


def test_cookie_from_welcome_flow_reaches_the_scout():
    """The .ROBLOSECURITY entered in onboarding must survive to the Roblox
    session — Streamlit used to drop it when the widget unmounted, so every
    contact lookup ran signed-out (401s, zero invites)."""
    at = _fresh_app()
    at.run()
    at.session_state["onboarding_cookie"] = "test-cookie-value"
    at.session_state["onboarding_complete"] = True
    at.session_state["pending_initial_scan"] = False
    at.session_state["welcome_scan_started"] = True
    at.session_state["data"] = _demo_frame()
    at.session_state["source"] = "demo"
    at.run()

    assert not at.exception
    captions = [el.value for el in at.sidebar.caption]
    assert any("Cookie configured: yes" in value for value in captions)


def test_discord_filter_reads_whole_catalog(monkeypatch):
    """'Discord Available' must query the catalog-wide contact state in SQL,
    not just the 20 in-memory rows — otherwise it always said 'No results'."""
    at = _render_dashboard()
    calls: list[dict] = []
    discord_frame = _demo_frame()
    discord_frame["has_discord"] = True
    discord_frame["discord_url"] = "https://discord.gg/test"

    def _fake(self, min_visits=0, min_ccu=0, discord=None):
        calls.append({"min_visits": min_visits, "min_ccu": min_ccu, "discord": discord})
        return discord_frame.copy() if discord else _demo_frame().drop(index=0)

    monkeypatch.setattr(scout_core.RobloxPlatformScout, "load_catalog_matches", _fake)

    at.sidebar.radio(key="discord_filter_radio").set_value(DISCORD_FILTER_TRUE).run()

    assert not at.exception
    assert calls and calls[-1]["discord"] is True
    assert "Blox Fruits" in _table_html(at)


def test_resolved_contacts_recorded_to_overlay(monkeypatch):
    """Contact verdicts resolved in the UI must be handed to the catalog_fetch
    overlay store, or the next pipeline asset swap erases them and the
    Discord filter 'loses' games the user already found."""
    recorded: dict = {}
    monkeypatch.setattr(catalog_fetch, "record_contacts", lambda records: recorded.update(records))

    refreshed = _demo_frame().copy()
    refreshed["contacts_checked_at"] = "2026-09-14 12:00:00"

    def _fake_scan(self, ids, force=False, run_id=None, progress_cb=None):
        return refreshed.copy()

    monkeypatch.setattr(scout_core.RobloxPlatformScout, "scan_contacts", _fake_scan)

    at = _render_dashboard()

    assert not at.exception
    assert 1 in recorded, "page-1 contact check must feed the overlay store"
    assert recorded[1]["has_discord"] == True  # noqa: E712
    assert recorded[1]["discord_url"] == "https://discord.gg/test"


def test_discord_filter_flip_resets_to_page_one(monkeypatch):
    """Toggling the Discord filter deep into the unfiltered list used to drop
    the user on the LAST page of the filtered set (clamped), which read as if
    results were sorted highest-visits-first. A filter flip must restart at
    page 1."""
    at = _render_dashboard()

    # 25 rows in session data and 25 filtered rows from the catalog read:
    # both lists span 2 pages at 20 per page, so page 2 is reachable.
    rows = []
    for i in range(1, 26):
        row = _demo_frame().iloc[0].copy()
        row["universe_id"] = i
        row["title"] = f"Game {i:02d}"
        row["visits"] = 1000 * i  # ascending
        rows.append(row)
    big = pd.DataFrame(rows)
    at.session_state["data"] = big.copy()

    discord_rows = big.copy()
    discord_rows["has_discord"] = True
    discord_rows["discord_url"] = "https://discord.gg/x"

    def _fake(self, min_visits=0, min_ccu=0, discord=None):
        return discord_rows.copy() if discord else big.copy()

    monkeypatch.setattr(scout_core.RobloxPlatformScout, "load_catalog_matches", _fake)

    at.sidebar.radio(key="discord_filter_radio").set_value(DISCORD_FILTER_TRUE).run()
    assert not at.exception
    at.sidebar.number_input(key="contact_page").set_value(2).run()
    assert at.session_state["contact_page"] == 2

    # Flipping the filter changes the result set entirely -> back to page 1.
    at.sidebar.radio(key="discord_filter_radio").set_value(DISCORD_FILTER_ALL).run()
    assert at.session_state["contact_page"] == 1


def test_discord_filter_survives_stale_scout_core_signature(monkeypatch):
    """Deploy skew self-heal: a running cloud process can hold the OLD
    load_catalog_matches (no ``discord`` param) in sys.modules while the
    freshly re-read app.py calls it with one — this used to crash with a
    red TypeError on the Discord filter. Now the call is signature-checked
    and the constraint is applied in memory instead."""
    at = _render_dashboard()

    stale = pd.concat([
        _demo_frame(),  # has_discord=True, has an invite
        _demo_frame().assign(
            universe_id=2, title="No Invite Game", has_discord=False, discord_url=None
        ),
    ], ignore_index=True)

    def _stale(self, min_visits=0, min_ccu=0):
        # Old-world signature: accepts no ``discord`` argument at all.
        return stale.copy()

    monkeypatch.setattr(scout_core.RobloxPlatformScout, "load_catalog_matches", _stale)

    at.sidebar.radio(key="discord_filter_radio").set_value(DISCORD_FILTER_TRUE).run()

    assert not at.exception  # the TypeError crash is the regression
    table = _table_html(at)
    assert "Blox Fruits" in table
    assert "No Invite Game" not in table


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
    at.button(key="onb1_next").click().run()  # targets -> cookie guide
    at.run()

    saved = profile_store.load_profile("test-ref-3")
    assert saved.get("onboarding_step") == 2  # cookie guide
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


def test_cookie_guide_renders_step_screenshots():
    """Every cookie-guide step shows a screenshot (assets/Step N SS.png).

    AppTest serves file images under /mock/media/<hash>.png, so the original
    filename is not visible in the element — one rendered image per step is
    the contract (guide_image resolves the right file per step).
    """
    at = _fresh_app()
    at.run()
    at.button(key="onb0_next").click().run()
    at.button(key="onb1_next").click().run()

    for expected_step in range(1, 5):
        assert not at.exception
        assert len(at.image) >= 1, f"guide step {expected_step} must show a screenshot"
        if expected_step < 4:
            at.button(key="onb2_next").click().run()
