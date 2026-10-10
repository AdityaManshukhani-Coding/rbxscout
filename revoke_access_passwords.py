#!/usr/bin/env python3
"""Terminate access passwords by plaintext: they stop working for everyone.

    .venv/bin/python revoke_access_passwords.py 'WynoRan=i2Vexa336' ...

Only hashes are stored: the plaintext goes on your command line (shell
history — that's between you and your machine), and the state file + the
committed manifest each record just the SHA-256 hash plus WHY the key died.

Two enforcement layers, so termination survives either half of a deploy:

* gate state file (``.access_users.json`` — synced with the gate state dir):
  the key is flagged ``terminated`` and future logins answer "terminated by
  the owner" instantly, no attempt burning.
* committed manifest (``access_passwords.json``): the key's hash is removed
  and appended to ``terminated_hashes`` for the audit trail. Deployments
  with a fresh state file reject the plaintext as wrong (it no longer
  matches); deployments that still carry the OLD manifest with the CURRENT
  state file reject it as terminated. Either way: dead.

No argument terminates nothing — pass the keys explicitly.

Also prints how many remembered unlocks were revoked (the flagged keys'
devices are kicked back to the password screen next load).

To verify without touching anything:

    .venv/bin/python revoke_access_passwords.py --check 'A#Key#You47'
"""

import argparse
import hashlib
import json
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SALT = "rbxscout-access-v1:"

sys.path.insert(0, str(APP_DIR))
import gate  # noqa: E402


def status(candidate: str) -> str:
    """What does the gate currently think of this key (no state change)?"""
    key = hashlib.sha256((SALT + candidate).encode("utf-8")).hexdigest()
    if key in gate._load_user_hashes():
        state = gate._load_user_state().get(key, {})
        if isinstance(state, dict) and state.get("terminated"):
            return "already terminated (manifest entry flagged)"
        return "active — still unlocks the app"
    manifest_raw = json.loads((APP_DIR / "access_passwords.json").read_text(encoding="utf-8")) if (APP_DIR / "access_passwords.json").exists() else {}
    if key in [str(h).lower() for h in (manifest_raw.get("terminated_hashes") or [])]:
        return "terminated (hash removed from manifest)"
    return "not a user key"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("keys", nargs="*", help="access password plaintexts to terminate")
    parser.add_argument("--check", action="append", default=[], metavar="KEY",
                        help="report a key's status without changing anything (repeatable)")
    args = parser.parse_args()

    for candidate in args.check:
        print(f"status: {candidate} -> {status(candidate)}")

    if not args.keys:
        if args.check:
            return 0
        parser.error("pass the plaintext keys to terminate (or --check to just look)")

    # guard.revoked_unlocks lives in gate.py's module namespace; keep the
    # count accurate through gate's own helpers rather than reimplementing.
    with gate._STATE_LOCK:
        before = set(gate._load_unlocked_refs())
    hit = gate.terminate_user_passwords(args.keys)
    with gate._STATE_LOCK:
        after = set(gate._load_unlocked_refs())
    revoked = before - after
    for key in hit:
        print(f"terminated: {key}")
    for pt in args.keys:
        if pt not in hit:
            print(f"NOT FOUND in the user-key manifest (no action taken): {pt}")
    print(f"remembered unlocks revoked for {len(revoked)} device(s)")
    return 0 if hit else 1


if __name__ == "__main__":
    raise SystemExit(main())
