#!/usr/bin/env python3
"""Generate one-time (burn-after-use) trial access passwords for gate.py.

The owner hands a temp key to someone who just wants to try the tool; the
FIRST successful sign-in burns the key, and any later login with the same
plaintext is refused with "this key is one-time and already used".

Artifacts (same split as generate_access_passwords.py):

* ``temp_access_passwords.json`` (committed) — SHA-256 hashes only, read by
  gate.py at sign-in. The deployment never needs the plaintexts.
* ``temp_access_passwords.txt`` (NEVER committed) — the plaintext list,
  one per line. Keep it private (e.g. the same Google Doc as the standing
  keys), cross out lines as you hand them out.

Random-backed (secrets), NOT seeded: every run produces a fresh batch, so
re-running rotates the trial set. Passwords follow the same pronounceable
shape as the standing keys so they read aloud just as easily.

They expense nothing from the standing 100-key set: the gate checks the
master password, then user keys, then temp keys, in that order.
"""

import argparse
import hashlib
import json
import random
import secrets
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SALT = "rbxscout-temp-v1:"
COUNT = 15

CHUNKS = [
    "Bam", "Vok", "Rani", "Zume", "Kafi", "Trex", "Mola", "Pynk", "Duso", "Grovi",
    "Hark", "Jolt", "Kwim", "Lupo", "Mnex", "Nobe", "Pyxa", "Quor", "Rivi", "Swole",
    "Tavo", "Umbra", "Vexa", "Wilo", "Xeno", "Yara", "Zeph", "Brix", "Clyde", "Dovu",
    "Enzo", "Fyra", "Glip", "Huxo", "Ivex", "Jynk", "Kryo", "Lume", "Miro", "Nyxo",
    "Orie", "Prax", "Quen", "Rozo", "Scal", "Trix", "Uvro", "Vant", "Wexl", "Yuko",
    "Zado", "Blip", "Crxo", "Daxo", "Fume", "Gawo", "Hexa", "Jixo", "Kova", "Lynx",
    "Mozo", "Nuvo", "Opyx", "Paxo", "Rume", "Sylo", "Tazo", "Vuko", "Wyno", "Zexi",
]
SYMBOLS = "#@$%&!?"   # subset without the ones users most often misread in chat


def make_password() -> str:
    """One pronounceable trial key, e.g. ``Kafi#Zume9#Rani37``."""
    rng = random.SystemRandom()
    parts = [rng.choice(CHUNKS) for _ in range(3)]
    body = "".join(p + (str(rng.randint(0, 9)) if rng.random() < 0.45 else "") for p in parts)
    middle = len(body) // 2
    sym = rng.choice(SYMBOLS)
    return f"{sym.join((body[:middle], body[middle:]))}{rng.randint(10, 99)}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=COUNT)
    args = parser.parse_args()

    passwords: list[str] = []
    seen: set[str] = set()
    while len(passwords) < args.count:
        pw = make_password()
        if pw in seen:
            continue
        seen.add(pw)
        passwords.append(pw)

    manifest = {
        "note": "SHA-256(salt + password) hashes for gate.py ONE-TIME trial keys. "
        "Burned on first successful sign-in by check_temp_password. The plaintext "
        "list lives with the owner (temp_access_passwords.txt) — never commit it.",
        "salt_marker": SALT,
        "count": len(passwords),
        "kind": "one-time",
        "passwords": [hashlib.sha256((SALT + pw).encode("utf-8")).hexdigest() for pw in passwords],
    }
    (APP_DIR / "temp_access_passwords.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    (APP_DIR / "temp_access_passwords.txt").write_text("\n".join(passwords) + "\n", encoding="utf-8")
    print(f"wrote {len(passwords)} hashes -> temp_access_passwords.json (committed)")
    print(f"wrote {len(passwords)} plaintexts -> temp_access_passwords.txt (gitignored, hand out one per person)")
    print("first three:", ", ".join(passwords[:3]))


if __name__ == "__main__":
    main()
