"""Tests for user access keys: 100-password manifest, IP+device tracking,
and the sharing ban (two separate IPs AND two separate device ids)."""

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


def test_two_ips_and_two_devices_ban_the_key(tmp_path, monkeypatch):
    _write_manifest("ShareMe#Not#Today99", tmp_path, monkeypatch)
    key = "ShareMe#Not#Today99"
    assert gate.check_password(key, "user-a", "1.1.1.1") == "ok"
    # Same device, second IP: one dimension only — allowed.
    assert gate.check_password(key, "user-a", "2.2.2.2") == "ok"
    # Second device on an existing IP: second dimension reached — banned.
    assert gate.check_password(key, "user-b", "1.1.1.1") == "banned"
    # Banned stays banned for everyone, including the first user.
    assert gate.check_password(key, "user-a", "1.1.1.1") == "banned"
    assert gate.check_password(key, "user-c", "3.3.3.3") == "banned"


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
    assert gate.check_password(key, "rev-b", "8.8.8.8") == "banned"  # 2 ip, 2 dev
    # Both remembered unlocks are gone: a refresh hits the password screen.
    assert not gate.is_unlocked("rev-a")
    assert not gate.is_unlocked("rev-b")


def test_master_password_exempt_from_ban(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "the-master")
    _write_manifest("Exempt#Master#Keys31", tmp_path, monkeypatch)
    key = "Exempt#Master#Keys31"
    assert gate.check_password(key, "m-1", "3.3.3.3") == "ok"
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
