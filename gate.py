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


def check_password(candidate: str, ref: str | None) -> str:
    """Evaluate one unlock attempt; returns ``ok``/``cooldown``/``wrong``/``unconfigured``.

    Like iOS: while cooling down, even the correct password cannot skip the
    timer — but entering it does not add another failure either.

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
    if not expected:
        # Fail closed: the owner has not configured APP_PASSWORD. Never treat
        # this as a wrong password (no attempt burning) and never let it pass.
        return "unconfigured"
    if _hash(candidate) != _hash(expected):
        record_failure(ref)
        return "wrong"
    reset_attempts(ref)
    return "ok"


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
