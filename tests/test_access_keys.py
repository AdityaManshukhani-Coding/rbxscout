"""Tests for user access keys: 100-password manifest, IP+device tracking,
and the sharing ban (two devices on two networks WITHIN the simultaneity
window — not merely two IPs and two devices at any time, which wrongly
banned honest returning scouts, fixed 2026-09-28)."""

import json

import gate


def _write_manifest(keys, tmp_path, monkeypatch) -> None:
    if isinstance(keys, str):
        keys = [keys]
    manifest = {"passwords": [gate._user_hash(key) for key in keys]}
    path = tmp_path / "access_passwords.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("SS_USER_HASHES_FILE", str(path))


def test_user_key_unlocks_and_records(tmp_path, monkeypatch):
    _write_manifest("Key1#Alpha#Beta42", tmp_path, monkeypatch)
    ref = "access-dev-1"
    assert gate.check_password("Key1#Alpha#Beta42", ref, "1.2.3.4") == "ok"
    state = gate._load_user_state()
    entry = next(iter(state.values()))
    assert entry["ips"] == ["1.2.3.4"]
    assert entry["refs"] == [ref]


def test_returning_scout_on_new_network_and_new_browser_is_not_banned(tmp_path, monkeypatch):
    """THE reported bug (2026-09-28): works first time, banned on the 2nd
    login. The device ref expired between logins (new browser / cleared
    localStorage) AND the network changed (wifi -> cellular). The old rule
    read that as two people; it is one person logging in twice."""
    _write_manifest("Second#Login#Victim22", tmp_path, monkeypatch)
    key = "Second#Login#Victim22"
    assert gate.check_password(key, "browser-a", "81.2.3.4") == "ok"
    # A day later: new device id, new network — still the same human.
    _backdate_last_login(key, days=1)
    assert gate.check_password(key, "browser-b", "25.5.10.9") == "ok"
    _backdate_last_login(key, days=1)
    assert gate.check_password(key, "browser-c", "172.16.0.9") == "ok"  # 3rd device even


def _backdate_last_login(key: str, days: float = 0.0, seconds: float = 0.0) -> None:
    """Age every stored login for a key: simulates time passing between
    visits (a day) or shrinks it into the simultaneity window (a handoff)."""
    state = gate._load_user_state()
    entry = state[gate._user_hash(key)]
    shift = days * 86400 - seconds
    entry["logins"] = [[d, a, t - shift] for d, a, t in entry["logins"]]
    gate._save_user_state(state)


def test_handoff_within_window_bans(tmp_path, monkeypatch):
    """Two devices, two networks, seconds apart -> banned for everyone."""
    _write_manifest("Handoff#Pass#Here00", tmp_path, monkeypatch)
    key = "Handoff#Pass#Here00"
    assert gate.check_password(key, "user-a", "1.1.1.1") == "ok"
    # 30 seconds later a DIFFERENT device on a DIFFERENT network: shared.
    _backdate_last_login(key, seconds=30)
    assert gate.check_password(key, "user-b", "2.2.2.2") == "banned"
    # With the self-heal rule (2026-09-28), the OWNER presenting the correct
    # key clears the ban — so user-a's next sign-in succeeds again.
    assert gate.check_password(key, "user-a", "1.1.1.1") == "ok"
    assert not gate._load_user_state()[gate._user_hash(key)].get("banned")


def test_same_ip_many_devices_never_bans(tmp_path, monkeypatch):
    """Classmates sharing school wifi on their own devices stay allowed."""
    _write_manifest("School#Wifi#Pass77", tmp_path, monkeypatch)
    key = "School#Wifi#Pass77"
    for device in ("dev-1", "dev-2", "dev-3", "dev-4"):
        assert gate.check_password(key, device, "10.0.0.1") == "ok"


def test_same_device_many_ips_never_bans(tmp_path, monkeypatch):
    """One person on the move (wifi -> cellular) stays allowed."""
    _write_manifest("Roaming#One#Device55", tmp_path, monkeypatch)
    key = "Roaming#One#Device55"
    for ip in ("25.1.1.1", "80.4.4.4", "172.16.0.9"):
        assert gate.check_password(key, "traveller", ip) == "ok"


def test_unknown_ip_never_counts_toward_ban(tmp_path, monkeypatch):
    """Localhost / bare tests report no IP; it must never count as one of two."""
    _write_manifest("Local#Host#Safe00", tmp_path, monkeypatch)
    key = "Local#Host#Safe00"
    assert gate.check_password(key, "loc-1", None) == "ok"
    assert gate.check_password(key, "loc-2", None) == "ok"
    assert gate.check_password(key, "loc-1", "5.5.5.5") == "ok"  # still 1 IP


def test_ban_revokes_remembered_unlocks(tmp_path, monkeypatch):
    _write_manifest("Revoke#Me#Maybe11", tmp_path, monkeypatch)
    key = "Revoke#Me#Maybe11"
    gate.remember_unlock("rev-a")
    gate.remember_unlock("rev-b")
    assert gate.is_unlocked("rev-a") and gate.is_unlocked("rev-b")
    assert gate.check_password(key, "rev-a", "9.9.9.9") == "ok"
    _backdate_last_login(key, seconds=10)  # inside the window
    assert gate.check_password(key, "rev-b", "8.8.8.8") == "banned"
    # Both remembered unlocks are gone: a refresh hits the password screen.
    assert not gate.is_unlocked("rev-a")
    assert not gate.is_unlocked("rev-b")


def test_wrongly_banned_key_self_heals_on_next_correct_login(tmp_path, monkeypatch):
    """Fix for currently existing passwords: a key banned under the OLD
    over-eager rule unbans itself the moment its owner signs in with the
    correct plaintext — no manual state surgery required."""
    _write_manifest("Heal#Me#Please09", tmp_path, monkeypatch)
    key = "Heal#Me#Please09"
    # Trip a ban (handoff inside the window).
    assert gate.check_password(key, "user-a", "1.1.1.1") == "ok"
    _backdate_last_login(key, seconds=20)
    assert gate.check_password(key, "user-b", "2.2.2.2") == "banned"
    # The owner comes back with the correct key: ban clears, login succeeds.
    assert gate.check_password(key, "user-a", "1.1.1.1") == "ok"
    assert not gate._load_user_state()[gate._user_hash(key)].get("banned")
    # And the key keeps working from there.
    assert gate.check_password(key, "user-a", "1.1.1.1") == "ok"


def test_master_password_exempt_from_ban(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "the-master")
    _write_manifest("Exempt#Master#Keys31", tmp_path, monkeypatch)
    key = "Exempt#Master#Keys31"
    assert gate.check_password(key, "m-1", "3.3.3.3") == "ok"
    _backdate_last_login(key, seconds=5)
    assert gate.check_password(key, "m-2", "4.4.4.4") == "banned"
    # The master still unlocks from either device.
    assert gate.check_password("the-master", "m-2", "4.4.4.4") == "ok"
    assert gate.check_password("the-master", "m-1", "3.3.3.3") == "ok"


def test_missing_manifest_degrades_to_master_only(monkeypatch):
    monkeypatch.setenv("SS_USER_HASHES_FILE", str(
        __import__("pathlib").Path("/nonexistent/access.json")
    ))
    assert gate.user_password_status("anything") == "none"
    assert gate.check_password("anything", "solo", "1.1.1.1") == "wrong"


def test_generated_manifest_round_trip(tmp_path, monkeypatch):
    """The real committed manifest authenticates the first plaintext key."""
    manifest_path = gate._user_hashes_path()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        import pytest

        pytest.skip("access_passwords.json not generated yet")
    assert data["count"] >= 100
    assert len(set(data["passwords"])) == len(data["passwords"]), "all hashes unique"
    # And the hash scheme matches what the generator produced.
    first_hash = data["passwords"][0]
    assert len(first_hash) == 64  # full sha256 hex
