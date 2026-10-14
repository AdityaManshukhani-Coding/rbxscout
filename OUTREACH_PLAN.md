# Outreach Plan — DM game owners from the UpScale catalog

The missing half of the scout: the catalog already finds qualifying games and
resolves their Discord server links. This plan adds **contact** — a legit
Discord bot that joins target servers (with one human click per server),
DMs the server owner, detects replies, and logs everything locally. No
Sheets, no burner accounts, no user-token automation — a real bot application.

## The pipeline

```
[Catalog: qualified games with discord_url]          rbx_scout.db (already done)
        |
        v
[Resolve invite -> guild]  GET /invites/{code}       anonymous, no token needed
        |
        |   stores guild_id + guild_name in outreach.sqlite (per universe)
        v
[Build "Add bot to server" links]                    one URL per queued game
        |
        |   YOU click Authorize — the only human step. ~2 clicks each,
        |   no captcha (bot OAuth was built for automation, humans pass free).
        |   Batchable: authorize 30 on Monday, ignore till Thursday.
        v
[Bot joins server -> looks up owner]                 GET /guilds/{id} -> owner_id
        |
        v
[Send one personalized DM]                           POST /users/@me/channels
        "Hey, I really like what you're doing with [Game Name]
         and I was hoping you're interested on hearing an offer for it!"
                                                     then POST .../messages
        |
        v
[Reply detection]                                    poll DM channels (user-set cadence)
        |
        v
[Status ledger]                                      outreach.sqlite: sent / replied /
                                                     blocked / dead-invite / error
        |
        v
[Replied tab]                                        dashboard lists owner, game,
                                                     message text, timestamps
```

## Why each constraint is what it is

- **Human click per server** — Discord has no API for a bot to accept an
  invite (`discord.gg` links are user-only). The OAuth authorize page is the
  supported entry, and it is deliberately captcha-free. This is the price of
  a non-bannable setup and it compresses to minutes per week.
- **Owner, not "highest role"** — `GET /guilds/{id}` returns `owner_id`
  without any privileged intent. Member-list scraping (roles, arbitrary
  users) needs the Server Members Intent plus pagination on big servers;
  the owner is the person we mean anyway.
- **Pacing** — sends are spaced by a simple pacer (minutes apart, a cap per
  day, never a burst). Mirrors the repo's Roblox token-bucket philosophy.
  A Discord rate limit becomes a queue pause, not an account loss.
- **One message, no follow-ups** — a non-responder stays non-messaged.
  Reply detection only ever reacts to the owner writing first.

## The state machine — one row per (universe, guild)

| status | meaning | set by |
|---|---|---|
| `queued` | resolved, authorize link waiting | resolver |
| `joined` | bot is in the server, owner found, DM not yet sent | runner |
| `sent` | DM delivered, waiting for reply | runner |
| `replied` | owner wrote back; last message stored | reply poll |
| `dmed_blocked` | owner has DMs closed | send failure |
| `dead_invite` | invite expired / server gone | resolver |
| `error` | anything else (message kept in `last_error`) | any step |

Transitions: `queued -> joined -> sent -> replied` is the happy path.
`joined` re-fires the DM attempt on the next runner tick, so a rate-limit
pause self-heals. `replied` is terminal — never contacted twice.

## Files

| file | role |
|---|---|
| `outreach.py` | resolver, outbox store, authorize-link builder, bot runner |
| `outreach.sqlite` | the outreach ledger (gitignored, like the catalog) |
| `discord_bot.token` | bot token, gitignored, never committed |
| `outreach_ui.py` | standalone Streamlit page for testing: queue + links + Replied |

The runner (`python outreach.py run`) is a plain loop: drain `joined`
(DM send), poll `sent` (reply check), pace, sleep. Designed to run on this
laptop exactly like `atlas_home.py` — nothing about it needs cloud.

## Future (not built yet)

- Auto-refresh member/owner changes on poll (owner can transfer).
- Batch authorize-page export (one file with all pending links).
- Neighborhood: reuse catalog momentum to draft better per-game hooks.
