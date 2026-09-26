#!/usr/bin/env python3
"""Split catalog: keep Discord links out of the public release asset.

The public catalog (`rbx_scout.db` asset on the rolling release) is
downloadable by anyone — the hosted dashboard fetches it anonymously, so it
cannot hold secrets. But the catalog's value to the owner is the resolved
Discord contacts. The split:

  * **Public copy** — everything EXCEPT the `discord_url` values. The
    `has_discord` flag, coverage counts, "With Discord" filter and the
    sidebar chip keep working for the world; nobody learns WHICH invite a
    game has.
  * **Contacts bundle** — a compact, encrypted sidecar file mapping
    universe_id -> discord_url (plus revival-archive rows for games that no
    longer exist in the live table), uploaded as a second release asset
    (`contacts.bundle`). Decryption requires CONTACTS_KEY — a key only the
    owner's deployments hold (laptop: gitignored `contacts.key`;
    Streamlit: the `CONTACTS_KEY` secret; Actions: a repository secret).

Crypto: stdlib-only (no third-party deps — Streamlit Cloud installs exactly
what requirements.txt pins). AES-CTR is not in the stdlib, so the bundle
uses HMAC-SHA256 in counter mode (HMAC acting as a PRF over a per-file
nonce + block counter, mirroring NIST SP 800-108 counter mode) as a stream
cipher, plus HMAC verification over the whole payload before any plaintext
is used. `secrets.token_bytes` and `hmac.compare_digest` are the CSPRNG and
constant-time comparison. Not AES, but for deterring catalog-scrapers (the
key never leaves the owner's machines) it is sound construction: a forged
or bit-flipped bundle fails the tag check before decryption.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
KEY_PATH = APP_DIR / "contacts.key"

# Asset name for the encrypted sidecar on the catalog-latest release.
ASSET_BUNDLE = "contacts.bundle"
# Bundle format version; bump when the payload layout changes.
BUNDLE_VERSION = 1

# Columns copied from game_analytics into the bundle.
CONTACT_COLUMNS = (
    "has_discord",
    "discord_url",
    "status",
    "found_via",
    "has_social_links",
    "contacts_checked_at",
)

# "No verdict" tombstones: rows whose only interesting bit is that they were
# checked and found empty. Keeping them in the bundle preserves the checked
# state through apply/restore; they add ~nothing to the size.
_TOMBSTONE = {"has_discord": False, "discord_url": None, "status": "No Contact Found"}


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------

def generate_key() -> bytes:
    """32 random bytes (256-bit key)."""
    return secrets.token_bytes(32)


def write_key(path: Path = KEY_PATH, key: bytes | None = None) -> bytes:
    """Write the key as hex with NO trailing newline (read back byte-exact)."""
    key = key or generate_key()
    path.write_text(key.hex())
    return key


def load_key() -> bytes | None:
    """The deployment's key: contacts.key (laptop), then CONTACTS_KEY env /
    Streamlit secret (hosted + Actions). None = no key available."""
    try:
        if KEY_PATH.exists():
            text = KEY_PATH.read_text().strip()
            if text:
                return bytes.fromhex(text)
    except (OSError, ValueError):
        pass
    candidates = (
        os.environ.get("CONTACTS_KEY", ""),
        _streamlit_secret("CONTACTS_KEY"),
    )
    for text in candidates:
        try:
            if text:
                return bytes.fromhex(text.strip())
        except ValueError:
            continue
    return None


def _streamlit_secret(name: str) -> str:
    """Read a Streamlit secret when running inside Streamlit (never raises,
    never imports streamlit outside a Streamlit runtime)."""
    try:
        import streamlit as st

        value = st.secrets.get(name)  # type: ignore[attr-defined]
        return str(value) if value else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Stream cipher: HMAC-SHA256 counter mode + whole-payload authentication
# ---------------------------------------------------------------------------

def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC(key, nonce || counter) blocks concatenated to `length` bytes."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def encrypt_bytes(key: bytes, plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(16)
    stream = _keystream(key, nonce, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    return nonce + tag + ciphertext


def decrypt_bytes(key: bytes, blob: bytes) -> bytes:
    """Decrypt + verify. Raises ValueError on any tampering/wrong key —
    the tag is checked in constant time BEFORE the plaintext is used."""
    if len(blob) < 16 + 32:
        raise ValueError("bundle too short to be valid")
    nonce, tag, ciphertext = blob[:16], blob[16:48], blob[48:]
    expect = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        raise ValueError("bundle authentication failed (wrong key or corrupted)")
    stream = _keystream(key, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))


# ---------------------------------------------------------------------------
# Bundle build / apply
# ---------------------------------------------------------------------------

def _existing_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Columns the table actually has (older/minimal catalogs may lack some)."""
    return {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})")
    }


def build_bundle_payload(db_path: Path) -> dict:
    """Collect every contact verdict from the live table + revival archive.

    Defensive: a table missing some contact columns (or missing entirely)
    contributes only the columns it has — a stats-style degrade, never an
    error, so a schema surprise can never block a push."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows: dict = {}
        archive: dict = {}
        for key, table in (("rows", "game_analytics"), ("archive", "contact_archive")):
            try:
                present = [c for c in CONTACT_COLUMNS
                           if c in _existing_columns(conn, table)]
                if not present:
                    continue
                cols = ", ".join(present)
                target = rows if key == "rows" else archive
                for row in conn.execute(f"SELECT universe_id, {cols} FROM {table}"):
                    uid, values = row[0], row[1:]
                    target[str(int(uid))] = {c: v for c, v in zip(present, values)}
            except sqlite3.Error:
                continue  # e.g. no contact_archive table yet
    finally:
        conn.close()
    return {"version": BUNDLE_VERSION, "rows": rows, "archive": archive}


def encrypt_bundle(key: bytes, payload: dict) -> bytes:
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return encrypt_bytes(key, plaintext)


def decrypt_bundle(key: bytes, blob: bytes) -> dict:
    payload = json.loads(decrypt_bytes(key, blob).decode("utf-8"))
    if not isinstance(payload, dict) or int(payload.get("version", 0)) != BUNDLE_VERSION:
        raise ValueError("unsupported bundle version")
    return payload


def apply_bundle(db_path: Path, payload: dict) -> tuple[int, int]:
    """Write bundle rows back into a catalog. Returns (rows, archive_rows).

    Contact columns missing from the target table are added on the fly
    (self-heal, mirroring scout_core's CREATE-if-missing pattern), so a
    restore always succeeds against any catalog generation."""
    rows = payload.get("rows") or {}
    archive = payload.get("archive") or {}
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            for table, data in (("game_analytics", rows), ("contact_archive", archive)):
                if not data:
                    continue
                existing = _existing_columns(conn, table)
                for column in CONTACT_COLUMNS:
                    if column not in existing:
                        try:
                            conn.execute(
                                f"ALTER TABLE {table} ADD COLUMN {column} "
                                + ("TIMESTAMP" if column == "contacts_checked_at"
                                   else "BOOLEAN" if column in ("has_discord", "has_social_links")
                                   else "TEXT")
                            )
                        except sqlite3.Error:
                            pass
                # Upsert only the contact columns; never touch other fields.
                assignments = ", ".join(f"{c}=excluded.{c}" for c in CONTACT_COLUMNS)
                placeholders = ", ".join("?" for _ in CONTACT_COLUMNS)
                conn.executemany(
                    f"INSERT INTO {table} (universe_id, {', '.join(CONTACT_COLUMNS)}) "
                    f"VALUES (?, {placeholders}) "
                    f"ON CONFLICT(universe_id) DO UPDATE SET {assignments}",
                    [
                        (int(uid), *(rec.get(c) for c in CONTACT_COLUMNS))
                        for uid, rec in data.items()
                    ],
                )
    finally:
        conn.close()
    return len(rows), len(archive)


# ---------------------------------------------------------------------------
# Public-copy stripping
# ---------------------------------------------------------------------------

def strip_public_copy(source_db: Path, public_db: Path) -> int:
    """Write `public_db` = `source_db` with discord_url values removed.

    Preserves every other column (has_discord, coverage timestamps, tiers,
    ccu history, the revival archive) so the public catalog keeps its
    counts, filters, and chip. Returns the number of link values removed.
    """
    import shutil

    public_db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source_db) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(public_db) + suffix))
    shutil.copy2(source_db, public_db)
    conn = sqlite3.connect(str(public_db))
    removed = 0
    try:
        with conn:
            if "discord_url" in _existing_columns(conn, "game_analytics"):
                cur = conn.execute("UPDATE game_analytics SET discord_url = NULL "
                                   "WHERE discord_url IS NOT NULL")
                removed = int(cur.rowcount or 0)
            try:
                if "discord_url" in _existing_columns(conn, "contact_archive"):
                    conn.execute("UPDATE contact_archive SET discord_url = NULL")
            except sqlite3.Error:
                pass
            # Descriptions are raw Roblox text and devs often type raw
            # invites into them — that would leak a link outside discord_url.
            try:
                if "description" in _existing_columns(conn, "game_analytics"):
                    conn.execute(
                        "UPDATE game_analytics SET description = NULL "
                        "WHERE description LIKE '%discord.gg%' "
                        "OR description LIKE '%discord.com/invite%'"
                    )
            except sqlite3.Error:
                pass
        conn.execute("VACUUM")
    finally:
        conn.close()
    return removed


if __name__ == "__main__":
    import sys

    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if cmd == "genkey":
        if KEY_PATH.exists():
            raise SystemExit(
                f"{KEY_PATH.name} already exists — refusing to overwrite it "
                "(every deployment decrypts with it). Delete it by hand first "
                "if you really mean to rotate the key."
        )
        write_key()
        print(f"key written to {KEY_PATH.name} (gitignored). Add the same hex "
              "value as CONTACTS_KEY in Streamlit secrets and as a GitHub "
              "Actions repository secret.")
    else:
        print(__doc__)
        raise SystemExit(2)
