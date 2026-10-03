"""Motion design system tests (UI_MICRO_ANIMATIONS_PLAN.md).

String-presence tests, same pattern as the existing table tests: a CSS
refactor must not be able to silently drop the reduced-motion guard or the
row-entrance keyframes. The rendered dashboard HTML is also checked so the
blocks provably ship to the browser, not just exist as constants.
"""

import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from conftest import TEST_PASSWORD

from test_app_flow import APP_PATH, _render_dashboard


@pytest.fixture()
def _gate_state(tmp_path, monkeypatch):
    """Isolated gate state, mirroring test_gate_app.py's fixture."""
    monkeypatch.setenv("SS_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setenv("APP_PASSWORD", TEST_PASSWORD)
    monkeypatch.delenv("SS_TEST_BYPASS_GATE", raising=False)
    monkeypatch.setenv("SS_CAPACITY_TEST_BYPASS", "1")
    return tmp_path / "gate"


def _motion_source() -> str:
    """The app.py source — where the motion blocks live as constants."""
    return Path(APP_PATH).read_text(encoding="utf-8")


def test_reduced_motion_guard_is_present():
    """The single media query that disables every animation must survive
    any CSS refactor (plan acceptance: prefers-reduced-motion honored)."""
    source = _motion_source()
    assert "prefers-reduced-motion: reduce" in source
    assert "animation-duration: 0.01ms !important" in source
    assert "transition-duration: 0.01ms !important" in source


def test_motion_tokens_are_defined():
    """The shared duration/easing vocabulary ships once, not ad-hoc per surface."""
    source = _motion_source()
    assert "--motion-fast: 120ms" in source
    assert "--motion-base: 200ms" in source
    assert "--motion-slow: 320ms" in source
    assert "--ease-out: cubic-bezier(0.22, 1, 0.36, 1)" in source


def test_row_entrance_keyframes_are_present():
    """The staggered row entrance (plan 1.1) must survive refactors: the
    ss-row-in keyframes and the 18ms per-row delay vocabulary."""
    source = _motion_source()
    assert "@keyframes ss-row-in" in source
    assert "animation-delay" in source
    assert "18ms" in source


def test_rendered_dashboard_ships_the_motion_blocks():
    """The rendered table HTML must carry the tokens, the guard, the
    row-stagger keyframes and the copy-check keyframes — proving the blocks
    ship to the browser, not just exist as constants in app.py."""
    at = _render_dashboard()
    assert not at.exception

    html = "".join(el.proto.body for el in at.main.get("html"))
    assert "prefers-reduced-motion: reduce" in html
    assert "--motion-fast: 120ms" in html
    assert "@keyframes ss-row-in" in html
    assert "@keyframes ss-check-draw" in html
    # Row stagger delays are generated per row position.
    assert re.search(r"tr:nth-child\(1\)\s*\{\s*animation-delay:\s*0ms", html)
    # The directional page-slide and blow-up-pulse vocabulary ships too.
    assert "@keyframes ss-page-left" in html
    assert "@keyframes ss-blowup-pulse" in html
    assert "@keyframes ss-gate-shake" not in html  # gate-only, not in the table


def test_gate_screen_ships_entrance_and_shake_keyframes(_gate_state):
    """The gate card entrance (plan 3.3) and the wrong-password shake
    keyframes ride on the gate screen."""
    at = AppTest.from_file(APP_PATH, default_timeout=45)
    at.session_state["_device_ref"] = "motion-gate-1"
    at.run()
    assert not at.exception
    html = " ".join(el.proto.body for el in at.markdown) + " ".join(
        el.proto.body for el in at.main.get("html")
    )
    assert "@keyframes ss-gate-in" in html
    assert "@keyframes ss-gate-shake" in html
    assert "gate-card" in html
