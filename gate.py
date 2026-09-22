"""Password gate for Studio Scouts.

The site is private, so every visitor must enter the shared password before
any app content renders. Wrong attempts get an iPhone-style escalating
cooldown: after ``MAX_ATTEMPTS`` misses the wait doubles with every further
wrong try (1 min, 2 min, 4 min, …) instead of ever becoming a permanent lock.

The attempt bookkeeping lives in a tiny JSON file next to this module so it
survives Streamlit's session resets *and* full restarts; it is keyed by an
anonymous per-browser id (the ``ss_ref`` device ref) so one stranger fat-
fingering the password never locks everyone out. The file is never written
with the password itself, only with attempt counts and timestamps.

The password can be overridden per-deployment with the ``APP_PASSWORD``
environment variable (or Streamlit secret) — there is deliberately NO
hardcoded fallback password in this repo: anyone who can read the source
could otherwise unlock every deployment. Set ``APP_PASSWORD`` as a Streamlit
secret (or env var) before deploying.


User access passwords (friend keys)
-----------------------------------

Besides the master password, the deployment can honour a set of per-user
access passwords (``access_passwords.json`` next to this module: SHA-256
hashes of the 100 generated keys; the plaintext list lives only with the
owner, e.g. pasted into a private Google Doc next to each friend's name).

Anti-sharing rule: each user password records the (client IP, device id)
pairs it is used from. The moment one password shows up from **two
different IPs AND two different device ids**, it is assumed shared between
two people: the password is banned and both users are locked out (their
remembered unlocks are revoked too, so a refresh sends them back to the
password screen). Same-IP multi-device use (one friend on laptop + phone
at home) never triggers the ban — the two conditions must hold together.
The master password is exempt from all of this by design.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

MAX_ATTEMPTS = 5  # wrong tries before the first cooldown kicks in
BASE_COOLDOWN_SECONDS = 60.0  # first lockout; doubles after every further miss

_UNLOCK_DAYS = 30  # how long an unlocked device stays unlocked

# Serializes the read-modify-write cycles on the lock/unlock JSON files.
# Concurrent attempts used to both read the same failure count and both
# write it back, losing cooldown escalations; under many users that quietly
# weakened the brute-force protection.
_STATE_LOCK = threading.Lock()


def _state_dir() -> Path:
    """Where lock/unlock state lives (env-overridable so tests can isolate)."""
    return Path(os.environ.get("SS_GATE_DIR", str(Path(__file__).resolve().parent)))


def _lock_path() -> Path:
    return _state_dir() / ".gate_lock.json"


def _unlock_path() -> Path:
    return _state_dir() / ".gate_unlocked.json"


def _expected_password() -> str:
    """The deployment's password; empty when the owner never configured one.

    No repo-side fallback: a password in a public repo is not a password.
    When unset, check_password fails closed (and the UI explains why).
    """
    value = os.environ.get("APP_PASSWORD") or _streamlit_secret() or ""
    return value


# --- user access passwords (friend keys) ----------------------------------- #

# Fixed application-side salt: domain-separates these hashes from other
# sha256 uses and makes off-the-shelf rainbow tables useless. The generated
# keys are long random strings, so hash lookup is not a realistic attack.
_USER_HASH_SALT = "rbxscout-access-v1:"
# Per-password history bounds (last-seen wins; sharing needs only 2 entries).
_MAX_TRACKED = 8


def _user_hashes_path() -> Path:
    """Where the committed user-password hash manifest lives (env-overridable)."""
    override = os.environ.get("SS_USER_HASHES_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "access_passwords.json"


def _user_state_path() -> Path:
    return _state_dir() / ".access_users.json"


def _user_hash(candidate: str) -> str:
    return hashlib.sha256((_USER_HASH_SALT + candidate).encode("utf-8")).hexdigest()


def _load_user_hashes() -> set:
    try:
        data = json.loads(_user_hashes_path().read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(data, dict):
        data = data.get("passwords") or []
    if not isinstance(data, list):
        return set()
    return {str(item).strip().lower() for item in data if str(item).strip()}


def _load_user_state() -> dict:
    try:
        data = json.loads(_user_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_user_state(state: dict) -> None:
    try:
        target = _user_state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp_name, target)
    except Exception:
        pass  # access bookkeeping must never take the app down


def user_password_status(candidate: str) -> str:
    """Classify a candidate against the user-key manifest: ``user``/``none``."""
    return "user" if _user_hash(candidate) in _load_user_hashes() else "none"


def check_user_password(candidate: str, ref: str | None, ip: str | None) -> str:
    """Validate one user-key attempt; returns ``ok``/``banned``/``wrong``.

    On ``ok`` the IP and the device id are recorded for that key. When the
    history shows TWO separate IPs AND TWO separate device ids, the key is
    being shared between two people: it is banned for everyone and its
    remembered unlocks are revoked, so both users hit the password screen
    again and are refused from there. Owner's rule: one password = one
    person = one device. (Edge case accepted by the owner: one person using
    two devices on two networks also trips the ban — stick to one device.)

    Lenient corners: an "unknown" IP (bare tests, localhost) never counts
    toward the two-IP condition, so local development is never banned.
    """
    key = _user_hash(candidate)
    if key not in _load_user_hashes():
        return "wrong"
    device = _ref_key(ref)
    banned_now = False
    with _STATE_LOCK:
        state = _load_user_state()
        entry = state.get(key) if isinstance(state.get(key), dict) else {}
        if entry.get("banned"):
            return "banned"
        ips = [str(x) for x in entry.get("ips", []) if x]
        refs = [str(x) for x in entry.get("refs", []) if x]
        if ip and ip != "unknown" and ip not in ips:
            ips = (ips + [ip])[-_MAX_TRACKED:]
        if device and device not in refs:
            refs = (refs + [device])[-_MAX_TRACKED:]
        now = time.time()
        entry.update(
            {
                "ips": ips,
                "refs": refs,
                "first_seen": entry.get("first_seen") or now,
                "last_seen": now,
            }
        )
        # The sharing tripwire, exactly as specified: two separate IPs AND
        # two separate device ids on the same key = shared password = ban.
        if len(ips) >= 2 and len(refs) >= 2:
            entry["banned"] = True
            entry["banned_at"] = now
            entry["banned_reason"] = (
                f"used from {len(ips)} IPs and {len(refs)} devices "
                f"({', '.join(ips)} / {', '.join(refs)})"
            )
            banned_now = True
        state[key] = entry
        _save_user_state(state)
    # Unlock revocation happens OUTSIDE _STATE_LOCK: it acquires the same
    # non-reentrant lock, and calling it under the lock deadlocked the ban
    # path (the original bug — every banned login hung its request forever).
    if banned_now:
        _revoke_unlocks_for(refs)
        return "banned"
    return "ok"


def _revoke_unlocks_for(refs: list) -> None:
    """Drop remembered unlocks for every device that used a banned key."""
    try:
        with _STATE_LOCK:
            state = _load_unlocks()
            hits = {r for r in refs if r in state}
            if not hits:
                return
            for r in hits:
                state.pop(r, None)
            target = _unlock_path()
            fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.replace(tmp_name, target)
    except Exception:
        pass


def _streamlit_secret() -> str | None:
    """Read ``APP_PASSWORD`` from Streamlit secrets when available (never raises)."""
    try:
        import streamlit as st

        value = st.secrets.get("APP_PASSWORD")  # type: ignore[attr-defined]
        return str(value) if value else None
    except Exception:
        return None


def _hash(value: str) -> str:
    """Short unsalted hash — enough to keep the password out of the lock file."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _load_state() -> dict:
    try:
        data = json.loads(_lock_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        target = _lock_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp_name, target)
    except Exception:
        pass  # a missing lock file must never take the app down


def _entry(state: dict, ref: str) -> dict:
    entry = state.get(ref)
    return entry if isinstance(entry, dict) else {}


def _ref_key(ref: str | None) -> str:
    """Lockout key: the device ref, or a UA fallback when cookies are unreadable."""
    if ref:
        return ref
    try:
        import streamlit as st

        headers = dict(getattr(st.context, "headers", None) or {})
        ua = str(headers.get("User-Agent") or headers.get("user-agent") or "unknown")
    except Exception:
        ua = "unknown"
    return "ua:" + _hash(ua)


def _client_ip() -> str:
    """Best-effort visitor IP for the user-key sharing tracker.

    On Streamlit Community Cloud the app sits behind a proxy, so the real
    client address arrives in X-Forwarded-For (first hop); st.context.ip_address
    is used as a fallback. Returns "unknown" when neither is available
    (bare tests, localhost) — unknown IPs never count toward the ban.
    """
    try:
        import streamlit as st

        headers = dict(getattr(st.context, "headers", None) or {})
        forwarded = str(
            headers.get("X-Forwarded-For") or headers.get("x-forwarded-for") or ""
        ).strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
        ip = getattr(st.context, "ip_address", None)
        if ip:
            return str(ip)
    except Exception:
        pass
    return "unknown"


def cooldown_remaining(ref: str | None) -> float:
    """Seconds this visitor must still wait (0 when not locked out)."""
    entry = _entry(_load_state(), _ref_key(ref))
    until = float(entry.get("locked_until", 0) or 0)
    return max(0.0, until - time.time())


def cooldown_total(ref: str | None) -> float:
    """Length of the *current* cooldown window (for progress displays)."""
    entry = _entry(_load_state(), _ref_key(ref))
    return float(entry.get("locked_total", 0) or 0)


def attempts_left(ref: str | None) -> int:
    """Free tries remaining before the first cooldown kicks in."""
    entry = _entry(_load_state(), _ref_key(ref))
    if cooldown_remaining(ref) > 0:
        return 0
    return max(0, MAX_ATTEMPTS - int(entry.get("fails", 0)))


def record_failure(ref: str | None) -> float:
    """Count a wrong password; start/double the cooldown when due.

    Returns the remaining cooldown (0 while attempts are still free).
    """
    with _STATE_LOCK:
        state = _load_state()
        key = _ref_key(ref)
        entry = _entry(state, key)
        if cooldown_remaining(ref) > 0:
            return cooldown_remaining(ref)  # already locked; don't extend on retries
        fails = int(entry.get("fails", 0)) + 1
        locked_until = 0.0
        locked_total = float(entry.get("locked_total", 0) or 0)
        if fails >= MAX_ATTEMPTS:
            # iPhone pattern: the first lockout is BASE, every further wrong try
            # doubles the window — escalating but never permanent.
            locked_total = BASE_COOLDOWN_SECONDS if locked_total <= 0 else locked_total * 2
            locked_until = time.time() + locked_total
        entry.update({"fails": fails, "locked_until": locked_until, "locked_total": locked_total})
        state[key] = entry
        _save_state(state)
    return max(0.0, locked_until - time.time())


def reset_attempts(ref: str | None) -> None:
    """Clear attempt bookkeeping after a successful unlock."""
    with _STATE_LOCK:
        state = _load_state()
        key = _ref_key(ref)
        if key in state:
            state.pop(key)
            _save_state(state)


def check_password(candidate: str, ref: str | None, ip: str | None = None) -> str:
    """Evaluate one unlock attempt; returns ``ok``/``banned``/``cooldown``/``wrong``/``unconfigured``.

    Like iOS: while cooling down, even the correct password cannot skip the
    timer — but entering it does not add another failure either.

    Order matters: the master password (APP_PASSWORD) is checked first and is
    exempt from the user-key sharing ban; user keys from access_passwords.json
    go through check_user_password (which enforces the IP+device anti-sharing
    rule). Anything else is a wrong password.

    The device ref is NOT part of the password decision: the ref only keys
    the lockout bookkeeping. Requiring it here too meant that visitors with
    cookies/localStorage blocked (strict Safari settings, some privacy
    browsers/extensions) had the CORRECT password rejected — they then burned
    five tries into a cooldown and could never get in at all.
    """
    if not candidate:
        return "cooldown" if cooldown_remaining(ref) > 0 else "wrong"
    if cooldown_remaining(ref) > 0:
        return "cooldown"
    expected = _expected_password()
    if expected and _hash(candidate) == _hash(expected):
        reset_attempts(ref)
        return "ok"
    if user_password_status(candidate) == "user":
        result = check_user_password(candidate, ref, ip)
        if result == "ok":
            reset_attempts(ref)
        elif result == "banned":
            return "banned"  # no failure counting: the key itself is dead
        else:
            record_failure(ref)
        return result
    if not expected:
        # Fail closed: the owner has not configured APP_PASSWORD and the
        # candidate is not a user key either. Never treat this as a wrong
        # password (no attempt burning) and never let it pass.
        return "unconfigured"
    record_failure(ref)
    return "wrong"


def lockdown() -> None:
    """Sign the current session back out (Forget this device)."""
    try:
        import streamlit as st

        st.session_state["gate_unlocked"] = False
    except Exception:
        pass


# --- persistent unlock ----------------------------------------------------- #
# A browser refresh starts a brand-new Streamlit session, so the session flag
# alone would re-ask for the password after every refresh. Unlocked device ids
# are therefore remembered server-side (never with the password itself).


def remember_unlock(ref: str | None) -> None:
    """Mark this visitor unlocked for the next ``_UNLOCK_DAYS`` days.

    When the device id is not known yet (first visit, before the mint reload),
    the User-Agent fallback key is used so the post-reload session — which has
    a fresh Streamlit session but the same browser — stays unlocked too.
    """
    key = _ref_key(ref)
    try:
        with _STATE_LOCK:
            state = _load_unlocks()
            state[key] = time.time() + _UNLOCK_DAYS * 86400
            target = _unlock_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.replace(tmp_name, target)
    except Exception:
        pass  # the session flag still unlocks this visit


def _load_unlocks() -> dict:
    try:
        data = json.loads(_unlock_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def forget_unlock(ref: str | None) -> None:
    """Drop the remembered unlock (Forget this device), both key flavours."""
    try:
        with _STATE_LOCK:
            state = _load_unlocks()
            keys = {_ref_key(ref)}
            if ref:
                keys.add(_ref_key(None))  # also drop a UA-fallback unlock
            if keys & set(state):
                for key in keys:
                    state.pop(key, None)
                target = _unlock_path()
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(state, handle)
                os.replace(tmp_name, target)
    except Exception:
        pass


def is_unlocked(ref: str | None) -> bool:
    """Session flag, a remembered unlock, or neither."""
    try:
        import streamlit as st

        if st.session_state.get("gate_unlocked", False):
            return True
    except Exception:
        pass
    if not ref:
        return False
    state = _load_unlocks()
    now = time.time()
    if float(state.get(ref, 0) or 0) > now:
        return True
    # A first visit unlocks under the UA fallback (device id unknown at that
    # moment); honor it so the mint reload does not re-ask the password.
    return float(state.get(_ref_key(None), 0) or 0) > now
