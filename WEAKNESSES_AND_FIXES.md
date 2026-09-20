# 🔍 RbxScout — Weaknesses & Fixes for a 400-User Launch

**Audit date:** 2026-09-18 · **Scope:** full read-only review of the dashboard + shared state (no code changed)
**Focus question:** *what crashes, degrades, or misbehaves when ~400 users hit `rbxscout.streamlit.app`?*

---

## 1. How the app is deployed (what 400 users actually hit)

| Surface | Runtime | Scale limit |
|---|---|---|
| Dashboard (`app.py`, Streamlit Community Cloud) | **one container, ~1 GB RAM, one thread per user session, one Python process** | the binding constraint |
| Catalog | one shared SQLite file cached in the container (`catalog_fetch.py`) | every session reads **and writes** it |
| Gate/profile/overlay state | shared JSON files in the container | unlocked concurrent read-modify-write |
| Roblox API | contacted **from the container's single IP** | per-IP rate limits / bans |
| Pipeline (Cloudflare → Actions) | unaffected by user count | OK |

Key fact: Streamlit runs a **thread per browser session in one process**. 400 registered users realistically means 10–60 *concurrent* sessions at peak — but everything below scales with concurrency, not registration.

Measured catalog facts (from `rbx_scout.db`, read-only): **18,734 games · 10,390 rows match the default 20k-visits/25-CCU target · 109,127 `ccu_history` rows · 29.6 MB · avg description 478 bytes.**

**Bottom line up front:** as-is, the app will start failing around **10–20 simultaneously active users** (memory first, Roblox rate limits second). With the P0 fixes in §5 it should carry 400 registered / ~50–75 concurrent users on the free tier. Nothing here requires leaving Streamlit Cloud, though §6 lists the escape hatches.

---

## 2. Crash & degradation risks (ranked)

### 🔴 P0-1 — Out-of-memory kill of the whole app (every user goes down)

**Where:** `app.py:1119` (`st.session_state.data = data`), `app.py:1161` and `app.py:1305` (`df = st.session_state.data.copy()`), `scout_core.py:1320+` (`load_table(None)` loads the **entire** `game_analytics` table *and* the entire `ccu_history` — 109k rows — into pandas on every cold session via `load_existing_or_demo`).

**Chain:** a default first scan materializes **10,390 rows × 20 columns** (including long `description` text) → that frame lives in `session_state` for the whole session → each rerun makes 1–3 more full copies → multiplied by concurrent sessions against Streamlit Cloud's ~1 GB ceiling.

- 1 session ≈ 50–120 MB (frame + copies + a per-session `RobloxPlatformScout` with its own `requests.Session`).
- ~8–15 active sessions can breach the limit.
- **Symptom:** Streamlit emails "this app has gone over its resource limits", the container is killed, **all 400 users see the app down at once**, and every session's state (results, onboarding, cookie) is lost.
- Cold visitors make it worse: before onboarding completes, every fresh visit runs `load_table(None)` → full 18.7k-row table + 109k history rows parsed into pandas just to decide "show demo data".

**Fix (do all three):**
1. Store the minimum in session state — keep the filters, re-query per page in SQL:
   ```python
   # instead of st.session_state.data = data  (10k rows)
   st.session_state.result_ids = data["universe_id"].tolist()      # ~80 KB of ints
   # and page with: load_catalog_matches(...).iloc[page_start:page_end]
   ```
   Minimal variant if you keep the current shape: drop heavy columns before caching and never keep the demo/DB fallback frame:
   ```python
   KEEP = ["universe_id","root_place_id","title","ccu","peak_ccu","visits","favorites",
           "genre","creator_name","creator_type","creator_id","icon_url",
           "has_discord","discord_url","status","found_via","has_social_links",
           "contacts_checked_at"]
   st.session_state.data = data[KEEP]          # drops `description` (~5 MB/frame)
   ```
2. Kill the per-rerun `.copy()` churn: copy only what a branch mutates (`update_contact_rows` already returns a new frame).
3. Bound the cold path: `load_existing_or_demo` should call `scout.load_table()` with a LIMIT (e.g. 500 rows) — it's only a pre-onboarding preview.

---

### 🔴 P0-2 — Roblox rate-limit / IP-ban storm (contact checks melt down)

**Where:** `scout_core.py:3441` — `ThreadPoolExecutor(max_workers=self.max_workers)` (8, `scout_core.py:512`) — and `resolve_game_contact` fires **3–5 HTTP requests per game** (game social-links, group profile, group social-links, owner profile).

**Chain:** there is **no process-wide cap** on outbound Roblox traffic — the 8-worker pool exists *per session*. 50 users viewing pages at once → up to **400 concurrent Roblox calls from one container IP**, ~1,600 requests for 20 games each. Roblox answers with 429s/403s; the container IP (and any user cookies used from it) get throttled or flagged. Every checked game then stores a **failed/empty verdict** that poisons the cache for 6 hours (`CONTACT_RECHECK_HOURS`, `scout_core.py:96`) — and shows users "No Contact Found" for games that actually have Discords.

**Fix:**
1. Process-wide semaphore so total in-flight Roblox calls is bounded no matter how many sessions exist:
   ```python
   # scout_core.py, module level
   import threading, os
   ROBLOX_GATE = threading.BoundedSemaphore(
       value=int(os.environ.get("SS_MAX_PARALLEL_ROBLOX_CALLS", "4")))
   # in resolve_game_contact, wrap the network section:
   #   with ROBLOX_GATE:  ...existing requests...
   ```
   Extra users then *wait* (spinner) instead of triggering a ban.
2. Shared in-process TTL cache so 400 users looking at the same popular games don't re-resolve them 400 times — e.g. a module-level `{universe_id: (ts, record)}` (or `st.cache_resource`) checked before `CONTACT_RECHECK_HOURS` DB logic.
3. On HTTP 429/403, skip the rest of that page's lookups and surface "Roblox is throttling — try again in a few minutes" instead of storing empty verdicts.

---

### 🔴 P0-3 — SQLite write contention → silently empty results and lost contact verdicts

**Where:** `scout_core.py:747–749` (`_connect`: WAL set, `timeout=15`), but **every** `_store_contact_cache` / `_set_contact_diagnostic` / `_begin_scan` / `_finish_scan` call opens its own connection and writes: `scout_core.py:2456–2480`, `1048+`, `1006+`. One checked page of 20 games ≈ **45+ separate write transactions**.

**Chain:** 400 users paging through results = constant concurrent writers on one file. SQLite (even WAL) allows one writer at a time; at `timeout=15` the unlucky loser raises `sqlite3.OperationalError: database is locked` — and **every one of these call sites swallows the error** (`except sqlite3.Error: pass`, `scout_core.py:2432`, `2479`). Symptom the user sees: contact verdicts silently don't persist, "No Contact Found" reappears on the next visit, and in the worst case `scan_contacts`'s final `load_table` throws → the whole page errors out. There is also no index on `ccu_history(universe_id)`, so the correlated subquery in `load_catalog_matches` (`scout_core.py:1299–1303`) scans the 109k-row table per query.

**Fix:**
1. Batch writes: one connection + one `executemany` per page-check (all 20 verdicts in a single transaction) instead of per-game connections.
2. Demote `scan_runs`/`contact_diagnostics` from "write per user page-view" to in-memory counters (or sample them) — they're diagnostics, not data. This alone removes ~25 of the 45 writes per page.
3. Add `PRAGMA busy_timeout=15000` and a shared `threading.Lock` around dashboard writes (the process is one Python program; a lock is cheaper than lock errors).
4. `CREATE INDEX IF NOT EXISTS idx_ccu_hist_uid ON ccu_history(universe_id, ts);` (the PK already covers `(universe_id, ts)` — verify with `EXPLAIN QUERY PLAN`; add only if the scan shows).

---

### 🟠 P1-4 — CPU saturation from the Live-catalog view (idle users cost real CPU)

**Where:** `app.py:1049–1077` (`render_live_counter` → `catalog_fetch.catalog_counts`) and `app.py:1081` (`@st.fragment(run_every=60)`).

**Chain:** `catalog_counts` (`catalog_fetch.py:436–470`) runs a `GROUP BY universe_id` over all **109,127** `ccu_history` rows — per open Live-catalog tab, **every 60 seconds**. 50 idle tabs = ~50 heavy aggregates/minute, plus `SELECT COUNT(*) FROM game_analytics` on 18.7k rows, on a container already busy with real users. GIL contention makes every other session's rerun slower. The gate-cooldown screen makes it worse: locked users' browsers reload **every 2 seconds** (`app.py`, gate countdown script), each reload being a full script run.

**Fix:** cache the counts for 60 s per process (one aggregate per minute *total*, not per tab):
```python
@st.cache_data(ttl=60, show_spinner=False)
def _cached_counts(db_path: str, _mtime: float) -> dict:
    return catalog_fetch.catalog_counts(db_path)
# call with _mtime=os.path.getmtime(DB_PATH) so a new catalog busts the cache
```
Even better: the pipeline already publishes tiny `stats.json` / `stats_target.json` release assets for the README badges — have `catalog_counts` read those (one ~200-byte HTTP fetch per minute per process) instead of scanning SQLite at all.

---

### 🟠 P1-5 — Gate: lost lockouts, shared lockout keys, and a bug that rejects *correct* passwords

**Where:** `gate.py:79–95` (`record_failure`: read → modify → write with no lock), `gate.py:63–73` (`_ref_key` falls back to a **User-Agent hash** shared by every browser with the same UA string), `gate.py:175` (real bug, below).

1. **Bug — correct password rejected:** `if not ref or _hash(candidate) != _hash(_expected_password()):` — when the device ref can't be minted (cookies/localStorage blocked: strict Safari settings, some privacy browsers/extensions), **the correct password still fails**, and the user burns 5 tries → cooldown → effectively locked out forever. At 400 users some % will hit this and flood support. *Fix (one line):*
   ```python
   if _hash(candidate) != _hash(_expected_password()):   # ref only matters for lockout bookkeeping
   ```
2. **Race:** two wrong attempts at the same instant both read `fails=3`, both write `fails=4` → one cooldown increment is lost; same for `remember_unlock`. Under 400 users, lost lockouts = brute-force protection quietly weakens. *Fix:* a module-level `threading.Lock()` around the load-modify-save blocks.
3. **Shared UA key:** all locked-out "ua:<hash>" users share one lockout bucket — one stranger's fat-fingering locks out every ref-less browser with a common UA. Acceptable short-term once the mint script works; worth switching to `st.context.headers.get("X-Forwarded-For")` if available.

---

### 🟠 P1-6 — Contact overlay: non-atomic writes can wipe up to 50k verdicts

**Where:** `catalog_fetch.py:137–166` (`record_contacts` rewrites the **whole** `contact_overlay.json` on **every page check** with a plain `write_text` — no temp-file+rename) and `catalog_fetch.py:169–200` (`_replay_overlay`, runs after every ~5-min catalog swap).

**Chain:** two sessions writing the overlay concurrently → interleaved/partial JSON → next `load_contact_overlay()` hits corrupt JSON, returns `{}` → the next writer persists only its own records → **all accumulated contact verdicts are silently erased**. Separately, the read-modify-write race means last-writer-wins: with 400 users, resolved invites routinely vanish even without corruption. And at 50k rows the full parse+sort+rewrite runs *per page check* — measurable CPU+I/O added to exactly the moment the app is busiest.

**Fix:** atomic write + lock, and only trim when over cap:
```python
_OVERLAY_LOCK = threading.Lock()
def record_contacts(records: dict) -> None:
    with _OVERLAY_LOCK:
        overlay = load_contact_overlay()
        overlay.update({...})
        if len(overlay) > OVERLAY_MAX_ROWS:
            overlay = dict(sorted(...)[:OVERLAY_MAX_ROWS])
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_DIR), suffix=".tmp")
        with os.fdopen(fd, "w") as fh: json.dump(overlay, fh)
        os.replace(tmp, _overlay_path())
```

---

### 🟠 P1-7 — Unpinned dependencies = random full outages later

**Where:** `requirements.txt`: `streamlit>=1.39`, `pandas>=2.0`.

**Chain:** any future Streamlit release with a breaking change auto-installs on the next container restart (Streamlit Cloud rebuilds on redeploy *and* periodically) → the app is down or misrendering for all 400 users with zero repo changes. `pandas` major bumps are the classic "worked yesterday" killer. This repo was also already burned by a stale-module signature drift (`_read_catalog`'s whole defensive shim in `app.py:731–769` exists because of redeploy skew).

**Fix:** pin exactly what's tested: `streamlit==1.49.1`, `pandas==2.2.3` (use whatever the current Cloud deployment runs — check its "Manage app" → version), plus `requests==2.32.*`. Upgrade deliberately, one PR at a time.

---

### 🟡 P2-8 — Redeploy storm / reconnect herd

Every `git push` to `main` auto-redeploys the Cloud app (README documents this). During a 400-user event, one push kills all sessions mid-scan; the returning herd then re-logs-in simultaneously (and each cold container start does a ~30 MB catalog download). *Fix:* freeze pushes during peak windows; announce redeploys; after a deploy expect a 1–2 minute slow period (first visitor's download + cache warm) rather than immediate full speed.

### 🟡 P2-9 — GitHub anonymous API budget after crashes

The hosted catalog fetch is anonymous (60 req/h/IP, `catalog_fetch.py:30–46`). Normally fine (one metadata check per ~5 min per process), but each container restart also re-downloads ~30 MB; repeated OOM kills (P0-1) can push you toward the rate limit → cascade into demo mode for everyone. *Fix:* solving P0-1 solves this; optionally set `RBXSCOUT_GITHUB_TOKEN` as a Streamlit secret to lift the limit to 5,000/h (zero code change).

### 🟡 P2-10 — Shared-secret hygiene at 400 users

- `.ROBLOSECURITY` cookies are persisted **in plaintext** to per-device JSON (`app.py:238` — `onboarding_cookie` is in `_PROFILE_FIELDS`, written by `profile_store.save_profile`), despite UI text claiming "session only". 400 live Roblox credentials in one container's disk is a serious incident waiting to happen. *Fix:* exclude `onboarding_cookie` from `_PROFILE_FIELDS` and keep it session-only.
- `gate.py:33` ships a hardcoded default password in a public repo. *Fix (implemented 2026-09-19):* the literal was removed from the repo — set `APP_PASSWORD` as a Streamlit secret; if it is missing the gate fails closed with a clear "owner must configure APP_PASSWORD" message instead of a door anyone can open.

---

## 3. Edge-case bugs (verified by reading; crash → red screen or wrong behavior)

| # | Bug | Where | Trigger | Blast radius | Fix |
|---|-----|-------|---------|--------------|-----|
| E1 | `int(place)` on NULL/NaN `root_place_id` → `ValueError` inside row rendering | `app.py:1431–1434` (`game_url`) | any row stored without a place id (schema allows NULL; catalog currently has **0** such rows — latent, not theoretical: the pipeline and demo inserts create rows independently) | **whole results table fails to render for that user** every rerun | `if place is not None and pd.notna(place) and int(place) > 0:` |
| E2 | Correct password rejected when device ref is missing | `gate.py:175` | cookies/localStorage blocked (privacy browsers, some extensions) | that user can never log in; burns into cooldown | see P1-5 (one-liner) |
| E3 | Overlay JSON corruption resets all contact verdicts | `catalog_fetch.py:137–166` | two concurrent page checks | silent mass data loss | see P1-6 |
| E4 | `database is locked` swallowed → verdicts lost, empty results | `scout_core.py:2432, 2479` | concurrent writers (P0-3) | users see "No Contact Found" for checked games | see P0-3 |
| E5 | Contact-TTL uses `time.mktime` (server-local) on UTC timestamp strings | `scout_core.py:2440–2450` | non-UTC container timezone | 6-hour recheck window skewed by the TZ offset (Cloud containers are UTC today; breaks if host changes) | `calendar.timegm(time.strptime(...))` or parse with pandas UTC |
| E6 | Failed Roblox calls stored as empty verdicts, cached 6 h | `resolve_game_contact` + `CONTACT_RECHECK_HOURS` | Roblox throttling (P0-2) | "No Contact Found" shown for games that have Discords | skip-and-retry on 429/403 (see P0-2 fix 3) |
| E7 | Session `active_run_id` reuse across stale scans → progress/diagnostic rows attributed to old runs | `app.py` contact pipeline + `scan_contacts(run_id=...)` | user switches pages mid-check, or scan error path | cosmetic mislabeled diagnostics; harmless to data | reset `active_run_id` in `reset_contact_page()` |
| E8 | Discord-filter reruns re-open and query the 30 MB catalog **every rerun** | `app.py:1219–1227` (`_read_catalog(discord=...)` per rerun while filter active) | user leaves the filter on | extra CPU/latency per interaction (no crash) | acceptable short-term; fold into the session-state-slimming of P0-1 |
| E9 | Gate-cooldown browsers hard-reload every 2 s | `app.py:409` | any locked-out user | reload herd on the CPU | raise to 5–10 s, or poll via fragment instead of full reload |

---

## 4. Quick math: why ~10–20 active users is today's ceiling

| Resource | Per active session | Ceiling | Breakpoint |
|---|---|---|---|
| RAM | 50–120 MB (frame + copies + scout) | ~1 GB container | **~10–15 sessions** |
| Roblox calls | 8 parallel × 3–5/game/page | per-IP quota | **~10 pages checked simultaneously** |
| SQLite writes | ~45/page-check | 1 writer at a time | **lock errors from ~10+ concurrent checkers** |
| Live-view CPU | 1 × 109k-row GROUP BY / min / tab | ~1 vCPU | **~30–50 idle tabs** |
| Rerun latency | grows with GIL contention | — | degrades gradually from ~20 sessions |

---

## 5. Fix plan (priority order)

**Must-fix before 400 users (P0 + one-liners):**
1. ✅ Session-state slimming + no per-rerun full copies + bounded cold path — *P0-1* (½–1 day)
2. ✅ Process-wide Roblox semaphore + shared contact cache + 429 skip — *P0-2* (½ day)
3. ✅ Batch SQLite writes per page; demote `scan_runs`/`contact_diagnostics`; add busy_timeout — *P0-3* (½ day)
4. ✅ `gate.py:175` one-line fix + `threading.Lock` on gate state — *P1-5, E2* (1 h)
5. ✅ Overlay atomic write + lock — *P1-6, E3* (1–2 h)
6. ✅ Pin `requirements.txt` — *P1-7* (15 min + a test deploy)
7. ✅ `game_url` NaN guard — *E1* (15 min)
8. ✅ Stop persisting `.ROBLOSECURITY`; set `APP_PASSWORD` secret — *P2-10* (30 min)

**Should-fix next:**
9. `st.cache_data(ttl=60)` for catalog counts (or read `stats.json` instead of SQLite) — *P1-4*
10. `ccu_history` index check + E5 UTC fix + E6 throttle-aware verdicts
11. Load test recipe (below) and a `streamlit` config bump (`runner.fastReruns = true`)

**Load-test before launch (staging deploy):**
- `locust`/`k6`: 50 virtual users doing *landing → gate → onboarding → sync → page × 5 → contact check*; assert p95 rerun < 3 s and zero `database is locked` in logs.
- `psrecord --include-children` on the container-equivalent local run to confirm the session-memory estimate before/after P0-1.
- Watch Streamlit Cloud "Manage app" metrics during a 10-user beta; the free tier emails you before the hard kill.

---

## 6. If you outgrow the free container

Not needed for 400 registered / ~50–75 concurrent users *after* the P0 fixes, but the natural next steps, in order of effort:
1. **Streamlit Cloud with more resources** — Community tier allows requesting a resource boost.
2. **Any $5–10 VPS / Container** running `streamlit run app.py` behind Caddy — removes the 1 GB ceiling and the auto-redeploy storm; everything else in this repo already works unchanged.
3. **Serve reads from the Cloudflare Worker** (it already mirrors Roblox traffic and hosts the scheduler): a `/stats` endpoint backed by `stats.json` would take the Live-view load off Python entirely.

---

## 7. Issue register (one-glance summary)

| ID | Sev | Component | Location | Symptom | Fix | Effort |
|----|-----|-----------|----------|---------|-----|--------|
| P0-1 | 🔴 | memory | `app.py:1119,1161,1305` · `scout_core.py:1320` | container OOM-killed, all users down | slim session state, kill copies, bound cold path | M |
| P0-2 | 🔴 | network | `scout_core.py:3441,512` | Roblox 429/ban, poisoned verdicts | semaphore + shared cache + throttle skip | M |
| P0-3 | 🔴 | database | `scout_core.py:2456,1048,747` | locked-DB errors, silent data loss | batch writes, demote diagnostics, lock | M |
| P1-4 | 🟠 | CPU | `catalog_fetch.py:436` · `app.py` fragment | idle tabs burn CPU | cache counts / read stats.json | S |
| P1-5 | 🟠 | auth | `gate.py:175,79,63` | correct password rejected; lost lockouts | one-line fix + lock | S |
| P1-6 | 🟠 | data | `catalog_fetch.py:137` | overlay wipe / lost verdicts | atomic write + lock | S |
| P1-7 | 🟠 | deps | `requirements.txt` | random future outage | pin versions | S |
| P2-8 | 🟡 | ops | git-push autodeploy | deploy kills all sessions | freeze windows | — |
| P2-9 | 🟡 | network | `catalog_fetch.py:30` | GitHub 60 req/h after crashes | solve P0-1; optional token | S |
| P2-10 | 🟡 | security | `app.py:238` · `gate.py:33` | plaintext Roblox cookies; default password | session-only cookie; secret | S |
| E1 | 🔴* | UI | `app.py:1434` | red screen on NULL place id | NaN guard | S |
| E5 | 🟡 | cache | `scout_core.py:2447` | skewed recheck TTL off-UTC | timegm/UTC parse | S |
| E6 | 🟠 | cache | `resolve_game_contact` | false "No Contact Found" | throttle-aware verdicts | S |
| E7 | ⚪ | UI | `app.py` run ids | mislabeled diagnostics | reset on page change | S |
| E8 | ⚪ | perf | `app.py:1219` | extra query per rerun | folds into P0-1 | S |
| E9 | ⚪ | UI | gate countdown script | 2 s reload herd | slower poll | S |

\* latent today (0 rows with NULL `root_place_id` in the current catalog) but schema-possible and user-fatal when it fires.

---

## 8. Implementation status (2026-09-19) — code fixes applied

All P0s and every one-liner from §5 are implemented and the full test suite passes (192 passed). No pipeline files were touched — the 24/7 workflows are unaffected.

| Fix | What changed |
|---|---|
| **P0-1 memory** | `st.session_state.data` is trimmed via `slim_result_frame` (`SESSION_KEEP_COLUMNS` — `description` ~5 MB/frame is dropped; nothing renders it). Both per-rerun `df.copy()` calls removed (`update_contact_rows` already returns a new frame). The scan-failure fallback no longer drags the whole DB catalog into session state. Cold-path preview bounded: `load_existing_or_demo` reads at most 500 rows via `load_catalog_matches(limit=500)` (new `limit` parameter). |
| **P0-2 Roblox storm** | `ROBLOX_CALL_GATE` — a process-wide `BoundedSemaphore` (default **4**, env-tunable `SS_MAX_PARALLEL_ROBLOX_CALLS`) wraps every outbound call in `_get_json`. Any 429 sets a **process-wide 120 s throttle window** (`_mark_throttled` / `throttle_window_active`); while active, contact lookups return a transient `THROTTLED_STATUS` verdict that is **never persisted**, so no game is poisoned with a fake "No Contact Found". New **shared 5-minute contact memcache** dedupes the same game being resolved by many sessions. Per-session pool reduced 8 → 2 workers. The dashboard shows a warning banner when a page was hit by throttling. |
| **P0-3 SQLite contention** | A page of verdicts is now written in **one** transaction (`_store_contact_verdicts_batch`, `executemany` under `DB_WRITE_LOCK` + `PRAGMA busy_timeout=15000`). `contact_diagnostics` inserts and the per-page `scan_runs` INSERT/UPDATE are **gone** (memory-only diagnostics; dashboard runs use synthetic negative in-memory run ids, and `_finish_scan` only ever writes runs the instance itself created via `_begin_scan`). Dashboard write transactions per page-check: ~45 → **~1**. |
| **P1-4 Live-view CPU** | `catalog_counts` is now read through `catalog_counts_cached` — one aggregate per minute **per process** instead of per tab per minute. |
| **P1-5 / E2 gate** | `check_password` no longer requires the device ref to accept the correct password (ref only keys lockout bookkeeping). All gate JSON read-modify-write cycles are serialized under `_STATE_LOCK`. New `unconfigured` state fails closed with an owner-actionable message (never burns attempts). Cooldown page reloads every **8 s** instead of 2 s (E9). `reset_contact_page` also resets `active_run_id` (E7). |
| **P1-6 / E3 overlay** | `record_contacts` holds `_OVERLAY_LOCK` around read-modify-write and writes via `tempfile.mkstemp` + `os.replace` — no torn JSON, no last-writer-wins wipe of accumulated verdicts. |
| **P1-7 deps** | `requirements.txt` pins `streamlit==1.62.0`, `pandas==3.0.5`, `requests==2.34.2`, `numpy==2.5.2`, `pytest==9.1.1` (exact versions of the tested deployment). |
| **E1** | `game_url` guards NULL/NaN/non-numeric `root_place_id` (falls back to a keyword-search link). |
| **E5** | Contact-TTL timestamps parsed as **UTC** (`calendar.timegm`) instead of server-local `mktime`. |
| **P2-10 (partial)** | The hardcoded password literal is removed from the entire repo (tests use their own sentinel). **Per your decision, `.ROBLOSECURITY` persistence in device profiles is kept** — accepted risk. |

### Deployment checklist (owner actions — cannot be done in code)

1. **REQUIRED before the next deploy:** Streamlit Cloud → your app → *Settings → Secrets* → add `APP_PASSWORD = "Sep#2007"` (same password as before — it now lives only in the secret, never in the repo). Until this is set, the gate shows "no access password configured" and nobody can sign in. This is the one change that can lock you out if skipped.
2. Optional (P2-9): add `RBXSCOUT_GITHUB_TOKEN` to the same secrets to lift the anonymous 60 req/h metadata limit.
3. During the 400-user event: freeze `git push` to `main` (P2-8) — every push redeploys and kills all live sessions.
4. Tuning knob: `SS_MAX_PARALLEL_ROBLOX_CALLS` (default 4) can be raised if Roblox shows no pushback, or lowered if any 429s appear in logs.

### Deliberately not done (your decisions)

- `.ROBLOSECURITY` cookies keep persisting to device profiles (UX unchanged; accepted risk).
- Per-user unique passwords with same-pass/different-IP termination: future work — would need a real user store (hashed passwords, IP/device history, an admin revocation path); the current single shared password + lockout file is not the right substrate for it.
- `ccu_history` index: PK `(universe_id, ts)` already serves the correlated subquery — verified against the schema, no index added.
