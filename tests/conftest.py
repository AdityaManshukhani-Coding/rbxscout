"""Shared test isolation for the Studio Scouts test suite.

App-level tests bypass the password gate (the gate has its own dedicated
tests) and both server-side stores (gate state, device profiles) are pointed
at per-test temp directories so tests never touch real state files.
"""

import pytest

import catalog_fetch
import scout_core

# Test-only password. Deliberately NOT the real deployment password: no
# production secret belongs in a repo. gate._expected_password() reads
# APP_PASSWORD, so the gate code paths are exercised identically.
TEST_PASSWORD = "test-pass-1234"


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("SS_PROFILE_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("SS_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setenv("SS_TEST_BYPASS_GATE", "1")
    monkeypatch.setenv("APP_PASSWORD", TEST_PASSWORD)
    # Process-wide caches (multi-user safety features) must not leak state
    # between tests: the shared contact-verdict memcache would otherwise
    # serve one test's mocked verdict to the next, and a 429 seen in one
    # test would suppress lookups in the following ones.
    scout_core._CONTACT_MEMCACHE.clear()
    scout_core.RobloxPlatformScout._throttle_until = 0.0
    catalog_fetch._reset_counts_cache()
    # Atlas Dev (atlasdev.gg) is the one third-party site the pipeline now
    # touches: tests must never hit it for real. Any stray requests.get to
    # atlasdev.gg fails fast unless the test deliberately fakes it first
    # (tests/test_atlas.py swaps scout_core.requests.get before harvesting).
    real_requests_get = scout_core.requests.get

    def _no_live_atlas(url, *args, **kwargs):
        if "atlasdev.gg" in str(url):
            raise AssertionError(f"test attempted a LIVE Atlas request: {url}")
        return real_requests_get(url, *args, **kwargs)

    monkeypatch.setattr(scout_core.requests, "get", _no_live_atlas)
