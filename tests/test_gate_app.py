"""App-level tests for the password gate and the reload-loop guards."""

import pytest
from streamlit.testing.v1 import AppTest

import app
import gate

from test_app_flow import APP_PATH


def _has_widget(at, kind, key):
    """True when a widget of ``kind`` with ``key`` exists in the last render."""
    try:
        getattr(at, kind)(key=key)
        return True
    except KeyError:
        return False


class FakeSession(dict):
    """Dict that also supports the attribute access st.session_state allows."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


@pytest.fixture()
def _gate_state(tmp_path, monkeypatch):
    """Isolated gate state (re-asserted here: this module tests the gate)."""
    monkeypatch.setenv("SS_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setenv("APP_PASSWORD", "Sep#2007")
    monkeypatch.delenv("SS_TEST_BYPASS_GATE", raising=False)
    return tmp_path / "gate"


def test_wrong_password_stays_on_gate(_gate_state):
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = "gate-dev-1"
    at.run()
    assert not at.exception
    assert _has_widget(at, "text_input", "gate_password"), "gate is shown"
    assert _has_widget(at, "button", "gate_unlock")

    at.text_input(key="gate_password").set_value("wrong-guess").run()
    at.button(key="gate_unlock").click().run()
    assert not at.exception
    assert any("attempt" in e.value for e in at.error), "wrong-password feedback"
    # The app itself never rendered: no welcome title, no dashboard.
    assert len(at.title) == 0, "locked out of app content"


def test_correct_password_reaches_app(_gate_state):
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = "gate-dev-2"
    at.run()
    at.text_input(key="gate_password").set_value("Sep#2007").run()
    at.button(key="gate_unlock").click().run()
    assert not at.exception
    assert len(at.title) > 0, "app content renders after unlock"
    # The unlock is remembered for this device (refresh keeps you in).
    assert gate.is_unlocked("gate-dev-2")


def test_wrong_password_shows_cooldown(_gate_state):
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = "gate-dev-3"
    at.run()
    for _ in range(gate.MAX_ATTEMPTS):
        at.text_input(key="gate_password").set_value("bad").run()
        at.button(key="gate_unlock").click().run()
        assert not at.exception
    assert gate.cooldown_remaining("gate-dev-3") > 0
    assert any("Try again in" in w.value for w in at.warning)


def test_fresh_visit_mounts_mint_script_once(_gate_state, monkeypatch):
    """No cookie and no ?ref= → the mint script is mounted exactly once."""
    mounted: list[str] = []

    def fake_html(body, *args, **kwargs):
        mounted.append(body)

    monkeypatch.setattr(app.st, "session_state", FakeSession())
    monkeypatch.setattr(app.st, "html", fake_html)

    ref = app._device_ref()
    assert ref is None, "no channels yet → mint script scheduled"
    assert len(mounted) == 1, "exactly one mint script, never a reload storm"

    # Every subsequent call in this tab (fresh server session or not) is inert.
    for _ in range(3):
        assert app._device_ref() is None
    assert len(mounted) == 1


def test_device_ref_from_query_param_never_mounts_script(_gate_state, monkeypatch):
    class FakeParams(dict):
        def get(self, key, default=None):
            return dict(self).get(key, default)

    mounted: list[str] = []

    def fake_html(body, *args, **kwargs):
        mounted.append(body)

    monkeypatch.setattr(app.st, "session_state", FakeSession())
    monkeypatch.setattr(app.st, "query_params", FakeParams(ref="ref-from-url"))
    monkeypatch.setattr(app.st, "html", fake_html)
    for _ in range(3):
        assert app._device_ref() == "ref-from-url"
    assert mounted == [], "query param is enough — no reload script"


def test_gate_renders_before_anything_else(_gate_state):
    """The gate renders instead of onboarding/dashboard, and app content is hidden."""
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = "gate-dev-4"
    at.run()
    body_text = " ".join(m.value for m in at.markdown)
    assert "Studio Scouts" in body_text
    assert not _has_widget(at, "button", "onb0_next"), "welcome flow not reachable"


def test_unlocked_device_skips_gate(_gate_state):
    gate.remember_unlock("gate-dev-5")
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = "gate-dev-5"
    at.run()
    assert not at.exception
    assert not _has_widget(at, "text_input", "gate_password"), "no password asked"
