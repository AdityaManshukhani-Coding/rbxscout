"""
RbxScout — Automated Roblox Scouting & Contact Identification (core engine).

Sourcing pipeline:
  1. Roblox Discovery (explore-api get-sorts)  -> live front-page charts (universeIds embedded)
  2. Rolimon's gamelist (fallback/bulk)        -> placeIds resolved to universeIds
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
import random
import re
import sqlite3
import threading
import time
import uuid
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
# Regexes
# --------------------------------------------------------------------------- #

DISCORD_REGEX = (
    r"(?:https?://)?(?:www\.)?"
    r"(?:discord\.(?:gg|io|me|li)|discordapp\.com/invite|dsc\.gg)"
    r"/[a-zA-Z0-9\-_]+"
)

DISCORD_LOGO_URL = "https://cdn.simpleicons.org/discord/5865F2"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

ROBLOX_BASE = "https://www.roblox.com"
CONTACT_RECHECK_HOURS = 6  # skip re-resolving contacts more often than this
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
    1: 1,
    2: 1,
    3: 2,
    4: 3,
    5: None,
    6: None,
    7: None,
}
WEEKLY_TIER_REFRESH_DAYS = 7
TIER8_ROTATION_DAYS = 3  # rotating slice every ~3–7 days
TIER8_STALE_PRUNE_DAYS = 14  # below-threshold games untouched for 14d get pruned
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

# Keyword dictionary for the omni-search crawler (Phase 2).
# 9 themed slices, deduped (675 raw → 661 unique; first occurrence kept).
# Each sync crawls the next KEYWORDS_PER_SYNC-word slice of this list.
# NOTE: slice rotation is positional — keep edits append-only, or reset
# keyword_crawl_state.next_index to 0 after big mid-list edits.
KEYWORD_DICTIONARY = [
    # -- Slice 1: Action Prefixes & Core Mechanics (words 1-87) ------------
    "steal a", "rob a", "grow a", "build a", "escape the", "survive the",
    "raise a", "feed a", "catch a", "collect the", "upgrade your", "buy a",
    "sell a", "duplicate", "duping", "trading", "auction", "steal", "rob",
    "heist", "loot", "snatch", "raid", "break into", "break out", "run from",
    "hide from", "beat the", "defeat the", "absorb", "merge", "fuse",
    "evolve", "hatch", "spin for", "roll for", "luck", "rng", "flex",
    "flexing", "wealth", "millionaire", "billionaire", "richest", "poorest",
    "zero to hero", "1% luck", "99% impossible", "hard mode", "hardcore",
    "infinite", "unlimited", "auto farm", "auto click", "rebirth",
    "prestige", "ascension", "multiplier", "speedrun", "obby but",
    "tycoon but", "simulator but", "game but", "world but", "every second",
    "every click", "every step", "+1 speed", "+1 jump", "+1 size",
    "+1 strength", "+1 brainrot", "+1 cash", "+1 power", "grow bigger",
    "get taller", "get stronger", "get richer", "reach the end",
    "reach the top", "climb the", "fall down", "don't fall", "don't die",
    "red light green light", "floor is lava", "glass bridge",
    # -- Slice 2: Brainrot, Meme & Viral Tropes (words 88-169) -------------
    "brainrot", "skibidi", "gyatt", "rizz", "rizzler", "mewing", "looksmax",
    "fanum tax", "ohio", "grimace", "sigma", "alpha", "omega", "sussy",
    "amogus", "imposter", "pibby", "glitch", "goon", "edge", "jelq",
    "zesty", "chungus", "bing chilling", "griddy", "quandale", "caseoh",
    "kaicenat", "speed", "streamer", "viral", "tiktoker", "youtube",
    "trending", "brainrot god", "la vacca", "saturno", "saturnita",
    "gassy", "pomni", "digital circus", "mascot horror", "huggy", "poppy",
    "banban", "garten", "fnaf", "freddy", "bendy", "baldi", "granny",
    "slap", "smurf cat", "strawberry elephant", "blud", "dawg", "capybara",
    "doge", "cheems", "nyan", "pepe", "wojak", "chad", "gigachad", "NPC",
    "doomer", "bloomer", "soyjak", "skull emoji", "brainrot tycoon",
    "brainrot simulator", "steal brainrot", "rob brainrot", "brainrot obby",
    "brainrot rng", "brainrot evolution", "brainrot fight", "brainrot merge",
    "brainrot box", "brainrot trade", "brainrot empire", "brainrot escape",
    # -- Slice 3: Game Genres & Setting Modifiers (words 170-265) ----------
    "obby", "tycoon", "simulator", "horror", "anime", "parkour", "clicker",
    "roleplay", "zombie", "pet", "race", "tower", "fighting", "shooter",
    "survival", "escape", "puzzle", "builder", "farming", "city", "story",
    "adventure", "magic", "sword", "ninja", "pirate", "space", "dragon",
    "monster", "dungeon", "arena", "battle", "war", "army", "kingdom",
    "empire", "castle", "hero", "superhero", "villain", "prison", "school",
    "hospital", "hotel", "restaurant", "cafe", "bakery", "salon", "spa",
    "gym", "dance", "music", "art", "fashion", "model", "beauty", "makeup",
    "dress", "wedding", "baby", "family", "date", "love", "romance",
    "vampire", "werewolf", "ghost", "haunted", "spooky", "creepy", "dark",
    "night", "murder", "mystery", "detective", "spy", "military", "naval",
    "aviation", "spaceflight", "sci-fi", "cyberpunk", "steampunk",
    "post apocalypse", "wasteland", "nuclear", "fallout", "wilderness",
    "ocean", "deep sea", "subterranean", "cave", "portal", "multiversal",
    "quantum", "apocalyptic",
    # -- Slice 4: Emerging Meta Mechanics & RNG Hooks (words 266-335) ------
    "aura", "rolls", "spins", "luck potion", "luck boost", "admin abuse",
    "admin event", "secret drop", "mythic drop", "legendary drop",
    "brainrot god drop", "pity system", "trade market", "market crash",
    "inflation", "base skin", "red carpet", "fuse machine", "rng machine",
    "luck machine", "mutation", "shiny", "inverted", "golden", "rainbow",
    "void", "cosmic", "celestial", "galactic", "divine", "cursed",
    "blessed", "enchanted", "awakened", "transcended", "infinite luck",
    "10x luck", "100x luck", "weekend event", "update log", "patch notes",
    "secret room", "secret code", "dev code", "free code", "free ugc",
    "robux boost", "vip pass", "gamepass", "private server", "custom server",
    "server hop", "auto spin", "auto roll", "potion brewing", "card pack",
    "gacha", "lootbox", "crate opening", "mystery box", "roulette",
    "wheel spin", "jackpot", "high roller", "fortune", "outcome",
    "probability", "odds", "golden roll", "secret luck",
    # -- Slice 5: Anime, Pop Culture & Fandom Hooks (words 336-398) --------
    "blox fruits", "anime battlegrounds", "strongest battlegrounds",
    "blade ball", "anime fighting", "anime tycoon", "anime adventures",
    "anime last stand", "jujutsu", "demon slayer", "one piece", "naruto",
    "dragon ball", "attack on titan", "chainsaw man", "spy x family",
    "my hero", "hunter hunter", "solo leveling", "tower of god",
    "god of high school", "fire force", "black clover", "dr stone",
    "re zero", "sword art online", "konosuba", "overlord", "slime isekai",
    "mushoku tensei", "shield hero", "blue lock", "haikyuu", "kaiju no 8",
    "wind breaker", "dandadan", "kagurabachi", "sakamoto days", "frieren",
    "apothecary diaries", "undead unluck", "shangri la frontier", "mashle",
    "domain expansion", "hollow purple", "bankai", "gear 5",
    "ultra instinct", "demon mark", "sun breathing", "shadow monarch",
    "aura flex", "haki", "devil fruit", "stand power", "chakra", "nen",
    "grimoire", "zanpakuto", "kagune", "titan shift", "breathing style",
    "cursed technique",
    # -- Slice 6: High-Retention Economy & Systems (words 399-466) ---------
    "level up", "max level", "level cap", "exponential", "stat point",
    "skill tree", "mastery", "rank", "tier list", "meta", "best build",
    "weapon craft", "blacksmith", "forging", "alchemy", "enchantment",
    "soulbound", "untradeable", "auction house", "player market", "economy",
    "stock market", "company", "business", "monopoly", "factory",
    "automation", "worker", "minion", "pet evolution", "pet fusion",
    "pet tier", "egg hatch", "giant pet", "huge pet", "titanic pet",
    "exclusive pet", "secret pet", "event pet", "limited edition", "badge",
    "achievement", "leaderboard", "top 1", "rank 1", "global rank",
    "season pass", "battle pass", "daily streak", "daily reward",
    "spin wheel", "login reward", "play time reward", "afk area", "world 1",
    "world 2", "dimension", "rebirth area", "rebirth currency", "gems",
    "diamonds", "coins", "cash", "tokens", "souls", "energy", "mana",
    "power",
    # -- Slice 7: Stealth, Horror & Social Friction (words 467-531) --------
    "doors", "piggy", "evade", "pressure", "grace", "specter",
    "phasmophobia", "lethal company", "content warning", "mimic", "entity",
    "stalker", "jumpscare", "flashlight", "stamina", "insanity", "anomaly",
    "backrooms", "level 0", "liminal space", "scp", "foundation",
    "containment", "outbreak", "anomaly scanner", "night guard",
    "camera monitor", "maze", "labyrinth", "hide and seek", "prop hunt",
    "sheriff", "innocent", "traitor", "deceiver", "lying",
    "social deduction", "lie", "betrayal", "backstab", "trust", "alliance",
    "voice chat", "proximity chat", "mic up", "roast battle", "rap battle",
    "court room", "judge", "jury", "executioner", "jailbreak", "prison life",
    "cop vs robber", "wanted level", "bank robbery", "vault breach",
    "laser dodge", "lockpick", "security cameras", "security guard",
    "trespassing", "escape room", "keycard", "vent system",
    # -- Slice 8: Social, Simulation & Creative Sandboxes (words 532-594) --
    "brookhaven", "royale high", "adopt me", "grow a garden", "bloxburg",
    "meepcity", "livtopia", "berry avenue", "club", "party", "house design",
    "mansion", "penthouse", "luxury car", "supercar", "hypercar", "driving",
    "drifting", "drag race", "offroad", "plane pilot", "flight sim",
    "train sim", "ship captain", "submarine", "space station", "colony",
    "civilization", "city builder", "empire builder", "castle defence",
    "tower defence", "wave survival", "base defense", "base building",
    "sandbox", "terraforming", "mining", "excavation", "digging",
    "underground", "ocean exploration", "scuba", "subnautica style",
    "raft building", "island survival", "crafting recipe",
    "survival simulator", "homestead", "farming sim", "livestock",
    "greenhouse", "crop yield", "harvest", "weather", "seasons", "winter",
    "summer", "disaster", "natural disaster", "tornado", "tsunami",
    "volcano",
    # -- Slice 9: Combat, PvP & Movement Mechanics (words 595-661) ---------
    "battlegrounds", "reflex pvp", "parry", "block", "dodge", "dash",
    "combo", "air combo", "knockback", "ragdoll", "execution", "finisher",
    "weapon skill", "sword fighting", "gun fight", "sniper", "hitscan",
    "projectile", "raycast", "fps", "tps", "battle royale", "deathmatch",
    "team deathmatch", "capture the flag", "king of the hill",
    "zone control", "faction war", "guild war", "clan war", "tournament",
    "ranked ladder", "elo", "matchmaking", "casual", "competitive", "sweat",
    "tryhard", "mechanics", "tech", "animation cancel", "combo extender",
    "passive skill", "ultimate", "cooldown", "stamina bar", "health bar",
    "shield", "armor pen", "lifesteal", "critical hit", "headshot",
    "true damage", "stun", "freeze", "burn", "poison", "shock", "wall run",
    "double jump", "grappling hook", "jetpack", "glider", "slide", "mantle",
    "vault", "sprint",
]

# Number of keywords to crawl per sync (rotating slice) — a 661-word
# dictionary swept 100 words at a time = full coverage in 7 syncs.
KEYWORDS_PER_SYNC = 100

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
        # Token-bucket pacer shared by ALL batched outbound calls (metric
        # batches, keyword slices, icons). Live evidence 2026-09-03:
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

    def _get_json(self, url: str, retries: int = 2) -> Tuple[int, Optional[Any]]:
        """Polite GET -> (status, json-or-None). Retries transient failures."""
        status, data = 0, None
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

    # ------------------------------------------------------------------ #
    # Search-proxy IP pool (keyword crawler escape hatch)
    # ------------------------------------------------------------------ #

    def _search_proxy_urls(self) -> List[str]:
        """Parse RBXSCOUT_SEARCH_PROXY_URLS into an ordered proxy URL list.

        Accepts comma, semicolon or newline separators; blank entries are
        dropped. The pool always ends with the direct Roblox URL so direct is
        the terminal fallback even when proxies are configured.
        """
        raw = os.environ.get(self.SEARCH_PROXY_URLS_ENV, "")
        entries: List[str] = []
        for part in re.split(r"[,;\n]+", raw or ""):
            part = part.strip().rstrip("/")
            if not part:
                continue
            if part == "direct" or re.match(r"^https?://[^/\s]+$", part):
                entries.append(part)
            else:
                log.warning("Ignoring malformed search proxy URL: %r", part)
        return entries + ["direct"]

    def _search_request_url(self, base: str, keyword: str, sid: str) -> str:
        """Build the omni-search URL for one pool entry.

        ``direct`` goes straight to Roblox; anything else is a proxy base URL
        that mirrors the same path+query, e.g.
        ``https://rbx-search-proxy.<you>.workers.dev`` →
        ``https://rbx-search-proxy.<you>.workers.dev/search-api/omni-search?...``.
        Malformed proxy entries (no scheme/host) are skipped.
        """
        q = quote(keyword)
        path_query = f"search-api/omni-search?searchQuery={q}&pageType=all&sessionId={sid}"
        if base == "direct":
            return f"https://apis.roblox.com/{path_query}"
        # Entries are validated in _search_proxy_urls; belt-and-suspenders:
        return f"{base}/{path_query}"

    def _search_pool_request(self, keyword: str) -> Tuple[int, Optional[Any]]:
        """Try the omni-search endpoint through the IP pool in order.

        Pool order = every configured proxy, then direct Roblox. A proxy
        attempt counts as failed on: network error, HTTP >= 500, or a 200
        whose body is not the expected omni-search JSON (bad JSON with a 200
        would otherwise poison the results). A 403/429 fails that PROXY only —
        Roblox's own limits differ per IP pool, so those statuses do not
        poison the direct attempt. Success = first 200 with parseable JSON.
        Per-proxy failures are remembered on the instance so one dead proxy
        stops costing a timeout on every keyword.
        """
        pool = self._search_proxy_urls()
        sid = str(uuid.uuid4())
        status, data = 0, None
        for i, base in enumerate(pool):
            if base != "direct" and self._search_pool_benched(base):
                continue  # benched proxy: skip straight past it
            url = self._search_request_url(base, keyword, sid)
            if i > 0:
                time.sleep(0.25)  # small settle between pool entries
            try:
                if base == "direct":
                    self._emit_pace()
                    status, data = self._get_json(url)
                else:
                    res = self.session.get(url, timeout=self.SEARCH_PROXY_TIMEOUT)
                    if res.status_code == 200:
                        try:
                            status, data = 200, res.json()
                        except ValueError:
                            status, data = 200, None
                    else:
                        status = res.status_code
                if status == 200 and data is not None:
                    self._search_pool_ok(base)
                    return status, data
                if base != "direct":
                    self._search_pool_fail(base)
            except requests.RequestException as exc:
                log.debug("Search proxy %s failed for %r: %s", base, keyword, exc)
                status, data = 0, None
                if base != "direct":
                    self._search_pool_fail(base)
        return status, data

    def _search_pool_ok(self, base: str) -> None:
        """A pool entry served a 200: clear its failure streak."""
        if not hasattr(self, "_search_pool_health"):
            self._search_pool_health = {}
        self._search_pool_health[base] = {"fails": 0}

    def _search_pool_fail(self, base: str) -> None:
        """Record a pool-entry failure; after 3 consecutive fails the entry is
        benched for 5 minutes so later keywords skip straight past it."""
        if not hasattr(self, "_search_pool_health"):
            self._search_pool_health = {}
        entry = self._search_pool_health.setdefault(base, {"fails": 0, "bench_until": 0.0})
        entry["fails"] = entry.get("fails", 0) + 1
        if entry["fails"] >= 3:
            entry["bench_until"] = time.monotonic() + 300.0
            entry["fails"] = 0
            log.warning("Search proxy %s benched for 5 minutes after repeated failures", base)

    def _search_pool_benched(self, base: str) -> bool:
        if not hasattr(self, "_search_pool_health"):
            self._search_pool_health = {}
        entry = self._search_pool_health.get(base)
        return bool(entry and time.monotonic() < entry.get("bench_until", 0.0))

    def _search_pool_snapshot(self) -> Dict[str, Any]:
        """Diagnostics: which pool entry answered, bench state at crawl end."""
        health = getattr(self, "_search_pool_health", {})
        return {
            "pool": self._search_proxy_urls(),
            "benched": [b for b in health if self._search_pool_benched(b)],
        }

    # ------------------------------------------------------------------ #
    # SQLite persistence
    # ------------------------------------------------------------------ #

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
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
                    last_updated     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(game_analytics)")}
            if "description" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN description TEXT")
            if "contact_schema_version" not in columns:
                conn.execute("ALTER TABLE game_analytics ADD COLUMN contact_schema_version INTEGER DEFAULT 0")
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
                CREATE TABLE IF NOT EXISTS keyword_crawl_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_index INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO keyword_crawl_state (id, next_index) VALUES (1, 0)"
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
            return int(cursor.lastrowid)

    def _finish_scan(self, **extra: Any) -> None:
        """Persist the full scan snapshot.

        Callers mutate ``self.last_scan`` as the scan progresses and then call
        this without arguments; any keyword arguments passed here override the
        snapshot. Everything is written to the DB row so a restart shows the
        real status and counters instead of a stuck 'running' row.
        """
        run_id = self.last_scan.get("run_id")
        if not run_id:
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
        import json
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO contact_diagnostics (run_id, universe_id, diagnostics_json) VALUES (?,?,?)",
                    (run_id, uid, json.dumps(diagnostics)),
                )
        except sqlite3.Error as exc:
            log.debug("Could not persist contact diagnostics for %s: %s", uid, exc)

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

    def upsert_game(self, record: Dict[str, Any]) -> None:
        """Insert/update metrics; peak_ccu grows via MAX(existing, current).

        Tier stamping happens automatically: the new tier is computed from the
        incoming stats, prev_tier keeps the last stamp (2+-tier climbs and 3x
        CCU jumps raise blowup_flag for the New and Upcoming watchlist).
        """
        uid = record.get("universe_id")
        if uid is not None and record.get("tier") is None:
            record = {**record, **self._tier_stamp_for(int(uid), record)}
        # 24 bind values + CURRENT_TIMESTAMP for last_updated = 25 columns.
        placeholders = ",".join("?" for _ in range(24))
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO game_analytics (
                    universe_id, root_place_id, title, ccu, peak_ccu, visits, favorites,
                    genre, creator_name, creator_type, creator_id, description, icon_url,
                    has_discord, discord_url, status, found_via,
                    has_social_links, contacts_checked_at, tier, prev_tier,
                    tier_since, blowup_flag, blowup_at, last_updated
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
                ),
            )
            if record.get("ccu") is not None:
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
                ref = None
                if len(grp) >= 2:
                    base = grp[grp["ts"] < now - pd.Timedelta(hours=18)]
                    ref = float(base.iloc[-1]["ccu"]) if not base.empty else None
                stats[int(uid)] = {"avg_ccu_1d": avg_1d, "ccu_ref": ref}
            df["avg_ccu_1d"] = df["universe_id"].map(
                lambda u: stats.get(int(u), {}).get("avg_ccu_1d")
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
            df["momentum_1d"] = None
        return df

    # ------------------------------------------------------------------ #
    # Sourcing
    # ------------------------------------------------------------------ #

    # Keyword slices MUST carry a sessionId. Live evidence 2026-09-03:
    # without it the endpoint answers HTTP 200 with searchResults: [] for
    # every keyword (93/100 calls "OK", zero games parsed); with it, 40
    # games per keyword return. Mirrors the explore-api get-sorts pattern.
    OMNI_SEARCH_URL = (
        "https://apis.roblox.com/search-api/omni-search"
        "?searchQuery={q}&pageType=all&sessionId={sid}"
    )
    # Search-proxy fallback pool: GitHub Actions runners share a small egress
    # IP range, so the omni-search endpoint throttles every sync to 429s
    # (breaker trips within the first keywords). The worker proxy below is a
    # Cloudflare Worker that mirrors the omni-search route from Cloudflare's
    # IP pool — the same trick as the RoProxy mirror used for place
    # resolution. Env var holds a comma/newline-separated URL list; the
    # "direct" pool (Roblox itself) is always the last fallback so a down
    # proxy can never take the crawler down.
    SEARCH_PROXY_URLS_ENV = "RBXSCOUT_SEARCH_PROXY_URLS"
    SEARCH_PROXY_TIMEOUT = 8.0  # workers cold-start; a bit above request_timeout

    def fetch_discovery_games(self) -> List[Dict[str, Any]]:
        """Roblox Discovery (explore-api): front-page charts incl. universeIds."""
        status, data = self._get_json(
            "https://apis.roblox.com/explore-api/v1/get-sorts?sessionId=rbxscout"
        )
        games: Dict[int, Dict[str, Any]] = {}
        self.source_diagnostics["discovery"] = {"status": status}
        if status == 200 and data:
            for sort in data.get("sorts") or []:
                for g in sort.get("games") or []:
                    uid = g.get("universeId")
                    if not uid:
                        continue
                    cur = games.setdefault(
                        int(uid),
                        {
                            "universe_id": int(uid),
                            "root_place_id": g.get("rootPlaceId"),
                            "name": g.get("name"),
                            "playing": g.get("playerCount"),
                            "up_votes": g.get("totalUpVotes"),
                            "down_votes": g.get("totalDownVotes"),
                        },
                    )
                    # prefer the highest playerCount seen across charts
                    if g.get("playerCount") and (cur.get("playing") or 0) < g["playerCount"]:
                        cur.update(
                            playing=g.get("playerCount"),
                            root_place_id=g.get("rootPlaceId"),
                        )
        self.source_diagnostics["discovery"]["records"] = len(games)
        log.info("Discovery API returned %d games", len(games))
        return list(games.values())

    def fetch_rolimons_games(self) -> Dict[int, Dict[str, Any]]:
        """Rolimon's index (fallback/bulk): placeId -> {name, ccu, icon_url}.

        Rolimon's only returns place IDs. It gives CCU (live playing), name, and
        icon — no universe IDs, no visits, no favorites. All metric hydration
        (CCU totals, visits, favorites, genre, creator) is done later by this
        engine against games.roblox.com.

        This method only parses the page; persisting the full catalog into the
        DB happens in ``import_rolimons_catalog`` so scans can load the snapshot
        instead of re-fetching the full index on every sync.
        """
        status, data = self._get_json("https://api.rolimons.com/games/v1/gamelist")
        out: Dict[int, Dict[str, Any]] = {}
        if hasattr(self, "source_diagnostics"):
            self.source_diagnostics["rolimons"] = {"status": status}
        if status == 200 and data and isinstance(data, dict) and data.get("success"):
            for place_id, entry in (data.get("games") or {}).items():
                try:
                    place_id = int(place_id)
                    name, playing, icon = entry[0], entry[1], entry[2]
                except (ValueError, IndexError, TypeError):
                    continue
                out[place_id] = {
                    "place_id": place_id,
                    "name": name,
                    "playing": playing or 0,
                    "icon_url": icon if isinstance(icon, str) else None,
                }
        if hasattr(self, "source_diagnostics"):
            self.source_diagnostics["rolimons"]["records"] = len(out)
        log.info("Rolimon's index returned %d games", len(out))
        return out

    def import_rolimons_catalog(self) -> int:
        """Persist the full Rolimon's gamelist into ``rolimons_catalog``.

        This is the one-time bulk export (~7,000 entries). Once persisted, scans
        load the local snapshot instead of re-fetching the Rolimon's index for
        every candidate they consider.

        Returns the number of rows in the catalog after the import (inserted +
        existing). The table is append-or-replace per ``place_id``, so future
        imports refresh stale entries without a separate delete pass.
        """
        live = self.fetch_rolimons_games()
        if not live:
            rol_status = (
                self.source_diagnostics.get("rolimons", {}).get("status")
                if hasattr(self, "source_diagnostics")
                else None
            )
            if rol_status != 200:
                log.warning("Rolimon's import skipped: index fetch failed (%s)", rol_status)
            return self.catalog_place_count()
        try:
            with self._connect() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO rolimons_catalog (place_id, name, playing, icon_url, cached_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    [
                        (info["place_id"], info["name"], info["playing"], info["icon_url"])
                        for info in live.values()
                    ],
                )
                count = conn.execute("SELECT COUNT(*) FROM rolimons_catalog").fetchone()[0]
            log.info("Rolimon's catalog persisted %d entries", count)
            return count
        except sqlite3.Error as exc:
            log.warning("Could not persist rolimons_catalog: %s", exc)
            return self.catalog_place_count()

    def catalog_place_count(self) -> int:
        """Current number of persisted Rolimon's place entries."""
        try:
            with self._connect() as conn:
                return int(conn.execute("SELECT COUNT(*) FROM rolimons_catalog").fetchone()[0])
        except sqlite3.Error:
            return 0

    def load_rolimons_catalog(self) -> pd.DataFrame:
        """Load the persisted Rolimon's snapshot.

        Returns columns ``place_id``, ``name``, ``playing``, ``icon_url``
        (plus any DB-added columns). Empty DataFrame when the catalog has not
        been imported yet.
        """
        try:
            with self._connect() as conn:
                return pd.read_sql_query("SELECT * FROM rolimons_catalog", conn)
        except (pd.errors.DatabaseError, sqlite3.Error):
            return pd.DataFrame()

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
        # scattered noise: some place IDs in the Rolimon's list are deleted
        # games (404) and occasional transient errors. Those must NOT trip the
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

    def fetch_search_games(self, keywords: List[str]) -> Dict[int, Dict[str, Any]]:
        """Omni-search keyword crawler (Phase 2): discover games by keyword.

        One request per keyword against
        ``apis.roblox.com/search-api/omni-search?searchQuery=KW&pageType=all``.
        The response already carries universe IDs (verified live: ~40 games per
        keyword, no cookie, ~0.5 s per call), so no place→universe conversion
        is needed.

        Every keyword request is routed through the search-proxy IP pool
        (``_search_pool_request``): configured Cloudflare Worker proxies first
        (GitHub Actions runners share a small egress IP range and get 429-
        throttled), direct Roblox always last as the terminal fallback.
        Threaded with a circuit breaker: if the first 5 keyword calls all
        fail, abort the slice — partial results are kept.

        Returns ``{universe_id: {"universe_id", "title", "root_place_id"}}``
        (no CCU — the batch hydrator supplies live stats afterwards).
        """
        if not hasattr(self, "source_diagnostics"):
            self.source_diagnostics = {}
        out: Dict[int, Dict[str, Any]] = {}
        statuses: List[int] = []
        if not keywords:
            self.source_diagnostics["keyword_crawl"] = {
                "keywords": 0, "records": 0, "breaker_tripped": False,
                "pool": self._search_pool_snapshot(),
            }
            return out

        def work(keyword: str):
            return self._search_pool_request(keyword)

        consecutive_failures = 0
        breaker_tripped = False
        with ThreadPoolExecutor(max_workers=min(self.max_workers, 8)) as pool:
            futures = {pool.submit(work, kw): kw for kw in keywords}
            for fut in as_completed(futures):
                kw = futures[fut]
                try:
                    status, data = fut.result()
                except Exception as exc:  # network-level failure counts as a miss
                    log.debug("Keyword %r failed: %s", kw, exc)
                    statuses.append(0)
                    consecutive_failures += 1
                    continue
                statuses.append(status)
                if status != 200 or not data:
                    consecutive_failures += 1
                    if consecutive_failures >= 5 and not breaker_tripped:
                        breaker_tripped = True
                        log.warning(
                            "Keyword crawler circuit breaker tripped after %d consecutive failures; "
                            "cancelling remaining keyword requests",
                            consecutive_failures,
                        )
                        for f in futures:
                            f.cancel()
                        break
                    continue
                consecutive_failures = 0
                # Response shape (verified live): searchResults[] each with
                # contents[] carrying universeId + name.
                for group in data.get("searchResults") or []:
                    for content in group.get("contents") or []:
                        try:
                            uid = int(content["universeId"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if uid not in out:
                            out[uid] = {
                                "universe_id": uid,
                                "title": content.get("name") or "Unknown",
                                "root_place_id": content.get("rootPlaceId"),
                            }
        crawl_diag = self._search_pool_snapshot()
        crawl_diag.update({
            "keywords": len(keywords),
            "successful_keywords": sum(200 == s for s in statuses),
            "failed_keywords": sum(200 != s for s in statuses),
            "breaker_tripped": breaker_tripped,
            "records": len(out),
        })
        self.source_diagnostics["keyword_crawl"] = crawl_diag
        log.info("Keyword crawler: %d keywords → %d unique games", len(keywords), len(out))
        return out

    def _load_keyword_cursor(self) -> int:
        """Read the rotating keyword-slice cursor from keyword_crawl_state."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT next_index FROM keyword_crawl_state WHERE id = 1"
                ).fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def _advance_keyword_cursor(self, next_index: int) -> None:
        """Persist the rotating keyword-slice cursor (wraps at the end)."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE keyword_crawl_state SET next_index = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = 1",
                    (next_index,),
                )
        except sqlite3.Error as exc:
            log.debug("Could not persist keyword_crawl_state: %s", exc)

    def next_keyword_slice(self) -> Tuple[List[str], int, int]:
        """Take the next ~KEYWORDS_PER_SYNC slice of the keyword dictionary.

        The cursor rotates: after the whole dictionary has been swept it wraps
        back to the top, so every sync keeps discovering newly published games.
        Returns ``(keywords, start_index, end_index)`` for diagnostics.
        """
        total = len(KEYWORD_DICTIONARY)
        if total == 0:
            return [], 0, 0
        start = self._load_keyword_cursor() % total
        end = min(start + KEYWORDS_PER_SYNC, total)
        keywords = KEYWORD_DICTIONARY[start:end]
        next_index = 0 if end >= total else end
        self._advance_keyword_cursor(next_index)
        return keywords, start, end

    def build_rolimons_candidate_pool(
        self,
        min_ccu: int = 0,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> Dict[int, Dict[str, Any]]:
        """Build the Rolimon's-backed candidate pool from the local catalog.

        When the Rolimon's catalog has been imported, every catalog entry is a
        candidate — not just the top N — so scans no longer discard the bulk of
        Rolimon's entries because they were never placed into the candidate pool.

        CCU pre-gate keeps potentially dead entries out of the expensive
        resolution + hydration step, but the catalog itself still retains them
        (the table is not truncated here).

        Returns ``{universe_id: {"universe_id", "root_place_id", "name", "playing", "icon_url"}}``.

        Universe IDs come from the place_map cache first, then from fresh
        resolution of unresolved catalog entries up to ``candidate_limit``.
        This means an imported Rolimon's catalog can feed the full candidate pool
        on its own, not just the already-resolved subset.
        """
        if not hasattr(self, "load_rolimons_catalog"):
            return {}
        catalog = self.load_rolimons_catalog()
        if catalog.empty:
            log.info("Rolimon's candidate pool empty: catalog not imported yet")
            return {}
        # CCU pre-gate: Rolimon's `playing` is fresh-ish; drop obvious
        # sub-threshold entries so the candidate pool stays aligned with the
        # live target and resolution work stays bounded.
        if min_ccu > 0:
            catalog = catalog[catalog["playing"].fillna(0) >= min_ccu]
        if catalog.empty:
            return {}
        # Rank by playing so the highest-CCU entries are resolved first.
        ranked = catalog.sort_values("playing", ascending=False)
        place_ids = [int(p) for p in ranked["place_id"]]

        # Phase 1: DB-resident universe mappings.
        cached: Dict[int, int] = {}
        try:
            with self._connect() as conn:
                if place_ids:
                    rows = conn.execute(
                        f"SELECT place_id, universe_id FROM place_map "
                        f"WHERE place_id IN ({','.join('?' for _ in place_ids)})",
                        place_ids,
                    ).fetchall()
                    cached = {int(pid): int(uid) for pid, uid in rows}
        except sqlite3.Error:
            cached = {}

        # Phase 2: fresh resolution for unresolved catalog entries up to the
        # candidate budget.
        unresolved = [pid for pid in place_ids if pid not in cached]
        resolved_live: Dict[int, int] = {}
        if unresolved:
            resolved_live = self.resolve_universe_ids(unresolved)
            try:
                with self._connect() as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO place_map (place_id, universe_id, resolved_at) "
                        "VALUES (?, ?, CURRENT_TIMESTAMP)",
                        list(resolved_live.items()),
                    )
                    for pid, uid in resolved_live.items():
                        cached[pid] = uid
            except sqlite3.Error:
                pass

        pool: Dict[int, Dict[str, Any]] = {}
        for _, row in ranked.iterrows():
            pid = int(row["place_id"])
            uid = cached.get(pid)
            if uid is None or uid in pool:
                continue
            pool[uid] = {
                "universe_id": uid,
                "root_place_id": pid,
                "name": row["name"],
                "playing": int(row["playing"] or 0),
                "icon_url": row["icon_url"],
            }
            if len(pool) >= candidate_limit:
                break
        return pool

    def prune_catalog(self, max_age_days: int = TIER8_STALE_PRUNE_DAYS) -> int:
        """Floor-prune the catalog (RoTrends-style): drop dead games.

        Removes rows where ccu = 0 AND untouched for ``max_age_days`` — the
        40M abandoned baseplates never enter the DB. Defaults to the Tier 8
        14-day rule (tightened from the original 30 days): below-threshold
        games that stay cold self-clean. Returns rows removed.
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM game_analytics "
                    "WHERE COALESCE(ccu, 0) = 0 "
                    "AND COALESCE(last_updated, '1970-01-01') < datetime('now', ?)",
                    (f"-{int(max_age_days)} days",),
                )
                removed = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            if removed:
                log.info("Catalog floor-prune removed %d dead games", removed)
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

        Cadence-ordered: T1–T2 (every sync) → T3 (every 2nd) → T4 (every 3rd)
        → T5–T7 weekly wall-clock bucket → T8 rotating 3-day slice. Tiers not
        due under their cadence cost zero requests. The scheduler caps the
        selected list at ``batch_size * budget_batches`` universes; the caller
        hydrates as many of those as its own budget allows — anything beyond
        rolls to the next sync naturally.

        T8 rotation: one deterministic 3-day bucket (epoch // TIER8_ROTATION_DAYS
        mod bucket_count) so every sub-threshold game is visited at least once
        every rotation cycle without spending budget on all of them each sync.
        """
        n = int(sync_number if sync_number is not None else self._sync_seq)
        cap = max(0, int(batch_size) * int(budget_batches))
        groups: Dict[str, List[int]] = {"t1_t2": [], "t3": [], "t4": [], "weekly": [], "t8": []}
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

                groups["t1_t2"] = ids_for(
                    "tier IN (1, 2)", (), "last_updated ASC, universe_id ASC"
                )
                if TIER_CADENCE_SYNC[3] and n % TIER_CADENCE_SYNC[3] == 0:
                    groups["t3"] = ids_for(
                        "tier = 3", (), "last_updated ASC, universe_id ASC"
                    )
                if TIER_CADENCE_SYNC[4] and n % TIER_CADENCE_SYNC[4] == 0:
                    groups["t4"] = ids_for(
                        "tier = 4", (), "last_updated ASC, universe_id ASC"
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
        for key in ("t1_t2", "t3", "t4", "weekly", "t8"):
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
        chunks = [universe_ids[i : i + 50] for i in range(0, len(universe_ids), 50)]

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

    def fetch_trending_universe_ids(self, limit: int = 500) -> List[int]:
        """Bulk popular universe ids (Discovery first, Rolimon's fallback)."""
        ids = [g["universe_id"] for g in self.fetch_discovery_games()]
        if not ids:
            roli = self.fetch_rolimons_games()
            top = sorted(roli.values(), key=lambda g: -g["playing"])[:limit]
            resolved = self.resolve_universe_ids([g["place_id"] for g in top])
            ids = list(resolved.values())
        return ids[:limit]

    # ------------------------------------------------------------------ #
    # Contact resolution (Tiers 1-4)
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

        cached = self._load_contact_cache(uid)
        if cached and not force and now - cached["ts"] < CONTACT_RECHECK_HOURS * 3600:
            self._set_contact_diagnostic(run_id, uid, {
                "cached": True,
                "selected_source": cached["record"].get("found_via"),
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            return cached["record"]

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
        try:
            ts = time.mktime(time.strptime(str(row[5]), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
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

    @staticmethod
    def _pick_discord(links: List[Dict[str, str]]) -> Optional[str]:
        for link in links:
            if (link.get("type") or "").lower() == "discord" and link.get("url"):
                return link["url"]
        return None

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
    ) -> pd.DataFrame:
        """Fetch metrics, apply target thresholds, and optionally check contacts.

        ``candidate_limit`` deliberately bounds place-to-universe expansion. The
        Roblox/Rolimon's sources do not expose one public, bulk endpoint for all
        visit metrics, so resolving every catalog entry would be needlessly slow.
        Contact requests are separate so the UI can load them page by page.
        """
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
        }
        # The sync counter drives tier cadences (every 2nd/3rd sync) and
        # persists across restarts via the .sync_state sidecar file.
        sync_number = self.bump_sync_sequence()
        self.last_scan["sync_number"] = sync_number

        try:
            report(0.02, "Fetching Discovery charts…")
            discovery = self.fetch_discovery_games()
            self.last_scan["keyword_slice_start"] = 0
            self.last_scan["keyword_slice_end"] = 0
            self.last_scan["keyword_discovered"] = 0

            # ------------------------------------------------------------------
            # Rolimon's backend: DB-backed full catalog, not a live top-N slice.
            # ------------------------------------------------------------------
            catalog_count = self.catalog_place_count()
            catalog_loaded = catalog_count > 0
            if not catalog_loaded:
                report(0.05, "Importing full Rolimon's catalog…")
                catalog_count = self.import_rolimons_catalog()
                catalog_loaded = catalog_count > 0
            self.source_diagnostics["rolimons"] = {
                "status": (
                    self.source_diagnostics.get("rolimons", {}).get("status")
                    if self.source_diagnostics.get("rolimons")
                    else 200
                ),
                "records": catalog_count,
                "catalog_loaded": catalog_loaded,
                "imported_now": not catalog_loaded or catalog_count == catalog_count,
            }
            # Fix: diagnostic `records` should reflect what was actually used
            # (catalog size), not a stale fetch count.
            self.source_diagnostics["rolimons"]["records"] = catalog_count

            candidates: Dict[int, Dict[str, Any]] = {}

            # Discovery: universe IDs direct, ranked by live CCU.
            discovery_ranked = sorted(
                discovery,
                key=lambda g: -(g.get("playing") or (g.get("up_votes") or 0) / 50 or 0),
            )
            if min_ccu > 0:
                discovery_ranked = [
                    g for g in discovery_ranked if int(g.get("playing") or 0) >= min_ccu
                ]
            for game in discovery_ranked:
                uid = int(game["universe_id"])
                if uid in candidates:
                    continue
                candidates[uid] = game
                if len(candidates) >= candidate_limit:
                    break

            # DB-backed Rolimon's pool.
            # Discovery already filled candidates from the front page; if slots
            # remain, Rolimon's catalog entries are added with full resolution so
            # more niche games can enter the candidate pool.
            slots = max(0, candidate_limit - len(candidates))
            roli_pool = self.build_rolimons_candidate_pool(
                min_ccu=min_ccu,
                candidate_limit=slots,
            )
            for uid, meta in roli_pool.items():
                if uid in candidates:
                    continue
                candidates[uid] = meta
                if len(candidates) >= candidate_limit:
                    break

            # Phase 2: keyword crawler — take the next rotating slice of the
            # keyword dictionary (KEYWORDS_PER_SYNC words) and advance the
            # cursor, wrapping back to the top of the dictionary after the
            # last slice. This makes the catalog grow every sync without a
            # separate cron server.
            keywords, kw_start, kw_end = self.next_keyword_slice()
            report(0.12, f"Keyword slice {kw_start + 1}–{kw_end} of {len(KEYWORD_DICTIONARY)}…")
            search_games = self.fetch_search_games(keywords)
            self.last_scan.update({
                "keyword_slice_start": kw_start,
                "keyword_slice_end": kw_end,
                "keyword_discovered": len(search_games),
            })
            for uid, info in search_games.items():
                if uid not in candidates:
                    candidates[uid] = {
                        "universe_id": uid,
                        "root_place_id": info.get("root_place_id"),
                        "name": info.get("title"),
                        "playing": 0,
                    }

            ranked = sorted(
                candidates.values(),
                key=lambda g: -(g.get("playing") or (g.get("up_votes") or 0) / 50 or 0),
            )[:candidate_limit]
            universe_ids = [int(g["universe_id"]) for g in ranked]
            self.last_scan["candidate_count"] = len(universe_ids)
            if not universe_ids:
                report(1.0, "No games sourced.")
                self.last_scan.update({"status": "complete", "metrics_count": 0, "matched_count": 0})
                self._finish_scan()
                return pd.DataFrame()

            # ------------------------------------------------------------------
            # Budgeted hydration: brand-new candidates first (they have no
            # stats yet, so they cannot be tiered — a mandatory one-time pass),
            # then known games selected by tier cadence until the per-sync
            # request budget is spent. Whatever does not fit rolls to the next
            # sync; tiers not due cost zero requests.
            # ------------------------------------------------------------------
            schedule = self.load_tier_refresh_ids(
                sync_number=sync_number, budget_batches=HYDRATION_BUDGET_PER_SYNC
            )
            self.last_scan["tier_schedule"] = schedule["groups"]
            self.last_scan["tier_counts"] = schedule["tier_counts"]
            known_due = schedule["ids"]
            existing: set = set()
            try:
                with self._connect() as conn:
                    for i in range(0, len(universe_ids), 900):
                        chunk = universe_ids[i : i + 900]
                        marks = ",".join("?" for _ in chunk)
                        rows = conn.execute(
                            f"SELECT universe_id FROM game_analytics WHERE universe_id IN ({marks})",
                            chunk,
                        ).fetchall()
                        existing.update(int(r[0]) for r in rows)
            except sqlite3.Error:
                existing = set()
            new_ids = [uid for uid in universe_ids if uid not in existing]
            new_set = set(new_ids)
            budget_cap = HYDRATION_BUDGET_PER_SYNC * 50
            budget_ids = new_ids + [uid for uid in known_due if uid not in new_set]
            hydration_ids = budget_ids[:budget_cap]
            self.last_scan["hydration_budget"] = {
                "new": len(new_ids),
                "known_due": len(known_due),
                "hydrated": len(hydration_ids),
                "deferred": max(0, len(budget_ids) - len(hydration_ids)),
                "budget_batches": HYDRATION_BUDGET_PER_SYNC,
            }

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
                        int(candidates.get(uid, {}).get("playing") or 0),
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
                        int(candidates.get(uid, {}).get("playing") or 0),
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
            run_id = self._begin_scan()
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
                    completed += 1
                    self._store_contact_cache(uid, record)
                report(index / max(1, len(metas)), f"Contacts checked {index}/{len(metas)}…")

        self.last_scan.update({
            "status": "complete",
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "contacts_attempted": prior_attempted + len(metas),
            "contacts_completed": prior_completed + completed,
            "contact_errors": prior_errors + errors,
        })
        self._finish_scan()
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