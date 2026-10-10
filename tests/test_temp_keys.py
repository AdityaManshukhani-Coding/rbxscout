"""One-time temp keys (burn after first use) + owner key termination.

Complements tests/test_access_keys.py, which covers the standing 100-key
user set and the sharing ban; here every key is used AT MOST once, so the
"up to 5 real uses" behavior of the record_user_login phase is not what is
under test — the burn is.
"""

import hashlib
import json

import pytest

import gate


@pytest.fixture()
def temp_manifest(tmp_path, monkeypatch):
    """A fresh temp-key manifest with ONE known plaintext inside it."""
    key = "Trial#Once#Only12"
    path = tmp_path / "temp_access_passwords.json"
    path.write_text(
        json.dumps({"passwords": [gate._temp_hash(key)]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SS_TEMP_HASHES_FILE", str(path))
    return key


def _user_manifest(tmp_path, monkeypatch, key: str) -> None:
    path = tmp_path / "access_passwords.json"
    path.write_text(
        json.dumps({"passwords": [gate._user_hash(key)]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SS_USER_HASHES_FILE", str(path))


def test_temp_key_unlocks_once(temp_manifest):
    key = temp_manifest
    # First use: unlocks.
    assert gate.check_password(key, "temp-ref-1", "1.2.3.4") == "ok"
    # Same plaintext again, from anywhere: refused as "used".
    assert gate.check_password(key, "temp-ref-1", "1.2.3.4") == "used"
    assert gate.check_password(key, "temp-ref-2", "5.6.7.8") == "used"


def test_used_temp_key_does_not_burn_attempts(temp_manifest):
    """A spent key is not a wrong password: it must not feed the cooldown."""
    key = temp_manifest
    assert gate.check_password(key, "burn-ref", None) == "ok"
    for _ in range(10):
        assert gate.check_password(key, "burn-ref", None) == "used"
    # The lockout bookkeeping was never touched.
    assert gate.cooldown_remaining("burn-ref") == 0.0


def test_temp_key_hash_not_in_user_manifest_space(temp_manifest, tmp_path, monkeypatch):
    """Temp keys are a separate namespace: a temp hash never classifies as a
    standing user key, and its burn writes a distinctly 'temp'-kind entry."""
    key = temp_manifest
    assert gate.check_password(key, "temp-ns", None) == "ok"
    state = gate._load_user_state()[gate._temp_hash(key)]
    assert state.get("kind") == "temp"
    assert state.get("used") is True


def test_wrong_password_still_wrong(temp_manifest):
    assert gate.check_password("Not#A#RealKey00", "temp-wrong", None) == "wrong"


def test_missing_temp_manifest_fails_closed(monkeypatch):
    monkeypatch.setenv("SS_TEMP_HASHES_FILE", "/nonexistent/temp.json")
    assert gate.temp_password_status("anything") == "none"
    assert gate.check_password("anything", "temp-missing", None) == "wrong"


def test_generated_temp_manifest_uniqueness():
    """The REAL committed temp manifest: 15 distinct hashes, all known."""
    manifest_path = gate._temp_hashes_path()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pytest.skip("temp_access_passwords.json not generated yet")
    assert data["count"] == 15
    assert len(set(data["passwords"])) == len(data["passwords"]), "all unique"
    for h in data["passwords"]:
        assert len(h) == 64  # full sha256 hex


# --------------------------------------------------------------------------- #
# Owner termination of standing user keys
# --------------------------------------------------------------------------- #


def test_terminate_user_key_stops_it_everywhere(tmp_path, monkeypatch):
    key = "Dead#On#Arrival33"
    _user_manifest(tmp_path, monkeypatch, key)
    # Works before.
    assert gate.check_password(key, "t-ref-1", "1.1.1.1") == "ok"
    # Owner kills it.
    hit = gate.terminate_user_passwords([key])
    assert hit == [key]
    # Dead for the previous device, a returning device, and a fresh one.
    assert gate.check_password(key, "t-ref-1", "1.1.1.1") == "terminated"
    assert gate.check_password(key, "t-ref-2", "2.2.2.2") == "terminated"
    # And the state entry is flagged with no plaintext in it.
    entry = gate._load_user_state()[gate._user_hash(key)]
    assert entry.get("terminated") is True
    assert "terminated_at" in entry


def test_terminate_revokes_remembered_unlocks(tmp_path, monkeypatch):
    """Devices that used a terminated key lose their remembered unlock, so
    they land on the password screen next load AND fail to re-unlock."""
    key = "Kick#The#Device44"
    _user_manifest(tmp_path, monkeypatch, key)
    # One device used the key before termination.
    assert gate.check_password(key, "kick-a", "1.1.1.1") == "ok"
    gate.remember_unlock("kick-a")
    gate.remember_unlock("kick-b")
    assert gate.is_unlocked("kick-a") and gate.is_unlocked("kick-b")
    gate.terminate_user_passwords([key])
    # The key recorded kick-a's ref, so its unlock is revoked; devices that
    # never used the key (kick-b) are not touched by THIS key's revocation.
    assert not gate.is_unlocked("kick-a")
    assert gate.is_unlocked("kick-b")


def test_terminate_unknown_key_reports_nothing(tmp_path, monkeypatch):
    _user_manifest(tmp_path, monkeypatch, "Real#Key#Here00")
    assert gate.terminate_user_passwords(["Typo#No#Match99"]) == []
    entry = gate._load_user_state().get(gate._user_hash("Typo#No#Match99"), {})
    assert not entry.get("terminated")


def test_terminated_key_cannot_self_heal(tmp_path, monkeypatch):
    """/login path must never clear owner termination (unlike share bans)."""
    key = "No#Heal#For#Terminated55"
    _user_manifest(tmp_path, monkeypatch, key)
    gate.terminate_user_passwords([key])
    # Presenting the right plaintext does NOT resurrect it.
    assert gate.check_password(key, "zombie", None) == "terminated"
    assert gate._load_user_state()[gate._user_hash(key)].get("terminated") is True


def test_terminate_prunes_the_committed_manifest(tmp_path, monkeypatch):
    """The manifest edit is the belt to the state-flag braces: after
    termination, the plaintext classifies as none (wrong) even with a FRESH
    state file — no stale 'active' classification anywhere."""
    key = "Manifest#Wipe#Test66"
    _user_manifest(tmp_path, monkeypatch, key)
    gate.terminate_user_passwords([key])
    # The manifest no longer contains the key (in this test, the manifest
    # file is our tmp copy).
    import os
    import pathlib

    data = json.loads(pathlib.Path(os.environ["SS_USER_HASHES_FILE"]).read_text())
    assert len(data["passwords"]) == 0
    assert gate._user_hash(key) in data.get("terminated_hashes", [])
