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

import scout_core
from scout_core import DEFAULT_MESSAGE_TEMPLATE, render_outreach_message

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
    return AppTest.from_file(APP_PATH, default_timeout=15)


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


def _table_html(at: AppTest) -> str:
    """Return the results-table HTML from the app's st.html element."""
    html_blocks = at.main.get("html")
    joined = "".join(el.proto.body for el in html_blocks)
    assert "ss-table" in joined, "results table should render after onboarding"
    return joined


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
