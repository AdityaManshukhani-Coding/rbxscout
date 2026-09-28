# DESKTOP_APP_PLAN.md — Studio Scouts, A–Z

**Decision this document implements:** mobile users get the **website**
(Streamlit Cloud, already live). Laptop users get a **native desktop app**
(macOS `.dmg` + Windows `.exe`) that runs the same dashboard locally and
instantly. Discord links never ship inside the app — they are served
per-request by the **Cloudflare Worker license server** we already operate,
which also enforces keys, device binding, bans, and rate limits.

**Why this shape:** a link is a fact — it cannot be made user-specific or
expired. So protection is not "hide the list"; it is **never shipping the
list**. The client asks for exactly the rows it is showing; the server
decides who gets what, how fast, and writes it all down. For our
non-technical audience, extraction becomes so slow, so loud, and so
attributable that "super confusing and difficult" is achieved structurally —
there is simply nothing on their machine to extract.

---

## 0. TL;DR

| Surface | Who | Discords links | Gate | Cost |
|---|---|---|---|---|
| Website (Streamlit Cloud) | Mobile + fallback | Worker API (or today's path) | Worker `/api/verify` | $0 |
| Desktop app (DMG/EXE) | Laptop users | **Worker `/api/contact` only** | Worker `/api/verify` | $0 (+ optional $99/yr Apple notarization later) |
| Worker (license server) | Everyone, invisibly | Decrypts the private bundle | Is the gate | $0 (free tier fits) |

Phases: **(1)** Worker license API → **(2)** desktop packaging CI →
**(3)** app contacts-source swap → **(4)** closed beta → **(5)** launch.
Estimated total build: 1–2 days.

---

## A. Product shape & audience split

- **Mobile users** → `https://upscaletool.streamlit.app/`. Nothing changes for
  them. If the Worker API exists, the site *also* moves its gate and link
  serving behind it (one key list for both surfaces), but this is optional
  for launch.
- **Laptop users** → download `StudioScout-<version>.dmg` (macOS) or
  `StudioScout-Setup-<version>.exe` (Windows) from the release page /
  direct link you hand out with their key.
- **You** → keep assigning per-user keys exactly as today
  (`generate_access_passwords.py`). One key works on both surfaces; bans and
  device limits apply to both because both now check with the Worker.

The desktop app is the **same Streamlit codebase** the owner runs locally
today. Only two behaviors change: the catalog loads from a bundled/downloaded
copy instead of a release fetch, and the Discord-link column is served by the
Worker instead of local decryption.

## B. Architecture

```text
                     ┌────────────────────────────────────────────┐
  Mobile browsers ──►│  Website: Streamlit Community Cloud        │
                     │  (fallback surface, always on)             │
                     └───────────────┬────────────────────────────┘
                                     │
  Laptop users ──► DMG / EXE ────────┤
  (Streamlit app runs locally:       │
   catalog local + instant UI,       │        HTTPS (TLS, Cloudflare)
   trends/watchlist/composer offline)│
                     └───────────────▼────────────────────────────┐
                     │  Cloudflare Worker = LICENSE SERVER         │
                     │  /api/verify   key+device → ok/banned       │
                     │  /api/contact  key+game_ids → links         │
                     │  /api/version  latest version + notes      │
                     │  /api/catalog  pointer to latest asset     │
                     │  • decrypts contacts bundle in-memory      │
                     │  • canary watermarks per key                │
                     │  • logs key+IP+count on every call          │
                     └───────────────┬────────────────────────────┘
                                     │ GITHUB_TOKEN (already held)
                     ┌───────────────▼────────────────────────────┐
                     │  GitHub: private release asset              │
                     │  contacts_worker.bundle (AES-GCM)          │
                     │  + public rbx_scout.db (no links, as today)│
                     └────────────────────────────────────────────┘
```

Principle: **the desktop app is a thin, dumb, pleasant client.** All
authority — keys, links, bans, versions — lives on the Worker.

## C. The Worker license server

All inside the existing `cloudflare-worker/` (one deploy, one `wrangler`).
Free tier: 100k requests/day, 10 ms CPU/invocation, KV 100k reads + 1k
writes/day, 1 GB — comfortable for hundreds of users.

### C1. Endpoints

| Endpoint | Method | Request | Response |
|---|---|---|---|
| `/api/verify` | POST | `{key, device_id, device_label}` | `{ok, tier, caps, cooldown}` or `{ok:false, reason:"banned"/"invalid"/"device_limit"}` |
| `/api/contact` | POST | `{key, device_id, universe_ids:[≤50]}` | `{links:{universe_id: discord_url}, remaining_today}` |
| `/api/version` | GET | — | `{latest:"1.2.0", notes_url, min_supported:"1.1.0"}` |
| `/api/catalog` | GET | — | `{asset_url, updated_at, sha256}` (public asset pointer) |

Rules common to all:

- **Auth header:** `Authorization: Bearer <key>`; `X-Device-Id` header
  carries the machine fingerprint. Unknown/missing device on a key that has
  already bound two devices → `device_limit`.
- **Every request is logged** (KV counter + `console.log`): key-hash, IP,
  endpoint, count. This is the moat — abuse is *observed*, not guessed.
- **Rate limits (per key):** `verify` 10/hour; `contact` 60 requests/hour
  **and** ≤3,000 links/day (a real user browsing needs ~10–50/day; caps are
  50–100× headroom). Trust tiers raise caps (§F4).
- **Responses never include the key, other users' data, or bulk payloads.**

### C2. Where the links live (the decrypt-on-demand design)

1. The pipeline push (existing) additionally produces
   **`contacts_worker.bundle`** — same JSON payload as today's
   `contacts.bundle`, but encrypted with **AES-256-GCM via WebCrypto**
   (native in Workers: one fast call, authenticated; avoids the Python
   HMAC-CTR construction whose JS re-implementation would blow the 10 ms
   CPU budget). A second random 32-byte key (`CONTACTS_WORKER_KEY`) is a
   **Worker secret**.
2. The bundle is uploaded as a **private release asset**. The Worker
   downloads it with the **`GITHUB_TOKEN` it already holds** for workflow
   dispatch — zero new storage, zero new accounts.
3. On first `/api/contact` after an isolate boot, the Worker fetches +
   decrypts once and **caches the plaintext map in isolate memory**
   (module-global). Subsequent requests are a Map lookup — microseconds.
   Cold-start cost ≈ one 2 MB asset fetch + one AES-GCM pass ≈ well under
   the CPU limit with WebCrypto.
4. **The key never leaves Cloudflare. The bundle never leaves the Worker.**
   A user's machine holds no ciphertext and no key — extraction of the app
   yields nothing.

### C3. KV namespaces

| Namespace | Contents | Writes |
|---|---|---|
| `KEYS` | key-hash → `{tier, status, created, note, max_devices:2, max_ips:2, daily_cap}` | sync script only |
| `STATE` | key-hash → `{device_ids[], ips[], links_today, last_seen, banned_until}` | on traffic (batched counters; ≤1k writes/day headroom — aggregate hourly if needed) |
| `CANARY` | key-hash → list of universe_ids whose responses are canary-marked for this key | sync script |
| `VERSION` | latest version, notes URL, min supported | owner updates per release |

`STATE` writes are the only pressure point; mitigation: increment counters
in memory per isolate and flush every 5 min (one write per active key per
5 min ≈ far below 1k/day at our scale).

### C4. Worker secrets (via `wrangler secret put`)

`CONTACTS_WORKER_KEY` (32-byte hex) · `GITHUB_TOKEN` (exists) · optional
`ALERT_WEBHOOK` (Discord webhook for abuse alerts, §L).

## D. Key lifecycle

1. **Generate:** `python generate_access_passwords.py` (existing) → plaintexts
   to your private doc; hashes to `access_passwords.json` (committed, used by
   the website gate today).
2. **Sync to the Worker (new, one command):**
   `python scripts/sync_keys_to_worker.py` — reads `access_passwords.json`,
   PUTs `{key_hash → record}` into KV `KEYS` (via a one-off
   `wrangler kv key put` batch or a tiny admin endpoint guarded by an
   `ADMIN_TOKEN` secret). Adds each key's **canary assignment** (§F3) in the
   same pass.
3. **Assign:** you hand a user their plaintext key. Same key on website and
   desktop app.
4. **Revoke:** `python scripts/revoke_key.py <key-or-note>` → sets
   `status:"revoked"` in KV. Both surfaces lock on next verify. (Website
   path: gate consults the same KV through the Worker, or keep the local
   JSON in sync — decision at implementation; recommended: website also goes
   through `/api/verify` so there is ONE ban list.)
5. **Rotate (catastrophic scenario only):** if `CONTACTS_WORKER_KEY` were
   ever compromised, re-encrypt the bundle with a new key, update the secret,
   redeploy — the pipeline push script gains a `--worker-bundle` step, so
   rotation is one command + one push.

## E. Anti-sharing on the desktop (device binding)

- **Device ID:** a random UUID generated on first launch, stored in the
  app's user-data dir (`platformdirs`-style path, outside the app bundle).
  Not a hardware hash — good enough, matches the website's ref concept, and
  avoids fingerprinting creepy-ness.
- **Binding rules (mirror `gate.py` semantics):** ≤2 devices and ≤2 distinct
  IPs per key; exceeding → `device_limit`, user messages you (you reset via
  `revoke_key.py --reset-devices`, e.g. "I got a new laptop").
- **Bans:** KV `STATE.banned_until` — the Worker is the single ban list for
  both surfaces. Every verify records IP; a key seen from a 3rd IP flags and
  auto-cools (configurable to hard-ban).
- The desktop app shows the **same friendly gate UI** it has today, but
  `check_password` becomes a Worker call (with a cached "unlocked" session
  so users verify once per day, not per launch).

## F. Link protection — the "super confusing & difficult" stack

Defense in depth; each layer is cheap. Layers 1–4 are v1; 5–7 optional.

1. **Nothing to steal locally (structural).** The app never receives the
   bundle or key. Attacker must go through the API — the front door we
   control. *This is the whole game; everything below is reinforcement.*
2. **Per-request economics.** Bulk theft = 13,341 API calls from one key:
   at 60/h that is 9+ days (or 3,000/day cap → 4.5 days) of glaringly
   abnormal traffic, fully logged (key-hash + IP + timestamps). One human
   eyeballing a daily summary catches it; the alert webhook (§L) catches it
   automatically.
3. **Canary watermarks (attribution).** Each key's `/api/contact` responses
   include 2–3 links that are *yours* — real invite links to Discord
   servers you own, or unique-coded invite aliases — injected from KV
   `CANARY` at serve time, mapped to universe_ids the user actually views.
   They look 100% normal. If a leaked spreadsheet ever circulates, your
   canary invites are sitting in it → you know *which key leaked*, ban it,
   and can publicly name-and-shame with evidence. Cost: ~20 lines.
4. **Trust tiers.** `tier:"trial"` (new key): 500 links/day,
   2 devices. `tier:"member"` after N days / at your discretion: 3,000/day.
   You already hand-pick keys; this just encodes your judgment as a number.
5. **Tarpit (optional, aggressive).** After a key exceeds 2× its daily cap,
   `/api/contact` keeps returning 200s but starts mixing in dead invites
   (you own them) — any dump the thief assembles is rotted without them
   knowing. Default: **off** for v1 (false positives annoy real users);
   enable per-key manually when you see abuse.
6. **Client-side friction (cosmetic only — for the non-technical crowd).**
   PyInstaller onefile + `--key` bytecode obfuscation + no plaintext
   strings ("discord", endpoint paths) in the binary. Honestly labeled:
   stops idle poking, stops nobody skilled. The real answer is that the
   skilled attacker finds nothing to poke.
7. **Product > list (always true).** Curation, the gate, revivals, the
   outreach composer, freshness — a leaked list is not the product. This is
   the soft layer; it never has to carry weight alone.

**What we explicitly do NOT do:** per-user encryption of shipped links
(extractable in-memory — false security), DRM, legal scare rails. Every
complexity must buy real deterrence; the above does.

## G. Desktop app build

- **Entry point:** `desktop_main.py` — spawns
  `streamlit run app.py --server.port=8517 --server.headless` on 127.0.0.1
  (port bump if taken), waits for `/_stcore/health`, opens the default
  browser, tray-less, quiet. Reuses every line of the dashboard.
- **Packaging:** PyInstaller **onedir** (not onefile — onefile's temp-extract
  adds 5–10 s boot and antivirus false-positive risk). `--windowed` launcher
  binary; Streamlit's `frontend/` static assets added as data via
  `collect_data_files('streamlit')`. Hidden imports for the existing deps.
- **Name/icon:** `StudioScout.app` / `StudioScout.exe` with a simple icon
  (one .icns + .ico — owner provides artwork).
- **Catalog at first run:** app ships with a **pointer** (not the DB). First
  launch downloads the latest public `rbx_scout.db` asset into user-data
  (~95 MB, once), then re-checks `/api/catalog` on startup daily. Offline
  after that for everything except links (§K).

## H. macOS specifics

1. **Ad-hoc signing in CI (mandatory, free):** `codesign --force --deep -s -
   StudioScout.app`. Without it users hit *"damaged and can't be opened"*
   — an unrecoverable dead end. With it, everyone gets the *recoverable*
   path.
2. **What unsigned (but ad-hoc signed) apps trigger (macOS 15.1+):**
   double-click → *"…can't be opened. Apple could not verify…"* (no Open
   button) → user opens **System Settings → Privacy & Security** →
   **"Open Anyway"** → admin password → opens. Later launches: clean.
3. **Install guide ships in the box:** a one-page PDF/screenshot guide for
   exactly the flow in (2) plus the Windows SmartScreen flow. Support
   tickets become picture-matching.
4. **DMG:** `hdiutil create` (or `create-dmg`) with drag-to-Applications
   background. Classic, familiar.
5. **Future ($99/yr Apple Developer):** Developer ID signing + notarization
   (`notarytool` + `stapler`, one extra CI job) → users get the friendly
   one-click *"…downloaded from the Internet. Are you sure?"* dialog and
   the whole Open Anyway class of issues disappears. First revenue-funded
   upgrade.

## I. Windows specifics

- Same onedir layout zipped via **Inno Setup** installer (optional; a plain
  `.zip` with an .exe also works and avoids installer-smartscreen noise).
- **SmartScreen:** *"Windows protected your PC"* → **More info → Run
  anyway**. Two clicks, no password. Materially easier than macOS.
- Optional later: OV code-signing cert (~$70–200/yr) fades the warning; EV
  removes it. Not launch-blocking.

## J. Updates

1. **You announce** the new version in Discord (as you planned) and attach
   the new DMG/EXE to the GitHub release.
2. **The app nags on its own:** on startup it calls `/api/version`; if
   `latest > running`, a banner shows *"Update available — grab it from
   <your link>"* (never auto-downloads; users re-download manually, your
   simple model).
3. `min_supported` hard-floors ancient versions with a full-screen
   "please update" (use sparingly — e.g., API-breaking changes only).
4. Catalog freshness is independent: the app refreshes the catalog from the
   release asset on startup, so data stays current even on old app versions.

## K. Offline / degraded matrix

| Capability | Offline / Worker unreachable | Online |
|---|---|---|
| Browse catalog, filters, trends, tiers | ✅ fully (local DB) | ✅ |
| Watchlist, outreach composer, notes | ✅ fully (local) | ✅ |
| First-run catalog download | ❌ needs internet once | ✅ |
| Discord links column | ❌ hidden with a clear "needs internet" state | ✅ served per-request |
| Sign-in | ❌ (cached unlock lasts ~24 h) | ✅ |

Degradation is honest and graceful — the app never pretends to have links
it cannot fetch.

## L. Observability & abuse playbook

- **Worker logs** (`wrangler tail`) + per-key counters in KV `STATE`.
- **Discord alert webhook** (`ALERT_WEBHOOK` secret) fires on: cap breaches,
  new-IP-on-bound-key, verify storms, contact-volume spikes. You get a ping
  instead of a surprise.
- **Daily glance:** one Worker-served admin page or a script
  (`scripts/license_report.py`): keys active today, top consumers, flags.
  Two minutes/day.
- **Incident runbook:** suspicious key → `revoke_key.py <key>` (10 seconds,
  both surfaces) → DM the user → canary check if a dump appeared → rotate
  only if the *Worker key itself* was somehow compromised (it cannot be
  reached through client abuse by construction).

## M. CI/CD (GitHub Actions — free on this public repo)

- **Trigger:** tag push `app-v*` (keeps pipeline tags untouched).
- **Matrix:** `macos-latest` → PyInstaller → ad-hoc codesign → DMG;
  `windows-latest` → PyInstaller → (optional Inno Setup) → zip/EXE.
- **Release:** artifacts attached to the GitHub release automatically;
  `/api/version` KV updated by the same workflow (one less manual step).
- Keep `docker-publish.yml` untouched — hosting path stays alive in
  parallel.

## N. Secrets inventory (where each lives)

| Secret | Lives in | Never in |
|---|---|---|
| `CONTACTS_WORKER_KEY` (new, Worker bundle) | Worker secret | repo, app, client |
| `CONTACTS_KEY` (existing website bundle) | laptop gitignored file, Streamlit secret, Actions secret | repo |
| `GITHUB_TOKEN` (private-asset fetch) | Worker secret (exists) | — |
| `ADMIN_TOKEN` (KV sync endpoint) | Worker secret + your laptop | repo |
| Access keys (plaintext) | your private doc | repo (hashes only, as today) |
| `ALERT_WEBHOOK` | Worker secret | repo |

The desktop app ships **no secrets at all** — that sentence is the plan.

## O. Costs & quotas

| Item | Free tier | Our load | Headroom |
|---|---|---|---|
| Worker requests | 100k/day | ~10–50/user/day → 400 users ≈ 20k/day | 5× |
| Worker CPU | 10 ms/req | WebCrypto AES-GCM + Map lookup ≈ <5 ms | OK |
| KV | 100k reads / 1k writes / day | reads trivial; writes batched (§C3) | OK |
| Actions build minutes | free (public repo) | ~15 min/tag, occasional | OK |
| Apple notarization (optional) | $99/yr | — | later |

## P. Rollout plan (build order)

1. **Worker license API** (~½ day): endpoints C1, KV namespaces, bundle
   AES-GCM push step, key-sync + revoke scripts, alert webhook. *Testable
   with curl immediately; website can adopt `/api/verify` now (single ban
   list).*
2. **Packaging CI** (~½ day): `desktop_main.py`, PyInstaller spec, macOS
   (ad-hoc sign + DMG) and Windows jobs on `app-v*` tags, release upload,
   version-KV update.
3. **App contacts swap** (~½ day): `contacts_client.py` (verify cache,
   per-request `/api/contact` with session-level link cache, honest offline
   state); owner/dev mode keeps local decryption for you.
4. **Closed beta** (a few days): 5–10 laptop users, keys tier `trial`, watch
   the dashboard; fix support friction (Open Anyway guide edits).
5. **Launch:** website for mobile, DMG/EXE + keys for laptop, waves as
   planned. Streams stay independent; either can fail without taking the
   other down.

## Q. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| "Damaged" Mac dialogs | eliminated | ad-hoc signing (§H1) is a CI gate, not a hope |
| Open Anyway support load | medium | guide + beta feedback; $99 notarization when revenue justifies |
| KV write pressure | low | in-memory aggregation, 5-min flush |
| Worker isolate cold-start latency on links | low (one fetch per boot) | acceptable; links column shows spinner once |
| Bulk scraping via one key | low + loud | caps + logs + canaries + webhook (§F) |
| Cloudflare Worker outage | rare | website fallback unaffected; desktop degrades gracefully (§K) |
| Two surfaces drift (features) | medium | same codebase; desktop-only changes confined to entry/clients |

## R. Testing plan

- Worker: `miniflare`/`vitest` unit tests for verify/contact/caps/canary;
  curl-based integration against a real deploy in beta.
- Client: pytest for `contacts_client` (mock Worker: success, 401, banned,
  device_limit, timeout→offline mode); existing 219-test suite stays green.
- Packaging: CI must pass `codesign -dv` (valid ad-hoc signature) and a
  smoke launch (spawn app, health-check, kill) on both runners.

## S. Support runbook (hand to future-you)

| User report | Answer |
|---|---|
| "Mac says it can't be opened" | System Settings → Privacy & Security → Open Anyway (screenshot guide) |
| "Windows protected my PC" | More info → Run anyway |
| "No Discord links showing" | Check internet; if online >5 min, send me your key note + what you see |
| "New laptop, locked out" | Owner: `revoke_key.py --reset-devices <key>` |
| "App won't start" | Delete user-data dir (path in guide) → relaunch (re-downloads catalog) |
