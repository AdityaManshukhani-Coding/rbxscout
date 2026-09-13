"""Shared test isolation for the Studio Scouts test suite.

App-level tests bypass the password gate (the gate has its own dedicated
tests) and both server-side stores (gate state, device profiles) are pointed
at per-test temp directories so tests never touch real state files.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("SS_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("SS_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setenv("SS_TEST_BYPASS_GATE", "1")
    monkeypatch.setenv("APP_PASSWORD", "Sep#2007")
