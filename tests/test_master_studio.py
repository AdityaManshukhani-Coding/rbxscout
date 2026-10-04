"""Master-password-only "Increates Studio" message set.

Signed in with the deployment's own password (the owner's key), the welcome
flow's message step gains a Studio switch between the five original UpScale
starters and the five Increates starters. Regular scout friend-keys never
see the switch and always get the UpScale set.

These tests reuse the same trick as test_access_keys.py: install a fake user
manifest with a known plaintext friend key, then drive the real gate through
``AppTest``.
"""

import json
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import gate
import scout_core
from scout_core import (
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_MESSAGE_TEMPLATE,
    INCREASES_MESSAGE_TEMPLATES,
)

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")

TEST_PASSWORD = "test-pass-1234"
FRIEND_KEY = "Friend#Studio#Tests71"


@pytest.fixture(autouse=True)
def _gate_state(tmp_path, monkeypatch):
    """Real gate (no bypass) with the master set to TEST_PASSWORD, plus one
    plaintext friend key in a fake manifest."""
    monkeypatch.setenv("SS_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("SS_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setenv("SS_CAPACITY_TEST_BYPASS", "1")
    monkeypatch.delenv("SS_TEST_BYPASS_GATE", raising=False)
    monkeypatch.setenv("APP_PASSWORD", TEST_PASSWORD)
    manifest_path = tmp_path / "access.json"
    monkeypatch.setenv("SS_USER_HASHES_FILE", str(manifest_path))
    manifest_path.write_text(
        json.dumps({"passwords": [gate._user_hash(FRIEND_KEY)]}),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _offline_scans():
    """Fail every scan instantly so the dashboard never attempts the network."""

    def _boom(self, *args, **kwargs):
        raise RuntimeError("offline test scan")

    original = scout_core.RobloxPlatformScout.scan
    scout_core.RobloxPlatformScout.scan = _boom
    scout_core.RobloxPlatformScout.scan_contacts = _boom
    yield
    scout_core.RobloxPlatformScout.scan = original
    scout_core.RobloxPlatformScout.scan_contacts = original


def _demo_frame() -> pd.DataFrame:
    """One game row shaped like the dashboard's demo/DB frame."""
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


def _master_app(ref: str) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = ref
    at.run()
    at.text_input(key="gate_password").set_value(TEST_PASSWORD).run()
    at.button(key="gate_unlock").click().run()
    assert not at.exception
    return at


def _friend_app(ref: str) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = ref
    at.run()
    at.text_input(key="gate_password").set_value(FRIEND_KEY).run()
    at.button(key="gate_unlock").click().run()
    assert not at.exception
    return at


def _walk_to_message_step(at: AppTest) -> None:
    at.button(key="onb0_next").click().run()
    at.button(key="onb1_next").click().run()
    at.text_input(key="discord_name").set_value("master_scout").run()
    at.button(key="onb3_next").click().run()


# --------------------------------------------------------------------------- #
# Template set content
# --------------------------------------------------------------------------- #


def test_increates_templates_shape():
    """Five rotations, [Game Name] kept, 🫡 sign-off, no identity tags."""
    assert len(INCREASES_MESSAGE_TEMPLATES) == 5
    for template in INCREASES_MESSAGE_TEMPLATES:
        assert "[Game Name]" in template
        assert "🫡" in template
        assert "[Your Name]" not in template  # deals pitch, not identity-led
        assert template.splitlines()[0] in ("Hey,", "Hi,"), template.splitlines()[0]
    # Message 1 is the owner's exact copy (modulo the tag).
    first = INCREASES_MESSAGE_TEMPLATES[0]
    assert "competitive price" in first
    assert "LiveOps, thumbnails, growth, monetisation & much more" in first
    assert "Thanks so much for your time 🫡" in first
    # All five differ, like the UpScale set.
    assert len(set(INCREASES_MESSAGE_TEMPLATES)) == 5


def test_increates_set_does_not_disturb_default_set():
    assert DEFAULT_MESSAGE_TEMPLATES[0] == DEFAULT_MESSAGE_TEMPLATE
    assert "UpScale" in DEFAULT_MESSAGE_TEMPLATES[0]
    for template in DEFAULT_MESSAGE_TEMPLATES:
        assert "🫡" not in template


# --------------------------------------------------------------------------- #
# Master login → studio choice appears
# --------------------------------------------------------------------------- #


def test_master_login_sets_flag_and_shows_studio_choice():
    at = _master_app("master-ref-1")
    _walk_to_message_step(at)
    assert not at.exception

    radios = [(r.label or "", r) for r in at.radio]
    studio = next((r for label, r in radios if label == "Studio"), None)
    assert studio is not None, "master login sees the Studio switch"
    assert list(studio.options) == ["UpScale Studio", "Increates Studio"]
    assert studio.value == "UpScale Studio", "UpScale is the default"
    # Starts on the standard set.
    assert at.text_area(key="message_template").value == DEFAULT_MESSAGE_TEMPLATES[0]

    # Switching studios swaps the whole set in, starting at its first starter.
    studio.set_value("Increates Studio").run()
    assert not at.exception
    assert at.text_area(key="message_template").value == INCREASES_MESSAGE_TEMPLATES[0]
    starter_radio = next(r for label, r in radios if label == "Starter message")
    starter_radio.set_value(starter_radio.options[2]).run()
    assert at.text_area(key="message_template").value == INCREASES_MESSAGE_TEMPLATES[2]
    # And back.
    studio.set_value("UpScale Studio").run()
    assert at.text_area(key="message_template").value == DEFAULT_MESSAGE_TEMPLATES[0]


def test_master_survives_profile_restore_in_later_session():
    """The master marker persists with the device profile, so a refresh (new
    Streamlit session, same device ref) keeps the studio switch available."""
    _master_app("master-ref-2")

    at2 = AppTest.from_file(APP_PATH, default_timeout=45)
    at2.session_state["_device_ref"] = "master-ref-2"
    at2.run()
    assert not at2.exception
    assert at2.session_state["master_unlocked"] is True
    _walk_to_message_step(at2)
    studio = next(r for r in at2.radio if (r.label or "") == "Studio")
    assert studio is not None


# --------------------------------------------------------------------------- #
# Friend key → no studio choice, unchanged flow
# --------------------------------------------------------------------------- #


def test_friend_key_never_sees_studio_choice():
    at = _friend_app("friend-ref-1")
    _walk_to_message_step(at)
    assert not at.exception

    assert "master_unlocked" not in at.session_state or at.session_state[
        "master_unlocked"
    ] is False
    assert not [r for r in at.radio if (r.label or "") == "Studio"], (
        "friend keys get the standard flow only"
    )
    # All five starters still come from the UpScale set.
    radio = next(r for r in at.radio if (r.label or "") == "Starter message")
    assert len(radio.options) == 5
    radio.set_value(radio.options[4]).run()
    assert at.text_area(key="message_template").value == DEFAULT_MESSAGE_TEMPLATES[4]


def test_master_copy_pool_follows_active_studio():
    """The rotation pool comes from the chosen studio's set."""
    at = _master_app("master-ref-3")
    at.session_state["onboarding_complete"] = True
    at.session_state["pending_initial_scan"] = False
    at.session_state["welcome_scan_started"] = True
    at.session_state["discord_name"] = "master_scout"
    at.session_state["data"] = _demo_frame()
    at.session_state["source"] = "demo"
    at.run()
    assert not at.exception
    # Flip the studio switch in the sidebar (the real user path): its
    # on_change callback loads the Increates set into the template.
    studio = at.sidebar.radio(key="message_studio")
    assert studio.value == "UpScale Studio"
    studio.set_value("Increates Studio").run()
    assert not at.exception
    assert at.sidebar.text_area(key="message_template").value == INCREASES_MESSAGE_TEMPLATES[0]

    html_blocks = at.main.get("html")
    table_html = "".join(el.proto.body for el in html_blocks)
    assert "ss-table" in table_html, "results table renders after onboarding"
    assert "data-alts" in table_html, "copy buttons embed the rotation pool"
    # The pool JSON is ascii-escaped, so decode it before checking the
    # Increates sign-off made it into the rotation.
    import html as html_module
    match = re.search(r'data-alts="([^"]+)"', table_html)
    pool = json.loads(html_module.unescape(match.group(1)))
    assert len(pool) == 5
    assert all("🫡" in entry["t"] for entry in pool)
