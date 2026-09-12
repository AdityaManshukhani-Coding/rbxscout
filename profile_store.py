"""Per-device profile persistence for Studio Scouts.

Each browser gets an opaque device id delivered as the ``ss_ref`` cookie. The
server keeps one small JSON profile per id in a gitignored directory: identity
fields (Discord name / User ID), targets, message template, onboarding
progress, and the session-only Roblox cookie. This is what lets a refresh or
an accidental back-navigation restore the dashboard instead of restarting the
welcome flow.

The store is deliberately dumb and local: atomic JSON writes, no locks, no
network. On Streamlit Community Cloud the directory lives with the app
container, so its lifetime matches the deployment; the ``ss_ref`` cookie in
the browser outlives it (Max-Age 1 year) and the app simply re-seeds a fresh
profile on the next visit.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

_DEFAULT_DIR = Path(__file__).resolve().parent / ".profiles"


def _dir() -> Path:
    """Profile directory (env-overridable so tests can point at tmp_path)."""
    return Path(os.environ.get("SS_PROFILE_DIR", str(_DEFAULT_DIR)))


def _path(ref: str) -> Path:
    safe = "".join(ch for ch in ref if ch.isalnum() or ch in "-_")[:80]
    if not safe:
        raise ValueError("empty device reference")
    return _dir() / f"{safe}.json"


def load_profile(ref: str) -> Dict[str, Any]:
    """Return the stored profile dict, or {} when missing/unreadable."""
    try:
        return dict(json.loads(_path(ref).read_text(encoding="utf-8")))
    except Exception:
        return {}


def save_profile(ref: str, profile: Dict[str, Any]) -> None:
    """Atomically write the profile; never raises to the caller."""
    try:
        target = _path(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(profile)
        payload["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        # Persistence is a convenience; the app must run without it.
        pass


def clear_profile(ref: str) -> None:
    """Delete the profile (Forget this device); never raises."""
    try:
        _path(ref).unlink(missing_ok=True)
    except Exception:
        pass
