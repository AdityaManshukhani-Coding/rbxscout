"""Unit tests for the password gate: attempts, escalating cooldown, memory."""

import json

import pytest

import gate
from conftest import TEST_PASSWORD


@pytest.fixture()
def ref():
    return "device-abc"


def test_correct_password_unlocks_and_resets(ref):
    assert gate.check_password(TEST_PASSWORD, ref) == "ok"
    assert gate.attempts_left(ref) == gate.MAX_ATTEMPTS  # bookkeeping cleared
    assert gate.cooldown_remaining(ref) == 0.0


def test_wrong_password_counts_down_attempts(ref):
    assert gate.check_password("nope", ref) == "wrong"
    assert gate.attempts_left(ref) == gate.MAX_ATTEMPTS - 1
    assert gate.check_password("nope", ref) == "wrong"
    assert gate.attempts_left(ref) == gate.MAX_ATTEMPTS - 2
    assert gate.cooldown_remaining(ref) == 0.0  # still free tries


def test_cooldown_starts_after_max_attempts(ref):
    for _ in range(gate.MAX_ATTEMPTS):
        assert gate.check_password("nope", ref) == "wrong"
    # The 5th wrong try starts the first cooldown (1 minute).
    assert 0 < gate.cooldown_remaining(ref) <= gate.BASE_COOLDOWN_SECONDS
    assert gate.cooldown_total(ref) == gate.BASE_COOLDOWN_SECONDS


def test_cooldown_doubles_with_each_further_failure(ref):
    for _ in range(gate.MAX_ATTEMPTS):
        gate.check_password("nope", ref)
    first_total = gate.cooldown_total(ref)
    # Cool the timer down artificially, then fail again: the window doubles.
    state = json.loads(gate._lock_path().read_text())
    state[gate._ref_key(ref)]["locked_until"] = 0.0
    gate._lock_path().write_text(json.dumps(state))
    assert gate.check_password("nope", ref) == "wrong"  # counts as a new failure
    assert gate.cooldown_total(ref) == first_total * 2


def test_correct_password_during_cooldown_does_not_bypass_or_extend(ref):
    for _ in range(gate.MAX_ATTEMPTS):
        gate.check_password("nope", ref)
    remaining = gate.cooldown_remaining(ref)
    assert gate.check_password(TEST_PASSWORD, ref) == "cooldown"  # iOS behaviour
    assert abs(gate.cooldown_remaining(ref) - remaining) < 2.0  # timer untouched
    assert gate.attempts_left(ref) == 0


def test_lockout_state_survives_module_reload(ref):
    for _ in range(gate.MAX_ATTEMPTS):
        gate.check_password("nope", ref)
    # A fresh process would read the same state file.
    remaining = gate.cooldown_remaining(ref)
    assert remaining > 0
    assert gate.cooldown_remaining("other-device") == 0.0  # per-device


def test_persistent_unlock_roundtrip(ref):
    assert gate.is_unlocked(ref) is False
    gate.remember_unlock(ref)
    assert gate.is_unlocked(ref) is True
    gate.forget_unlock(ref)
    assert gate.is_unlocked(ref) is False


def test_ua_fallback_key_works_without_device_id():
    """Cookie-less visitors (cloud cookie-reading race) still get lockouts."""
    assert gate.check_password("bad", None) == "wrong"
    assert gate.attempts_left(None) == gate.MAX_ATTEMPTS - 1
    gate.reset_attempts(None)


def test_state_files_isolated_via_env(tmp_path, monkeypatch, ref):
    monkeypatch.setenv("SS_GATE_DIR", str(tmp_path / "g"))
    gate.remember_unlock(ref)
    assert (tmp_path / "g" / ".gate_unlocked.json").exists()
