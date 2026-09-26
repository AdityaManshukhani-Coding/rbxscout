# Launching Studio Scouts from GitHub Codespaces (free, no card)

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

   Restarting a stopped codespace keeps the URL; just re-run
   `scripts/codespaces_bootstrap.sh` (the Streamlit process doesn't survive
   a stop) and hand users the same link again.

## When you outgrow it

60 h/month free is a launch window. The ladder up, cheapest first:
1. **DigitalOcean $200/60-day trial (PayPal OK)** — 8 GB box 24/7 for
   ~4 months on the credit; deploy `Dockerfile` unchanged.
2. **Hetzner (~$5/mo, PayPal OK)** — 4 GB RAM permanent home.
3. **Any 8 GB box** — the same image runs anywhere Docker does.
