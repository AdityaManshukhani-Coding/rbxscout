# Launching UpScale Scouting Tool from GitHub Codespaces (free, no card)

Your GitHub account includes **120 core-hours/month = 60 hours on a 2-core,
8 GB machine** — comfortably enough for your 100-user launch. No credit card
is charged while inside the quota.

## The 60-hours question: how many days is that?

The meter counts **hours the machine is ON** (cores × hours), not user
activity, and the quota resets every calendar month:

| Usage pattern | Math | Days it lasts |
|---|---|---|
| Launch weekend (Fri 6pm–midnight, Sat+Sun 10am–2am) | 6+16+16 = 38 h | 1 weekend, 22 h left |
| Evenings only (3 h × 20 nights) | 60 h | **~4 weeks** |
| All-day every day | 24 h/day | **2.5 days** |
| 24/7, never stopped | 720 h/month | ~4 days, then paid ($0.36/h) or stop |

So: a launch event easily fits; it is not a 24/7 home. **80 hours of "app
usage" by users would only be possible if the machine runs longer than the
quota — the machine hours are what's billed, and users being active doesn't
burn extra.**

## One-time setup (~5 minutes)

1. **Codespace secrets** (scoped to the repo): go to
   <https://github.com/settings/codespaces> → *Codespaces secrets* →
   **New secret** → add both:
   - `APP_PASSWORD` — your master gate password (required; the gate fails
     closed without it, which is by design)
   - `CONTACTS_KEY` — the hex contents of your local `contacts.key` (without
     it the app runs but Discord links stay hidden)
2. **Idle timeout**: same page → *Default idle timeout* → **240 minutes**.
   This is the fix for the one real gotcha: GitHub only counts editor
   activity, so users clicking around the dashboard will NOT keep the
   machine awake — with 240 min the machine survives a whole evening on one
   manual touch.
3. **(Recommended) stop protection**: Codespace settings → check *Stop
   protection*… or simply remember: machine off = URL still valid, machine
   just needs restarting from github.com/codespaces.

## Launch day (~2 minutes)

1. <https://github.com/codespaces> → **New codespace** on `main` (2-core is
   the default and now sufficient — the shared catalog cache cut CPU 100×).
2. When the terminal is ready, run the one command:

   ```bash
   bash scripts/codespaces_bootstrap.sh
   ```

   It installs deps, checks both secrets, flips port 8501 to **Public**,
   health-checks the app, and prints the shareable URL:
   `https://<codespace-name>-8501.app.github.dev`
   (the same URL is always in the PORTS panel; it stays identical across
   stop/start of the same codespace).
3. Hand the URL + wave 1 passwords to your first 25 users. Repeat the
   message per wave.

## While it's live

- **Logs**: `tail -f /tmp/streamlit.log`
- **Restart the app**: `pkill -f 'streamlit run' && bash scripts/codespaces_bootstrap.sh`
- **Stop burning quota**: GitHub → Your codespaces → Stop (or let the idle
  timeout do it). The URL does not change when you start it again.
- **Monthly rhythm**: the 120 core-hours reset on the 1st; check remaining
  quota at <https://github.com/settings/billing>

## Saving hours: short idle timeout + keepalive

Two dials decide how much of the 60 hours you actually burn:

1. **Default idle timeout → 60 minutes** (github.com/settings/codespaces).
   With no editor activity the machine stops after 1 h instead of 4 —
   a forgotten tab costs 1 hour, not 4.
2. **Keepalive during events** (on your Mac):

   ```bash
   brew install gh && gh auth login      # one-time
   bash scripts/codespace_keepalive.sh   # or: ... 6  (auto-stop after 6 h)
   ```

   It touches the codespace every 25 min (counts as editor activity), so the
   machine stays up exactly while your event runs, then idles out on its own
   when you Ctrl-C. Zero leaked hours.

   Restarting a stopped codespace keeps the URL; one command from the Mac
   wakes it and brings the dashboard back:

   ```bash
   bash scripts/codespace_resume.sh        # start machine + relaunch app
   bash scripts/codespace_keepalive.sh 4   # keep alive for the event window
   ```

   Then share the SAME URL again — it never changed.

## 24/7 with zero laptop: the Worker doorbell

The dream setup — open 24/7, auto-close after 1 idle hour, auto-wake when
someone shows up, users in any timezone — is built. The bridge is the
Cloudflare Worker your pipeline already runs (`cloudflare-worker/`): it is
always on, and it can both probe the app and start the machine.

**How a user visit flows:**

```text
user opens  https://rbx-search-proxy.<you>.workers.dev/app   (the ONE link)
      │
      ├─ app healthy? ──► 302 straight into the dashboard (instant)
      │
      └─ app down?    ──► Worker dispatches codespace_wake.yml
                          (Actions starts the machine + relaunches Streamlit,
                           ~1–2 min) and serves a splash page that
                          auto-refreshes every 20 s until the app is live
```

After the machine has been idle for 60 minutes (GitHub's idle timeout) it
stops itself and the meter pauses — nothing pings it. The next user's
doorbell hit wakes everything again. Your 60 hours become "only the hours
people actually use the app", spread across every timezone.

**One-time setup (~10 minutes):**

1. Create the codespace and run the bootstrap once (sections above) — the
   machine must exist and have run `scripts/codespaces_bootstrap.sh` at
   least once (port Public, deps installed).
2. **Wake credential** — the wake workflow needs a token allowed to start
   YOUR codespace:
   - github.com/settings/tokens → **Generate new token (classic)** →
     scopes `repo`, `workflow`, `codespace` → copy it.
   - Repo → Settings → Secrets and variables → Actions → **New repository
     secret** → name `CODESPACES_PAT`, paste the token.
3. **Point the doorbell at your app** (from `cloudflare-worker/`):

   ```bash
   npx wrangler login                      # the account that owns the Worker
   npx wrangler vars put APP_PUBLIC_URL    # paste:
                                           # https://<codespace-name>-8501.app.github.dev
   ```

   Optional: `npx wrangler vars put FALLBACK_APP_URL https://rbxscout.streamlit.app/`
   (a second URL shown on the splash page while waking; defaults to the
   keep-alive URL already configured).
4. Deploy the updated worker once: `npx wrangler deploy`.

**Give users the doorbell link**, not the direct codespace URL:
`https://rbx-search-proxy.<your-subdomain>.workers.dev/app` — it never
changes, works from any timezone, and wakes the machine when needed. The
direct `*.app.github.dev` URL remains your internal shortcut.

**Failure modes (all have a floor):**

| Problem | What happens |
|---|---|
| PAT missing/expired | wake workflow fails; splash keeps retrying; fallback link still works |
| No codespace yet | same — create it once (step 1) and the next hit wakes it |
| Wake throttled (2-min cooldown, 10/hour) | splash keeps refreshing; next allowed hit dispatches |
| Worker down (rare) | Streamlit Cloud fallback is independent of it |

**Quota check:** even with the doorbell, only real usage hours burn. If
usage is sparse enough that the machine fully closes between visits, a
busy day of scattered visitors might total 4–6 machine hours (8–12
core-hours) — **10–15 such days per free month**. If visitors keep it
continuously alive for, say, 8 h/day (never a 60-min gap), that is 16
core-hours/day ≈ 7 days/month. Watch the meter at
<https://github.com/settings/billing>; when the pace outgrows the quota,
move the app to a paid box (the `Dockerfile` is ready) and the doorbell
simply points at the new URL.

## The corrected usage model (read this twice)

- **Stopped = free.** The 60-hour meter only runs while the machine is up.
  Hours do not tick away while it sleeps.
- **Stopped ≠ auto-wake — by default.** A user opening the direct codespace
  URL of a stopped machine gets a connection error. The **doorbell** above
  is the fix: users open the Worker's `/app` link and the machine wakes
  itself; only direct links dead-end. Manual wake remains available via
  github.com/codespaces or `scripts/codespace_resume.sh`.
- **Therefore:** treat it like a shop. You open (resume + keepalive) when
  your users are expected, close (Ctrl-C, 1 h later it sleeps) when not.
  Used 3 h/evening, the free quota covers ~10 evenings a month; used 2 full
  weekend days, ~4 weekends. Used carelessly (left running 24/7), 5 days.

## When you outgrow it

60 h/month free is a launch window. The ladder up, cheapest first:
1. **DigitalOcean $200/60-day trial (PayPal OK)** — 8 GB box 24/7 for
   ~4 months on the credit; deploy `Dockerfile` unchanged.
2. **Hetzner (~$5/mo, PayPal OK)** — 4 GB RAM permanent home.
3. **Any 8 GB box** — the same image runs anywhere Docker does.
