#!/usr/bin/env python3
"""Generate 100 random access passwords for Studio Scouts.

Writes TWO artifacts:

* ``access_passwords.json`` (committed) — SHA-256 hashes only, read by
  gate.py at sign-in. The deployment never needs the plaintexts.
* ``access_passwords.txt`` (NEVER committed) — the plaintext list, one per
  line. This is the copy that goes into your private Google Doc, next to
  each friend's name. Kept out of git via .gitignore.

Format: 3 pronounceable chunks + digits + a symbol, e.g. ``Vok4#Rani#Zume42``
— easy to read aloud when assigning one to a classmate, still 20+ chars of
entropy. Deterministic (seeded) so re-runs don't rotate the whole list; a
seed can be overridden with ``--seed``.
"""

import argparse
import hashlib
import json
import random
import secrets
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SALT = "rbxscout-access-v1:"
COUNT = 100

CHUNKS = [
    "Bam", "Vok", "Rani", "Zume", "Kafi", "Trex", "Mola", "Pynk", "Duso", "Grovi",
    "Hark", "Jolt", "Kwim", "Lupo", "Mnex", "Nobe", "Pyxa", "Quor", "Rivi", "Swole",
    "Tavo", "Umbra", "Vexa", "Wilo", "Xeno", "Yara", "Zeph", "Brix", "Clyde", "Dovu",
    "Enzo", "Fyra", "Glip", "Huxo", "Ivex", "Jynk", "Kryo", "Lume", "Miro", "Nyxo",
    "Orie", "Prax", "Quen", "Rozo", "Scal", "Trix", "Uvro", "Vant", "Wexl", "Yuko",
    "Zado", "Blip", "Crxo", "Daxo", "Fume", "Gawo", "Hexa", "Jixo", "Kova", "Lynx",
    "Mozo", "Nuvo", "Opyx", "Paxo", "Rume", "Sylo", "Tazo", "Vuko", "Wyno", "Zexi",
]
SYMBOLS = "#$%&@!?*+=-"


def make_password(rng: random.Random) -> str:
    parts = [rng.choice(CHUNKS), rng.choice(CHUNKS), rng.choice(CHUNKS)]
    body = "".join(p + (rng.choice("0123456789") if rng.random() < 0.45 else "") for p in parts)
    sym = rng.choice(SYMBOLS)
    tail = str(rng.randint(10, 99))
    return f"{sym.join((body[: len(body) // 2], body[len(body) // 2 :]))}{tail}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260922)
    args = parser.parse_args()
    rng = random.Random(args.seed)

    passwords: list[str] = []
    seen: set[str] = set()
    while len(passwords) < COUNT:
        pw = make_password(rng)
        if pw in seen:
            continue
        seen.add(pw)
        passwords.append(pw)

    manifest = {
        "note": "SHA-256(salt + password) hashes for gate.py user access keys. "
        "The plaintext list lives with the owner (access_passwords.txt / private doc) — never commit it.",
        "salt_marker": SALT,
        "count": len(passwords),
        "passwords": [hashlib.sha256((SALT + pw).encode("utf-8")).hexdigest() for pw in passwords],
    }
    (APP_DIR / "access_passwords.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    (APP_DIR / "access_passwords.txt").write_text("\n".join(passwords) + "\n", encoding="utf-8")
    print(f"wrote {len(passwords)} hashes -> access_passwords.json (committed)")
    print(f"wrote {len(passwords)} plaintexts -> access_passwords.txt (gitignored, paste into your doc)")
    print("first three:", ", ".join(passwords[:3]))


if __name__ == "__main__":
    main()
