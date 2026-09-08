# Free search-proxy mirrors — options & how to wire one in

**Why:** the keyword trial's only failures cluster at 06:00–08:00 UTC, when
Roblox throttles the shared Cloudflare egress IPs our single worker uses.
A second mirror on a *different provider* gives the pool a genuinely
different IP range to fall back to.

**Load math:** the keyword crawler makes ≈ 400 search requests per finder run
(200 keywords × 2 pages) × 144 runs/day ≈ **58k req/day ≈ 1.7M req/month**.
Pick a free tier that either covers that, or use the mirror as *overflow
only* (the pool tries it only after the primary fails — benching makes a
quiet mirror cost nothing).

**Gotcha before deploying anything:** `wrangler.toml` has `[triggers]` — a
cron. Deploying the same worker as-is on a second account would **double the
workflow dispatcher** (hydrator/finder fired twice every 5 min). Any mirror
must be deployed **without cron triggers** — use `wrangler.mirror.toml`
(next to this file) which has them stripped.

---

## Option A — second Cloudflare account (your pick)

Same code, new account → new `*.workers.dev` subdomain, separate Cloudflare
abuse/rate budget, failover. **Honest caveat:** Workers egress from a shared
global Cloudflare IP pool regardless of account, and both accounts will be
served by the same nearby colo — so Roblox sees the *same Cloudflare ranges*
either way. You gain capacity + failover, not new IPs. For new IPs use
Option B/C/D.

```bash
cd cloudflare-worker
npx wrangler login          # log into the OTHER account
npx wrangler deploy --config wrangler.mirror.toml
# prints: https://rbx-search-proxy-mirror.<other-subdomain>.workers.dev
```

Free limits: 100k req/day (we use ~58k) — covers full load.

## Option B — Deno Deploy (new IP pool: Google infra) ✅ READY

~20 lines, paste-in-browser deploy, **1M requests/month free** → use as
**overflow only** (after Cloudflare). The paste-ready file is already in the
repo: **`deno-mirror.ts`** (next to this file) — same route guard, one 429
retry, and a per-isolate 120 req/min limiter so stray public traffic can't
burn the monthly quota.

Deploy:
1. Sign in at https://dash.deno.com with GitHub (free account).
2. **New Playground** → name it e.g. `rbx-search-mirror`.
3. Delete the sample code, paste all of `deno-mirror.ts`, **Save & Deploy**.
4. Note the URL: `https://rbx-search-mirror.<you>.deno.dev`

Limits: 1M req/mo, 100 GB egress — overflow-only fits easily (failures are
~5% of requests → ~90k/mo). No cron to worry about: a Playground is
fetch-only, so it can never double-fire the dispatcher.

## Option C — Oracle Cloud Always Free VPS (new IP pool: Oracle ASN)

Genuinely forever-free (2 AMD micro VMs, 10 TB egress/mo, no request cap) and
covers full load. On an Ubuntu instance:

```bash
sudo apt install -y python3-pip && pip install aiohttp
# mirror.py (20-line proxy) → run under systemd
```

```python
# mirror.py — `pip install aiohttp`, then `python3 mirror.py`
from aiohttp import web, ClientSession

UPSTREAM = "https://apis.roblox.com"

async def proxy(req: web.Request) -> web.Response:
    if req.path != "/search-api/omni-search" or not req.query.get("searchQuery"):
        return web.Response(status=404, text="not found")
    async with ClientSession() as s:
        async with s.get(UPSTREAM + req.path_qs,
                         headers={"Accept": "application/json"}) as r:
            body = await r.read()
            return web.Response(body=body, status=r.status,
                                content_type=r.content_type)

app = web.Application()
app.router.add_get("/{tail:.*}", proxy)
web.run_app(app, port=8080)
```

(Production shape: add a 2-retry on 429 like `index.js`, run it under
`systemd` so it survives reboots.) Point the pool at `http://<vm-ip>:8080` —
the pool accepts `http://` entries. Plain HTTP is fine here (public search
queries only).

## Option D — Google Cloud Run (new IP pool: Google ASN)

**2M requests/month free** → covers full load. Wrap ~15 lines of Node in a
container, then:

```bash
gcloud run deploy rbx-search-mirror --source . --region us-central1 --allow-unauthenticated
```

## Option E — home machine + Cloudflare Tunnel (residential IP)

A residential IP is what Roblox throttles *least* — it looks like a player.
Any always-on device (old laptop, Raspberry Pi) running the 20-line proxy +
`cloudflared` tunnel (free, stable `https://rbx-mirror.<you>.trycloudflare.com`-style
hostname via your own Cloudflare zone). Caveat: only as reliable as your
home internet.

## Avoid

- **Glitch / HF Spaces / Render free** — sleep/cold-start behavior breaks the
  8-second proxy timeout (`SEARCH_PROXY_TIMEOUT`) exactly when you fail over.
- **Same-account second Cloudflare Worker** — zero benefit (same IPs, same
  limits, and it double-fires the scheduler if triggers slip in).

---

## Wiring it in (after any option is live)

Append the URL to `RBXSCOUT_SEARCH_PROXY_URLS` in
`.github/workflows/finder.yml` (+ hydrator.yml harmlessly):

```yaml
RBXSCOUT_SEARCH_PROXY_URLS: >-
  https://rbx-search-proxy.thegamingbuddiesarethebest.workers.dev,
  https://rbx-search-mirror.<you>.deno.dev,
  https://rbx-search-proxy-mirror.<other-subdomain>.workers.dev,
  direct
```

The pool (`scout_core.py`) needs **no code changes**: entries are tried in
order, each is benched independently after 3 consecutive failures (5 min),
and `direct` stays the terminal fallback. The run-log line becomes
`search IP pool: <primary> -> <mirror> -> direct`, and the capacity pilot's
bench/success stats pick the mirror up automatically — that's the number to
watch at 06:00–08:00 UTC next day.

**Recommended order:** primary (current CF) → mirror (Option A now; add
Option B/C later if the bad hour persists) → direct.
