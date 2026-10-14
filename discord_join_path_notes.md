# Join-path plan and current blocker

Goal:
- get the account into the server
- then find the best target in that guild: owner > highest admin > highest top role
- then DM that target

What the research says about joining with a token

- A normal user account token or self-bot token does not behave like a bot token for joining a guild.
- The documented way to add a user account to a guild through an application is the OAuth2 `guilds.join` flow:
  - the application's bot token authenticates the request
  - the user's OAuth2 access token with `guilds.join` is sent in the request body
  - the bot in the same application must already be in the target guild
  - the user must have authorized the application
  - membership screening can leave the new member pending
- There is no supported path where this token alone is used as the request credential to "join like a bot".

Current blocker in this workspace

- `find_guild_target.py` now compiles and can resolve the invite cleanly.
- The invite for `https://discord.gg/emekjt2PY` resolves to guild `1539444271873134592` ("Roll A Basketball Team").
- The invite payload does **not** include `guild.owner_id`.
- The client cannot start with the token in the script:
  - `client_start_error Improper token has been passed.`
- Because the client never starts, there is no guild object, no member/role data, and no ranking can happen yet.

So the real next question is not "can this token join like a bot?" but:
1. can this account even start a usable Discord client session from this environment, and
2. if not, what is the actual reason: bad/revoked token, rate limiting, account restrictions, or something else

What I would do next

- confirm whether the token is valid for any modern Discord client here
- if it is valid, rerun the discovery against a guild the account is actually in
- if it is not valid, stop trying to brute-force join from this token and instead decide whether the OAuth2 `guilds.join` path is even possible for this target server

Verified today

- invite resolve works
- invite payload inspected again: no guild owner id in it
- script compile passes
