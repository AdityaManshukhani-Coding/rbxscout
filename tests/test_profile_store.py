"""Unit tests for the per-device profile store."""

import json

import pytest

import profile_store


@pytest.fixture(autouse=True)
def _isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SS_PROFILE_DIR", str(tmp_path / "profiles"))


def test_save_and_load_roundtrip():
    profile_store.save_profile("dev-1", {"discord_name": "tester", "onboarding_complete": True})
    loaded = profile_store.load_profile("dev-1")
    assert loaded["discord_name"] == "tester"
    assert loaded["onboarding_complete"] is True
    assert "saved_at" in loaded  # bookkeeping timestamp added


def test_load_missing_returns_empty():
    assert profile_store.load_profile("never-saved") == {}


def test_save_creates_directory_and_valid_json(tmp_path):
    profile_dir = tmp_path / "profiles"  # SS_PROFILE_DIR per the fixture
    profile_store.save_profile("dev-2", {"target_min_visits": 42})
    assert profile_dir.exists()  # store created it
    files = list(profile_dir.glob("dev-2.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["target_min_visits"] == 42


def test_clear_profile_removes_file():
    profile_store.save_profile("dev-3", {"discord_name": "x"})
    assert profile_store.load_profile("dev-3")
    profile_store.clear_profile("dev-3")
    assert profile_store.load_profile("dev-3") == {}


def test_dangerous_ref_is_sanitized():
    profile_store.save_profile("../../etc/passwd", {"a": 1})
    # Path traversal must not escape the profile dir.
    loaded = profile_store.load_profile("../../etc/passwd")
    assert loaded.get("a") == 1


def test_corrupt_file_loads_as_empty(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "broken.json").write_text("{not json", encoding="utf-8")
    assert profile_store.load_profile("broken") == {}
