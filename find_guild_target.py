import os
import asyncio
import json
import urllib.parse
import socket
import ssl
import sys
import time
import uuid
import hashlib
import base64
import secrets

import discord as dc

INVITE_URL = "https://discord.gg/emekjt2PY"

# ---------------------------------------------------------------------------
# Important expectation check
# ---------------------------------------------------------------------------
# This script does NOT use the user/self-bot token as if it were a bot token.
# A plain user token cannot be used as the sole credential to "join like a bot".
#
# If the account cannot join manually, the realistic documented path for a user
# account is the OAuth2 authorization code flow:
#   1. open a browser to Discord's authorization page
#   2. the user completes login + consent in that browser
#   3. Discord redirects to a local callback with an authorization code
#   4. your backend exchanges that code for an access token
#
# Whether that is enough for this goal depends on what you actually need:
#   - if you only need to act as the authorizing user, an authorized user token
#     may let you read/write as that user through supported endpoints
#   - if you need the account to be *in* the target guild via an application,
#     the Add Guild Member flow (guilds.join) has strict prerequisites:
#       * the application's bot must already be in the target guild
#       * the bot needs CREATE_INSTANT_INVITE there
#       * the user must authorize with guilds.join
#       * the user's access token is sent in the body, authenticated with the
#         application's bot token
#     That is not an invite-code shortcut and not a way around bans, verification
#     levels, membership screening, member limits, or missing permissions.
# ---------------------------------------------------------------------------

# OAuth2 constants — fill these in from a real Discord application if you go
# the browser-login route.
CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "http://localhost:53134/oauth/callback")
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"


def _unchunk_all(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    if n >= 2 and data[0:2] == b"\r\n":
        i = 2
    while i < n:
        crlf = data.find(b"\r\n", i)
        if crlf == -1:
            break
        hex_line = data[i:crlf].decode(errors="replace").strip()
        i = crlf + 2
        try:
            size = int(hex_line, 16)
        except ValueError:
            break
        if size == 0:
            break
        end = i + size
        if end > n:
            break
        out.extend(data[i:end])
        i = end
        if i + 2 <= n and data[i:i + 2] == b"\r\n":
            i += 2
    return bytes(out)


def parse_invite_code(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.path.strip("/")


def raw_resolve_invite(code):
    base = "discord.com"
    path = f"/api/v10/invites/{code}"
    ctx = ssl.create_default_context()
    sock = socket.create_connection((base, 443), timeout=10)
    ssock = ctx.wrap_socket(sock, server_hostname=base)
    req = (
        "GET " + path + " HTTP/1.1\r\n"
        "Host: " + base + "\r\n"
        "Accept: application/json\r\n"
        "User-Agent: okhttp/4.12.0\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    ssock.sendall(req.encode())
    resp = b""
    while True:
        chunk = ssock.recv(16384)
        if not chunk:
            break
        resp += chunk
    he = resp.find(b"\r\n\r\n")
    if he == -1:
        return None, "no_header_boundary"
    post_headers = resp[he + 4:]
    body = _unchunk_all(post_headers)
    try:
        payload = json.loads(body.decode(errors="replace"))
        return payload, None
    except Exception as e:
        return None, f"json_parse_error: {e}; raw_sample={body[:200]!r}"


def build_authorize_url(scopes, state=None):
    if not CLIENT_ID:
        return None, "missing DISCORD_CLIENT_ID"
    if state is None:
        state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": " ".join(scopes),
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "prompt": "consent",
    }
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    return url, state


def build_token_request_body(code):
    return urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })


async def pick_rank_target(guild: dc.Guild):
    """Owner > highest admin by role position > highest top role."""
    if not guild or not guild.available:
        return None, "guild not available"

    diag = []
    if guild.owner_id:
        diag.append("owner_id_known " + str(guild.owner_id))

    # 1) owner
    if guild.owner_id:
        try:
            owner_member = guild.get_member(guild.owner_id)
            if owner_member is None:
                owner_member = await guild.fetch_member(guild.owner_id)
            if owner_member:
                return owner_member, "owner"
        except Exception as e:
            diag.append("owner_fetch_error " + str(e))

    # 2) highest administrator by role position
    try:
        admins = []
        async for m in guild.fetch_members(limit=None):
            if m.guild_permissions.administrator:
                admins.append(m)
        if admins:
            admins.sort(key=lambda m: m.top_role.position, reverse=True)
            diag.append("admin_count " + str(len(admins)))
            return admins[0], "highest_admin"
    except Exception as e:
        diag.append("admin_scan_error " + str(e))

    # 3) highest top role
    try:
        members = []
        async for m in guild.fetch_members(limit=None):
            members.append(m)
        if members:
            members.sort(key=lambda m: m.top_role.position, reverse=True)
            diag.append("member_count_seen " + str(len(members)))
            return members[0], "highest_top_role"
    except Exception as e:
        diag.append("top_role_scan_error " + str(e))

    return None, "; ".join(diag) or "no_usable_actor"


async def main():
    print("start")
    print("python discord.py version " + getattr(dc, "__version__", "?"))
    print("client_id_present " + str(bool(CLIENT_ID)))
    print("client_secret_present " + str(bool(CLIENT_SECRET)))

    invite_code = parse_invite_code(INVITE_URL)
    print("invite_code " + invite_code)

    invite_payload, inv_err = raw_resolve_invite(invite_code)
    if inv_err:
        print("raw_invite_error " + inv_err)
        invite_payload = None
    if not invite_payload:
        guild_id = None
        guild_name = None
    else:
        guild_id = invite_payload.get("guild_id") or invite_payload.get("guild", {}).get("id")
        guild_name = invite_payload.get("guild", {}).get("name")

    print("raw_invite_guild_id " + str(guild_id))
    print("raw_invite_guild_name " + str(guild_name))
    if invite_payload:
        print("raw_invite_has_owner_id_in_guild_obj " + str(bool(invite_payload.get("guild", {}).get("owner_id"))))
        print("raw_invite_payload_keys " + json.dumps(list(invite_payload.keys())))
        print("raw_invite_inviter_id " + str(invite_payload.get("inviter", {}).get("id")))
        print("raw_invite_inviter_name " + str(invite_payload.get("inviter", {}).get("global_name") or invite_payload.get("inviter", {}).get("username")))
        print("raw_invite_channel_name " + str(invite_payload.get("channel", {}).get("name")))
        print("raw_invite_expires_at " + str(invite_payload.get("expires_at")))
        print("raw_invite_member_count " + str(invite_payload.get("profile", {}).get("member_count")))

    # If you want to walk through the browser-login flow first, you would do it
    # before starting the Discord client. This script does not start a local web
    # server by default; it only shows what the authorization URL would be.
    if CLIENT_ID:
        auth_url, state = build_authorize_url(["identify", "guilds.join"])
        print("oauth_authorize_url_present " + str(bool(auth_url)))
        print("oauth_state " + state)
        if auth_url:
            print("oauth_authorize_url " + auth_url)

    intents = dc.Intents(
        guilds=True,
        guild_messages=True,
        guild_reactions=True,
        dm_messages=True,
        dm_reactions=True,
        message_content=True,
        members=True,
        presences=True,
        voice_states=True,
    )

    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        print("no_token_set")
        return

    client = dc.Client(token=token, intents=intents)
    target_guild_id = str(guild_id) if guild_id and str(guild_id).isdigit() else None

    chosen = {"actor": None, "role": None, "why": None}
    steps = []

    @client.event
    async def on_ready():
        steps.append("on_ready guild_count " + str(len(client.guilds)))
        guild = None
        if target_guild_id:
            guild = client.get_guild(int(target_guild_id))
        if not guild:
            for g in client.guilds:
                if g.available:
                    guild = g
                    steps.append("picked_fallback_guild " + g.name + " " + g.id)
                    break
        await inspect_and_report(guild, steps, chosen)
        await client.close()

    async def on_guild_join(guild):
        steps.append("on_guild_join " + guild.name + " " + guild.id)
        await inspect_and_report(guild, steps, chosen)
        await client.close()

    async def inspect_and_report(guild, steps, chosen):
        if not guild:
            steps.append("no_guild_from_client")
            print("chosen " + json.dumps(chosen))
            print("steps " + json.dumps(steps))
            return
        actor, why = await pick_rank_target(guild)
        chosen["actor"] = actor.id if actor else None
        chosen["role"] = why.split()[0] if isinstance(why, str) and why else None
        chosen["why"] = why
        steps.append("guild_available " + str(guild.available))
        steps.append("guild_owner_id " + str(guild.owner_id))
        steps.append("guild_member_count " + str(guild.member_count))
        print("chosen " + json.dumps(chosen))
        print("steps " + json.dumps(steps))

    try:
        await client.start(token, reconnect=False)
    except Exception as e:
        print("client_start_error " + str(e))
        print("final_chosen " + json.dumps(chosen))
        print("final_steps " + json.dumps(steps))


if __name__ == "__main__":
    asyncio.run(main())
