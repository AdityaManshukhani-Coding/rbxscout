#!/usr/bin/env python3
"""Outreach: resolve Discord invites, build authorize links, DM server owners
via a real Discord bot application, detect replies, keep a local ledger.

The automation split (on purpose):
  * EVERYTHING except one step is code: resolution, authorize-link building,
    owner lookup, DM sending, reply polling, logging.
  * THE ONE human step: clicking the OAuth "Add bot" link, once per server.
    A bot cannot accept a discord.gg invite through the API — Discord only
    lets a server admin authorize a bot from the OAuth page. Automating that
    click with a user account is self-botting (bannable) and out of scope.

Pacing: sends are spaced by a simple minimum-interval pacer so the bot never
bursts. A Discord rate limit is a queue pause, never an account problem.

Usage:
    python outreach.py resolve                     # catalog links -> guild ids
    python outreach.py links                       # print pending authorize links
    python outreach.py run --once                  # one runner tick
    python outreach.py run                         # loop forever (Ctrl+C exits)
    python outreach.py status                      # ledger summary
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

APP_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# Config — all env overridable, all defaults safe
# --------------------------------------------------------------------------- #

OUTREACH_DB = APP_DIR / "outreach.sqlite"          # the ledger
CATALOG_DB = APP_DIR / "rbx_scout.db"              # the existing catalog
TOKEN_FILE = APP_DIR / "discord_bot.token"         # gitignored

REST = "https://discord.com/api/v10"

# DM copy — [Game Name] is filled from the ledger row.
DEFAULT_MESSAGE = (
    "Hey, I really like what you're doing with [Game Name] and I was hoping "
    "you're interested on hearing an offer for it!"
)

# Pacing (seconds). Conservative by design; tune up, never down.
SEND_GAP_SECONDS = 75          # between two DM sends
BATCH_PAUSE_SECONDS = 300      # after every 10 sends
REPLY_POLL_GAP_SECONDS = 20    # between reply polls
MAX_SENDS_PER_RUN = 40         # loop safety; also a daily-ish ceiling

# A DM send that fails with these codes is terminal for that row.
_BLOCK_DM_CODES = {50007}      # "Cannot send messages to this user"


def _bot_token() -> str | None:
    """Token from env or the gitignored token file. None = unconfigured."""
    tok = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if tok:
        return tok
    try:
        tok = TOKEN_FILE.read_text().strip()
        return tok or None
    except OSError:
        return None


def load_game_name_overrides(path: Path) -> dict[str, str]:
    """Optional universe_id -> display-name map (for the test server row).

    Live catalog attribution normally names the game; the test-server row is
    keyed to a guild, not a universe, so it needs its own mapping.
    """
    try:
        return {str(k).strip(): str(v) for k, v in
                json.loads(path.read_text()).items()}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Ledger (sqlite)
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach (
    universe_id    INTEGER PRIMARY KEY,
    guild_id       TEXT,
    guild_name     TEXT,
    invite_code    TEXT,
    game_name      TEXT,
    owner_id       TEXT,
    owner_name     TEXT,
    status         TEXT NOT NULL DEFAULT 'queued',
    last_error     TEXT,
    last_message   TEXT,
    replied_text   TEXT,
    authorize_url  TEXT,
    sent_at        TEXT,
    replied_at     TEXT,
    updated_at     TEXT
);
"""


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    # Resolve the ledger path at CALL time, not def time — tests swap
    # outreach.OUTREACH_DB for a temp path and must be honored.
    if db_path is None:
        db_path = OUTREACH_DB
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _upsert(conn: sqlite3.Connection, row: dict) -> None:
    cols = list(row.keys())
    conn.execute(
        f"INSERT INTO outreach ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT(universe_id) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "universe_id"),
        list(row.values()),
    )


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM outreach GROUP BY status")}


# --------------------------------------------------------------------------- #
# Discord REST (requests — already the repo's HTTP client)
# --------------------------------------------------------------------------- #

def _headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}"}


def discord_get(url: str, token: str | None = None) -> requests.Response:
    headers = _headers(token) if token else {}
    return requests.get(url, headers=headers, timeout=15)


def resolve_invite(code: str) -> dict | None:
    """Anonymous invite resolution. Returns {guild_id, guild_name} or None."""
    try:
        r = discord_get(f"{REST}/invites/{quote(code)}?with_counts=true")
    except requests.RequestException:
        return None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        return None
    j = r.json()
    guild = j.get("guild") or {}
    if not guild.get("id"):
        return None
    return {"guild_id": guild["id"], "guild_name": guild.get("name", "")}


def get_guild(token: str, guild_id: str) -> dict | None:
    r = discord_get(f"{REST}/guilds/{guild_id}", token=token)
    return r.json() if r.status_code == 200 else None


def create_dm_channel(token: str, user_id: str) -> dict | None:
    r = requests.post(f"{REST}/users/@me/channels",
                      headers=_headers(token), json={"recipient_id": user_id},
                      timeout=15)
    return r.json() if r.status_code in (200, 201) else None


def send_dm(token: str, recipient_id: str, text: str) -> tuple[bool, str]:
    """Returns (ok, detail). detail is the reply text on ok, error text not."""
    channel = create_dm_channel(token, recipient_id)
    if not channel or not channel.get("id"):
        return False, "could not open DM channel"
    r = requests.post(f"{REST}/channels/{channel['id']}/messages",
                      headers=_headers(token), json={"content": text},
                      timeout=15)
    if r.status_code in (200, 201):
        return True, r.json().get("id", "")
    try:
        code = r.json().get("code")
    except Exception:
        code = None
    if code == 50007:
        return False, "owner has DMs closed"
    if r.status_code == 429:
        return False, "rate limited"
    return False, f"HTTP {r.status_code}"


def last_message_in_dm(token: str, recipient_id: str) -> dict | None:
    channel = create_dm_channel(token, recipient_id)
    if not channel or not channel.get("id"):
        return None
    r = discord_get(f"{REST}/channels/{channel['id']}/messages?limit=1",
                    token=token)
    if r.status_code != 200 or not r.json():
        return None
    first = r.json()[0]
    return {
        "id": first["id"],
        "author_id": first["author"]["id"],
        "author_bot": bool(first["author"].get("bot")),
        "content": first.get("content", ""),
    }


# --------------------------------------------------------------------------- #
# Authorize links
# --------------------------------------------------------------------------- #

def build_authorize_url(client_id: str, guild_id: str, state: str = "") -> str:
    params = {"client_id": client_id, "scope": "bot", "permissions": 0}
    if guild_id:
        params["guild_id"] = guild_id
    if state:
        params["state"] = state
    return f"https://discord.com/oauth2/authorize?{urlencode(params)}"


def bot_client_id(token: str) -> str | None:
    r = discord_get(f"{REST}/users/@me", token=token)
    return r.json().get("id") if r.status_code == 200 else None


# --------------------------------------------------------------------------- #
# Step 1 — resolve catalog links into the ledger
# --------------------------------------------------------------------------- #

def sync_catalog_edges(limit: int = 200) -> dict:
    """For every catalog row with a discord link, ensure a ledger row exists.

    Reads discord_url straight from the catalog (the laptop copy has the
    decrypted contacts thanks to contacts.key). Resolution is anonymous
    (no token), so this step is safe to run any time; retries are cheap.
    """
    if not CATALOG_DB.exists():
        return {"error": "catalog missing — run db_sync.py pull first"}

    out_conn = _connect()
    init_db(out_conn)
    existing = {r[0] for r in out_conn.execute("SELECT universe_id FROM outreach")}
    out_conn.close()

    cat = sqlite3.connect(f"file:{CATALOG_DB}?mode=ro", uri=True)
    cat.row_factory = sqlite3.Row
    try:
        rows = cat.execute(
            "SELECT universe_id, name, discord_url FROM game_analytics "
            "WHERE has_discord = 1 AND discord_url IS NOT NULL "
            "AND universe_id NOT IN (%s) LIMIT ?" % (
                ",".join("?" * len(existing)) if existing else "SELECT 0"),
            (*existing, limit)).fetchall()
    finally:
        cat.close()

    added, dead, errors = 0, 0, 0
    out_conn = _connect()
    init_db(out_conn)
    try:
        for row in rows:
            uid, name, url = row["universe_id"], row["name"], row["discord_url"]
            code = (url or "").rstrip("/").rsplit("/", 1)[-1]
            if not code:
                continue
            info = resolve_invite(code)
            if info is None:
                dead += 1
                _upsert(out_conn, {
                    "universe_id": uid, "invite_code": code, "game_name": name,
                    "status": "dead_invite", "last_error": "invite unresolved",
                    "updated_at": _now()})
            else:
                added += 1
                _upsert(out_conn, {
                    "universe_id": uid, "guild_id": info["guild_id"],
                    "guild_name": info["guild_name"], "invite_code": code,
                    "game_name": name, "status": "queued",
                    "authorize_url": "", "updated_at": _now()})
            out_conn.commit()
            time.sleep(0.4)  # pace the anonymous resolver too
    finally:
        out_conn.close()
    return {"added": added, "dead": dead, "errors": errors}


def add_manual_row(universe_id: int, invite_code_or_url: str,
                   game_name: str = "Test server") -> str:
    """Queue a single invite by hand (used for the test server)."""
    code = invite_code_or_url.rstrip("/").rsplit("/", 1)[-1]
    info = resolve_invite(code)
    conn = _connect()
    init_db(conn)
    try:
        if info:
            _upsert(conn, {"universe_id": universe_id,
                           "guild_id": info["guild_id"],
                           "guild_name": info["guild_name"],
                           "invite_code": code, "game_name": game_name,
                           "status": "queued", "authorize_url": "",
                           "updated_at": _now()})
            conn.commit()
            return f"queued guild {info['guild_id']} ({info['guild_name']})"
        _upsert(conn, {"universe_id": universe_id, "invite_code": code,
                       "game_name": game_name, "status": "dead_invite",
                       "last_error": "invite unresolved", "updated_at": _now()})
        conn.commit()
        return "invite did not resolve (expired?)"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Step 2 — runner: authorize check, owner lookup, DM, reply poll
# --------------------------------------------------------------------------- #

def runner_tick(token: str, once: bool = False) -> dict:
    """One pass: fills authorize URLs, sends queued DMs, polls for replies."""
    conn = _connect()
    init_db(conn)
    summary = {"authorize_filled": 0, "joined": 0, "sent": 0,
               "blocked": 0, "replied": 0, "errors": 0}
    try:
        cid = bot_client_id(token)
        if not cid:
            summary["errors"] += 1
            return summary

        # -- a) fill authorize links for every queued row --------------------
        for row in conn.execute(
                "SELECT universe_id, guild_id FROM outreach WHERE status='queued' "
                "AND guild_id IS NOT NULL"):
            url = build_authorize_url(cid, row["guild_id"])
            conn.execute("UPDATE outreach SET authorize_url=?, updated_at=? "
                         "WHERE universe_id=?",
                         (url, _now(), row["universe_id"]))
            summary["authorize_filled"] += 1
        conn.commit()

        # -- b) rows whose guild the bot is ALREADY in (from a click past) ---
        joined_rows = []
        for row in conn.execute(
                "SELECT universe_id, guild_id FROM outreach WHERE status='queued' "
                "AND guild_id IS NOT NULL AND authorize_url != ''"):
            guild = get_guild(token, row["guild_id"])
            if guild:
                owner_id = str(guild.get("owner_id") or "")
                owner_name = _resolve_username(token, owner_id)
                conn.execute(
                    "UPDATE outreach SET owner_id=?, owner_name=?, "
                    "status='joined', updated_at=? WHERE universe_id=?",
                    (owner_id, owner_name, _now(), row["universe_id"]))
                summary["joined"] += 1
                joined_rows.append((row["universe_id"], owner_id, owner_name))
        conn.commit()

        # -- c) send the DM to every joined row (paced) ----------------------
        sends = 0
        for row in conn.execute(
                "SELECT universe_id, game_name, owner_id FROM outreach "
                "WHERE status='joined' LIMIT ?", (MAX_SENDS_PER_RUN,)):
            if sends >= MAX_SENDS_PER_RUN:
                break
            body = DEFAULT_MESSAGE.replace("[Game Name]",
                                           row["game_name"] or "your game")
            ok, detail = send_dm(token, row["owner_id"], body)
            sends += 1
            if ok:
                conn.execute(
                    "UPDATE outreach SET status='sent', last_message=?, "
                    "sent_at=?, updated_at=? WHERE universe_id=?",
                    (body, _now(), _now(), row["universe_id"]))
                summary["sent"] += 1
            elif detail == "owner has DMs closed":
                conn.execute(
                    "UPDATE outreach SET status='dmed_blocked', "
                    "last_error=?, updated_at=? WHERE universe_id=?",
                    (detail, _now(), row["universe_id"]))
                summary["blocked"] += 1
            elif detail == "rate limited":
                break  # stop this tick; next tick picks it up
            else:
                conn.execute(
                    "UPDATE outreach SET last_error=?, updated_at=? "
                    "WHERE universe_id=?",
                    (detail, _now(), row["universe_id"]))
                summary["errors"] += 1
            conn.commit()
            time.sleep(SEND_GAP_SECONDS)

        # -- d) poll 'sent' rows for owner replies ---------------------------
        for row in conn.execute(
                "SELECT universe_id, game_name, owner_id FROM outreach "
                "WHERE status='sent' LIMIT ?", (MAX_SENDS_PER_RUN,)):
            msg = last_message_in_dm(token, row["owner_id"])
            if not msg:
                continue
            if msg["author_id"] != str(cid) and not msg["author_bot"]:
                conn.execute(
                    "UPDATE outreach SET status='replied', replied_text=?, "
                    "replied_at=?, updated_at=? WHERE universe_id=?",
                    (msg["content"], _now(), _now(), row["universe_id"]))
                summary["replied"] += 1
                conn.commit()
            time.sleep(REPLY_POLL_GAP_SECONDS)
        conn.commit()
    finally:
        conn.close()
    return summary


def _resolve_username(token: str, user_id: str) -> str:
    if not user_id:
        return ""
    r = discord_get(f"{REST}/users/{user_id}", token=token)
    if r.status_code != 200:
        return ""
    j = r.json()
    return j.get("global_name") or j.get("username") or ""


def run_loop(token: str) -> None:
    print(f"outreach runner started; pacing {SEND_GAP_SECONDS}s/gap, "
          f"poll {REPLY_POLL_GAP_SECONDS}s. Ctrl+C to stop.")
    while True:
        t0 = time.time()
        try:
            print("tick:", runner_tick(token, once=True), flush=True)
        except Exception as exc:
            print("tick error (next tick retries):", exc, flush=True)
        time.sleep(max(10, 2 * REPLY_POLL_GAP_SECONDS) - (time.time() - t0) % 10)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S ", time.gmtime()) + "UTC"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["resolve", "links", "run", "status", "test"])
    p.add_argument("--once", action="store_true", help="run one tick, not a loop")
    p.add_argument("--universe", type=int, help="manual test row universe id")
    p.add_argument("--invite", help="manual test row invite code or url")
    args = p.parse_args()

    if args.cmd == "resolve":
        print(json.dumps(sync_catalog_edges(), indent=2))
    elif args.cmd == "links":
        token = _bot_token()
        cid = bot_client_id(token) if token else None
        conn = _connect()
        init_db(conn)
        for row in conn.execute(
                "SELECT game_name, guild_name, authorize_url FROM outreach "
                "WHERE status='queued'"):
            url = row["authorize_url"] or (
                build_authorize_url(cid, row["guild_id"]) if cid else None)
            print(f"- {row['game_name']} ({row['guild_name']}): {url}")
        conn.close()
        if not cid:
            print("\n(no token — authorize URLs need the bot token to build; "
                  "run 'python outreach.py run --once' after saving the token)")
    elif args.cmd == "run":
        token = _bot_token()
        if not token:
            sys.exit("no token: save the bot token in discord_bot.token "
                     "(gitignored) or export DISCORD_BOT_TOKEN")
        if args.once:
            print("tick:", runner_tick(token, once=True))
        else:
            run_loop(token)
    elif args.cmd == "status":
        conn = _connect()
        init_db(conn)
        print(json.dumps(status_counts(conn), indent=2))
        for row in conn.execute(
                "SELECT universe_id, game_name, status FROM outreach "
                "WHERE status='replied' ORDER BY replied_at DESC"):
            print(f"REPLIED: {row['game_name']} (universe {row['universe_id']})")
        conn.close()
    elif args.cmd == "test":
        if not args.invite:
            sys.exit("usage: outreach.py test --invite <code-or-url> "
                     "[--universe <id>]")
        uid = args.universe or -1
        print(add_manual_row(uid, args.invite,
                             args.universe and "Test server" or "Test server"))


if __name__ == "__main__":
    main()
