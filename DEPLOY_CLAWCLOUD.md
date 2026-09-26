# Deploying the dashboard on ClawCloud Run (free)

Why here: the only verified free host that survives a 100-simultaneous-user
launch without a credit card. $5 monthly credit forever for GitHub accounts
older than 180 days (yours: Aug 2025), which covers a 4 vCPU / 8 GB container
running 24/7. Hugging Face closed free Streamlit/Docker Spaces in July 2026;
Streamlit Community Cloud caps out around 20–40 concurrent users.

Architecture on the container:

    user browser ──► ClawCloud container (this image)
                       ├─ Streamlit app (app.py + gate.py, password-gated)
                       ├─ catalog cache  /data/catalog  ◄── public GitHub release asset
                       ├─ contacts bundle decrypt (CONTACTS_KEY env var)
                       └─ user profiles  /data/profiles (persistent volume)

No secrets are baked into the image; everything arrives via env vars.
The app code needs zero changes — this is the same code Streamlit Cloud runs.

---

## One-time: publish the image (already automated)

1. Merge these files to `main`: `Dockerfile`, `.dockerignore`,
   `.github/workflows/docker-publish.yml`.
2. GitHub Actions builds and pushes
   `ghcr.io/adityamanshukhani-coding/rbxscout-dashboard:latest`.
3. Make the package public once (so ClawCloud can pull anonymously):
   GitHub → your profile → Packages → rbxscout-dashboard → Package settings
   → Change visibility → Public.

## Deploy on ClawCloud Run (~10 minutes)

1. Sign in at **run.claw.cloud** with **Sign in with GitHub**
   (the 180-day rule applies to the GitHub account, not ClawCloud).
2. Dashboard → **Create** → deploy from **Docker image** (choose the
   "Advanced" / custom-image flow, not a template).
3. Image: `ghcr.io/adityamanshukhani-coding/rbxscout-dashboard:latest`
4. **Usage counter**: select the *fixed* (not scale-to-zero) configuration
   and set **Replicas = 1**, **CPU = 2–4**, **Memory = 8 GB** (4 GB also
   works for launch; 8 GB is the comfortable ceiling for ~100 sessions).
5. **Network**: expose port **8501** and enable the free public domain
   (you get a `xxx.run.claw.cloud` URL; a custom domain can be added later).
6. **Environment variables** (from your local files — never commit them):

   | Variable | Value | Notes |
   |---|---|---|
   | `APP_PASSWORD` | the master gate password you use today | required — the app fails closed without it |
   | `CONTACTS_KEY` | contents of `contacts.key` (64 hex chars) | restores the 13,341 Discord links from the encrypted bundle; omit to run link-free |
   | `RBXSCOUT_GITHUB_OWNER` | `AdityaManshukhani-Coding` | optional; defaults are already correct |

7. **Advanced → Local storage (persistent volume)**: mount
   `/data` with 5 GB. This keeps the catalog cache and every user's
   remembered profile across restarts — without it, users re-enter their
   Discord name after each redeploy and the container re-downloads 95 MB.
8. **Update strategy**: enable image auto-update (or hit Redeploy after the
   workflow completes) so deploys reach the container within minutes.
9. Create. First boot: ~1–2 min (downloads the catalog once), then verify:
   - the login gate appears and `APP_PASSWORD` works,
   - the dashboard renders with real games (catalog fetched),
   - Discord links visible (CONTACTS_KEY correct),
   - open the app in a private window and confirm your device stays
     remembered after a refresh.

## Launch-day load plan (100 people at once)

- The 8 GB container holds ~100+ Streamlit sessions; CPU is the softer
  ceiling — expect some spinner latency at absolute peak, not crashes.
- Stage the invites in waves of ~25 every 30–60 min anyway. Your per-user
  passwords make this trivial and it flattens the spike entirely.
- Watch ClawCloud's metrics during wave 1; if CPU pegs at 100% for long
  stretches, that's the signal to enable the Parquet precompute before
  wave 2.

## Ops notes

- **Billing guard**: the $5 credit renews monthly; check Usage in the
  ClawCloud dashboard after week 1. A 4C/8G fixed container is the reported
  sweet spot that fits inside the credit — downsize CPU to 2 if ever over.
- **Gate state resets**: ban/attempt counters live in the container FS and
  reset on every image update (harmless: worst case a banned device can
  retry). User profiles survive via the /data volume.
- **Catalog freshness**: automatic — the app re-checks the release asset
  every 5 minutes; pushes from your pipeline appear without any action.
- **Rollback**: ClawCloud keeps previous deployments; pin the image tag to
  a specific `${{ github.sha }}` if you ever need a frozen version.
