"""
RbxScout — Automated Roblox Scouting & Contact Identification (core engine).

Sourcing pipeline:
  1. Atlas Dev analyze index                   -> mid-tier universeIds (daily harvest)
  2. discovery-queue drain (expander)          -> strict-gate verified hydration
  3. games.roblox.com batch metrics            -> CCU, visits, favorites, genre, creator
  4. thumbnails.roblox.com batch icons

Contact resolution (sequential tiers):
  T1 regex game description
  T2 community/group description
  T3 community owner bio

Peak CCU is persisted in SQLite and grown via MAX(existing, current) on every scan;
snapshot history powers Avg CCU (1d) and Momentum (1d).
"""

from __future__ import annotations

import logging
import os
import calendar
import random
import re
import sqlite3
import threading
import time
from datetime import datetime
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger("rbxscout")

# --------------------------------------------------------------------------- #
# Process-wide outbound throttling (multi-user safety)
# --------------------------------------------------------------------------- #
# Streamlit runs one thread per browser session inside a single process, and
# every session gets its own RobloxPlatformScout with its own worker pool.
# Without a shared cap, N users checking pages at once fire N × 8 concurrent
# Roblox calls from ONE container IP → 429/403 storms → the IP gets throttled
# and every verdict resolved during the storm comes back empty and is then
# cached for hours. This semaphore bounds TOTAL in-flight Roblox HTTP calls
# for the whole process no matter how many sessions exist; extra callers wait
# (spinner) instead of triggering a ban.


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


ROBLOX_CALL_GATE = threading.BoundedSemaphore(
    _env_int("SS_MAX_PARALLEL_ROBLOX_CALLS", 4)
)

# Process-wide short-lived contact-verdict cache. 400 users paging through the
# same popular games must not re-resolve the same universe 400 times in a row:
# the first resolution is shared with everyone for CONTACT_MEMCACHE_TTL
# seconds. (The DB-level CONTACT_RECHECK_HOURS cache remains authoritative.)
CONTACT_MEMCACHE_TTL = 300.0  # seconds
_CONTACT_MEMCACHE: Dict[int, Tuple[float, Dict[str, Any]]] = {}
_CONTACT_MEMCACHE_LOCK = threading.Lock()
_CONTACT_MEMCACHE_MAX = 2_000  # entries; well under a MB — no memory risk


def _contact_memcache_get(uid: int) -> Optional[Dict[str, Any]]:
    with _CONTACT_MEMCACHE_LOCK:
        hit = _CONTACT_MEMCACHE.get(uid)
        if not hit:
            return None
        ts, record = hit
        if time.time() - ts > CONTACT_MEMCACHE_TTL:
            _CONTACT_MEMCACHE.pop(uid, None)
            return None
        return dict(record)


def _contact_memcache_put(uid: int, record: Dict[str, Any]) -> None:
    with _CONTACT_MEMCACHE_LOCK:
        if len(_CONTACT_MEMCACHE) >= _CONTACT_MEMCACHE_MAX:
            oldest = min(_CONTACT_MEMCACHE, key=lambda k: _CONTACT_MEMCACHE[k][0])
            _CONTACT_MEMCACHE.pop(oldest, None)
        _CONTACT_MEMCACHE[uid] = (time.time(), dict(record))


# Serializes dashboard-write bursts in this process. SQLite (even WAL) allows
# one writer at a time; serializing the short verdict writes here is much
# cheaper than letting them fight for the file lock ("database is locked") —
# and unlike the file lock, losing that fight means a *silently dropped* write.
DB_WRITE_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# Regexes
# --------------------------------------------------------------------------- #

DISCORD_REGEX = (
    r"(?:https?://)?(?:www\.)?"
    r"(?:discord\.(?:gg|io|me|li)|discordapp\.com/invite|dsc\.gg)"
    r"/[a-zA-Z0-9\-_]+"
)

DISCORD_LOGO_URL = "https://cdn.simpleicons.org/discord/5865F2"

# Default outreach message copied per game in the dashboard. The [Your Name]
# and [Game Name] tags are placeholders: they are filled in automatically
# when the user copies a message (name from the welcome flow, game from the
# row's title). Users may edit the template freely in the welcome flow or
# sidebar, but the tags must stay for auto-fill to keep working.
DEFAULT_MESSAGE_TEMPLATE = """\
Hey,

I'm [Your Name] from Studio Scouts. We help Roblox developers scale, fund, and monetize their games by connecting them with leading industry partners.

We’ve been following your progress and are really impressed by **[Game Name]**. I'm reaching out to see if you'd be open to discussing potential growth opportunities—whether that's selling a percentage of the game, securing investment/funding, or tapping into LiveOps, publishing, and marketing support.

I directly represent studios and investors like Jae Studio, Khalid Games, Ascend Studios, and several others (portfolio references below):

* https://jaeceo.com/
* https://spong.pro/
* https://www.vexedinteractive.com/
* https://summitinteractive.co.uk/
* https://playastudios.org/
* https://games.worldent.online/

If you're open to exploring options, I'd love to share a few details and see what makes the most sense for your project.

Looking forward to connecting!

Best regards,

**[Your Name]**

Studio Scouts"""

# Four more phrasings of the same outreach, so a scout can rotate between
# copies and Discord's anti-spam heuristics see varied text instead of one
# identical message pasted at 20 servers. Every variant keeps the [Your Name]
# and [Game Name] tags (render_outreach_message fills them) and the same
# portfolio links; openings, closings and sentence structures differ.
DEFAULT_MESSAGE_TEMPLATES = (
    DEFAULT_MESSAGE_TEMPLATE,
    """\
Hi there,

[Your Name] here, reaching out on behalf of Studio Scouts. We connect Roblox developers with industry partners who help their games grow and earn more.

Your work on **[Game Name]** caught our team's eye — the momentum around it is hard to miss. I wanted to ask whether you'd be interested in a conversation about where the game could go next: a partial sale, outside investment, or hands-on publishing, LiveOps and marketing support.

The studios and investors I work with directly include Jae Studio, Khalid Games, Ascend Studios and others — a few portfolio references:

* https://jaeceo.com/
* https://spong.pro/
* https://www.vexedinteractive.com/
* https://summitinteractive.co.uk/
* https://playastudios.org/
* https://games.worldent.online/

If any of that sounds worth a chat, I'm happy to walk you through the details and see what fits your project.

Hope to hear from you,

**[Your Name]**

Studio Scouts""",
    """\
Hello!

My name is [Your Name] and I scout for Studio Scouts, a team that helps Roblox developers scale their games, secure funding and unlock new revenue by matching them with leading industry partners.

We've had our eye on **[Game Name]** for a while — genuinely impressive growth. The reason I'm writing: would you be open to exploring growth opportunities? That could mean selling a percentage of the game, bringing in investment, or plugging into publishing, LiveOps and marketing support.

On our side, I represent studios and investors such as Jae Studio, Khalid Games, Ascend Studios and several more (a few portfolio links below):

* https://jaeceo.com/
* https://spong.pro/
* https://www.vexedinteractive.com/
* https://summitinteractive.co.uk/
* https://playastudios.org/
* https://games.worldent.online/

Open to a quick conversation? I'd love to share the details and find what makes sense for your game.

Best,

**[Your Name]**

Studio Scouts""",
    """\
Hey [Game Name] team,

I'm [Your Name] with Studio Scouts. We partner with Roblox developers to help their games scale, raise funding and monetize better — by connecting them with leading industry partners.

**[Game Name]** has been on our radar and the progress stands out. I'd love to hear whether you'd consider discussing what's next: a percentage sale, investment or funding, or support across LiveOps, publishing and marketing.

I work directly with studios and investors — Jae Studio, Khalid Games, Ascend Studios among them (portfolio references below):

* https://jaeceo.com/
* https://spong.pro/
* https://www.vexedinteractive.com/
* https://summitinteractive.co.uk/
* https://playastudios.org/
* https://games.worldent.online/

If you're open to it, I'll share a few details and we can see what makes the most sense for the project.

Talk soon,

**[Your Name]**

Studio Scouts""",
    """\
Hi,

This is [Your Name] from Studio Scouts — we help Roblox developers grow, fund and monetize their games by introducing them to leading industry partners.

Congrats on what you've built with **[Game Name]**; it hasn't gone unnoticed. I'm getting in touch to see if you'd be up for a conversation about growth options: selling a stake, securing investment or funding, or tapping LiveOps, publishing and marketing support.

Among the studios and investors I represent directly: Jae Studio, Khalid Games, Ascend Studios and several others (portfolio references below):

* https://jaeceo.com/
* https://spong.pro/
* https://www.vexedinteractive.com/
* https://summitinteractive.co.uk/
* https://playastudios.org/
* https://games.worldent.online/

Would love to swap a few details and figure out what works best for your project, if you're interested.

Cheers,

**[Your Name]**

Studio Scouts""",
)
# Backwards-compatible alias: the first variant IS the original default.
DEFAULT_MESSAGE_TEMPLATE = DEFAULT_MESSAGE_TEMPLATES[0]

NAME_TAG_REGEX = re.compile(r"\[(?:your name|name)\]", re.IGNORECASE)
GAME_TAG_REGEX = re.compile(r"\[(?:game name|game)\]", re.IGNORECASE)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

ROBLOX_BASE = "https://www.roblox.com"
CONTACT_RECHECK_HOURS = 6  # skip re-resolving contacts more often than this
# Transient verdict used while Roblox is throttling this IP: shown as-is by the
# dashboard but NEVER persisted, so no game is ever poisoned with a fake
# "No Contact Found" just because the container hit a rate-limit window.
THROTTLED_STATUS = "Throttled — try again shortly"
# Bulk place→universe resolution goes through the RoProxy mirror first:
# direct apis.roblox.com rate-limits this endpoint to ~60 requests per
# window per IP (measured 2026-09-02: 200×60 then sustained 429s), which
# aborts a ~4,400-place sweep. RoProxy is a separate IP pool and carries
# no credential — the .ROBLOSECURITY cookie is scoped to *.roblox.com
# domains only, so nothing sensitive is ever routed through the mirror.
# If the mirror fails, each request falls back to direct Roblox.
ROPROXY_UNIVERSES_URL = "https://apis.roproxy.com/universes/v1/places/{pid}/universe"
ROBLOX_UNIVERSES_URL = "https://apis.roblox.com/universes/v1/places/{pid}/universe"
# Bump whenever a contact source changes, so cached verdicts resolved by the
# old pipeline are ignored instead of shadowing newly reachable sources.
CONTACT_RESOLVER_VERSION = 2
DEFAULT_CANDIDATE_LIMIT = 10_000  # safety ceiling after batch place resolution

# Hydration request budget per sync: roughly 150 batched metric calls of 50
# universes = up to ~7,500 games per sync. Tiers not due under their refresh
# cadence cost zero requests, so this budget is aimed at games that could
# still blow up. Anything that does not fit rolls to the next sync.
HYDRATION_BUDGET_PER_SYNC = 150

# --------------------------------------------------------------------------- #
# Catalog expansion (Atlas Dev seed harvest + discovery-queue drain). The
# legacy discovery engines (creator spiderweb, frontier scan, recommendations
# mining) were REMOVED 2026-09-20 — Atlas Dev is the sole discovery source
# now (ATLAS_PLAN_REVIEW.md).
#   * the metrics batch endpoint REJECTS >50 universe IDs ("Too many
#     universe IDs were requested."), so the drain batch is 50, not 100
# --------------------------------------------------------------------------- #
METRICS_BATCH_SIZE = 50          # verified hard cap of /v1/games?universeIds
EXPANSION_TARGET_VISITS = 20_000   # strict gate: nothing below target is stored
EXPANSION_TARGET_CCU = 25
EXPAND_QUEUE_BATCHES_DEFAULT = 10        # 10 x 50 = 500 queued candidates hydrated/run
EXPANSION_QUEUE_MAX_ROWS = 2_000_000     # dedup-memory ceiling (trim oldest)

# --------------------------------------------------------------------------- #
# Atlas Dev seed ingestion (ATLAS_PLAN_REVIEW.md — proven live 2026-09-19).
# atlasdev.gg/analyze lists mid-tier games (>=20k visits, >=25 CCU) — exactly
# the band the strict gate targets. Replaces the retired discovery engines
# (frontier: 0 qualified from 11,500 evaluated; spiderweb: 37 qualifiers
# ever; recs: ~0.4 new IDs/request vs Atlas's ~65-74% new per page).
# Discovery only: Atlas exposes NO stats API — CCU/visits exist solely as
# SEO meta text on game pages, usable at most for a provisional first-paint
# row (found_via='atlas_dev') that the hydrator overwrites with fresh Roblox
# stats. ccu_history is NEVER written from Atlas data; the strict gate
# always verifies via Roblox.
# --------------------------------------------------------------------------- #
ATLAS_BASE_URL = "https://atlasdev.gg/analyze"
ATLAS_QUERY = "sort=totalVisits&dir=asc&totalVisitsMin=20000&ccuMin=25"
ATLAS_PRIORITY_DEFAULT = 2          # queue tier (drain processes lowest first)
ATLAS_HOURS_DEFAULT = 23            # self-throttle: one harvest per day (23h so a fixed daily slot never slips)
ATLAS_SWEEP_PAGES_DEFAULT = 3       # steady-state pages per daily harvest
ATLAS_DEEP_PAGES_DEFAULT = 275      # one-off catch-up sweep ceiling (full index)
ATLAS_DEEP_EVERY_DAYS_DEFAULT = 30  # re-run the deep sweep this often (0 = never)
ATLAS_STAT_PAGES_DEFAULT = 100      # provisional first-paint rows per harvest
ATLAS_REQUEST_DELAY_DEFAULT = 3.0   # polite per-request delay (seconds)
ATLAS_USER_AGENT = "RbxScout/1.0 (Roblox game discovery; contact via repo)"
ATLAS_PROXY_URLS_ENV = "RBXSCOUT_SEARCH_PROXY_URLS"  # flip-ready: shared pool var


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Read a bounded int from the environment (pilot-rate tuning knobs)."""
    try:
        return max(minimum, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default

# --------------------------------------------------------------------------- #
# Tier system — monotonic thresholds + higher-axis classification
# --------------------------------------------------------------------------- #
# Tier membership is defined by visits + CCU, which only exist AFTER hydration.
# So tiers are stamped on the DB row at each upsert (pure DB logic, free), and
# the hydrator spends its request budget by tier cadence instead of refreshing
# every catalog row every sync. Classification takes the HIGHER of the two
# axis tiers (visits-axis vs CCU-axis): a 1M-visit/30-CCU corpse lands cold,
# while a 30k-visit/300-CCU rocket lands warm — CCU is the leading indicator
# of a blowup; total visits lag behind.
TIER_THRESHOLDS: Dict[int, Tuple[int, int]] = {
    # tier: (min_visits, min_ccu) — both axes strictly monotonic per tier.
    1: (25_000, 25),
    2: (50_000, 75),
    3: (75_000, 150),
    4: (100_000, 200),
    5: (200_000, 250),
    6: (500_000, 550),
    7: (1_000_000, 1_000),
}
TIER_CADENCE_SYNC: Dict[int, Optional[int]] = {
    # tier: re-hydrate every N syncs; None = weekly bucket (7-day wall clock).
    # Kept for UI/summary compatibility; the live scheduler uses the
    # wall-clock hours below so cadence holds at any hydrator frequency.
    1: 1,
    2: 1,
    3: 2,
    4: 3,
    5: None,
    6: None,
    7: None,
}

# Live refresh cadences for T1–T4, expressed in wall-clock hours. Floats so a
# 5-minute hydrator can subdivide. Semantics: a tier's games enter the refresh
# queue when last_updated is older than the tier's hours (most-stale first).
TIER_CADENCE_WALL_HOURS: Dict[int, float] = {
    1: 1.0,   # hot watchlist: refreshed within the hour
    2: 2.0,
    3: 4.0,
    4: 6.0,
}
WEEKLY_TIER_REFRESH_DAYS = 7
TIER8_ROTATION_DAYS = 2  # cold games all revisited within ~2 days
TIER8_STALE_PRUNE_DAYS = 14  # unobserved 0-CCU fallback prune age
ZERO_CCU_STRIKES_TO_PRUNE = 4  # consecutive observed 0-CCU visits = dead (~8d under 2d rotation)
NEW_TIER = 0  # never-hydrated (no stats yet); always first in line


def _visits_axis_tier(visits: Optional[int]) -> int:
    """Highest tier whose min_visits threshold the value meets (0 = below all)."""
    v = int(visits or 0)
    for tier in sorted(TIER_THRESHOLDS, reverse=True):
        if v >= TIER_THRESHOLDS[tier][0]:
            return tier
    return 0


def _ccu_axis_tier(ccu: Optional[int]) -> int:
    """Highest tier whose min_ccu threshold the value meets (0 = below all)."""
    c = int(ccu or 0)
    for tier in sorted(TIER_THRESHOLDS, reverse=True):
        if c >= TIER_THRESHOLDS[tier][1]:
            return tier
    return 0


def classify_tier(visits: Optional[int], ccu: Optional[int]) -> int:
    """Classify a hydrated game: the higher of its visits-axis and CCU-axis tier.

    The dead-giant trap: 1M visits + 30 CCU meets Tier 1's *minimums* but is a
    corpse. Taking the higher axis puts it in T7 (coldest). A 30k-visit/300-CCU
    rocket lands in T3 (warm) because CCU leads a blowup, visits lag.
    Returns 0 ("new/unclassified") when both axes are missing/zero.
    """
    if not visits and not ccu:
        return NEW_TIER
    return max(_visits_axis_tier(visits), _ccu_axis_tier(ccu))


def tier_jump_count(previous: Optional[int], current: Optional[int]) -> int:
    """Signed tier climb between two stamps (positive = moved up N tiers)."""
    if previous is None or current is None:
        return 0
    return int(current) - int(previous)

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def normalize_discord_url(raw: str) -> str:
    """Ensure a found discord invite is a full clickable https URL."""
    raw = (raw or "").strip().rstrip(".,);!'\"/")
    if raw.lower().startswith("http"):
        return raw
    return "https://" + raw.lstrip("/")


def truncate(text: str, max_len: int, suffix: str = "...") -> str:
    """Truncate long text with an ellipsis suffix (used for game/discord cells)."""
    text = text or ""
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - len(suffix))].rstrip() + suffix


def normalize_discord_user_id(value: object) -> str:
    """Return a clean Discord user ID (15-21 digits), or an empty string if invalid.

    Accepts the raw number, spaced/dashed groupings, or a full mention
    token like ``<@123456789012345678>`` pasted by mistake. Anything else
    (usernames, too-short fragments) normalizes to "" so callers can fall
    back to the plain-text name — the ID is optional, never mandatory.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if 15 <= len(digits) <= 21 else ""


def render_outreach_message(
    template: str,
    scout_name: str,
    game_title: str,
    discord_user_id: str = "",
) -> str:
    """Fill a message template's [Your Name] and [Game Name] tags for one game.

    The scout name comes from the welcome-flow Discord question and the game
    title from each table row. Missing values leave the corresponding tag in
    place so a broken message is never copied silently. An empty template
    falls back to the default.

    When a valid Discord User ID is supplied (optional), [Your Name] is
    filled as a real mention token ``<@ID>`` — pasted into Discord it
    renders as a clickable, pingable @name instead of plain text. Without
    an ID the plain username is used, exactly as before.
    """
    text = (template or "").strip() or DEFAULT_MESSAGE_TEMPLATE
    name = (scout_name or "").strip()
    game = (game_title or "").strip()
    mention = normalize_discord_user_id(discord_user_id)
    fill = f"<@{mention}>" if mention else name
    if fill:
        text = NAME_TAG_REGEX.sub(lambda _match: fill, text)
    if game:
        text = GAME_TAG_REGEX.sub(lambda _match: game, text)
    return text


def escape_md(text: str) -> str:
    """Make arbitrary Roblox game titles safe inside dataframe markdown cells."""
    return (text or "").replace("[", "［").replace("]", "］")


def slugify_name(name: str) -> str:
    """Roblox-style URL slug for fallback game links."""
    name = unicodedata.normalize("NFKD", name or "game")
    name = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    return name or "game"


def compact_num(n: Optional[float]) -> str:
    """86.9B / 437.2K style formatting for metrics and demo data."""
    if n is None:
        return "—"
    n = float(n)
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suffix}"
    return f"{int(n)}"


# --------------------------------------------------------------------------- #
# Scout engine
# --------------------------------------------------------------------------- #


class RobloxPlatformScout:
    """Scans public Roblox games, tracks metrics and resolves Discord contacts."""

    DISCORD_REGEX = DISCORD_REGEX

    def __init__(
        self,
        db_path: str = "rbx_scout.db",
        roblox_cookie: Optional[str] = None,
        max_workers: int = 8,
        request_timeout: float = 10.0,
    ):
        self.db_path = db_path
        self.max_workers = max(1, max_workers)
        self.request_timeout = request_timeout
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.set_cookie(roblox_cookie)
        self.last_contact_diagnostics: Dict[int, Dict[str, Any]] = {}
        self.last_scan: Dict[str, Any] = {}
        self.last_metrics: Dict[int, Dict[str, Any]] = {}
        self.source_diagnostics: Dict[str, Any] = {}
        self.blowup_watch_events: Dict[int, Dict[str, Any]] = {}
        self._sync_counter_path = Path(self.db_path + ".sync_state")
        self._sync_seq = self._load_sync_sequence()
        self._lock = threading.Lock()
        # Run rows this instance created (see _finish_scan): dashboard
        # sessions never own one, so they never rewrite pipeline rows.
        self._own_run_ids: set = set()
        # Token-bucket pacer shared by ALL batched outbound calls (metric
        # batches, icons). Live evidence 2026-09-03:
        # games.roblox.com grants ~11 metric batches (≈550 games) per window
        # before hard 429s (no Retry-After). The interval therefore adapts:
        # 429s stretch it (window refill beats re-hammering), 200s decay it
        # back toward the base — same model as sweep_place_map's Pacer.
        self._emit_pace_lock = threading.Lock()
        self._next_emit = 0.0
        self._emit_interval = self.BATCH_EMIT_INTERVAL
        self._init_sqlite()
        self._load_persisted_diagnostics()

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #

    def set_cookie(self, roblox_cookie: Optional[str]) -> None:
        """Attach the credential only to Roblox domains; never send it to third parties."""
        self.session.headers.pop("Cookie", None)
        self.session.cookies.clear()
        self.has_cookie = False
        if roblox_cookie:
            value = roblox_cookie.strip()
            if value.lower().startswith(".roblosecurity="):
                value = value.split("=", 1)[1]
            if value:
                self.session.cookies.set(
                    ".ROBLOSECURITY",
                    value,
                    domain=".roblox.com",
                    path="/",
                )
                self.has_cookie = True

    TRANSIENT_STATUSES = {429, 500, 502, 503, 504}

    BATCH_EMIT_INTERVAL = 0.2  # base seconds between batched request emits
    EMIT_MAX_INTERVAL = 6.0    # backoff ceiling — the window refills in ~1–2 min

    def _emit_pace(self) -> None:
        """Block until this caller may emit one batched request (thread-safe)."""
        with self._emit_pace_lock:
            now = time.monotonic()
            wait = max(0.0, self._next_emit - now)
            self._next_emit = now + wait + self._emit_interval
        if wait:
            time.sleep(wait)

    def _emit_ok(self) -> None:
        """A 200 arrived: decay the interval back toward the base, slowly."""
        with self._emit_pace_lock:
            self._emit_interval = max(self.BATCH_EMIT_INTERVAL, self._emit_interval * 0.97)

    def _emit_throttled(self) -> None:
        """A 429 arrived: stretch the interval and push the next emit out so
        the per-IP window can drain instead of being re-hammered."""
        with self._emit_pace_lock:
            self._emit_interval = min(self.EMIT_MAX_INTERVAL, max(self._emit_interval, 0.2) * 1.6)
            self._next_emit = max(self._next_emit, time.monotonic() + 2.0)

    THROTTLE_BACKOFF_SECONDS = 120.0
    _throttle_until = 0.0
    _throttle_lock = threading.Lock()

    def _mark_throttled(self) -> None:
        """A 429 from Roblox: put the whole process on a short contact-lookup
        pause so pages being checked right now skip the rest of their lookups
        instead of piling more calls onto a throttled IP (which is how empty
        verdicts and IP flags happen)."""
        with RobloxPlatformScout._throttle_lock:
            RobloxPlatformScout._throttle_until = max(
                RobloxPlatformScout._throttle_until,
                time.time() + self.THROTTLE_BACKOFF_SECONDS,
            )

    def throttle_window_active(self) -> bool:
        with RobloxPlatformScout._throttle_lock:
            return time.time() < RobloxPlatformScout._throttle_until

    def _get_json(self, url: str, retries: int = 2) -> Tuple[int, Optional[Any]]:
        """Polite GET -> (status, json-or-None). Retries transient failures.

        Every HTTP call passes through the process-wide ROBLOX_CALL_GATE so
        total outbound concurrency stays bounded across all user sessions.
        The gate is held across retries (the point is to bound in-flight
        traffic, and a retry that backs off 0.6–1.2 s keeps a slot only for
        that long). ``self.throttled`` flips True for a short window after a
        429 so per-page orchestration can skip further lookups instead of
        hammering a throttled IP and caching empty verdicts.
        """
        status, data = 0, None
        with ROBLOX_CALL_GATE:
            for attempt in range(retries + 1):
                try:
                    time.sleep(random.uniform(0.02, 0.10))  # gentle rate limiting
                    res = self.session.get(url, timeout=self.request_timeout)
                    if res.status_code == 200:
                        try:
                            self._emit_ok()
                            return 200, res.json()
                        except ValueError:
                            self._emit_ok()
                            return 200, None
                    status = res.status_code
                    if status == 429:
                        self._emit_throttled()
                        self._mark_throttled()
                    if status not in self.TRANSIENT_STATUSES or attempt >= retries:
                        return status, None
                    retry_after = float(res.headers.get("Retry-After") or 0)
                    time.sleep(max(retry_after, 0.6 * (attempt + 1)))
                except requests.RequestException as exc:
                    status = 0
                    if attempt >= retries:
                        log.debug("GET failed %s: %s", url, exc)
                        return 0, None
                    time.sleep(0.5 * (attempt + 1))
        return status, data

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        # Wait up to 15 s for a competing writer instead of failing instantly;
        # combined with the process-wide DB_WRITE_LOCK this makes concurrent
        # dashboard writes serialize instead of erroring.
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_sqlite(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS game_analytics (
                    universe_id      INTEGER PRIMARY KEY,
                    root_place_id    INTEGER,
                    title            TEXT,
                    ccu              INTEGER,
                    peak_ccu         INTEGER,
                    visits           INTEGER,
                    favorites        INTEGER,
                    genre            TEXT,
                    creator_name     TEXT,
                    creator_type     TEXT,
                    creator_id       INTEGER,
                    description      TEXT,
                    icon_url         TEXT,
                    has_discord      BOOLEAN,
                    discord_url      TEXT,
                    status           TEXT,
                    found_via        TEXT,
                    has_social_links BOOLEAN,
                    contacts_checked_at TIMESTAMP,
                    zero_ccu_strikes INTEGER DEFAULT 0,
                    last_updated     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(game_analytics)")}
            if "description" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN description TEXT")
            if "contact_schema_version" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN contact_schema_version INTEGER DEFAULT 0")
            if "zero_ccu_strikes" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN zero_ccu_strikes INTEGER DEFAULT 0")
                # One-time backfill: every existing cold catalog row gets two
                # strikes so the observed-death prune (4 strikes) cleanses the
                # accumulated corpse stock within ~4 days of re-observations
                # instead of ~8, without purging unverified rows.
                conn.execute(
                    "UPDATE game_analytics SET zero_ccu_strikes = 2 "
                    "WHERE COALESCE(ccu, 0) = 0 AND COALESCE(tier, 0) = 0"
                )
            # Tier stamping (free, pure DB logic after hydration): `tier` is the
            # current stamp, `prev_tier` is the stamp before the most recent
            # re-hydration (powers 2+-tier climb detection), `tier_since` is
            # when the game entered its CURRENT tier (weekly-bucket scheduler).
            if "tier" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN tier INTEGER DEFAULT 0")
            if "prev_tier" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN prev_tier INTEGER DEFAULT 0")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ga_tier ON game_analytics(tier)"
                )
            if "tier_since" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN tier_since TIMESTAMP")
            # Blow-up-watch flags: 1 when a re-hydration observed a 2+-tier
            # climb or a 3x+ CCU multiplication. The New and Upcoming tab
            # reads exactly these rows.
            if "blowup_flag" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN blowup_flag INTEGER DEFAULT 0")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ga_blowup ON game_analytics(blowup_flag)"
                )
            if "blowup_at" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN blowup_at TIMESTAMP")
            # Rating + first-seen stamp. upvotes/downvotes come from
            # games.roblox.com/v1/games/votes (fetched by the same pipeline
            # passes that fetch icons); first_seen is the catalog insertion
            # time. The dashboard no longer shows a Created column (Roblox
            # exposes no cookieless creation date), but the stamp still
            # accumulates for future use.
            if "upvotes" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN upvotes INTEGER")
            if "downvotes" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN downvotes INTEGER")
            if "first_seen" not in columns:
                # Backfilled from the earliest ccu_history snapshot where one
                # exists (older games), CURRENT_TIMESTAMP otherwise (new rows).
                conn.execute("ALTER TABLE game_analytics ADD COLUMN first_seen TIMESTAMP")
                try:
                    conn.execute(
                        """
                        UPDATE game_analytics SET first_seen = (
                            SELECT MIN(h.ts) FROM ccu_history h
                            WHERE h.universe_id = game_analytics.universe_id
                        )
                        """
                    )
                except sqlite3.Error:
                    # Fresh database: ccu_history is created a few statements
                    # below and holds no rows yet — nothing to backfill.
                    pass
                conn.execute(
                    "UPDATE game_analytics SET first_seen = CURRENT_TIMESTAMP "
                    "WHERE first_seen IS NULL"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ga_first_seen "
                    "ON game_analytics(first_seen)"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP,
                    status TEXT,
                    source_count INTEGER DEFAULT 0,
                    metrics_count INTEGER DEFAULT 0,
                    contacts_attempted INTEGER DEFAULT 0,
                    contacts_completed INTEGER DEFAULT 0,
                    contact_errors INTEGER DEFAULT 0,
                    candidate_count INTEGER DEFAULT 0,
                    matched_count INTEGER DEFAULT 0,
                    candidate_limit INTEGER DEFAULT 0,
                    min_visits INTEGER DEFAULT 0,
                    min_ccu INTEGER DEFAULT 0,
                    error TEXT
                )
                """
            )
            run_columns = {row[1] for row in conn.execute("PRAGMA table_info(scan_runs)")}
            for name, definition in (
                ("candidate_count", "INTEGER DEFAULT 0"),
                ("matched_count", "INTEGER DEFAULT 0"),
                ("candidate_limit", "INTEGER DEFAULT 0"),
                ("min_visits", "INTEGER DEFAULT 0"),
                ("min_ccu", "INTEGER DEFAULT 0"),
            ):
                if name not in run_columns:
                    conn.execute(f"ALTER TABLE scan_runs ADD COLUMN {name} {definition}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_diagnostics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    universe_id INTEGER NOT NULL,
                    diagnostics_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ccu_history (
                    universe_id INTEGER NOT NULL,
                    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ccu         INTEGER,
                    PRIMARY KEY (universe_id, ts)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS place_map (
                    place_id    INTEGER PRIMARY KEY,
                    universe_id INTEGER NOT NULL,
                    resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_health_log (
                    run_id       INTEGER PRIMARY KEY,
                    mode         TEXT,
                    ts           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    kw_total     INTEGER DEFAULT 0,
                    kw_ok        INTEGER DEFAULT 0,
                    kw_benched   INTEGER DEFAULT 0,
                    kw_breaker   INTEGER DEFAULT 0,
                    metrics_failed INTEGER DEFAULT 0,
                    metrics_total  INTEGER DEFAULT 0,
                    known_due    INTEGER DEFAULT 0,
                    hydrated     INTEGER DEFAULT 0,
                    deferred     INTEGER DEFAULT 0,
                    utilization_pct REAL DEFAULT 0,
                    recommendations TEXT,
                    extra TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ga_visits ON game_analytics(visits)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ga_ccu ON game_analytics(ccu)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rolimons_catalog (
                    place_id    INTEGER PRIMARY KEY,
                    name        TEXT,
                    playing     INTEGER DEFAULT 0,
                    icon_url    TEXT,
                    cached_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_roli_playing ON rolimons_catalog(playing)")
            # --- Catalog expansion pilot tables (EXPANSION_PILOT.md) ------ #
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_pointers (
                    id               TEXT PRIMARY KEY,
                    last_universe_id INTEGER,
                    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discovery_queue (
                    universe_id  INTEGER PRIMARY KEY,
                    source       TEXT,
                    priority     INTEGER DEFAULT 3,
                    status       TEXT DEFAULT 'pending',
                    outcome      TEXT,
                    seen_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    evaluated_at TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dq_status ON discovery_queue(status, priority)"
            )

    def _load_persisted_diagnostics(self) -> None:
        import json
        try:
            now_minus_hour = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 3600))
            with self._connect() as conn:
                # Runs left 'running' by a previous dead session (app killed
                # mid-scan, or a completed scan that never recorded results)
                # would otherwise pollute the diagnostics forever.
                conn.execute(
                    "UPDATE scan_runs SET status='aborted', "
                    "error=COALESCE(error, 'Session ended before the scan recorded results.') "
                    "WHERE status='running' AND (finished_at IS NOT NULL OR started_at < ?)",
                    (now_minus_hour,),
                )
                rows = conn.execute("SELECT universe_id, diagnostics_json FROM contact_diagnostics ORDER BY id DESC LIMIT 5").fetchall()
                run = conn.execute("SELECT run_id, status, started_at, finished_at, source_count, metrics_count, contacts_attempted, contacts_completed, contact_errors, candidate_count, matched_count, candidate_limit, min_visits, min_ccu, error FROM scan_runs ORDER BY run_id DESC LIMIT 1").fetchone()
            for uid, raw in reversed(rows):
                try:
                    self.last_contact_diagnostics[int(uid)] = json.loads(raw)
                except (TypeError, ValueError):
                    continue
            if run:
                keys = ("run_id", "status", "started_at", "finished_at", "source_count", "metrics_count", "contacts_attempted", "contacts_completed", "contact_errors", "candidate_count", "matched_count", "candidate_limit", "min_visits", "min_ccu", "error")
                self.last_scan = dict(zip(keys, run))
        except sqlite3.Error:
            pass

    def _begin_scan(self) -> int:
        # started_at is written explicitly in local time so it uses the same
        # clock as finished_at (SQLite's CURRENT_TIMESTAMP default is UTC,
        # which made runs look two hours long in local timezones).
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO scan_runs (status, started_at) VALUES ('running', ?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            run_id = int(cursor.lastrowid)
            own = getattr(self, "_own_run_ids", None)
            if own is not None:
                own.add(run_id)
            return run_id

    def _finish_scan(self, **extra: Any) -> None:
        """Persist the full scan snapshot.

        Callers mutate ``self.last_scan`` as the scan progresses and then call
        this without arguments; any keyword arguments passed here override the
        snapshot. Everything is written to the DB row so a restart shows the
        real status and counters instead of a stuck 'running' row.

        Only runs THIS instance created via ``_begin_scan`` are ever written:
        a dashboard session is a read-only catalog consumer, so its synthetic
        in-memory run must never rewrite the pipeline's persisted rows (they
        feed the "last sync" tracker).
        """
        run_id = self.last_scan.get("run_id")
        if not run_id:
            return
        own = getattr(self, "_own_run_ids", None)
        if own is not None and run_id not in own:
            self.last_scan = {**self.last_scan, **extra}
            return
        allowed = {"status", "source_count", "metrics_count", "contacts_attempted", "contacts_completed", "contact_errors", "candidate_count", "matched_count", "candidate_limit", "min_visits", "min_ccu", "error"}
        merged = {**self.last_scan, **extra}
        values = {key: merged[key] for key in allowed if key in merged}
        values["finished_at"] = merged.get("finished_at") or time.strftime("%Y-%m-%d %H:%M:%S")
        assignments = ", ".join(f"{key}=?" for key in values)
        with self._connect() as conn:
            conn.execute(f"UPDATE scan_runs SET {assignments} WHERE run_id=?", (*values.values(), run_id))
        self.last_scan = {**self.last_scan, **values}

    def _persist_diagnostic(self, run_id: Optional[int], uid: int, diagnostics: Dict[str, Any]) -> None:
        # Memory-only by design under multi-user load: diagnostics are UI
        # snapshots, not data. Persisting one row per resolved game turned
        # every user page-view into a burst of write transactions on the
        # shared catalog file — the main source of "database is locked"
        # losses. The authoritative contact state still reaches the DB via
        # _store_contact_verdicts_batch (one transaction per page).
        return None

    def _set_contact_diagnostic(self, run_id: Optional[int], uid: int, diagnostics: Dict[str, Any]) -> None:
        """Update the UI snapshot and persist one diagnostic without breaking mocks."""
        lock = getattr(self, "_lock", None)
        if lock:
            with lock:
                self.last_contact_diagnostics[uid] = diagnostics
        else:
            self.last_contact_diagnostics[uid] = diagnostics
        persist = getattr(self, "_persist_diagnostic", None)
        if persist:
            try:
                persist(run_id, uid, diagnostics)
            except (AttributeError, sqlite3.Error) as exc:
                log.warning("Could not persist contact diagnostics for %s: %s", uid, exc)

    def mark_scan_failed(self, error: Exception | str) -> None:
        """Persist a failed run when the UI catches an orchestration exception."""
        message = str(error)
        run_id = self.last_scan.get("run_id")
        self.last_scan.update({
            "status": "failed",
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": message,
        })
        if run_id:
            try:
                self._finish_scan(status="failed", error=message)
            except sqlite3.Error as exc:
                log.warning("Could not persist failed scan: %s", exc)

    def upsert_game(self, record: Dict[str, Any], record_ccu_history: bool = True) -> None:
        """Insert/update metrics; peak_ccu grows via MAX(existing, current).

        Tier stamping happens automatically: the new tier is computed from the
        incoming stats, prev_tier keeps the last stamp (2+-tier climbs and 3x
        CCU jumps raise blowup_flag for the New and Upcoming watchlist).

        ``record_ccu_history=False`` (Atlas provisional first-paint rows) skips
        the ccu_history snapshot: Atlas numbers are cached third-party prose,
        and letting them into the trend table would corrupt hydrator-built
        history. The hydrator's next refresh writes the first real snapshot.
        """
        uid = record.get("universe_id")
        if uid is not None and record.get("tier") is None:
            record = {**record, **self._tier_stamp_for(int(uid), record)}
        # First-seen stamp: set only on brand-new rows; upserts of existing
        # games can never rewrite it (first_seen backfill lives in _init_sqlite).
        try:
            with self._connect() as conn:
                if conn.execute(
                    "SELECT 1 FROM game_analytics WHERE universe_id = ?", (int(uid),)
                ).fetchone():
                    record = {**record, "first_seen": None}
                else:
                    record = {
                        **record,
                        "first_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
        except (sqlite3.Error, TypeError, ValueError):
            pass
        # 25 bind values + a strike update + CURRENT_TIMESTAMP.
        # zero_ccu_strikes counts CONSECUTIVE observed 0-CCU visits: reset to 0
        # whenever the game has players, +1 when it is seen empty. This is what
        # makes prune_catalog work under the fast T8 rotation — a game dies from
        # being OBSERVED dead repeatedly, not from being ignored.
        placeholders = ",".join("?" for _ in range(25))
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO game_analytics (
                    universe_id, root_place_id, title, ccu, peak_ccu, visits, favorites,
                    genre, creator_name, creator_type, creator_id, description, icon_url,
                    has_discord, discord_url, status, found_via,
                    has_social_links, contacts_checked_at, tier, prev_tier,
                    tier_since, blowup_flag, blowup_at, first_seen, last_updated
                ) VALUES ({placeholders}, CURRENT_TIMESTAMP)
                ON CONFLICT(universe_id) DO UPDATE SET
                    root_place_id   = excluded.root_place_id,
                    title           = excluded.title,
                    ccu             = excluded.ccu,
                    peak_ccu        = MAX(COALESCE(game_analytics.peak_ccu, 0), COALESCE(excluded.peak_ccu, excluded.ccu, 0)),
                    visits          = COALESCE(excluded.visits, game_analytics.visits),
                    favorites       = COALESCE(excluded.favorites, game_analytics.favorites),
                    genre           = COALESCE(excluded.genre, game_analytics.genre),
                    creator_name    = COALESCE(excluded.creator_name, game_analytics.creator_name),
                    creator_type    = COALESCE(excluded.creator_type, game_analytics.creator_type),
                    creator_id      = COALESCE(excluded.creator_id, game_analytics.creator_id),
                    description    = COALESCE(excluded.description, game_analytics.description),
                    icon_url        = COALESCE(excluded.icon_url, game_analytics.icon_url),

                    has_discord     = COALESCE(excluded.has_discord, game_analytics.has_discord),
                    discord_url     = COALESCE(excluded.discord_url, game_analytics.discord_url),
                    status          = COALESCE(excluded.status, game_analytics.status),
                    found_via       = COALESCE(excluded.found_via, game_analytics.found_via),
                    has_social_links= COALESCE(excluded.has_social_links, game_analytics.has_social_links),
                    contacts_checked_at = COALESCE(excluded.contacts_checked_at, game_analytics.contacts_checked_at),
                    tier           = excluded.tier,
                    prev_tier      = excluded.prev_tier,
                    tier_since     = COALESCE(excluded.tier_since, game_analytics.tier_since),
                    blowup_flag    = CASE WHEN COALESCE(excluded.blowup_flag, 0) = 1
                                          THEN 1 ELSE COALESCE(game_analytics.blowup_flag, 0) END,
                    blowup_at      = COALESCE(excluded.blowup_at, game_analytics.blowup_at),
                    first_seen     = COALESCE(game_analytics.first_seen, excluded.first_seen),
                    zero_ccu_strikes = CASE WHEN COALESCE(excluded.ccu, 0) = 0
                                            THEN COALESCE(game_analytics.zero_ccu_strikes, 0) + 1
                                            ELSE 0 END,
                    last_updated    = CURRENT_TIMESTAMP
                """,
                (
                    record.get("universe_id"),
                    record.get("root_place_id"),
                    record.get("title"),
                    record.get("ccu"),
                    record.get("peak_ccu"),
                    record.get("visits"),
                    record.get("favorites"),
                    record.get("genre"),
                    record.get("creator_name"),
                    record.get("creator_type"),
                    record.get("creator_id"),
                    record.get("description"),
                    record.get("icon_url"),
                    record.get("has_discord"),
                    record.get("discord_url"),
                    record.get("status"),
                    record.get("found_via"),
                    record.get("has_social_links"),
                    record.get("contacts_checked_at"),
                    record.get("tier"),
                    record.get("prev_tier"),
                    record.get("tier_since"),
                    record.get("blowup_flag", 0),
                    record.get("blowup_at"),
                    record.get("first_seen"),
                ),
            )
            if record.get("ccu") is not None and record_ccu_history:
                # Microsecond-precision local timestamp. SQLite's
                # strftime('%f') only has millisecond precision, so two
                # rapid upserts shared one ts and the PRIMARY KEY silently
                # dropped a snapshot. Microseconds make that impossible.
                # datetime supports %f (microseconds); time.strftime does not.
                snapshot_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                conn.execute(
                    "INSERT OR IGNORE INTO ccu_history (universe_id, ts, ccu) "
                    "VALUES (?, ?, ?)",
                    (record["universe_id"], snapshot_ts, record["ccu"]),
                )

    def _tier_stamp_for(self, universe_id: int, record: Dict[str, Any]) -> Dict[str, Any]:
        """Compute tier bookkeeping for one upsert from the row's PREVIOUS state.

        Must be called BEFORE the upsert writes, so the old row (tier,
        tier_since, ccu) is still readable. Returns the tier/prev_tier/tier_since
        values to write plus a blowup flag when the game climbed 2+ tiers since
        its previous stamp, OR multiplied its CCU by 3x+ (floored at 10 CCU so
        0→25 noise never flags). A brand-new row stamps without events — the
        first classification is not news, a climb is.
        """
        new_tier = classify_tier(record.get("visits"), record.get("ccu"))
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT tier, tier_since, ccu FROM game_analytics WHERE universe_id=?",
                    (universe_id,),
                ).fetchone()
        except sqlite3.Error:
            row = None
        if not row:
            return {
                "tier": new_tier,
                "prev_tier": 0,
                "tier_since": now if new_tier else None,
                "blowup_flag": 0,
                "blowup_at": None,
            }
        old_tier = int(row[0] or 0)
        old_since = row[1]
        old_ccu = int(row[2] or 0)
        tier_since = old_since if new_tier == old_tier else now
        blowup = False
        new_ccu = int(record.get("ccu") or 0)
        if old_tier > 0 and new_tier - old_tier >= 2:
            blowup = True
        if old_ccu >= 10 and new_ccu >= 3 * old_ccu:
            blowup = True
        if blowup:
            self._note_blowup_event(universe_id, old_tier, new_tier, old_ccu, new_ccu)
        return {
            "tier": new_tier,
            "prev_tier": old_tier,
            "tier_since": tier_since,
            "blowup_flag": 1 if blowup else 0,
            "blowup_at": now if blowup else None,
        }

    def _note_blowup_event(
        self,
        universe_id: int,
        old_tier: int,
        new_tier: int,
        old_ccu: int,
        new_ccu: int,
    ) -> None:
        """Log and count one blow-up-watch trigger for the diagnostics panel.

        The authoritative watchlist is the ``blowup_flag`` column on
        ``game_analytics`` (survives restarts); this in-memory event only feeds
        the per-sync diagnostics counters.
        """
        events = getattr(self, "blowup_watch_events", None)
        if events is None:
            events = self.blowup_watch_events = {}
        events[universe_id] = {
            "universe_id": universe_id,
            "prev_tier": old_tier,
            "new_tier": new_tier,
            "prev_ccu": old_ccu,
            "ccu": new_ccu,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        log.info(
            "Blow-up watch: universe %s climbed T%s → T%s (CCU %s → %s)",
            universe_id, old_tier, new_tier, old_ccu, new_ccu,
        )

    def load_blowup_watch(self) -> pd.DataFrame:
        """The New and Upcoming watchlist: games flagged for tier/CCU blowups."""
        try:
            with self._connect() as conn:
                return pd.read_sql_query(
                    "SELECT * FROM game_analytics WHERE COALESCE(blowup_flag, 0) = 1 "
                    "ORDER BY COALESCE(blowup_at, last_updated) DESC",
                    conn,
                )
        except (pd.errors.DatabaseError, sqlite3.Error):
            return pd.DataFrame()


    def load_catalog_matches(
        self,
        min_visits: int = 0,
        min_ccu: int = 0,
        discord: Optional[bool] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Instant result set: games already in the catalog that meet the targets.

        Pure SQLite — no discovery, no Roblox requests. This
        backs the dashboard's "Sync live data" / "Start my first scan" buttons:
        the 24/7 pipeline (Cloudflare cron → hydrator/expander workflows) already
        discovered and hydrated the games, so the UI must only read and filter,
        not re-run discovery. With no thresholds set, only games the pipeline
        has actually hydrated (they carry a ccu_history snapshot) are returned.

        ``discord`` narrows by known contact state: True = games with a
        resolved Discord invite, False = games without one (including not
        yet checked), None = no constraint.

        The target thresholds are applied in SQL and the result is ranked
        exactly like the dashboard sorts it (closest to the target first, CCU
        as tiebreaker) so the first page of the session is ready with zero
        network cost. Never raises: a missing/corrupt catalog returns an empty
        frame and the caller falls back to demo data.
        """
        min_visits = max(0, int(min_visits or 0))
        min_ccu = max(0, int(min_ccu or 0))
        limit = max(0, int(limit)) if limit is not None else None
        where = ["COALESCE(visits, 0) >= ?", "COALESCE(ccu, 0) >= ?"]
        params: List[int] = [min_visits, min_ccu]
        if discord is True:
            where.append("COALESCE(has_discord, 0) = 1")
        elif discord is False:
            where.append("COALESCE(has_discord, 0) != 1")
        if min_visits == 0 and min_ccu == 0:
            # Degenerate "no target" session: only games the pipeline has
            # actually hydrated (every hydration writes a ccu_history
            # snapshot), so bulk-inserted stat-less rows cannot surface.
            where.append(
                "EXISTS (SELECT 1 FROM ccu_history h "
                "WHERE h.universe_id = game_analytics.universe_id)"
            )
        try:
            with self._connect() as conn:
                return pd.read_sql_query(
                    "SELECT * FROM game_analytics "
                    f"WHERE {' AND '.join(where)} "
                    "ORDER BY COALESCE(visits, 0) ASC, COALESCE(ccu, 0) ASC, "
                    "COALESCE(first_seen, '2999-01-01') ASC"
                    + (f" LIMIT {int(limit)}" if limit else ""),
                    conn,
                    params=tuple(params),
                )
        except (pd.errors.DatabaseError, sqlite3.Error) as exc:
            log.warning("load_catalog_matches failed: %s", exc)
            return pd.DataFrame()

    def load_table(self, universe_ids: Optional[Iterable[int]] = None) -> pd.DataFrame:
        """Load tracked games, optionally restricted to a set of universe IDs."""
        with self._connect() as conn:
            try:
                params: Tuple[int, ...] = ()
                query = "SELECT * FROM game_analytics"
                if universe_ids is not None:
                    ids = [int(uid) for uid in universe_ids]
                    if not ids:
                        return pd.DataFrame()
                    placeholders = ",".join("?" for _ in ids)
                    query += f" WHERE universe_id IN ({placeholders})"
                    params = tuple(ids)
                df = pd.read_sql_query(query, conn, params=params)
                hist_query = "SELECT universe_id, ts, ccu FROM ccu_history"
                hist_params: Tuple[int, ...] = ()
                if universe_ids is not None:
                    placeholders = ",".join("?" for _ in ids)
                    hist_query += f" WHERE universe_id IN ({placeholders})"
                    hist_params = tuple(ids)
                hist = pd.read_sql_query(hist_query, conn, params=hist_params)
            except (pd.errors.DatabaseError, sqlite3.Error):
                return pd.DataFrame()
        if df.empty:
            return df

        df["ts"] = pd.to_datetime(df["last_updated"], errors="coerce")
        if not hist.empty:
            hist["ts"] = pd.to_datetime(hist["ts"], errors="coerce")
            now = pd.Timestamp.now("UTC").tz_localize(None)
            day_ago = now - pd.Timedelta(hours=24)

            stats: Dict[int, Dict[str, float]] = {}
            for uid, grp in hist.groupby("universe_id"):
                grp = grp.sort_values("ts")
                win = grp[grp["ts"] >= day_ago]
                avg_1d = win["ccu"].mean() if len(win) >= 2 else None
                win_3d = grp[grp["ts"] >= now - pd.Timedelta(days=3)]
                # Mirrors the 1d rule: needs ≥2 snapshots in the window, so a
                # game discovered within the last 3 days shows "-" instead of
                # a one-sample average pretending to be a trend.
                avg_3d = win_3d["ccu"].mean() if len(win_3d) >= 2 else None
                ref = None
                if len(grp) >= 2:
                    base = grp[grp["ts"] < now - pd.Timedelta(hours=18)]
                    ref = float(base.iloc[-1]["ccu"]) if not base.empty else None
                stats[int(uid)] = {
                    "avg_ccu_1d": avg_1d,
                    "avg_ccu_3d": avg_3d,
                    "ccu_ref": ref,
                }
            df["avg_ccu_1d"] = df["universe_id"].map(
                lambda u: stats.get(int(u), {}).get("avg_ccu_1d")
            )
            df["avg_ccu_3d"] = df["universe_id"].map(
                lambda u: stats.get(int(u), {}).get("avg_ccu_3d")
            )
            df["momentum_1d"] = df.apply(
                lambda r: (
                    r["ccu"] - stats[int(r["universe_id"])]["ccu_ref"]
                    if int(r["universe_id"]) in stats
                    and stats[int(r["universe_id"])]["ccu_ref"] is not None
                    else None
                ),
                axis=1,
            )
        else:
            df["avg_ccu_1d"] = None
            df["avg_ccu_3d"] = None
            df["momentum_1d"] = None
        return df

    # ------------------------------------------------------------------ #
    # Place resolution (contact/scan support)
    # ------------------------------------------------------------------ #
    def resolve_universe_ids(self, place_ids: Iterable[int]) -> Dict[int, int]:
        """Resolve place→universe mappings.

        Strategy (verified live 2026-08-31 / 2026-09-02):
        1. DB cache (place_map table) — instant, zero network cost. The
           place→universe mapping is immutable, so a resolved mapping is
           valid forever.
        2. Unresolved places hit the universes-by-place endpoint — the one
           place-resolution route that works without a cookie (the
           multiget-place-details batch route is dead: 401 without cookie,
           400 with 2+ IDs even with cookie). Direct apis.roblox.com caps
           this endpoint at ~60 requests per window per IP, so the bulk
           sweep goes through the RoProxy mirror first with a direct
           fallback per request.
        3. Threaded (8-12 workers) with a circuit breaker: ≥25 consecutive
           hard failures abort the sweep; partial results are kept.
           Per-place 404s (deleted games) do NOT feed the breaker.
        4. Every success is persisted to place_map → paid once, reused forever.
        """
        if not hasattr(self, "source_diagnostics"):
            self.source_diagnostics = {}
        ids = list(dict.fromkeys(int(pid) for pid in place_ids))
        if not ids:
            return {}

        # Step 1: DB cache — instant, zero network cost.
        cached: Dict[int, int] = {}
        try:
            with self._connect() as conn:
                placeholders = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT place_id, universe_id FROM place_map WHERE place_id IN ({placeholders})",
                    tuple(ids),
                ).fetchall()
                for pid, uid in rows:
                    cached[int(pid)] = int(uid)
        except sqlite3.Error:
            pass

        unresolved = [pid for pid in ids if pid not in cached]

        # Step 2: threaded individual resolution for unresolved places only.
        resolved: Dict[int, int] = {}
        via_roproxy = 0
        via_direct = 0
        consecutive_failures = 0
        hard_failures = 0
        not_found = 0
        breaker_tripped = False
        # Per-place resolution against the healthy universes endpoint sees
        # scattered noise: some place IDs resolved from deleted games are
        # dead (404) and occasional transient errors occur. Those must NOT trip the
        # breaker — only sustained endpoint failure should. The plan target is
        # ≥25 consecutive hard failures (see GAMES_DB_PLAN.md); a fully dead
        # endpoint fails every request and still aborts quickly.
        max_consecutive_failures = 25
        if unresolved:
            def work(pid: int):
                # Primary: RoProxy mirror (separate IP pool, no credential).
                status, data = self._get_json(ROPROXY_UNIVERSES_URL.format(pid=pid))
                if status == 200 and data and data.get("universeId"):
                    return pid, int(data["universeId"]), "roproxy"
                # Fallback: direct Roblox (rate-limited to ~60 req/window).
                # A "place deleted" verdict (404) is only accepted from Roblox
                # itself, never from the mirror — a stale mirror must not be
                # able to mark live games as deleted.
                status, data = self._get_json(ROBLOX_UNIVERSES_URL.format(pid=pid))
                if status == 200 and data and data.get("universeId"):
                    return pid, int(data["universeId"]), "direct"
                if status == 404:
                    # Deleted/placeholder place: legitimate per-place outcome,
                    # not an endpoint-health failure. Distinguish it so the
                    # breaker only counts real hard failures (network errors,
                    # 5xx, rate limits).
                    return pid, None, "direct"
                return None

            # Submit only a small window. This makes the breaker meaningful:
            # cancelling a large already-running pool cannot prevent network
            # calls that have already started.
            worker_count = min(self.max_workers, 12)
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                pending = iter(unresolved)
                futures = {
                    pool.submit(work, next(pending)): True
                    for _ in range(min(worker_count, len(unresolved)))
                }
                while futures:
                    done = next(as_completed(futures))
                    futures.pop(done, None)
                    got = done.result()
                    if got:
                        pid_out, uid_out, route = got
                        if uid_out is None:
                            # 404: the place itself is gone — not a service
                            # failure. Count it, but keep the breaker fed only
                            # by hard failures.
                            not_found += 1
                        else:
                            resolved[pid_out] = uid_out
                            if route == "roproxy":
                                via_roproxy += 1
                            else:
                                via_direct += 1
                        consecutive_failures = 0
                    else:
                        hard_failures += 1
                        consecutive_failures += 1
                        if consecutive_failures >= max_consecutive_failures:
                            breaker_tripped = True
                            log.warning(
                                "Place resolution circuit breaker tripped after %d consecutive hard failures "
                                "(%d places resolved, %d not found); cancelling remaining futures",
                                consecutive_failures,
                                len(resolved),
                                not_found,
                            )
                            for f in futures:
                                f.cancel()
                            break
                    if not breaker_tripped:
                        try:
                            pid = next(pending)
                        except StopIteration:
                            continue
                        futures[pool.submit(work, pid)] = True

        # Step 3: persist every success to place_map — paid once, reused forever.
        if resolved:
            try:
                with self._connect() as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO place_map (place_id, universe_id, resolved_at) "
                        "VALUES (?, ?, CURRENT_TIMESTAMP)",
                        list(resolved.items()),
                    )
            except sqlite3.Error as exc:
                log.debug("Could not persist place_map: %s", exc)

        place_diagnostics = {
            "requested": len(ids),
            "cache_hits": len(cached),
            "newly_resolved": len(resolved),
            "unresolved": len(ids) - len(cached) - len(resolved),
            "not_found": not_found,
            "hard_failures": hard_failures,
            "via_roproxy": via_roproxy,
            "via_direct": via_direct,
            "breaker_tripped": breaker_tripped,
            "aborted_early": breaker_tripped,
            "resolved": len(cached) + len(resolved),
        }
        self.source_diagnostics["place_map"] = place_diagnostics
        # Preserve the legacy diagnostic key used by existing callers/tests.
        self.source_diagnostics["place_details"] = {
            "batches": len(cached) + len(resolved) + not_found + hard_failures,
            "aborted_early": breaker_tripped,
        }

        return {**cached, **resolved}

        return pool

    def prune_catalog(self, max_strikes: int = ZERO_CCU_STRIKES_TO_PRUNE) -> int:
        """Floor-prune the catalog (RoTrends-style): drop dead games.

        A game is dead when it has been OBSERVED at 0 CCU on
        ``ZERO_CCU_STRIKES_TO_PRUNE`` consecutive visits (~8 days with the
        2-day T8 rotation) — the old 14-days-untouched rule never fired once
        rotation started refreshing every cold game, which let corpses
        accumulate and inflate the catalog count. Games that were dead but
        never re-observed are caught by ``max_age_days`` staleness as a
        fallback: 0-CCU rows not seen for 14 days still die.
        Returns rows removed.

        Pruned games' ``discovery_queue`` rows are deleted in the same
        transaction: the queue is dedup memory, so a stale ``qualified`` row
        would otherwise block Atlas (or any engine) from ever re-enqueuing a
        pruned game that later revives. Clearing the row lets the daily
        harvest naturally re-discover it through the strict gate.
        """
        try:
            with self._connect() as conn:
                doomed = [int(r[0]) for r in conn.execute(
                    "SELECT universe_id FROM game_analytics "
                    "WHERE COALESCE(ccu, 0) = 0 AND ("
                    "  COALESCE(zero_ccu_strikes, 0) >= ?"
                    "  OR COALESCE(last_updated, '1970-01-01') < datetime('now', ?)"
                    ")",
                    (int(max_strikes), f"-{int(TIER8_STALE_PRUNE_DAYS)} days"),
                )]
                if not doomed:
                    return 0
                removed = 0
                for i in range(0, len(doomed), 900):
                    chunk = doomed[i : i + 900]
                    marks = ",".join("?" for _ in chunk)
                    cur = conn.execute(
                        f"DELETE FROM game_analytics WHERE universe_id IN ({marks})",
                        chunk,
                    )
                    removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                    # Re-discovery unblock: purge every queue memory of the
                    # corpse (any source/status — it must look brand-new).
                    conn.execute(
                        f"DELETE FROM discovery_queue WHERE universe_id IN ({marks})",
                        chunk,
                    )
            if removed:
                log.info(
                    "Catalog floor-prune removed %d dead games (queue rows cleared for re-discovery)",
                    removed,
                )
            return removed
        except sqlite3.Error as exc:
            log.debug("Catalog floor-prune failed: %s", exc)
            return 0

    # ------------------------------------------------------------------ #
    # Sync counter + tier cadence scheduler
    # ------------------------------------------------------------------ #

    def _load_sync_sequence(self) -> int:
        """Read the persisted sync counter (survives restarts)."""
        if str(self.db_path) == ":memory:":
            return 0  # in-memory DBs (tests, ephemeral scouts) persist nothing
        try:
            return int(Path(self._sync_counter_path).read_text().strip() or "0")
        except (OSError, ValueError):
            return 0

    def _save_sync_sequence(self) -> None:
        if str(self.db_path) == ":memory:":
            return
        try:
            self._sync_counter_path.write_text(str(self._sync_seq))
        except OSError as exc:
            log.debug("Could not persist sync counter: %s", exc)

    def bump_sync_sequence(self) -> int:
        """Advance and persist the sync counter; returns the new 1-based number."""
        self._sync_seq += 1
        self._save_sync_sequence()
        return self._sync_seq

    def load_tier_refresh_ids(
        self,
        sync_number: Optional[int] = None,
        batch_size: int = 50,
        budget_batches: int = HYDRATION_BUDGET_PER_SYNC,
    ) -> Dict[str, Any]:
        """Pick which known games deserve re-hydration this sync.

        Cadence-ordered: T1–T2 (stale past 1–2 wall-clock hours) → T3 (4h)
        → T4 (6h) → T5–T7 weekly wall-clock bucket → T8 rotating 2-day slice.
        Tiers not due under their cadence cost zero requests. The scheduler
        caps the selected list at ``batch_size * budget_batches`` universes;
        the caller hydrates as many of those as its own budget allows —
        anything beyond rolls to the next sync naturally.

        T8 rotation: one deterministic 2-day bucket (epoch // TIER8_ROTATION_DAYS
        mod bucket_count) so every sub-threshold game is visited at least once
        every rotation cycle without spending budget on all of them each sync.
        """
        n = int(sync_number if sync_number is not None else self._sync_seq)  # weekly T5–T7 marker
        cap = max(0, int(batch_size) * int(budget_batches))
        groups: Dict[str, List[int]] = {
            "t1_t2": [], "t2": [], "t3": [], "t4": [], "weekly": [], "t8": []
        }
        counts: Dict[int, int] = {}
        try:
            with self._connect() as conn:
                for tier in sorted(TIER_CADENCE_SYNC):
                    counts[tier] = int(conn.execute(
                        "SELECT COUNT(*) FROM game_analytics WHERE tier=?", (tier,)
                    ).fetchone()[0])
                counts[0] = int(conn.execute(
                    "SELECT COUNT(*) FROM game_analytics WHERE COALESCE(tier, 0)=0"
                ).fetchone()[0])

                def ids_for(where: str, params: tuple, order: str) -> List[int]:
                    rows = conn.execute(
                        f"SELECT universe_id FROM game_analytics WHERE {where} "
                        f"ORDER BY {order} LIMIT ?",
                        (*params, cap),
                    ).fetchall()
                    return [int(r[0]) for r in rows]

                # T1–T4 go stale on wall-clock hours, not sync counts, so the
                # scheduler stays correct whether hydration runs every 5
                # minutes or every 30. Most-stale game hydrates first.
                def _stale(hours: float) -> str:
                    return time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.gmtime(time.time() - hours * 3600),
                    )

                groups["t1_t2"] = ids_for(
                    "tier = 1 AND COALESCE(last_updated, '1970-01-01') <= ?",
                    (_stale(TIER_CADENCE_WALL_HOURS[1]),),
                    "last_updated ASC, universe_id ASC",
                )
                groups["t2"] = ids_for(
                    "tier = 2 AND COALESCE(last_updated, '1970-01-01') <= ?",
                    (_stale(TIER_CADENCE_WALL_HOURS[2]),),
                    "last_updated ASC, universe_id ASC",
                )
                for key, tier in (("t3", 3), ("t4", 4)):
                    groups[key] = ids_for(
                        f"tier = {tier} AND COALESCE(last_updated, '1970-01-01') <= ?",
                        (_stale(TIER_CADENCE_WALL_HOURS[tier]),),
                        "last_updated ASC, universe_id ASC",
                    )
                weekly_cutoff = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.gmtime(time.time() - WEEKLY_TIER_REFRESH_DAYS * 86400),
                )
                groups["weekly"] = ids_for(
                    "tier IN (5, 6, 7) AND COALESCE(last_updated, '1970-01-01') < ?",
                    (weekly_cutoff,),
                    "last_updated ASC, universe_id ASC",
                )
                # T8 rotating bucket: epoch-days // rotation mod bucket_count
                # picks this sync's slice; ordering by universe_id keeps the
                # slice deterministic across restarts.
                bucket_count = max(1, TIER8_ROTATION_DAYS)
                epoch_days = int(time.time() // 86400)
                bucket = epoch_days // bucket_count % bucket_count
                groups["t8"] = ids_for(
                    "COALESCE(tier, 0)=0 AND (universe_id % ?) = ?",
                    (bucket_count, bucket),
                    "last_updated ASC, universe_id ASC",
                )
        except sqlite3.Error as exc:
            log.debug("tier scheduler failed: %s", exc)
        selected: List[int] = []
        for key in ("t1_t2", "t2", "t3", "t4", "weekly", "t8"):
            selected.extend(groups[key])
        selected = list(dict.fromkeys(selected))[:cap]
        return {
            "ids": selected,
            "groups": {k: len(v) for k, v in groups.items()},
            "tier_counts": counts,
            "sync_number": n,
        }

    def fetch_game_metrics(self, universe_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Threaded batched metrics (50 universes per call — verified cap: 50→200, 100→400).

        Paced with the shared token bucket (BATCH_EMIT_INTERVAL between emits
        across all threads; live evidence 2026-09-03: unpaced bursts draw
        ~66% HTTP 429). Circuit breaker: ≥5 consecutive failed batches abort
        the sweep; partial results are kept. Every parsed row is returned for
        catalog upsert.
        """
        out: Dict[int, Dict[str, Any]] = {}
        statuses: List[int] = []
        consecutive_failures = 0
        breaker_tripped = False
        chunks = [
            universe_ids[i : i + METRICS_BATCH_SIZE]
            for i in range(0, len(universe_ids), METRICS_BATCH_SIZE)
        ]

        def work(chunk: List[int]):
            q = ",".join(str(u) for u in chunk)
            self._emit_pace()  # hydration bursts 429 hard without this
            return self._get_json(f"https://games.roblox.com/v1/games?universeIds={q}")

        with ThreadPoolExecutor(max_workers=min(self.max_workers, 8)) as pool:
            for fut in as_completed([pool.submit(work, chunk) for chunk in chunks]):
                status, data = fut.result()
                statuses.append(status)
                if status != 200 or not data:
                    consecutive_failures += 1
                    if consecutive_failures >= 5 and not breaker_tripped:
                        breaker_tripped = True
                        log.warning("Metrics circuit breaker tripped after %d consecutive failed batches", consecutive_failures)
                    continue
                consecutive_failures = 0
                for g in data.get("data") or []:
                    try:
                        uid = g.get("id")
                        if not uid:
                            continue
                        gl1 = g.get("genre_l1")
                        if isinstance(gl1, dict):
                            genre_l1 = gl1.get("name")
                        elif isinstance(gl1, str) and gl1:
                            genre_l1 = gl1
                        else:
                            genre_l1 = None
                        out[int(uid)] = {
                            "universe_id": int(uid),
                            "root_place_id": g.get("rootPlaceId"),
                            "title": g.get("name"),
                            "ccu": g.get("playing") or 0,
                            "visits": g.get("visits") or 0,
                            "favorites": g.get("favoritedCount") or 0,
                            "genre": genre_l1 or g.get("genre") or "Unknown",
                            "creator_id": (g.get("creator") or {}).get("id"),
                            "creator_name": (g.get("creator") or {}).get("name"),
                            "creator_type": (g.get("creator") or {}).get("type", "User"),
                            "description": g.get("description") or "",
                        }
                    except (AttributeError, TypeError, ValueError) as exc:
                        log.debug("Skipping malformed game record: %s", exc)
        self.source_diagnostics["metrics"] = {
            "batches": len(statuses),
            "successful_batches": sum(status == 200 for status in statuses),
            "failed_batches": sum(status != 200 for status in statuses),
            "breaker_tripped": breaker_tripped,
            "records": len(out),
        }
        return out

    def fetch_game_icons(self, universe_ids: List[int]) -> Dict[int, str]:
        """Batched game icons (50 per call) from thumbnails.roblox.com."""
        out: Dict[int, str] = {}
        statuses: List[int] = []
        for i in range(0, len(universe_ids), 50):
            chunk = universe_ids[i : i + 50]
            q = ",".join(str(u) for u in chunk)
            status, data = self._get_json(
                "https://thumbnails.roblox.com/v1/games/icons"
                f"?universeIds={q}&size=150x150&format=Png&isCircular=false"
            )
            statuses.append(status)
            if status != 200 or not data:
                continue
            for item in data.get("data") or []:
                if item.get("state") == "Completed" and item.get("imageUrl"):
                    out[int(item["targetId"])] = item["imageUrl"]
        self.source_diagnostics["icons"] = {
            "batches": len(statuses),
            "successful_batches": sum(status == 200 for status in statuses),
            "failed_batches": sum(status != 200 for status in statuses),
            "records": len(out),
        }
        return out

    # ------------------------------------------------------------------ #

    def fetch_vote_totals(self, universe_ids: List[int]) -> Dict[int, Dict[str, int]]:
        """Batched up/down votes (50 per call, cookieless — verified live).

        Backs the dashboard's Rating column (up / (up + down)). Missing or
        failed batches simply yield no entry: the UI shows "-" and the next
        refresh retries. Never raises.
        """
        out: Dict[int, Dict[str, int]] = {}
        if not universe_ids:
            return out
        if getattr(self, "session", None) is None:
            return out  # unit-test mocks carry no HTTP session
        for i in range(0, len(universe_ids), 50):
            chunk = universe_ids[i : i + 50]
            q = ",".join(str(u) for u in chunk)
            status, data = self._get_json(
                f"https://games.roblox.com/v1/games/votes?universeIds={q}"
            )
            if status != 200 or not data:
                continue
            for item in data.get("data") or []:
                try:
                    out[int(item["id"])] = {
                        "up": int(item.get("upVotes") or 0),
                        "down": int(item.get("downVotes") or 0),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
        return out

    def upsert_icons(self, icons: Dict[int, str]) -> None:
        """Persist icon URLs without touching the game rows' metrics.

        One indexed UPDATE per URL-bearing id — far cheaper than a full
        upsert_game (which would restamp tiers and rewrite every column).
        """
        if not icons:
            return
        try:
            with self._connect() as conn:
                for uid, url in icons.items():
                    conn.execute(
                        "UPDATE game_analytics SET icon_url = ? WHERE universe_id = ?",
                        (url, int(uid)),
                    )
        except sqlite3.Error as exc:
            log.warning("icon upsert failed: %s", exc)

    def upsert_votes(self, votes: Dict[int, Dict[str, int]]) -> None:
        """Persist like/dislike totals without touching the game rows' metrics."""
        if not votes:
            return
        try:
            with self._connect() as conn:
                for uid, totals in votes.items():
                    conn.execute(
                        "UPDATE game_analytics SET upvotes = ?, downvotes = ? "
                        "WHERE universe_id = ?",
                        (int(totals.get("up") or 0), int(totals.get("down") or 0), int(uid)),
                    )
        except sqlite3.Error as exc:
            log.warning("vote upsert failed: %s", exc)

    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_discord(text: Optional[str]) -> Optional[str]:
        """Regex-scan arbitrary bio/description text for a Discord invite."""
        if not text:
            return None
        match = re.search(DISCORD_REGEX, text, re.IGNORECASE)
        return normalize_discord_url(match.group(0)) if match else None

    @staticmethod
    def _links_of(payload: Optional[dict]) -> List[Dict[str, str]]:
        if not payload:
            return []
        links = payload.get("data") if isinstance(payload, dict) else None
        if links is None and isinstance(payload, dict):
            links = payload.get("socialLinks")
        return [
            {"type": str(l.get("type") or l.get("name") or ""), "url": str(l.get("url") or l.get("link") or "")}
            for l in (links or [])
            if isinstance(l, dict)
        ]

    def _fetch_links(self, url: str) -> Optional[List[Dict[str, str]]]:
        return self._fetch_links_diagnostic(url)[1]

    def _fetch_links_diagnostic(self, url: str) -> Tuple[int, Optional[List[Dict[str, str]]]]:
        status, data = self._get_json(url)
        return status, self._links_of(data) if status == 200 else None

    def resolve_game_contact(
        self,
        meta: Dict[str, Any],
        force: bool = False,
        run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Sequential fallback pipeline for one game:
          T1 game social links -> game description regex
          T2 group social links -> group description regex   (owner type Group)
          T3 user description regex                          (owner type User)
          T4 Twitter/X fallback -> status flag
        Returns {has_discord, discord_url, status, found_via, has_social_links,
                 contacts_checked_at}
        """
        uid = meta["universe_id"]
        now = time.time()
        if not hasattr(self, "last_contact_diagnostics"):
            self.last_contact_diagnostics = {}

        # Process-wide dedupe: another session may have resolved this exact
        # game seconds ago — share that verdict instead of re-hitting Roblox.
        shared = _contact_memcache_get(int(uid))
        if shared is not None and not force:
            return shared

        cached = self._load_contact_cache(uid)
        if cached and not force and now - cached["ts"] < CONTACT_RECHECK_HOURS * 3600:
            self._set_contact_diagnostic(run_id, uid, {
                "cached": True,
                "selected_source": cached["record"].get("found_via"),
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            return cached["record"]

        # Roblox is actively throttling this IP (any session's 429 sets the
        # flag). Skip live resolution entirely instead of hammering the IP and
        # caching empty verdicts for hours. The verdict is NOT persisted — the
        # game simply keeps its unchecked state and the next attempt re-runs.
        if self.throttle_window_active():
            self._set_contact_diagnostic(run_id, uid, {
                "cached": False,
                "throttled": True,
                "selected_source": None,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            return {
                "universe_id": int(uid),
                "has_discord": False,
                "discord_url": None,
                "status": THROTTLED_STATUS,
                "found_via": None,
                "has_social_links": False,
                "contacts_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

        diagnostics: Dict[str, Any] = {}

        # Priority: game sources, community sources, then community owner sources.
        # NOTE: Roblox retired the plain /social-links path (404 even with a
        # valid cookie, confirmed 2026-08-30). The live website uses the /list
        # variant below, which requires the .ROBLOSECURITY cookie (401
        # otherwise) and returns {"data": [{type, url, ...}, ...]}.
        game_link_result = self._fetch_links_diagnostic(
            f"https://games.roblox.com/v1/games/{uid}/social-links/list"
        )
        diagnostics["game_social_links"] = game_link_result[0]
        game_links = game_link_result[1] or []
        discord_url = self._pick_discord(game_links)
        found_via = "game_social_links" if discord_url else None
        if not discord_url:
            discord_url = self.extract_discord(meta.get("description"))
            found_via = "game_description" if discord_url else None

        # Community bio/social links, then the community owner's bio/social links.
        creator_type = meta.get("creator_type")
        creator_id = meta.get("creator_id")
        if not discord_url and creator_type == "Group" and creator_id:
            status_code, g_data = self._get_json(
                f"https://groups.roblox.com/v1/groups/{creator_id}"
            )
            diagnostics["group_profile"] = status_code
            g_bio = (g_data or {}).get("description", "") if status_code == 200 else ""
            discord_url = self.extract_discord(g_bio)
            if discord_url:
                found_via = "group_description"

            if not discord_url:
                group_link_result = self._fetch_links_diagnostic(
                    f"https://groups.roblox.com/v1/groups/{creator_id}/social-links"
                )
                diagnostics["group_social_links"] = group_link_result[0]
                group_links = group_link_result[1] or []
                discord_url = self._pick_discord(group_links)
                game_links.extend(group_links)
                if discord_url:
                    found_via = "group_social_links"

            # If the community has no Discord, inspect its owner's profile bio.
            # (users/.../social-links was retired by Roblox — 404 even with a
            # valid cookie — and its replacement, promotion-channels, can never
            # contain a Discord URL, so the bio scan is the remaining owner check.)
            owner_id = (g_data or {}).get("owner", {}).get("userId") if isinstance(g_data, dict) else None
            if not discord_url and owner_id:
                u_status, u_data = self._get_json(f"https://users.roblox.com/v1/users/{owner_id}")
                diagnostics["owner_profile"] = u_status
                if u_status == 200:
                    discord_url = self.extract_discord((u_data or {}).get("description", ""))
                    if discord_url:
                        found_via = "owner_description"
                    else:
                        # Some profile payloads embed link arrays directly.
                        owner_links = self._links_of(u_data)
                        game_links.extend(owner_links)
                        discord_url = self._pick_discord(owner_links)
                        if discord_url:
                            found_via = "owner_profile_links"

        status = "OK" if discord_url else "No Contact Found"
        diagnostics["selected_source"] = found_via
        diagnostics["cached"] = False
        diagnostics["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._set_contact_diagnostic(run_id, uid, diagnostics)

        record = {
            "universe_id": uid,
            "has_discord": discord_url is not None,
            "discord_url": discord_url,
            "status": status,
            "found_via": found_via,
            "has_social_links": bool(game_links or discord_url),
            "contacts_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._store_contact_cache(uid, record)
        _contact_memcache_put(int(uid), record)
        return record

    def _load_contact_cache(self, universe_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT has_discord, discord_url, status, found_via, "
                    "has_social_links, contacts_checked_at, contact_schema_version "
                    "FROM game_analytics WHERE universe_id = ? AND status IS NOT NULL",
                    (universe_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        # Verdicts resolved before the current resolver version (e.g. while the
        # game social-links endpoint was still broken) must not shadow the new
        # sources — treat them as missing so the next check re-resolves live.
        if not row or not row[5] or int(row[6] or 0) != CONTACT_RESOLVER_VERSION:
            return None
        # contacts_checked_at is stored naive-but-UTC (the pipeline writes
        # UTC everywhere); parse it as UTC, not server-local time. mktime
        # would skew the 6-hour recheck window by the host's UTC offset.
        try:
            parsed = time.strptime(str(row[5]), "%Y-%m-%d %H:%M:%S")
            ts = calendar.timegm(parsed)
        except (ValueError, OverflowError):
            return None
        return {
            "ts": ts,
            "record": {
                "universe_id": universe_id,
                "has_discord": bool(row[0]),
                "discord_url": row[1],
                "status": row[2],
                "found_via": row[3],
                "has_social_links": bool(row[4]),
                "contacts_checked_at": str(row[5]),
            },
        }

    def _store_contact_cache(self, universe_id: int, record: Dict[str, Any]) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE game_analytics SET has_discord=?, discord_url=?, "
                    "status=?, found_via=?, has_social_links=?, contacts_checked_at=?, "
                    "contact_schema_version=? WHERE universe_id=?",
                    (
                        record["has_discord"],
                        record["discord_url"],
                        record["status"],
                        record["found_via"],
                        record["has_social_links"],
                        record["contacts_checked_at"],
                        CONTACT_RESOLVER_VERSION,
                        universe_id,
                    ),
                )
        except sqlite3.Error:
            pass

    def _store_contact_verdicts_batch(self, records: Dict[int, Dict[str, Any]]) -> None:
        """Persist a page of contact verdicts in ONE transaction.

        Per-page checking used to open ~3 short write transactions per game
        (verdict + diagnostic + counter updates) on a shared SQLite file —
        dozens of concurrent writers per page across sessions, each error
        swallowed, so lost writes surfaced only as mysteriously empty results.
        This batches the verdict UPDATEs (the data that matters) into a single
        locked transaction; diagnostics stay in memory only (they are UI
        snapshots, not data — a restart losing the last page of them is
        harmless and by design).
        """
        if not records:
            return
        rows = []
        for uid, record in records.items():
            try:
                rows.append(
                    (
                        bool(record.get("has_discord")),
                        record.get("discord_url"),
                        str(record.get("status") or ""),
                        record.get("found_via"),
                        bool(record.get("has_social_links")),
                        str(record.get("contacts_checked_at") or ""),
                        CONTACT_RESOLVER_VERSION,
                        int(uid),
                    )
                )
            except (TypeError, ValueError):
                continue
        if not rows:
            return
        try:
            with DB_WRITE_LOCK:
                with self._connect() as conn:
                    conn.execute("PRAGMA busy_timeout=15000")
                    with conn:
                        conn.executemany(
                            "UPDATE game_analytics SET has_discord=?, discord_url=?, "
                            "status=?, found_via=?, has_social_links=?, contacts_checked_at=?, "
                            "contact_schema_version=? WHERE universe_id=?",
                            rows,
                        )
        except sqlite3.Error as exc:
            log.warning("Batched contact write failed (%d rows): %s", len(rows), exc)
        for uid, record in records.items():
            _contact_memcache_put(int(uid), dict(record))

    @staticmethod
    def _pick_discord(links: List[Dict[str, str]]) -> Optional[str]:
        for link in links:
            if (link.get("type") or "").lower() == "discord" and link.get("url"):
                return link["url"]
        return None

    # ------------------------------------------------------------------ #
    # Catalog expansion: Atlas Dev seed harvest + discovery-queue drain
    # (ATLAS_PLAN_REVIEW.md — strict gate: nothing below 20k visits / 25 CCU
    # is ever stored in game_analytics; discards live in discovery_queue)
    # ------------------------------------------------------------------ #

    def _enqueue_discovery(self, items: Iterable[Tuple[int, str, int]]) -> int:
        """Insert candidate universe IDs into the discovery queue (dedup).

        ``items`` yields (universe_id, source, priority). Existing rows keep
        their original source/priority — an ID already queued or evaluated is
        never duplicated or re-prioritized. Returns the number of NEW rows.
        The table is the dedup memory that lets discards stay OUT of
        game_analytics without ever wasting a second hydration on them.
        """
        rows: Dict[int, Tuple[str, int]] = {}
        for uid, source, priority in items:
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                continue
            if uid > 0 and uid not in rows:
                rows[uid] = (source, int(priority))
        if not rows:
            return 0
        inserted = 0
        try:
            with self._connect() as conn:
                uid_list = list(rows)
                for i in range(0, len(uid_list), 900):
                    chunk = uid_list[i : i + 900]
                    marks = ",".join("?" for _ in chunk)
                    have = conn.execute(
                        f"SELECT universe_id FROM discovery_queue WHERE universe_id IN ({marks})",
                        chunk,
                    ).fetchall()
                    for (existing,) in have:
                        rows.pop(int(existing), None)
                if rows:
                    # Dedup-memory ceiling: trim the OLDEST processed rows
                    # first (pending rows are the live workload, keep them).
                    overflow = (
                        conn.execute("SELECT COUNT(*) FROM discovery_queue").fetchone()[0]
                        + len(rows)
                        - EXPANSION_QUEUE_MAX_ROWS
                    )
                    if overflow > 0:
                        conn.execute(
                            "DELETE FROM discovery_queue WHERE universe_id IN ("
                            "SELECT universe_id FROM discovery_queue "
                            "WHERE status != 'pending' ORDER BY seen_at ASC LIMIT ?)",
                            (int(overflow),),
                        )
                    conn.executemany(
                        "INSERT INTO discovery_queue "
                        "(universe_id, source, priority, status, seen_at) "
                        "VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP)",
                        [(uid, src, pri) for uid, (src, pri) in rows.items()],
                    )
                    inserted = len(rows)
        except sqlite3.Error as exc:
            log.warning("discovery_queue insert failed: %s", exc)
        return inserted

    def _mark_discovery_outcome(self, uid: int, outcome: str) -> None:
        """Record the strict-gate verdict for one queued ID."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE discovery_queue SET status='processed', outcome=?, "
                    "evaluated_at=CURRENT_TIMESTAMP WHERE universe_id=?",
                    (outcome, int(uid)),
                )
        except sqlite3.Error as exc:
            log.warning("discovery_queue outcome update failed for %s: %s", uid, exc)

    def _qualified_only(self, metas: Dict[int, Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """Strict gate: keep ONLY games meeting the 20k-visits / 25-CCU target.

        Everything else must never reach game_analytics (the agreed catalog
        policy — no graveyard rows); the queue records the discard instead.
        """
        return {
            uid: meta
            for uid, meta in metas.items()
            if int(meta.get("visits") or 0) >= EXPANSION_TARGET_VISITS
            and int(meta.get("ccu") or 0) >= EXPANSION_TARGET_CCU
        }


    def _reset_stale_queue_claims(self) -> None:
        """Self-heal 'processing' claims left by a crashed run (claims only
        live minutes; anything stuck longer than 2h returns to pending)."""
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 7200))
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE discovery_queue SET status='pending' "
                    "WHERE status='processing' AND seen_at < ?",
                    (cutoff,),
                )
        except sqlite3.Error:
            pass

    def _claim_discovery_batch(self, limit_ids: int) -> List[Tuple[int, str]]:
        """Atomically claim the next pending slice (priority first, FIFO)."""
        out: List[Tuple[int, str]] = []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT universe_id, COALESCE(source, '') FROM discovery_queue
                    WHERE status='pending'
                    ORDER BY priority ASC, seen_at ASC, universe_id ASC
                    LIMIT ?
                    """,
                    (max(1, int(limit_ids)),),
                ).fetchall()
                for uid, source in rows:
                    # seen_at becomes the claim timestamp: 'processing' rows
                    # with an old seen_at are then unambiguously crashed
                    # claims (self-healed by _reset_stale_queue_claims),
                    # even when the enqueue happened seconds earlier.
                    conn.execute(
                        "UPDATE discovery_queue SET status='processing', "
                        "seen_at=CURRENT_TIMESTAMP WHERE universe_id=?",
                        (int(uid),),
                    )
                out = [(int(uid), str(source)) for uid, source in rows]
        except sqlite3.Error as exc:
            log.warning("discovery_queue claim failed: %s", exc)
        return out

    def drain_discovery_queue(
        self,
        batches: int = EXPAND_QUEUE_BATCHES_DEFAULT,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> Dict[str, int]:
        """Hydrate queued candidates and store ONLY strict-gate qualifiers.

        Claims up to ``batches`` x 50 pending IDs in priority order,
        skips IDs already in the catalog (the hydrator owns refreshing those),
        hydrates the rest through the shared paced metrics endpoint, and
        upserts qualifying games via upsert_game — full tier stamping, blow-up
        flag, ccu_history snapshot, found_via='expansion'. Every evaluated ID
        gets an outcome in the queue so nothing is re-hydrated before the
        daily Atlas harvest re-enqueues it.
        """
        report = progress_cb or (lambda p, m: None)
        self._reset_stale_queue_claims()
        stats = {"claimed": 0, "duplicates": 0, "hydrated": 0, "qualified": 0, "below_gate": 0, "metrics_failed": 0, "batches": 0}
        if batches <= 0:
            # Budget 0 is a deliberate kill switch — the old max(1, limit)
            # clamp still claimed and hydrated one row per run.
            return stats
        claimed = self._claim_discovery_batch(batches * METRICS_BATCH_SIZE)
        if not claimed:
            report(0.5, "Discovery queue empty — nothing to drain.")
            return stats
        stats["claimed"] = len(claimed)
        ids = [uid for uid, _ in claimed]
        existing: set = set()
        try:
            with self._connect() as conn:
                for i in range(0, len(ids), 900):
                    chunk = ids[i : i + 900]
                    marks = ",".join("?" for _ in chunk)
                    have = conn.execute(
                        f"SELECT universe_id FROM game_analytics WHERE universe_id IN ({marks})",
                        chunk,
                    ).fetchall()
                    existing.update(int(r[0]) for r in have)
        except sqlite3.Error:
            existing = set()
        for uid in sorted(existing):
            self._mark_discovery_outcome(uid, "already_in_catalog")
        stats["duplicates"] = len(existing)
        to_check = [uid for uid in ids if uid not in existing]
        if not to_check:
            report(0.9, "All queued candidates already in catalog.")
            return stats
        report(0.2, f"Hydrating {len(to_check)} queued expansion candidates…")
        all_metas = self.fetch_game_metrics(to_check)
        stats["hydrated"] = len(all_metas)
        stats["batches"] = (len(to_check) + METRICS_BATCH_SIZE - 1) // METRICS_BATCH_SIZE
        qualified = self._qualified_only(all_metas)
        stats["qualified"] = len(qualified)
        stats["below_gate"] = sum(1 for uid in to_check if uid in all_metas and uid not in qualified)
        stats["metrics_failed"] = sum(1 for uid in to_check if uid not in all_metas)
        for uid, meta in qualified.items():
            # upsert_game does its own tier stamping (via _tier_stamp_for),
            # peak-CCU growth and the ccu_history snapshot — identical to the
            # main pipeline. found_via only lands on brand-new rows; the
            # COALESCE in the upsert keeps it from clobbering real sources.
            self.upsert_game({
                "universe_id": int(uid),
                "root_place_id": meta.get("root_place_id"),
                "title": meta.get("title"),
                "ccu": meta.get("ccu"),
                "peak_ccu": meta.get("ccu"),
                "visits": meta.get("visits"),
                "favorites": meta.get("favorites"),
                "genre": meta.get("genre"),
                "creator_name": meta.get("creator_name"),
                "creator_type": meta.get("creator_type"),
                "creator_id": meta.get("creator_id"),
                "description": meta.get("description"),
                "found_via": "expansion",
            })
            self._mark_discovery_outcome(uid, "qualified")
        for uid in to_check:
            if uid not in qualified:
                self._mark_discovery_outcome(
                    uid, "below_gate" if uid in all_metas else "metrics_failed"
                )
        if qualified:
            # Thumbnails for the new qualifiers: the drain used to store rows
            # without icons, so every Atlas-discovered game rendered the
            # placeholder tile until a much later hydrator pass happened to
            # fill it (this is why recent Atlas games showed no thumbnail).
            report(0.93, f"Fetching icons for {len(qualified)} new games…")
            self.upsert_icons(self.fetch_game_icons(list(qualified)))
        report(
            0.9,
            f"Queue drained: {stats['qualified']} qualified · "
            f"{stats['below_gate']} below gate · {stats['duplicates']} already known",
        )
        return stats


    def run_expansion(
        self,
        queue_batches: Optional[int] = None,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> Dict[str, Any]:
        """One expansion pass: Atlas Dev harvest → discovery-queue drain.

        Atlas Dev is the sole discovery engine — the creator spiderweb,
        frontier scan, and recommendations mining were removed 2026-09-20
        (ATLAS_PLAN_REVIEW.md). The drain hydrates Atlas seeds through the
        strict 20k/25 gate. Both sub-steps are bounded and share the pacer,
        so this pass can never breach the hydrator's T1/T2 floors — it runs
        inside the expander workflow slot, not the 5-minute hydrator.
        """
        report = progress_cb or (lambda p, m: None)
        drain_limit = (
            queue_batches
            if queue_batches is not None
            else _env_int("EXPAND_QUEUE_BATCHES", EXPAND_QUEUE_BATCHES_DEFAULT)
        )
        result: Dict[str, Any] = {}
        # Atlas first (enqueue-only; its seeds drain from the NEXT run on —
        # daily cadence), then the drain so same-run enqueued candidates
        # (manual/forced harvests) still process this run.
        report(0.02, "Expansion pass: Atlas Dev seed harvest…")
        try:
            result["atlas"] = self.harvest_atlas_seeds(progress_cb=report)
        except Exception as exc:  # Atlas must never take the drain down
            log.warning("Atlas harvest failed (drain continues): %s", exc)
            result["atlas"] = {"error": str(exc), "enqueued": 0}
        report(0.5, "Expansion pass: draining the discovery queue…")
        result["drain"] = self.drain_discovery_queue(drain_limit, progress_cb=report)
        self.source_diagnostics["expansion"] = result
        self.last_scan["expansion"] = result
        return result

    # ------------------------------------------------------------------ #
    # Atlas Dev seed ingestion (ATLAS_PLAN_REVIEW.md) — discovery only.
    # ------------------------------------------------------------------ #

    def _atlas_proxy_pool(self) -> List[str]:
        """Flip-ready proxy pool for Atlas fetches (mirrors _search_proxy_urls).

        Default is direct (robots.txt allows crawling, volume is tiny); if
        Atlas ever throttles runner IPs, set RBXSCOUT_SEARCH_PROXY_URLS and
        traffic shifts through the mirrors with zero code change. ``direct``
        is the terminal entry, exactly like the search pool.
        """
        raw = os.environ.get(ATLAS_PROXY_URLS_ENV, "")
        entries: List[str] = []
        for part in re.split(r"[,;\n]+", raw or ""):
            part = part.strip().rstrip("/")
            if not part:
                continue
            if part == "direct" or re.match(r"^https?://[^/\s]+$", part):
                if part not in entries:
                    entries.append(part)
            else:
                log.warning("Ignoring malformed Atlas proxy URL: %r", part)
        if "direct" not in entries:
            entries.append("direct")
        return entries

    @staticmethod
    def _parse_atlas_stat_text(text: str) -> Optional[Tuple[str, int, int]]:
        """Extract (title, ccu, visits) from Atlas game-page meta text.

        Atlas exposes stats ONLY as SEO prose — e.g.
        ``The Black Bell [HORROR] on Atlas - 145 playing now, 29,664 total
        visits.`` (verified live 2026-09-19: no JSON, no API routes; the
        index payload carries IDs only). Returns None when the format changed.
        """
        if not text:
            return None
        match = re.search(
            r"(?P<title>.+?)\s+on Atlas\s*-\s*(?P<ccu>\d[\d,]*)\s+playing now,\s+"
            r"(?P<visits>\d[\d,]*)\s+total visits",
            text,
        )
        if not match:
            return None
        try:
            return (
                match.group("title").strip(),
                int(match.group("ccu").replace(",", "")),
                int(match.group("visits").replace(",", "")),
            )
        except ValueError:
            return None

    def _atlas_fetch(self, url: str, delay: float) -> Optional[requests.Response]:
        """One polite Atlas fetch: honest UA, optional mirror pool, backoff.

        Pool entries are ``direct`` or a path-mirror service: a request to
        ``<mirror>/<target-url>`` must return the target's body (the shape
        the rbxscout Cloudflare Worker serves). Any single-entry failure —
        403/404, network error, or repeated 429/5xx — falls through to the
        next entry; ``direct`` is always the terminal fallback so a broken
        mirror can never abort a sweep. Only when every entry fails does
        the caller receive None (abort, pointers untouched, next run
        retries the same range).
        """
        last_status: Optional[int] = None
        for attempt, entry in enumerate(self._atlas_proxy_pool()):
            target = url if entry == "direct" else f"{entry}/{url}"
            try:
                resp = requests.get(
                    target,
                    headers={"User-Agent": ATLAS_USER_AGENT, "Accept-Language": "en"},
                    timeout=30,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_status = resp.status_code
                    time.sleep(2.0 * (attempt + 1))
                    continue
                if resp.status_code == 200:
                    time.sleep(delay)
                    return resp
                last_status = resp.status_code
                log.warning(
                    "Atlas fetch %s via %s -> HTTP %s", url, entry, resp.status_code
                )
            except requests.RequestException as exc:
                last_status = -1
                log.warning("Atlas fetch failed via %s: %s", entry, exc)
                time.sleep(1.0)
        if last_status is not None and last_status > 0:
            log.warning("Atlas pool exhausted (last HTTP %s): %s", last_status, url)
        return None

    def _atlas_pointer(self, pointer_id: str) -> Optional[float]:
        """Read one of the atlas_* scan_pointers rows (timestamp or cursor)."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT last_universe_id, updated_at FROM scan_pointers WHERE id = ?",
                    (pointer_id,),
                ).fetchone()
            if not row:
                return None
            if pointer_id == "atlas_page":
                if row[0] is not None:
                    return float(row[0])
                return None
            stamp = row[1] or row[0]
            if stamp is None:
                return None
            # 'atlas_last_run' stores an epoch in last_universe_id — that is
            # the authoritative stamp (updated_at refreshes on any write).
            if pointer_id == "atlas_last_run" and row[0] is not None:
                return float(row[0])
            try:
                return datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                return None
        except sqlite3.Error:
            return None

    def _set_atlas_pointer(self, pointer_id: str, value: float) -> None:
        """Upsert one atlas_* pointer (24h throttle stamp or page cursor)."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO scan_pointers (id, last_universe_id, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        last_universe_id = excluded.last_universe_id,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (pointer_id, int(value)),
                )
        except sqlite3.Error as exc:
            log.warning("atlas pointer update failed (%s): %s", pointer_id, exc)

    def _provisional_atlas_row(self, uid: int, title: str, ccu: int, visits: int) -> bool:
        """First-paint row from Atlas meta prose (optional, budgeted).

        Writes ccu/visits/title with found_via='atlas_dev' and NO ccu_history
        snapshot (Atlas numbers must never enter the trend table). The strict
        gate still applies — only rows meeting 20k/25 are stored, mirroring
        the queue's drain behavior. The hydrator overwrites with fresh Roblox
        stats on first refresh; found_via is COALESCE'd so the original
        source stamp survives. Returns True when a NEW row was created.
        """
        if ccu < EXPANSION_TARGET_CCU or visits < EXPANSION_TARGET_VISITS:
            return False
        try:
            with self._connect() as conn:
                known = conn.execute(
                    "SELECT 1 FROM game_analytics WHERE universe_id = ?", (uid,)
                ).fetchone()
            if known:
                return False
            self.upsert_game(
                {
                    "universe_id": uid,
                    "title": title,
                    "ccu": ccu,
                    "peak_ccu": ccu,
                    "visits": visits,
                    "found_via": "atlas_dev",
                },
                record_ccu_history=False,
            )
            return True
        except sqlite3.Error as exc:
            log.warning("provisional atlas row failed for %s: %s", uid, exc)
            return False

    def harvest_atlas_seeds(
        self,
        pages: Optional[int] = None,
        stat_pages: Optional[int] = None,
        throttle_hours: Optional[int] = None,
        deep_every_days: Optional[int] = None,
        priority: Optional[int] = None,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> Dict[str, Any]:
        """One Atlas harvest: 24h-throttled sweep of /analyze → discovery queue.

        - ``pages``: index pages fetched per run (steady state; 0 disables).
        - ``stat_pages``: budget of game pages fetched for provisional
          first-paint rows (0 disables the bootstrap entirely).
        - ``throttle_hours``: minimum hours between harvests (0 disables the
          throttle — used by tests and manual forced runs).
        - ``deep_every_days``: a full catch-up sweep (up to ATLAS_DEEP_PAGES)
          runs this often; 0 = never beyond the page budget.

        Freshly harvested IDs go through the existing dedup/trim
        _enqueue_discovery with source='atlas_dev'; the queue drain owns the
        strict gate. Failed fetches advance nothing (next run retries); 429s
        bail out immediately. stat_pages provisional rows are written FIRST
        (newest seeds first) and never displace queue ingestion.
        """
        report = progress_cb or (lambda p, m: None)
        stats: Dict[str, Any] = {
            "fetched_ids": 0,
            "pages_fetched": 0,
            "enqueued": 0,
            "provisional_rows": 0,
            "throttled": False,
            "aborted": False,
            "deep_sweep": False,
        }
        page_budget = (
            max(0, int(pages))
            if pages is not None
            else _env_int("ATLAS_SWEEP_PAGES", ATLAS_SWEEP_PAGES_DEFAULT)
        )
        stat_budget = (
            max(0, int(stat_pages))
            if stat_pages is not None
            else _env_int("ATLAS_STAT_PAGES", ATLAS_STAT_PAGES_DEFAULT)
        )
        throttle = (
            ATLAS_HOURS_DEFAULT
            if throttle_hours is None
            else max(0.0, float(throttle_hours))
        )
        if throttle_hours is None:
            # Ops override: ATLAS_THROTTLE_HOURS=0 forces an immediate harvest.
            try:
                throttle = max(0.0, float(os.environ.get("ATLAS_THROTTLE_HOURS", "") or ATLAS_HOURS_DEFAULT))
            except ValueError:
                pass
        deep_days = (
            ATLAS_DEEP_EVERY_DAYS_DEFAULT
            if deep_every_days is None
            else max(0, int(deep_every_days))
        )
        queue_priority = (
            ATLAS_PRIORITY_DEFAULT if priority is None else max(1, int(priority))
        )
        try:
            # Default 3s is the polite posture; 0 is allowed explicitly
            # (tests / forced local runs) — never silently increased here.
            delay = max(0.0, float(os.environ.get("ATLAS_REQUEST_DELAY", "") or ATLAS_REQUEST_DELAY_DEFAULT))
        except ValueError:
            delay = ATLAS_REQUEST_DELAY_DEFAULT
        if page_budget == 0 and stat_budget == 0:
            return stats  # kill switch: module fully disabled

        # ---- 24h self-throttle (atlas_last_run pointer) --------------------
        if throttle > 0:
            last = self._atlas_pointer("atlas_last_run")
            if last is not None and (time.time() - last) < throttle * 3600:
                stats["throttled"] = True
                return stats

        # ---- deep-sweep decision (catch-up cursor in atlas_page) ----------
        now = time.time()
        cursor_row = None
        try:
            with self._connect() as conn:
                cursor_row = conn.execute(
                    "SELECT last_universe_id, updated_at FROM scan_pointers WHERE id = 'atlas_page'"
                ).fetchone()
        except sqlite3.Error:
            cursor_row = None
        start_page = 1
        deep_due = False
        if cursor_row is not None and cursor_row[0] is not None:
            start_page = int(cursor_row[0]) or 1
            try:
                stamp = datetime.strptime(str(cursor_row[1] or "")[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                deep_due = deep_days > 0 and (now - stamp) >= deep_days * 86400
            except ValueError:
                deep_due = False
        elif deep_days > 0:
            deep_due = True
        sweep_pages = page_budget
        if deep_due:
            sweep_pages = ATLAS_DEEP_PAGES_DEFAULT
            stats["deep_sweep"] = True
        if sweep_pages <= 0:
            # No index fetching (stat bootstrap only) — still stamp the run.
            self._set_atlas_pointer("atlas_last_run", now)
            return stats

        # ---- index sweep ---------------------------------------------------
        harvested: Dict[int, None] = {}
        fetched = 0
        for page in range(start_page, start_page + sweep_pages):
            if fetched >= sweep_pages:
                break
            url = f"{ATLAS_BASE_URL}?{ATLAS_QUERY}" + (f"&page={page}" if page > 1 else "")
            resp = self._atlas_fetch(url, delay)
            if resp is None:
                stats["aborted"] = True
                break  # pointer untouched: next run retries this range
            fetched += 1
            stats["pages_fetched"] += 1
            for uid_s in re.findall(r'href="/analyze/(\d{8,12})"', resp.text):
                uid = int(uid_s)
                if uid > 0 and uid not in harvested:
                    harvested[uid] = None
            report(0.1 + 0.5 * fetched / max(1, sweep_pages), f"Atlas page {page}: +{len(harvested)} IDs")
        stats["fetched_ids"] = len(harvested)
        stats["sweep_pages"] = fetched
        if fetched == 0:
            return stats  # nothing succeeded; leave throttle un-stamped

        # ---- provisional first-paint rows (budgeted, newest seeds first) ---
        if stat_budget > 0:
            done = 0
            for uid in sorted(harvested, reverse=True):
                if done >= stat_budget:
                    break
                game_html = self._atlas_fetch(f"{ATLAS_BASE_URL}/{uid}", delay)
                if game_html is None:
                    continue
                done += 1
                meta = re.search(r'<meta name="description" content="([^"]+)"', game_html.text)
                if not meta:
                    continue
                parsed = self._parse_atlas_stat_text(meta.group(1))
                if not parsed:
                    continue
                title, ccu, visits = parsed
                if self._provisional_atlas_row(uid, title, ccu, visits):
                    stats["provisional_rows"] += 1
            report(0.7, f"Atlas first-paint rows: {stats['provisional_rows']}")

        # ---- enqueue → the existing drain + strict gate own the rest -------
        stats["enqueued"] = self._enqueue_discovery(
            (uid, "atlas_dev", queue_priority) for uid in harvested
        )

        # ---- commit pointers ONLY after a successful harvest ---------------
        self._set_atlas_pointer("atlas_last_run", now)
        completed = fetched >= sweep_pages
        self._set_atlas_pointer("atlas_page", 1 if completed else start_page + fetched)
        report(0.95, f"Atlas harvest: +{stats['enqueued']} queued, {stats['provisional_rows']} first-paint rows")
        return stats

    # ------------------------------------------------------------------ #
    # Full scan orchestration
    # ------------------------------------------------------------------ #

    def scan(
        self,
        limit: Optional[int] = None,
        deep_contacts: bool = True,
        force_contacts: bool = False,
        min_visits: int = 0,
        min_ccu: int = 0,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        initial_contact_limit: int = 20,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        phases: Optional[Iterable[str]] = None,
    ) -> pd.DataFrame:
        """Fetch metrics, apply target thresholds, and optionally check contacts.

        ``candidate_limit`` is a legacy bound from the removed discovery
        phases; the discovery-queue drain and the tier scheduler own all
        selection now. Contact requests are separate so the UI can load them
        page by page.

        ``phases`` splits the pipeline's jobs:
        - ``"hydrate"`` — drain the tier-due refresh queue for games ALREADY
          in the catalog. No discovery requests; the cheap, fast pass the
          5-minute cron runs. Tier cadences are wall-clock hours, so this
          stays correct at any call frequency.
        - ``"expand"`` — Atlas Dev harvest + discovery-queue drain through
          the strict gate (the expander workflow's pass).
        - ``None`` (default) — full pipeline: hydrate + expand.
        """
        selected = [p.strip().lower() for p in (phases or ()) if p and p.strip()]
        invalid = [p for p in selected if p not in ("hydrate", "expand")]
        if invalid:
            raise ValueError(
                f"Unknown scan phases: {invalid}. Use 'hydrate', 'expand', or None."
            )
        phase_set = set(selected) or {"hydrate", "expand"}
        do_hydrate = "hydrate" in phase_set
        do_expand = "expand" in phase_set
        report = progress_cb or (lambda p, m: None)
        run_id = self._begin_scan()
        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        candidate_limit = max(1, int(candidate_limit or DEFAULT_CANDIDATE_LIMIT))
        min_visits = max(0, int(min_visits or 0))
        min_ccu = max(0, int(min_ccu or 0))
        self.last_metrics = {}
        self.last_contact_diagnostics = {}
        self.source_diagnostics = {}
        # Fresh per-sync set: blowup_watch_count must reflect THIS sync's
        # detections, not an accumulation across every sync in the session.
        self.blowup_watch_events = {}
        self.last_scan = {
            "run_id": run_id,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "source_count": 0,
            "candidate_count": 0,
            "matched_count": 0,
            "candidate_limit": candidate_limit,
            "min_visits": min_visits,
            "min_ccu": min_ccu,
            "metrics_count": 0,
            "contacts_attempted": 0,
            "contacts_completed": 0,
            "contact_errors": 0,
            "error": None,
            "catalog_count": 0,
            "keyword_slice_start": 0,
            "keyword_slice_end": 0,
            "keyword_discovered": 0,
            "sync_number": 0,
            "tier_schedule": {},
            "tier_counts": {},
            "hydration_budget": {},
            "pruned_stale": 0,
            "blowup_watch_count": 0,
            "mode": "expand" if do_expand else None,
            "expansion": {},
        }
        # The sync counter persists across restarts via the .sync_state
        # sidecar file (still used as the weekly T5–T7 marker).
        sync_number = self.bump_sync_sequence()
        self.last_scan["sync_number"] = sync_number

        # Expansion is a standalone mode: the expander workflow runs ONLY the
        # expansion pass so its request budget can never crowd out the
        # finder/hydrator tiers (the PILOT_PLAN refresh floors stay untouchable). It runs
        # AFTER the run bookkeeping above so the pass lands in scan_runs with
        # the standard lifecycle.
        if do_expand:
            return self._scan_expand(min_visits, min_ccu, progress_cb)

        try:
            # Discovery (the former find phase: deep charts, Rolimons,
            # keyword crawler) was removed 2026-09-20 — Atlas Dev is the sole
            # candidate source now; full runs hydrate the tier-due queue only.
            # ------------------------------------------------------------------
            # Budgeted hydration: known games selected by tier cadence until
            # the per-sync request budget is spent. Whatever does not fit
            # rolls to the next sync; tiers not due cost zero requests.
            # New-game hydration is owned by the discovery-queue drain, which
            # runs on expand passes (Atlas seeds) — never here.
            # ------------------------------------------------------------------
            if do_hydrate:
                schedule = self.load_tier_refresh_ids(
                    sync_number=sync_number, budget_batches=HYDRATION_BUDGET_PER_SYNC
                )
            else:
                schedule = {"ids": [], "groups": {}, "tier_counts": {}}
            self.last_scan["tier_schedule"] = schedule["groups"]
            self.last_scan["tier_counts"] = schedule["tier_counts"]
            known_due = schedule["ids"]
            budget_cap = HYDRATION_BUDGET_PER_SYNC * 50
            hydration_ids = known_due[:budget_cap]
            self.last_scan["hydration_budget"] = {
                "new": 0,
                "known_due": len(known_due),
                "hydrated": len(hydration_ids),
                "deferred": max(0, len(known_due) - len(hydration_ids)),
                "budget_batches": HYDRATION_BUDGET_PER_SYNC,
            }

            if not hydration_ids:
                # Nothing due (or nothing new to hydrate): finish cleanly
                # without spending a single request — the normal 5-minute
                # hydrator outcome once the due-queue is drained.
                self.last_scan.update({
                    "status": "complete",
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "matched_count": 0,
                    "metrics_count": 0,
                })
                self._finish_scan()
                report(1.0, "Hydration pass complete — nothing was due.")
                return pd.DataFrame()
            report(0.35, f"Fetching metrics for {len(hydration_ids)} games (tier-budgeted)…")
            all_metrics = self.fetch_game_metrics(hydration_ids)

            # Catalog-grade upsert: store EVERY hydrated game, not only
            # matches — that's what makes game_analytics a real filtering
            # database instead of a scan result. Discord columns are
            # COALESCE-preserved across upserts.
            report(0.42, f"Upserting {len(all_metrics)} games to catalog…")
            for meta in all_metrics.values():
                uid = int(meta["universe_id"])
                meta.setdefault("icon_url", None)
                self.upsert_game({
                    "universe_id": uid,
                    "root_place_id": meta.get("root_place_id"),
                    "title": meta.get("title"),
                    "ccu": meta.get("ccu"),
                    "peak_ccu": max(
                        int(meta.get("ccu") or 0),
                        int(meta.get("peak_ccu") or 0),
                    ),
                    "visits": meta.get("visits"),
                    "favorites": meta.get("favorites"),
                    "genre": meta.get("genre"),
                    "creator_name": meta.get("creator_name"),
                    "creator_type": meta.get("creator_type"),
                    "creator_id": meta.get("creator_id"),
                    "description": meta.get("description"),
                    "icon_url": meta.get("icon_url"),
                })
            matched = {
                uid: meta for uid, meta in all_metrics.items()
                if int(meta.get("visits") or 0) >= min_visits
                and int(meta.get("ccu") or 0) >= min_ccu
            }
            if limit and limit > 0:
                ranked_matches = sorted(matched.values(), key=lambda g: -int(g.get("ccu") or 0))[:int(limit)]
                matched = {int(meta["universe_id"]): meta for meta in ranked_matches}
            matched_ids = list(matched)
            self.last_metrics = matched
            self.last_scan.update({
                "matched_count": len(matched_ids),
                "metrics_count": len(matched_ids),
            })

            report(0.48, f"{len(matched_ids)} games meet your targets. Fetching icons…")
            icons = self.fetch_game_icons(matched_ids)
            report(0.52, "Saving icons to SQLite…")
            for meta in matched.values():
                uid = int(meta["universe_id"])
                meta["icon_url"] = icons.get(uid)
                self.upsert_game({
                    "universe_id": uid,
                    "root_place_id": meta.get("root_place_id"),
                    "title": meta.get("title"),
                    "ccu": meta.get("ccu"),
                    "peak_ccu": max(
                        int(meta.get("ccu") or 0),
                        int(meta.get("peak_ccu") or 0),
                    ),
                    "visits": meta.get("visits"),
                    "favorites": meta.get("favorites"),
                    "genre": meta.get("genre"),
                    "creator_name": meta.get("creator_name"),
                    "creator_type": meta.get("creator_type"),
                    "creator_id": meta.get("creator_id"),
                    "description": meta.get("description"),
                    "icon_url": meta.get("icon_url"),
                })

            self.last_scan["catalog_count"] = int(self.load_table().shape[0])
            self.last_scan["pruned_stale"] = self.prune_catalog()
            self.last_scan["blowup_watch_count"] = len(self.blowup_watch_events)
            if self.blowup_watch_events:
                self.last_scan["blowup_watch"] = dict(self.blowup_watch_events)

            if deep_contacts and matched_ids:
                first_page_ids = matched_ids[:max(1, int(initial_contact_limit or 20))]
                report(0.55, f"Checking Discord contacts for the first {len(first_page_ids)} results…")
                self.scan_contacts(
                    first_page_ids,
                    force=force_contacts,
                    run_id=run_id,
                    progress_cb=lambda p, m: report(0.55 + 0.35 * p, m),
                )
            else:
                report(0.95, "Metrics ready. Contact checks load one page at a time.")

            self.last_scan.update({
                "status": "complete",
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "metrics_count": len(matched_ids),
            })
            self._finish_scan()
            report(1.0, f"Scan complete — {len(matched_ids)} matching games ready.")
            return self.load_table(matched_ids)
        except Exception as exc:
            self.mark_scan_failed(exc)
            raise

    def _scan_expand(
        self,
        min_visits: int,
        min_ccu: int,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> pd.DataFrame:
        """Run one expansion pass (strict gate) as a full scan-mode workflow.

        Runs inside scan() so the run lands in scan_runs (dashboard diagnostics)
        and inherits the standard run lifecycle; the strict gate is enforced by
        drain_discovery_queue regardless of the caller's min_visits/min_ccu
        arguments (the expander workflow passes 20000/25).
        """
        report = progress_cb or (lambda p, m: None)
        result = self.run_expansion(progress_cb=report)
        atlas = result.get("atlas") or {}
        drain = result.get("drain")
        qualified = int(drain.get("qualified") or 0)
        self.last_scan.update({
            "status": "complete",
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "metrics_count": qualified,
            "matched_count": qualified,
            "candidate_count": int(atlas.get("enqueued") or 0),
            "catalog_count": int(self.load_table().shape[0]),
        })
        self._finish_scan()
        report(1.0, f"Expansion complete — {qualified} new qualified games stored.")
        return self.load_catalog_matches(min_visits=EXPANSION_TARGET_VISITS, min_ccu=EXPANSION_TARGET_CCU)

    def scan_contacts(
        self,
        universe_ids: Iterable[int],
        force: bool = False,
        run_id: Optional[int] = None,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> pd.DataFrame:
        """Resolve contacts only for the requested page of matching games."""
        report = progress_cb or (lambda p, m: None)
        ids = list(dict.fromkeys(int(uid) for uid in universe_ids))
        if not ids:
            return pd.DataFrame()

        metas: Dict[int, Dict[str, Any]] = {}
        for uid in ids:
            meta = self.last_metrics.get(uid)
            if meta:
                metas[uid] = meta
                continue
            loaded = self.load_table([uid])
            if not loaded.empty:
                row = loaded.iloc[0].to_dict()
                metas[uid] = {
                    key: (None if pd.isna(value) else value)
                    for key, value in row.items()
                }
                metas[uid]["universe_id"] = uid
        if not metas:
            return pd.DataFrame()

        if run_id is None:
            run_id = self.last_scan.get("run_id")
        if run_id is None:
            # Synthetic in-memory run id (negative: can never collide with a
            # pipeline scan_runs row). Dashboard sessions are read-only catalog
            # consumers — INSERTing a scan_runs row per user page re-check was
            # pure write contention on the shared catalog for zero value.
            # _finish_scan skips non-owned ids, so nothing is ever written.
            run_id = -(int(time.time() * 1000) % 1_000_000_000) - 1
            self.last_scan = {
                "run_id": run_id,
                "status": "running",
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source_count": 0,
                "candidate_count": len(metas),
                "matched_count": len(metas),
                "metrics_count": len(metas),
                "contacts_attempted": 0,
                "contacts_completed": 0,
                "contact_errors": 0,
                "error": None,
            }

        prior_attempted = int(self.last_scan.get("contacts_attempted") or 0)
        prior_completed = int(self.last_scan.get("contacts_completed") or 0)
        prior_errors = int(self.last_scan.get("contact_errors") or 0)
        errors = 0
        completed = 0
        report(0.0, f"Checking Discord contacts 0/{len(metas)}…")
        records: Dict[int, Dict[str, Any]] = {}
        throttled_ids: List[int] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self.resolve_game_contact, meta, force, run_id): uid
                for uid, meta in metas.items()
            }
            for index, future in enumerate(as_completed(futures), start=1):
                uid = futures[future]
                try:
                    record = future.result()
                except Exception as exc:
                    errors += 1
                    log.warning("Contact resolution failed for %s: %s", uid, exc)
                    self._set_contact_diagnostic(run_id, uid, {
                        "cached": False,
                        "error": str(exc),
                        "selected_source": None,
                        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    record = None
                if record:
                    if record.get("status") == THROTTLED_STATUS:
                        throttled_ids.append(uid)
                    else:
                        completed += 1
                        records[uid] = record
                report(index / max(1, len(metas)), f"Contacts checked {index}/{len(metas)}…")

        # One transaction for the whole page: dozens of tiny concurrent write
        # transactions on the shared catalog file were the main source of
        # "database is locked" losses under multiple users.
        self._store_contact_verdicts_batch(records)
        for uid in throttled_ids:
            self._set_contact_diagnostic(run_id, uid, {
                "cached": False,
                "throttled": True,
                "selected_source": None,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

        # In-memory scan counters only. The dashboard is a read-only catalog
        # consumer: writing one scan_runs row per user page-view (plus the
        # finish UPDATE) was pure write contention — the 24/7 pipeline still
        # persists its own runs from its own process.
        self.last_scan.update({
            "status": "complete",
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "contacts_attempted": prior_attempted + len(metas),
            "contacts_completed": prior_completed + completed,
            "contact_errors": prior_errors + errors,
        })
        return self.load_table(ids)


# --------------------------------------------------------------------------- #
# Filter engine (pure, unit-testable)
# --------------------------------------------------------------------------- #

DISCORD_FILTER_ALL = "All Games"
DISCORD_FILTER_TRUE = "Discord Available (True)"
DISCORD_FILTER_FALSE = "No Discord (False)"

SOCIAL_FILTER_ALL = "All"
SOCIAL_FILTER_ON = "Social Links On"
SOCIAL_FILTER_OFF = "Social Links Off"


def apply_filters(
    df: pd.DataFrame,
    search: str = "",
    min_visits: int = 0,
    max_visits: Optional[int] = None,
    min_ccu: int = 0,
    max_ccu: Optional[int] = None,
    min_peak_ccu: int = 0,
    max_peak_ccu: Optional[int] = None,
    discord_filter: str = DISCORD_FILTER_ALL,
    social_filter: str = SOCIAL_FILTER_ALL,
    genres: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Dynamic multi-variable filter engine over the tracked-games frame."""
    out = df
    if search:
        needle = search.strip().lower()
        out = out[
            out["title"].str.lower().str.contains(needle, na=False)
            | out.get("creator_name", pd.Series("", index=out.index))
            .astype(str)
            .str.lower()
            .str.contains(needle, na=False)
        ]
    if min_visits:
        out = out[out["visits"] >= min_visits]
    if max_visits is not None:
        out = out[out["visits"] <= max_visits]
    if min_ccu:
        out = out[out["ccu"] >= min_ccu]
    if max_ccu is not None:
        out = out[out["ccu"] <= max_ccu]
    if min_peak_ccu:
        out = out[out["peak_ccu"].fillna(0) >= min_peak_ccu]
    if max_peak_ccu is not None:
        out = out[out["peak_ccu"].fillna(0) <= max_peak_ccu]

    if discord_filter == DISCORD_FILTER_TRUE:
        out = out[out["has_discord"] == True]  # noqa: E712
    elif discord_filter == DISCORD_FILTER_FALSE:
        out = out[out["has_discord"] != True]  # noqa: E712
    if social_filter == SOCIAL_FILTER_ON:
        out = out[out["has_social_links"] == True]  # noqa: E712
    elif social_filter == SOCIAL_FILTER_OFF:
        out = out[out["has_social_links"] != True]  # noqa: E712

    if genres:
        out = out[out["genre"].isin(genres)]
    return out