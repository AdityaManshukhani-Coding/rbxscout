"""
UpScale Scouting Tool - Roblox Game Scouting & Contact Dashboard.

Run: streamlit run app.py
Deploy marker: table reorder + No-Discord label + literal search + genre-✕ fix (2026-09-23).
"""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

from scout_core import (
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_MESSAGE_TEMPLATE,
    DISCORD_FILTER_ALL,
    DISCORD_FILTER_TRUE,
    DISCORD_FILTER_FALSE,
    DISCORD_LOGO_URL,
    RobloxPlatformScout,
    apply_filters,
    compact_num,
    normalize_discord_user_id,
    render_outreach_message,
    truncate,
)
import scout_core as _scout_core
import capacity
import catalog_fetch
from catalog_fetch import floored_targets
import gate
import profile_store

logging.basicConfig(level=logging.INFO)

# Hot-update resilience (2026-10-04 outage): when Streamlit Cloud's git-pull
# hot-update reruns this script, the running Python process may still hold a
# scout_core module imported BEFORE the latest pull — the master-only Increates
# set is then missing from it, and a hard ``from scout_core import`` would
# crash the whole app with an ImportError until someone reboots the container.
# Fall back to the classic starters instead: a wedged deploy degrades to the
# original message set (feature off) instead of a dead site, and the real set
# activates on the next full app reboot.
INCREASES_MESSAGE_TEMPLATES = getattr(
    _scout_core, "INCREASES_MESSAGE_TEMPLATES", DEFAULT_MESSAGE_TEMPLATES
)

APP_DIR = Path(__file__).resolve().parent
BRAND_DIR = APP_DIR / "assets" / "brand"

# Brand tokens — the source of truth is BRAND.md; keep in sync with
# generate_brand_assets.py. The palette leans quieter than Streamlit's
# defaults so the UpScale Orange accent reads as THE brand color.
BRAND_ACCENT = "#FF6E01"    # UpScale Orange (owner's logo): primary buttons, the arrow, positive deltas
BRAND_NEGATIVE = "#EF4444"  # red is reserved for negative momentum only
BRAND_INK = "#0B0E14"
BRAND_PANEL = "#12161E"
BRAND_HAIRLINE = "#262A33"
BRAND_TEXT = "#E7ECF3"
BRAND_MUTED = "#9AA4B2"


def _brand_png(name: str) -> str:
    """Read a raster brand asset as a base64 data URI (None when missing).

    Streamlit has no first-class favicon/page-icon file API, so the browser
    favicon is injected once per run as a data URI in the input-guard script
    block below. A missing asset degrades to the default icon — never an
    exception on the render path.
    """
    import base64

    path = BRAND_DIR / name
    try:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return ""


def _resolve_catalog() -> tuple[str, bool]:
    """Point the dashboard at the right catalog and make it exist.

    Local mode (repo has rbx_scout.db, or RBXSCOUT_LOCAL_DB=1): read the local
    file, exactly as before. Hosted mode (Streamlit Cloud): download the
    public release asset into a cache dir and keep it fresh there. Cached, so
    the ~5-min freshness check and any download happen once per process —
    not once per user, page view, or rerun.

    Never raises. If the release store cannot be reached (e.g. the catalog
    asset is missing during a pipeline outage), returns the demo-mode sentinel
    so the dashboard still renders — with a banner explaining what happened —
    instead of crashing with a red traceback. The ttl below re-runs this
    every 10 minutes, so when the store recovers the app heals itself with
    no restart or user action.
    """

    @st.cache_resource(show_spinner=False, ttl=600)
    def _ensure(_version: int) -> tuple[str, bool]:
        if not catalog_fetch.is_hosted():
            return str(APP_DIR / "rbx_scout.db"), False
        try:
            path = catalog_fetch.ensure_catalog()
            return str(path), False
        except catalog_fetch.CatalogFetchError as exc:
            stale = catalog_fetch.stale_cache_fallback(exc)
            if stale is not None:
                return str(stale), True
            # Store unreachable (missing asset / GitHub outage): run in demo
            # mode rather than crash. The negative-DB_PATH sentinel makes the
            # demo fallback select itself on every path below; the cache ttl
            # (10 min) retries automatically, so recovery needs no restart.
            print(f"catalog unavailable, running in demo mode: {exc}")
            return "", False

    return _ensure(1)


DB_PATH, _USING_STALE_CATALOG = _resolve_catalog()
_CATALOG_UNAVAILABLE = DB_PATH == ""
_HOSTED_MODE = catalog_fetch.is_hosted()

_INPUT_GUARD_SCRIPT = r"""
<script>
(function () {
  // The .ROBLOSECURITY cookie field is a real password box; the Discord
  // fields are plain text but Chrome's heuristic treats labels like "User ID"
  // as credentials. Left alone, Chrome offers to "save your password" on
  // every step of onboarding. Mark every Streamlit text input as
  // non-credential (autocomplete=off + 1Password/LastPass opt-outs) and tag
  // the app as non-login so the browser stops asking.
  if (window.__ssInputGuard) { return; }  // observers survive Streamlit reruns
  window.__ssInputGuard = true;
  var sweep = function () {
    var app = document.querySelector("section.stApp, [data-testid='stApp']") || document.body;
    if (app) {
      app.setAttribute('data-1p-ignore', '');
      app.setAttribute('data-lpignore', 'true');
    }
    var fields = document.querySelectorAll("input[data-testid='stTextInput'], input[aria-label]");
    for (var j = 0; j < fields.length; j++) {
      var el = fields[j];
      if (el.getAttribute('data-1p-ignore')) { continue; }
      el.setAttribute('data-1p-ignore', '');
      el.setAttribute('data-lpignore', 'true');
      el.setAttribute('autocomplete', 'off');
    }
  };
  sweep();
  // Streamlit mounts inputs after this script in the element stream, and
  // replaces them on every rerun — watch the DOM and re-sweep as they appear.
  var pending = null;
  var observer = new MutationObserver(function () {
    if (pending) { return; }
    pending = window.setTimeout(function () { pending = null; sweep(); }, 120);
  });
  observer.observe(document.body, { childList: true, subtree: true });
})();
</script>
"""
# Browser favicon: Streamlit's page_icon only covers the fallback emoji, so
# the real radar favicon is injected once per run as data URIs. Runs on the
# gate screen too — the lock is the first thing a visitor sees.
_FAVICON_SCRIPT = r"""
<script>
(function () {
  var set = function (href) {
    if (!href) { return; }
    var link = document.querySelector("link[rel*='icon']");
    if (!link) {
      link = document.createElement('link');
      link.rel = 'icon';
      document.head.appendChild(link);
    }
    link.href = href;
  };
  set('%FAVICON32%');
  var apple = document.createElement('link');
  apple.rel = 'apple-touch-icon';
  if ('%APPLEICON%'.length > 2) { apple.href = '%APPLEICON%'; document.head.appendChild(apple); }
})();
</script>
""".replace("%FAVICON32%", _brand_png("favicon/favicon-32.png")) \
     .replace("%APPLEICON%", _brand_png("favicon/apple-touch-icon.png"))

# Duplicate eye icons on the password field (fixed 2026-09-28): Streamlit's
# password box ships its OWN reveal toggle (BaseWeb "Show password text"
# button), and Safari additionally injects its AutoFill key/eye INSIDE the
# field. Two icons, same purpose. This script keeps the Streamlit toggle and
# suppresses the browser-injected one: attributes the field so Safari's
# AutoFill (and any other password-management overlay) stands down, plus a
# MutationObserver sweep because Streamlit re-renders inputs on every rerun.
_PASSWORD_ICON_GUARD_SCRIPT = r"""
<style>
/* Belt: never let the native AutoFill button through, on any engine. */
input[type="password"]::-webkit-credentials-auto-fill-button {
  display: none !important; visibility: hidden !important;
}
</style>
<script>
(function () {
  if (window.__ssPasswordIconGuard) { return; }  // one guard per page, rerun-proof
  window.__ssPasswordIconGuard = true;
  var sweep = function () {
    var fields = document.querySelectorAll('input[type="password"]');
    for (var i = 0; i < fields.length; i++) {
      fields[i].setAttribute('autocomplete', 'new-password');
      fields[i].setAttribute('readonly', 'readonly');
      window.setTimeout(function (el) { return function () { el.removeAttribute('readonly'); }; }(fields[i]), 0);
    }
  };
  sweep();
  var pending = null;
  var observer = new MutationObserver(function () {
    if (pending) { return; }
    pending = window.setTimeout(function () { pending = null; sweep(); }, 120);
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['type'] });
})();
</script>
"""


# --------------------------------------------------------------------------- #
# Motion design system (UI_MICRO_ANIMATIONS_PLAN.md Phase 0).
#
# One shared vocabulary for every animation in the app: the same three
# durations, one outgoing easing curve, and a single reduced-motion guard
# that a CSS refactor cannot silently drop (pinned by tests/test_motion_css.py).
# Rules of the road: animate only transform/opacity, 150-320ms, orange only
# for positive/active signal, and every entrance must survive st.rerun()
# (Streamlit remounts HTML blocks, which re-triggers CSS animations for free).
_MOTION_CORE = """
<style>
:root {
  /* Durations — never longer than 320ms for feedback */
  --motion-fast: 120ms;   /* hover, press, toggle          */
  --motion-base: 200ms;   /* enter/exit, color transitions */
  --motion-slow: 320ms;   /* panels, page transitions      */
  /* Easing — a single outgoing curve for everything small */
  --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
}
/* One media query disables ALL motion for users whose OS asks for less
   (and for @test snapshot runs): durations collapse to effectively zero
   while end states still apply via forwards/both fills. */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
"""

# Full main-app motion block: the core tokens plus the app-wide interaction
# rules (buttons, inputs). Rendered once per run right after the gate.
_MOTION_CSS = _MOTION_CORE + """
<style>
/* Buttons: press depth + hover glow (plan 1.3). Transform/box-shadow only. */
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button {
  transition: transform var(--motion-fast) var(--ease-out),
              background var(--motion-fast) linear,
              box-shadow var(--motion-fast) linear,
              border-color var(--motion-fast) linear,
              color var(--motion-fast) linear;
}
[data-testid="stButton"] button:hover:not(:disabled),
[data-testid="stDownloadButton"] button:hover:not(:disabled) {
  box-shadow: 0 0 0 1px rgba(255, 110, 1, 0.35);
}
[data-testid="stButton"] button:active:not(:disabled),
[data-testid="stDownloadButton"] button:active:not(:disabled) {
  transform: scale(0.97);
}
/* Sidebar focus-ring polish (plan 1.7): thin orange ring replaces the
   browser default on every text-ish input. */
.stApp input, .stApp textarea {
  transition: box-shadow var(--motion-fast) linear,
              border-color var(--motion-fast) linear;
}
.stApp input:focus, .stApp textarea:focus {
  box-shadow: 0 0 0 2px rgba(255, 110, 1, 0.5) !important;
  border-color: rgba(255, 110, 1, 0.5) !important;
}
</style>
"""

# Wrong-password shake (plan 3.3): a 4px horizontal shake on the gate card —
# instant physical feedback that needs no extra text. Mounted as a script
# because the class must be added after the card is in the DOM.
_GATE_SHAKE_SCRIPT = r"""
<script>
(function () {
  var card = document.querySelector('.gate-card');
  if (!card || card.classList.contains('ss-shake')) { return; }
  card.classList.add('ss-shake');
})();
</script>
"""

# Sync-completion toast (plan 2.3). The server drops a pending flag when a
# sync succeeds; this script consumes it on the NEXT rerun and slides a
# small card up from the bottom-right (translateY + opacity, motion-slow),
# auto-dismissing after 3s. Pure DOM, no libraries; the guard key keeps the
# toast one-shot even if Streamlit remounts the block mid-display. The
# message text is substituted server-side (%TOAST_TEXT%) — DOMPurify strips
# custom body attributes, so no data-attribute round-trip.
_SYNC_TOAST_SCRIPT = r"""
<script>
(function () {
  if (window.__ssToastShown) { return; }
  window.__ssToastShown = true;
  var el = document.createElement('div');
  el.className = 'ss-toast';
  el.setAttribute('role', 'status');
  el.textContent = '%TOAST_TEXT%';
  document.body.appendChild(el);
  window.requestAnimationFrame(function () {
    window.requestAnimationFrame(function () { el.classList.add('ss-toast-in'); });
  });
  window.setTimeout(function () {
    el.classList.add('ss-toast-out');
    window.setTimeout(function () { el.remove(); }, 400);
  }, 3000);
})();
</script>
"""

PAGE_SIZE = 20
DEFAULT_MIN_VISITS = 20_000
DEFAULT_MIN_CCU = 25
EMPTY_DATA_COLUMNS = [
    "universe_id", "root_place_id", "title", "ccu", "peak_ccu", "visits", "favorites",
    "genre", "creator_name", "creator_type", "creator_id", "description", "icon_url",            "has_discord", "discord_url", "status", "found_via", "has_social_links",
    "avg_ccu_1d", "avg_ccu_3d", "momentum_1d", "upvotes", "downvotes",
    "contacts_checked_at",
]
# Columns kept in st.session_state.data. ``description`` is dropped on purpose:
# the catalog's average description is ~0.5 KB per game, so a default result
# set of ~10k rows carries ~5 MB of text nobody renders — per session. At
# hundreds of users that text alone pushed the container toward its RAM
# ceiling (an OOM kill takes every user down at once). Everything still
# renders: no dashboard widget reads the description column.
# ``favorites``/``peak_ccu`` ride along (filter engine + other views may read
# them) but the results table no longer renders them.
SESSION_KEEP_COLUMNS = [
    "universe_id", "root_place_id", "title", "ccu", "peak_ccu", "visits", "favorites",
    "genre", "creator_name", "creator_type", "creator_id", "icon_url",
    "has_discord", "discord_url", "status", "found_via", "has_social_links",
    "avg_ccu_1d", "avg_ccu_3d", "momentum_1d", "upvotes", "downvotes",
    "contacts_checked_at",
]


def slim_result_frame(data: pd.DataFrame) -> pd.DataFrame:
    """Trim the result set to the columns the dashboard actually keeps.

    Session state holds one result frame PER browser session in one process;
    this is the single biggest per-user memory lever. Missing columns are
    tolerated (schema drift, demo frames) and a malformed frame passes
    through unchanged — trimming must never be the thing that breaks a rerun.
    """
    try:
        if data.empty:
            return data
        keep = [col for col in SESSION_KEEP_COLUMNS if col in data.columns]
        return data[keep] if keep else data
    except Exception:
        return data

st.set_page_config(
    page_title="UpScale Scouting Tool — Roblox Game Scouting",
    # The radar badge (assets/brand/favicon/favicon-32.png) is also injected
    # as the real browser favicon below; this emoji is only the fallback
    # page icon for contexts that cannot load the data URI.
    page_icon=str(BRAND_DIR / "favicon" / "favicon-32.png"),
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------------------------------------------------------------- #
# Demo data (offline fallback so the dashboard remains explorable)
# --------------------------------------------------------------------------- #

DEMO_GAMES = [
    dict(universe_id=1, root_place_id=4924922222, title="Brookhaven 🏡 RP", ccu=437200,
         visits=86_900_000_000, favorites=41_500_000, genre="Roleplay & Avatar Sim", creator_name="Wolfpaq",
         creator_type="User", icon_url="https://tr.rbxcdn.com/180DAY-03529af97a21dcc29156c5384cc1b01b/150/150/Image/Webp/noFilter",
         discord_url="https://discord.gg/brookhavenrp", status="OK", found_via="game_description"),
    dict(universe_id=2, root_place_id=2753915549, title="⚔️ Blox Fruits", ccu=361900,
         visits=63_800_000_000, favorites=32_100_000, genre="RPG", creator_name="Gamer Robot Inc",
         creator_type="Group", icon_url="https://tr.rbxcdn.com/180DAY-4884ac5cad0006f5c02e2a7ef0903a41/150/150/Image/Webp/noFilter",
         discord_url="https://discord.gg/bloxfruits", status="OK", found_via="game_social_links"),
    dict(universe_id=3, root_place_id=7436755782, title="[🪶] 99 Nights in the Forest", ccu=280600,
         visits=29_200_000_000, favorites=12_400_000, genre="Survival", creator_name="Boxpanda",
         creator_type="Group", icon_url="https://tr.rbxcdn.com/180DAY-bf9d9546a7607979a3d3f6f86ace512e/150/150/Image/Webp/noFilter",
         discord_url="https://discord.gg/99nights", status="OK", found_via="group_description"),
    dict(universe_id=4, root_place_id=142823291, title="Murder Mystery 2", ccu=259200,
         visits=29_800_000_000, favorites=15_200_000, genre="Survival", creator_name="Nikilis",
         creator_type="Group", icon_url="https://tr.rbxcdn.com/180DAY-ac1c764a99cfae201fd4fe916170a218/150/150/Image/Webp/noFilter",
         discord_url=None, status="No Contact Found", found_via=None),
    dict(universe_id=5, root_place_id=920587237, title="[🍓] Adopt Me!", ccu=249300,
         visits=44_500_000_000, favorites=28_900_000, genre="Roleplay & Avatar Sim", creator_name="Uplift Games",
         creator_type="Group", icon_url="https://tr.rbxcdn.com/180DAY-8a2116bd9d541c179d7bd4e611fe58b8/150/150/GameIcon3/Png/noFilter",
         discord_url=None, status="No Contact Found", found_via=None),
    dict(universe_id=6, root_place_id=155955794, title="[X20] +1 Speed Keyboard Escape", ccu=235300,
         visits=5_300_000_000, favorites=2_200_000, genre="Simulation", creator_name="Speed Studio",
         creator_type="Group", icon_url="https://tr.rbxcdn.com/180DAY-ae06e2703a3f516a9946173e656912c0/150/150/Image/Webp/noFilter",
         discord_url=None, status="No Contact Found", found_via=None),
    dict(universe_id=7, root_place_id=10449761463, title="RIVALS", ccu=212700,
         visits=17_700_000_000, favorites=6_100_000, genre="Shooter", creator_name="Nosniy Games",
         creator_type="Group", icon_url="https://tr.rbxcdn.com/180DAY-03529af97a21dcc29156c5384cc1b01b/150/150/Image/Webp/noFilter",
         discord_url="https://discord.gg/rivals", status="OK", found_via="group_social_links"),
    dict(universe_id=8, root_place_id=109983668079237, title="[🐝] Steal a Brainrot", ccu=146700,
         visits=73_000_000_000, favorites=9_800_000, genre="Simulation", creator_name="StealaBrainrot",
         creator_type="Group", icon_url="https://tr.rbxcdn.com/180DAY-bf9d9546a7607979a3d3f6f86ace512e/150/150/Image/Webp/noFilter",
         discord_url="https://discord.gg/stealabrainrot", status="OK", found_via="game_description"),
]


def empty_dataframe() -> pd.DataFrame:
    """Return a schema-stable frame for a valid live scan with zero matches."""
    return pd.DataFrame(columns=EMPTY_DATA_COLUMNS)


def demo_dataframe() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "universe_id": game["universe_id"],
            "root_place_id": game["root_place_id"],
            "title": game["title"],
            "ccu": game["ccu"],
            "peak_ccu": int(game["ccu"] * 1.15),
            "visits": game["visits"],
            "favorites": game["favorites"],
            "genre": game["genre"],
            "creator_name": game["creator_name"],
            "creator_type": game["creator_type"],
            "creator_id": None,
            "description": "",
            "icon_url": game["icon_url"],
            "has_discord": game["discord_url"] is not None,
            "discord_url": game["discord_url"],
            "status": game["status"],
            "found_via": game["found_via"],
            "has_social_links": bool(game["discord_url"]),
            "avg_ccu_1d": game["ccu"] * 0.93,
            "avg_ccu_3d": game["ccu"] * 0.91,
            "momentum_1d": int(game["ccu"] * 0.07),
            "upvotes": int(game["favorites"] * 0.9),
            "downvotes": int(game["favorites"] * 0.1),
            "contacts_checked_at": None,
        }
        for game in DEMO_GAMES
    ])


# --------------------------------------------------------------------------- #
# Onboarding
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Device profile: remembers you across refreshes and back-navigation.
# --------------------------------------------------------------------------- #


def _is_watch_view() -> bool:
    """True when the current render is the New & Upcoming watchlist.

    Read from the workspace radio's session key (the same source the render
    path uses) so table helpers can branch without threading a parameter
    through every call. Never raises.
    """
    try:
        return st.session_state.get("workspace_view") == "New & Upcoming"
    except Exception:
        return False


_PROFILE_FIELDS = (        "discord_filter_radio",
    "discord_name",
    "discord_user_id",
    "message_template",
    "message_variant",
    "message_studio",
    "master_unlocked",
    "target_min_visits",
    "target_min_ccu",
    "onboarding_step",
    "onboarding_complete",
)


def _device_ref() -> str | None:
    """Stable per-browser id: ``ss_ref`` cookie or ``?ref=`` URL param.

    Streamlit cannot set cookies from the server, so a one-shot client script
    mints a random id in localStorage, mirrors it into a 1-year ``ss_ref``
    cookie **and the URL query string**, then reloads the page once. From the
    next run on, the server reads it from ``st.context.cookies`` — or, when
    cookie reading fails (observed on Streamlit Cloud), from
    ``st.query_params``, which travels with every request.

    Loop safety (this caused a real infinite-reload incident): the reload
    script must run **at most once**. Two independent guards:

    * server-side: ``_device_cooked`` is set for the tab's lifetime, so a
      second run never re-mounts the script — even after
      ``session_state.clear()`` (Forget this device re-seeds it);
    * client-side: the script refuses to reload again within 30s
      (localStorage ``ss_ref_attempt``) and skips entirely once the cookie
      and query param are both in place — the previous reload starts a fresh
      server session, so the server-side guard alone cannot stop a loop.
    """
    if "_device_ref" in st.session_state:
        return st.session_state["_device_ref"]
    try:
        ref = (st.context.cookies.get("ss_ref") or "").strip()
    except Exception:
        ref = ""
    if not ref:
        try:
            ref = str(st.query_params.get("ref") or "").strip()
        except Exception:
            ref = ""
    if ref:
        st.session_state["_device_ref"] = ref
        return ref
    if st.session_state.get("_device_cooked"):
        # This tab already ran the mint script and neither channel surfaced
        # the id. Continue without a device id — never reload again.
        return None
    st.session_state._device_cooked = True
    st.html(
        r"""
<script>
(function () {
  var KEY = 'ss_ref';
  var GUARD = 'ss_ref_attempt';
  var ref = null;
  try { ref = window.localStorage.getItem(KEY); } catch (e) {}
  if (!ref) {
    ref = (window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID() : 'r-' + Date.now() + '-' + Math.random().toString(36).slice(2));
    try { window.localStorage.setItem(KEY, ref); } catch (e) {}
  }
  var hasCookie = /(^|;\s*)ss_ref=/.test(document.cookie);
  var q = new URLSearchParams(window.location.search);
  var hasRef = !!q.get('ref');
  if (hasCookie && hasRef) { return; }  // both channels in place — done
  var last = 0;
  try { last = parseInt(window.localStorage.getItem(GUARD) || '0', 10) || 0; } catch (e) {}
  if (Date.now() - last < 30000) { return; }  // already reloaded once just now
  var changed = false;
  if (!hasCookie) {
    document.cookie = 'ss_ref=' + encodeURIComponent(ref) + '; Path=/; Max-Age=31536000; SameSite=Lax';
    changed = true;
  }
  if (!hasRef) {
    q.set('ref', ref);
    history.replaceState(null, '', '?' + q.toString());
    changed = true;
  }
  if (!changed) { return; }
  try { window.localStorage.setItem(GUARD, String(Date.now())); } catch (e) {}
  window.location.reload();
})();
</script>
""",
        unsafe_allow_javascript=True,
    )
    return None


def _profile_save() -> None:
    """Snapshot the remembered fields into the device profile (never raises)."""
    ref = _device_ref()
    if not ref:
        return
    profile_store.save_profile(
        ref,
        {field: st.session_state.get(field) for field in _PROFILE_FIELDS},
    )


def _restore_from_profile() -> bool:
    """Seed session state from the device profile; True when one applied."""
    ref = _device_ref()
    if not ref:
        return False
    profile = profile_store.load_profile(ref)
    if not profile:
        return False
    for field in _PROFILE_FIELDS:
        if field in st.session_state:
            continue  # this session already has a live value; never clobber
        if field in profile and profile[field] is not None:
            st.session_state[field] = profile[field]
    # Hard floor (2026-10-05): profiles saved before the UI locked targets to
    # the pipeline's entry bar can still carry below-bar values; restore must
    # never surface a target the catalog cannot serve.
    st.session_state["target_min_visits"], st.session_state["target_min_ccu"] = (
        floored_targets(
            st.session_state.get("target_min_visits", DEFAULT_MIN_VISITS),
            st.session_state.get("target_min_ccu", DEFAULT_MIN_CCU),
        )
    )
    return True


def _render_gate() -> None:
    """Password screen in front of the whole app; ``st.stop()``s when locked."""
    if os.environ.get("SS_TEST_BYPASS_GATE") == "1":
        return  # automated tests exercise the app itself, not the lock
    ref = _device_ref()
    if gate.is_unlocked(ref):
        return

    st.markdown("<style>section[data-testid='stSidebar']{display:none}</style>", unsafe_allow_html=True)
    gate_icon = _brand_png("logo-mark-192.png")
    gate_icon_html = (
        f'<img src="{gate_icon}" alt="" width="84" height="84" '
        'style="border-radius:20px;margin-bottom:1rem">'
        if gate_icon else '<div class="gate-lock">🔐</div>'
    )
    st.markdown(
        """
        <style>
           .gate-card {
                max-width: 420px; margin: 9vh auto 0 auto; padding: 2.4rem 2.4rem;
                border: 1px solid rgba(250, 250, 250, 0.12); border-radius: 18px;
                background: rgba(250, 250, 250, 0.04); text-align: center;
                /* Gate entrance (plan 3.3): card fades up 12px, mark scales in. */
                animation: ss-gate-in var(--motion-slow) var(--ease-out) backwards;
            }
            @keyframes ss-gate-in {
                from { opacity: 0; transform: translateY(12px); }
                to { opacity: 1; transform: none; }
            }
            .gate-card img {
                animation: ss-gate-mark 400ms var(--ease-out) backwards;
            }
            @keyframes ss-gate-mark {
                from { opacity: 0; transform: scale(0.96); }
                to { opacity: 1; transform: none; }
            }
            /* Wrong-password shake: one 240ms cycle, triggered by the shake
               script adding .ss-shake after a failed attempt rerun. */
            .gate-card.ss-shake { animation: ss-gate-shake 240ms linear 1; }
            @keyframes ss-gate-shake {
                0%, 100% { transform: none; }
                25% { transform: translateX(-4px); }
                75% { transform: translateX(4px); }
            }
            .gate-lock { font-size: 2.6rem; }
            .gate-brand {
                font-size: 1.6rem; font-weight: 700; letter-spacing: 0.04em;
                margin-bottom: 0.2rem;
            }
            .gate-brand .ss-accent { color: #FF6E01; }
            .gate-tag {
                font-size: 0.72rem; font-weight: 600; letter-spacing: 0.22em;
                text-transform: uppercase; color: #9AA4B2; margin-top: 0;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="gate-card">{gate_icon_html}'
        '<div class="gate-brand">Up<span class="ss-accent">Scale</span> Scouting Tool</div>'
        '<p class="gate-tag">Roblox game scouting</p>'
        '<p style="opacity:0.75;margin-top:0.8rem">This site is private. Enter the access password to continue.</p>',
        unsafe_allow_html=True,
    )

    remaining = gate.cooldown_remaining(ref)
    if gate._expected_password() == "":
        # Deployment not configured: fail closed with an actionable message
        # instead of a door nobody can open (and never burn attempts on it).
        st.error(
            "🔒 **This deployment has no access password configured.** The "
            "owner must set ``APP_PASSWORD`` in the app's Streamlit secrets "
            "— until then nobody can sign in."
        )
    elif remaining <= 0:
        candidate = st.text_input("Password", type="password", key="gate_password")
        if st.button("Unlock", type="primary", width="stretch", key="gate_unlock"):
            result = gate.check_password(candidate, ref, gate._client_ip())
            if result == "ok":
                st.session_state.gate_unlocked = True
                # Master-key marker: only the deployment's own password (the
                # owner's key) unlocks the Increates Studio choice. Friend
                # keys never see it. Kept in the device profile so it
                # survives refreshes — the gate itself only remembers THAT
                # the device is unlocked, not which password did it.
                if candidate == gate._expected_password():
                    st.session_state.master_unlocked = True
                    _profile_save()
                gate.remember_unlock(ref)
                st.rerun()
            if result == "banned":
                st.error(
                    "⛔ **This access password has been disabled.** It was used "
                    "from multiple devices and locations, which the owner treats "
                    "as sharing. Ask them for your own password."
                )
            if result == "wrong":
                if gate.cooldown_remaining(ref) > 0:
                    st.rerun()  # a cooldown just started — show the timer now
                left = gate.attempts_left(ref)
                if left > 0:
                    st.error(
                        f"Wrong password. {left} "
                        f"attempt{'s' if left != 1 else ''} left before the wait starts."
                    )
                # Physical feedback (plan 3.3): the card shakes once. Set now,
                # consumed on the rerun the error line itself rides on.
                st.session_state["_gate_wrong_attempt"] = True
    else:
        total = gate.cooldown_total(ref) or 60.0
        mm, ss = divmod(int(remaining + 0.999), 60)
        st.warning(f"Too many wrong attempts. Try again in {mm}:{ss:02d}.")
        st.progress(min(1.0, max(0.02, 1.0 - remaining / total)))
        st.caption("Keep this tab open — the timer runs on the server, refreshing does not help.")
        # Live countdown: the browser re-checks every 8 s; once the
        # server-side timer expires this very reload renders the password
        # field again. 2 s used to make every locked-out browser a full-page
        # reload machine — hundreds of users in cooldown would reload-storm
        # the container (a full script run each time).
        st.html(
            r"<script>setTimeout(function () { window.location.reload(); }, 8000);</script>",
            unsafe_allow_javascript=True,
        )

    st.markdown("</div>", unsafe_allow_html=True)
    shake = _GATE_SHAKE_SCRIPT if st.session_state.pop("_gate_wrong_attempt", False) else ""
    st.html(_MOTION_CORE + shake + _INPUT_GUARD_SCRIPT + _FAVICON_SCRIPT + _PASSWORD_ICON_GUARD_SCRIPT, unsafe_allow_javascript=True)  # no save-password prompt here either
    st.stop()


def initialize_session() -> bool:
    """Seed defaults, then overlay the device profile; True when one applied."""
    restored = _restore_from_profile()  # first, so defaults only fill gaps
    defaults = {
        "onboarding_step": 0,
        "onboarding_complete": False,
        "target_min_visits": DEFAULT_MIN_VISITS,
        "target_min_ccu": DEFAULT_MIN_CCU,
        "discord_name": "",  # asked in the welcome flow; auto-fills the outreach message
        "discord_user_id": "",  # optional; turns [Your Name] into a real <@ID> mention
        "message_template": DEFAULT_MESSAGE_TEMPLATE,
        "message_variant": 0,  # which of the 5 starter templates is active
        "message_studio": "UpScale Studio",  # master-only: UpScale vs Increates
        "master_unlocked": False,  # True when the owner's own password unlocked
        "pending_initial_scan": False,
    "welcome_scan_started": False,
        "active_run_id": None,
        "contact_page": 1,
        "scan_error": "",
        "source": "demo",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    # Keep onboarding widget keys in sync with the canonical target values.
    if "onboard_visits" not in st.session_state:
        st.session_state.onboard_visits = st.session_state.target_min_visits
    if "onboard_ccu" not in st.session_state:
        st.session_state.onboard_ccu = st.session_state.target_min_ccu
    return restored


def set_preset(min_visits: int, min_ccu: int) -> None:
    min_visits, min_ccu = floored_targets(min_visits, min_ccu)
    st.session_state.target_min_visits = min_visits
    st.session_state.target_min_ccu = min_ccu
    st.session_state.onboard_visits = min_visits
    st.session_state.onboard_ccu = min_ccu


def _save_onboarding_targets() -> None:
    """Persist the number_input values so they survive when the widgets
    disappear from the render tree on the next rerun."""
    st.session_state.target_min_visits = max(
        int(st.session_state["onboard_visits"] or 0), DEFAULT_MIN_VISITS
    )
    st.session_state.target_min_ccu = max(
        int(st.session_state["onboard_ccu"] or 0), DEFAULT_MIN_CCU
    )
    st.session_state.onboard_visits = st.session_state.target_min_visits
    st.session_state.onboard_ccu = st.session_state.target_min_ccu
    _profile_save()


def _save_profile_identity() -> None:
    """Snapshot identity fields (name / ID / template) on change."""
    _profile_save()


def _message_variant_options() -> list:
    """Labels for the 5 starter templates ('Starter 1 — original default' …)."""
    return [
        ("Starter 1 — original" if i == 0 else f"Starter {i + 1} — variation")
        for i in range(len(DEFAULT_MESSAGE_TEMPLATES))
    ]


def _master_unlocked() -> bool:
    """True when the deployment's own (owner/master) password unlocked this
    device. Regular scout friend-keys get the standard flow only."""
    return bool(st.session_state.get("master_unlocked", False))


def _active_message_templates() -> tuple:
    """The starter set for the chosen studio (master-only switch)."""
    if _master_unlocked() and st.session_state.get("message_studio") == "Increates Studio":
        return INCREASES_MESSAGE_TEMPLATES
    return DEFAULT_MESSAGE_TEMPLATES


def _apply_message_studio() -> None:
    """Load the freshly chosen studio's first starter into the editable
    template and remember the choice (master-only control)."""
    st.session_state.message_variant = _message_variant_options()[0]
    st.session_state.message_template = _active_message_templates()[0]
    _profile_save()


def _apply_message_variant() -> None:
    """Load the picked starter into the editable template and remember it.

    The radio picks a starting point; the text_area stays the single source
    of truth that the Copy button renders. The radio holds the selected
    LABEL (Streamlit stores option values, not indices), so the label is
    mapped back to its template; an unknown label falls back to Starter 1.
    """
    options = _message_variant_options()
    try:
        index = options.index(st.session_state.get("message_variant"))
    except (ValueError, TypeError):
        index = 0
    st.session_state.message_template = _active_message_templates()[index]
    _profile_save()


def render_onboarding() -> bool:
    """Render the first-run flow; return True when it has completed."""
    step = int(st.session_state.onboarding_step)
    st.markdown("<style>section[data-testid='stSidebar']{display:none}</style>", unsafe_allow_html=True)

    if step == 0:
        st.title("Welcome Fellow Scout")
        st.subheader("to the UpScale Scouting Tool")
        st.write("Find Roblox games that fit your targets, then check only the results you care about.")
        st.info("Your first scan uses the targets you choose next. Contact lookups are loaded page by page.")
        if st.button("Next", type="primary", width="stretch", key="onb0_next"):
            st.session_state.onboarding_step = 1
            _profile_save()  # progress survives a refresh even this early
            st.rerun()
        return False

    if step == 1:
        st.title("What is your target?")
        st.caption("Choose the minimum activity a game must have to appear in your scouting results.")

        st.write("Quick presets")
        preset_columns = st.columns(4)
        presets = [
            ("20k+ visits · 25+ CCU", 20_000, 25),
            ("100k+ visits · 125+ CCU", 100_000, 125),
            ("50k+ visits · 75+ CCU", 50_000, 75),
            ("75k+ visits · 250+ CCU", 75_000, 250),
        ]
        for column, (label, visits, ccu) in zip(preset_columns, presets):
            column.button(
                label,
                key=f"preset_{visits}_{ccu}",
                on_click=set_preset,
                args=(visits, ccu),
                width="stretch",
            )

        left, right = st.columns(2)
        left.number_input(
            "Minimum visits",
            min_value=DEFAULT_MIN_VISITS,
            step=1_000,
            key="onboard_visits",
            on_change=_save_onboarding_targets,
            persist_state="session",
            help="Hard floor at the pipeline's entry bar — games below this lifetime visit count never enter the catalog.",
        )
        right.number_input(
            "Minimum CCU",
            min_value=DEFAULT_MIN_CCU,
            step=25,
            key="onboard_ccu",
            on_change=_save_onboarding_targets,
            persist_state="session",
            help="Hard floor at the pipeline's entry bar — games below this current player count never enter the catalog.",
        )
        if int(st.session_state.target_min_visits) <= DEFAULT_MIN_VISITS and int(
            st.session_state.target_min_ccu
        ) <= DEFAULT_MIN_CCU:
            st.warning("Both targets are at the entry bar — fine, but you can raise them any time.")
        if st.button("Next", type="primary", width="stretch", key="onb1_next"):
            if int(st.session_state.target_min_visits) or int(st.session_state.target_min_ccu):
                st.session_state.onboarding_step = 3
                _profile_save()  # targets + step survive a refresh
                st.rerun()
        return False

    if step == 3:
        st.title("What is your Discord username?")
        st.caption(
            "Your username is filled into the outreach message automatically, so game "
            "owners see who is contacting them. You can change it later in the sidebar."
        )
        st.text_input(
            "Discord username",
            key="discord_name",
            max_chars=64,
            placeholder="e.g. dev_razor10 or rip_indra",
            persist_state="session",
            on_change=_save_profile_identity,
        )
        if not str(st.session_state.discord_name or "").strip():
            st.caption("Tip: add your username so the message template can fill it in for you.")
        st.text_input(
            "Discord User ID (optional)",
            key="discord_user_id",
            max_chars=32,
            placeholder="e.g. 53908099506183680",
            persist_state="session",
            on_change=_save_profile_identity,
        )
        st.caption(
            "How to find it: 1) Discord → User Settings → Advanced → turn on Developer Mode. "
            "2) Right-click your own name anywhere in a server or DM. "
            "3) Click “Copy User ID” and paste it here."
        )
        st.caption(
            "Why an ID? Pasting “@username” into Discord is plain text — only messages "
            "carrying your numeric ID ping and link to you. Leave it blank and your plain "
            "username is used instead; this field is always optional."
        )
        if str(st.session_state.discord_user_id or "").strip() and not normalize_discord_user_id(
            st.session_state.discord_user_id
        ):
            st.warning(
                "That doesn't look like a User ID (it should be 15–21 digits). "
                "It will be ignored and your plain username used — or clear the field."
            )
        back, next_column = st.columns(2)
        if back.button("Back", width="stretch", key="onb3_back"):
            st.session_state.onboarding_step = 1
            st.rerun()
        if next_column.button("Next", type="primary", width="stretch", key="onb3_next"):
            st.session_state.onboarding_step = 4
            _profile_save()  # keep name / ID / progress across refreshes
            st.rerun()
        return False

    if step == 4:
        st.title("Your outreach message")
        st.caption(
            "This is the message the Copy button prepares for each game. Edit it "
            "however you like, or continue with the default."
        )
        if _master_unlocked():
            # Owner-only (master password): pick which studio's starter set
            # the rotation draws from. Regular scout keys never see this.
            st.radio(
                "Studio",
                options=("UpScale Studio", "Increates Studio"),
                key="message_studio",
                persist_state="session",
                on_change=_apply_message_studio,
                help=(
                    "UpScale Studio rotates the five original revenue-share "
                    "starters. Increates Studio swaps in a deals-first set "
                    "instead."
                ),
            )
        st.radio(
            "Starter message",
            options=_message_variant_options(),
            key="message_variant",
            persist_state="session",
            on_change=_apply_message_variant,
        )
        st.text_area(
            "Message template",
            key="message_template",
            height=430,
            persist_state="session",
            on_change=_save_profile_identity,
        )
        st.caption(
            "Every copy click picks one of the 5 starters at random and never "
            "repeats the previous one, so two servers never get identical "
            "messages back to back — that's what Discord's anti-spam filters "
            "flag. Your customized template (if it differs from all starters) "
            "joins the rotation as a sixth voice. Edit any starter above; "
            "picking one just loads it here as your starting point."
        )
        st.caption(
            "Make sure you use the [Your Name] tag for your Discord username and the "
            "[Game Name] tag for the game's name — both are filled in automatically "
            "when you copy a message, so keep them if you edit the text."
        )
        st.caption(
            "With a User ID saved, [Your Name] copies as a real @mention; without one, "
            "your plain username is used. Both work — the ID is optional."
        )
        preview = render_outreach_message(
            st.session_state.message_template,
            st.session_state.discord_name,
            "Blox Fruits",
            discord_user_id=st.session_state.get("discord_user_id", ""),
        )
        with st.expander("Preview — your name + a sample game", expanded=False):
            st.code(preview, language="markdown")
        back, next_column = st.columns(2)
        if back.button("Back", width="stretch", key="onb4_back"):
            st.session_state.onboarding_step = 3
            st.rerun()
        if next_column.button("Start my first scan", type="primary", width="stretch", key="onb4_start"):
            # Bulletproof target capture: read the live onboarding widget
            # values at this exact moment and copy them into the canonical
            # keys that the sidebar and the first scan consume.
            st.session_state.target_min_visits = max(
                int(st.session_state.get("onboard_visits", st.session_state.target_min_visits) or 0),
                DEFAULT_MIN_VISITS,
            )
            st.session_state.target_min_ccu = max(
                int(st.session_state.get("onboard_ccu", st.session_state.target_min_ccu) or 0),
                DEFAULT_MIN_CCU,
            )
            st.session_state.onboarding_complete = True
            _profile_save()  # completion + targets survive refreshes now
            # Run the real first scan on the post-onboarding rerun with the
            # captured targets. The dashboard paints as soon as it finishes;
            # speed hardening in scout_core keeps that well under a minute.
            st.session_state.pending_initial_scan = True
            st.session_state.contact_page = 1
            st.rerun()
        return False




_restored = initialize_session()
_render_gate()  # private site: nothing below renders without the password

# Motion tokens + app-wide interaction rules, once per run (Phase 0).
st.html(_MOTION_CSS, unsafe_allow_javascript=True)

# --- Capacity metering (free-container self-protection) -------------------- #
# 1 GB ceiling, no auto-scaling: the app meters itself. First the watchdog
# fragment (it may have observed an expiry/dead-slot release since the last
# full run), then the admission decision for THIS rerun. Queue position and
# the session countdown render right after (queue screen st.stop()s).
if os.environ.get("SS_TEST_BYPASS_GATE") != "1" and os.environ.get("SS_CAPACITY_TEST_BYPASS") != "1":
    _cap_ref = _device_ref()
    if capacity.ensure_session(_cap_ref):
        capacity.render_session_chip(_cap_ref)
    else:
        capacity.render_queue_screen(_cap_ref)
        st.stop()
if _restored and st.session_state.onboarding_complete:
    # Returning scout: auto-load their results once per session instead of
    # dropping them on an empty table after a refresh.
    st.session_state.pending_initial_scan = not st.session_state.get("welcome_scan_started")
if not st.session_state.onboarding_complete:
    if _CATALOG_UNAVAILABLE:
        st.warning(
            "⚠️ **The catalog is temporarily unavailable.** The shared catalog "
            "store is unreachable right now (usually fixed automatically "
            "within minutes, when the next pipeline sync succeeds). Showing "
            "a small demo until then."
        )
    render_onboarding()
    st.stop()

if _CATALOG_UNAVAILABLE:
    st.warning(
        "⚠️ **The catalog is temporarily unavailable.** The shared catalog "
        "store could not be fetched. The next successful pipeline sync "
        "(usually within minutes) fixes this automatically — reload the page "
        "after an hour at the latest. Showing a small demo until then."
    )


def _warn_stale_catalog() -> None:
    """Announce a stalled pipeline instead of serving silently old stats.

    The 2026-09-21 scheduler outage went unnoticed for 3 days because every
    workflow run was green — none were being started at all. A dashboard that
    says "stats last refreshed X hours ago" makes any future stall visible
    to the user immediately. Reads MAX(ccu_history.ts): every hydrator/expander
    run writes fresh samples, so that timestamp is the true data heartbeat
    (last_updated moves for other reasons, e.g. tier restamps). Never raises.
    """
    # App-side staleness (2026-10-05 "stats are days old" complaints): when
    # the freshness check fails, the app pins to its cached copy silently —
    # _USING_STALE_CATALOG was assigned but never consumed. The banner below
    # reads the CACHED db's own heartbeat, so a pinned cache always looks
    # fresh no matter how old the data is. Say it out loud instead.
    if _USING_STALE_CATALOG:
        try:
            st.warning(
                "📦 **You're viewing a cached copy of the catalog** — the live "
                "catalog could not be reached from this server just now, so "
                "stats may be out of date. This usually clears itself within "
                "minutes; hit Refresh in a bit if it persists."
            )
        except Exception:
            pass  # never block the dashboard on banner rendering

    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(ts) FROM ccu_history").fetchone()
        finally:
            conn.close()
        latest = row[0] if row else None
        if not latest:
            return
        import pandas as pd

        age_hours = (
            pd.Timestamp.now("UTC").tz_localize(None) - pd.to_datetime(latest, errors="coerce")
        ).total_seconds() / 3600.0
        if pd.isna(age_hours):
            return
        if age_hours >= 6:
            st.warning(
                f"⏳ **Stats are {age_hours:.0f} hours old.** The 24/7 pipeline "
                "that refreshes CCU/visits appears to be stalled — this is not "
                "your filters. It self-heals on the next successful sync; if it "
                "persists, the scheduler (Cloudflare Worker cron / GitHub "
                "Actions) needs attention."
            )
        elif age_hours >= 1:
            st.caption(f"Stats refreshed {age_hours:.0f} h ago.")
    except Exception:
        pass  # a staleness probe must never block the dashboard


_warn_stale_catalog()


# --------------------------------------------------------------------------- #
# Session and scan helpers
# --------------------------------------------------------------------------- #


# Process-wide shared catalog cache.
#
# Before: every user's sync ran load_catalog_matches itself (full SQL scan +
# trend-window GROUP BY over ccu_history) and stored its own private frame.
# 100 users on one target preset = the same heavy query computed 100 times
# and 100 copies of the same data. That per-user CPU spike is exactly what
# killed the free container at scale.
#
# After: the FIRST user with a given (min_visits, min_ccu, discord) combo
# computes the frame once; everyone else on the same targets reuses it
# (a cheap copy-on-share). Catalog pushes change the DB, not the frame, so
# entries carry a short TTL and users can always hit Refresh to force a
# recompute. Ten presets ≈ ten frames — bounded, no matter how many users.

CATALOG_CACHE_TTL = 300  # seconds a shared frame stays fresh
_CATALOG_CACHE: dict = {}
_CATALOG_CACHE_LOCK = threading.Lock()


def _downcast_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Shrink numeric columns to the smallest safe dtype.

    int64 → int32/int16 where values fit; float64 → float32. Visits, CCU,
    favorites and vote counts all fit comfortably in int32; halving the
    numeric bytes directly halves every session's resident copy. Labels and
    strings are untouched. Never raises: a deprecation in pandas downcast
    paths must degrade to returning the frame unchanged.
    """
    try:
        out = frame.copy()
        for column in out.columns:
            series = out[column]
            if pd.api.types.is_integer_dtype(series) and str(series.dtype) != "int32":
                out[column] = pd.to_numeric(series, downcast="integer")
            elif pd.api.types.is_float_dtype(series) and str(series.dtype) != "float32":
                out[column] = pd.to_numeric(series, downcast="float")
        return out
    except Exception:
        return frame


def _shared_catalog_frame(min_visits: int, min_ccu: int, discord: bool | None, force: bool = False) -> pd.DataFrame:
    """Return the shared, downcast frame for this target combo.

    Cache key is (min_visits, min_ccu, discord). On a hit the caller gets a
    shallow copy (session-owned edits like contact-cell state must never
    leak between users); on a miss exactly one caller computes while the
    rest wait on the lock and then reuse the result. ``force`` (Refresh
    button) bypasses freshness and recomputes.
    """
    key = (int(min_visits or 0), int(min_ccu or 0), discord)
    now = time.monotonic()
    with _CATALOG_CACHE_LOCK:
        entry = _CATALOG_CACHE.get(key)
        if entry and not force and now - entry[0] < CATALOG_CACHE_TTL:
            return entry[1].copy()
    frame = _read_catalog(min_visits, min_ccu, discord)
    if frame.empty:
        # Do not cache empties: a half-finished pipeline push or an outage
        # would otherwise pin a blank catalog for the whole TTL.
        return frame
    frame = _downcast_frame(frame)
    with _CATALOG_CACHE_LOCK:
        # Bound the cache: keep only the most recent 12 target combos.
        if len(_CATALOG_CACHE) >= 12:
            oldest = min(_CATALOG_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CATALOG_CACHE.pop(oldest, None)
        _CATALOG_CACHE[key] = (now, frame)
    return frame.copy()


def _read_catalog(min_visits: int, min_ccu: int, discord: bool | None = None) -> pd.DataFrame:
    """Read the catalog, tolerating a stale ``scout_core`` in the running process.

    During a Streamlit Cloud redeploy the script on disk (app.py) is re-read on
    every rerun, but the already-imported ``scout_core`` module stays in
    ``sys.modules`` until the container restarts. In that window the fresh
    caller can meet the old ``load_catalog_matches`` signature (no ``discord``
    parameter) and crash with a red TypeError instead of showing results.

    The stale signature is detected per call; the fallback applies the
    Discord constraint in memory so the filter keeps working until the
    restart clears the skew. Never raises.
    """
    import inspect

    frame = pd.DataFrame()
    try:
        method = scout.load_catalog_matches
        if "discord" in inspect.signature(method).parameters:
            frame = method(min_visits=min_visits, min_ccu=min_ccu, discord=discord)
        else:
            # Stale module in sys.modules: call the old signature, filter below.
            frame = method(min_visits=min_visits, min_ccu=min_ccu)
    except Exception:
        return pd.DataFrame()
    if discord is None or frame.empty:
        return frame
    has = pd.to_numeric(frame.get("has_discord"), errors="coerce").fillna(0) == 1
    return frame.loc[has if discord else ~has].copy()


def get_scout() -> RobloxPlatformScout:
    if "scout" not in st.session_state:
        st.session_state.scout = RobloxPlatformScout(
            db_path=(DB_PATH or ""),
            # Per-session worker pool: every session gets its own, so keep it
            # small — the process-wide ROBLOX_CALL_GATE (scout_core) is what
            # actually bounds total outbound Roblox concurrency across all
            # users; 8 workers per session just multiplied queue pressure.
            max_workers=2,
        )
    return st.session_state.scout


def load_existing_or_demo(scout: RobloxPlatformScout) -> tuple[pd.DataFrame, str]:
    # Pre-onboarding preview only: bound the read (a fresh catalog holds
    # ~19k games plus a 100k+ row history table — parsing all of it just to
    # decide "show demo data" made every cold visitor pay full freight).
    existing = scout.load_catalog_matches(limit=500)
    if existing.empty:
        return demo_dataframe(), "demo"
    return existing, "db"


def shift_contact_page(delta: int, page_count: int) -> None:
    current = int(st.session_state.get("contact_page", 1))
    st.session_state.contact_page = max(1, min(int(page_count), current + int(delta)))


def reset_contact_page() -> None:
    st.session_state.contact_page = 1
    st.session_state.active_run_id = None


# --------------------------------------------------------------------------- #
# Main sidebar controls
# --------------------------------------------------------------------------- #

scout = get_scout()

# Sidebar brand lockup: the radar badge + name treatment ("Scouts" in Scout
# Green), replacing the emoji title. Rendered as one st.html block so the
# mark and the type scale together.
_sidebar_badge = _brand_png("logo-mark-192.png")
_sidebar_badge_src = (
    f'src="{_sidebar_badge}"' if _sidebar_badge else 'src="" style="display:none"'
)
st.sidebar.markdown(
    f"""
    <style>
      .ss-brand {{ display: flex; align-items: center; gap: 12px; margin: 0.4rem 0 0.15rem; }}
      .ss-brand img {{ width: 44px; height: 44px; border-radius: 12px; }}
      .ss-brand-name {{
        font-size: 1.18rem; font-weight: 700; letter-spacing: 0.03em;
        line-height: 1.1; color: #E7ECF3;
      }}
      .ss-brand-name .ss-accent {{ color: #FF6E01; }}
      .ss-brand-tag {{
        font-size: 0.68rem; font-weight: 600; letter-spacing: 0.18em;
        text-transform: uppercase; color: #9AA4B2; margin-top: 2px;
      }}
    </style>
    <div class="ss-brand">
      <img {_sidebar_badge_src} alt="UpScale Scouting Tool">
      <div>
        <div class="ss-brand-name">Up<span class="ss-accent">Scale</span> Scouting Tool</div>
        <div class="ss-brand-tag">Roblox game scouting</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)
st.sidebar.caption("Find games. Find owners. Make contact.")

# Workspace switch: the New and Upcoming view reuses the exact same paging,
# Discord-check and table pipeline as the main view — only the data source
# (blow-up watchlist) and the absence of filters differ.
view = st.sidebar.radio(
    "Workspace",
    options=["Main scout", "New & Upcoming"],
    key="workspace_view",
)
is_watch_view = view == "New & Upcoming"
# Switching workspaces lands you on page 1 of the new view; contact state
# stays per-view so neither side loses its checked pages.
if st.session_state.get("last_workspace_view") != view:
    st.session_state.last_workspace_view = view
    st.session_state.contact_page = 1

# Stop Chrome's "save your password?" bubble on the cookie / Discord fields.
# Page-level flag inside the script makes repeated mounts harmless.
st.html(_INPUT_GUARD_SCRIPT + _FAVICON_SCRIPT, unsafe_allow_javascript=True)

with st.sidebar.expander("Current target", expanded=True):
    min_visits = st.number_input(
        "Minimum visits",
        min_value=DEFAULT_MIN_VISITS,
        step=1_000,
        value=st.session_state.get("target_min_visits", DEFAULT_MIN_VISITS),
        key="target_min_visits",
        persist_state="session",
        help="Hard floor: the pipeline only stores games at or above this bar, so lower settings would always return nothing.",
    )
    min_ccu = st.number_input(
        "Minimum CCU",
        min_value=DEFAULT_MIN_CCU,
        step=25,
        value=st.session_state.get("target_min_ccu", DEFAULT_MIN_CCU),
        key="target_min_ccu",
        persist_state="session",
        help="Hard floor: the pipeline only stores games at or above this bar, so lower settings would always return nothing.",
    )
    st.caption(
        f"Targets filter your results the moment you sync (locked at {DEFAULT_MIN_VISITS // 1000}k visits / {DEFAULT_MIN_CCU} CCU — the pipeline's entry bar). New games appear as the 24/7 pipeline discovers them."
    )
    if st.button("Apply filters", type="primary", width="stretch", key="apply_filters"):
        st.session_state.contact_page = 1
        st.rerun()

with st.sidebar.expander("Your message", expanded=False):
    if _master_unlocked():
        # Owner-only (master password): the studio switch follows the scout
        # out of the welcome flow, so the set can be flipped any time.
        st.radio(
            "Studio",
            options=("UpScale Studio", "Increates Studio"),
            key="message_studio",
            persist_state="session",
            on_change=_apply_message_studio,
        )
    st.radio(
        "Starter message",
        options=_message_variant_options(),
        key="message_variant",
        persist_state="session",
        on_change=_apply_message_variant,
    )
    st.text_input(
        "Discord username",
        key="discord_name",
        max_chars=64,
        persist_state="session",
        on_change=_save_profile_identity,
    )
    st.text_input(
        "Discord User ID (optional)",
        key="discord_user_id",
        max_chars=32,
        help="Pasted “@username” is plain text in Discord — real pings need your numeric ID.",
        persist_state="session",
        on_change=_save_profile_identity,
    )
    st.caption(
        "ID set → [Your Name] copies as a clickable, pingable @mention. "
        "Find it: Developer Mode → right-click your name → Copy User ID."
    )
    st.text_area(
        "Message template",
        key="message_template",
        height=250,
        persist_state="session",
        on_change=_save_profile_identity,
    )
    st.caption(
        "Each Copy click picks a random starter (never the same one twice "
        "in a row) and auto-fills [Your Name] / [Game Name]. Edit the text "
        "above to customize what gets rotated."
    )

if st.sidebar.button("Forget this device", width="stretch", key="forget_device"):
    ref = _device_ref()
    if ref:
        profile_store.clear_profile(ref)
        gate.forget_unlock(ref)
        capacity.leave(ref)  # release the slot and the queue place too
    had_device = "_device_ref" in st.session_state or "_device_cooked" in st.session_state
    st.session_state.clear()
    # Keep the tab-level device flags so clearing the profile does not
    # re-trigger the one-shot cookie-mint reload script.
    if had_device:
        st.session_state._device_cooked = True
    if ref:
        st.session_state._device_ref = ref
    gate.lockdown()  # back to the password screen, signed out
    st.rerun()

sync = st.sidebar.button(
    "Sync live data", type="primary", width="stretch", key="sync_live_data"
)

if sync or st.session_state.pending_initial_scan:
    _profile_save()  # targets/identity snapshot rides along with the scan
    st.session_state.pending_initial_scan = False
    st.session_state.welcome_scan_started = True
    st.session_state.scan_error = ""
    _sync_was_explicit = bool(sync)
    with st.spinner("Loading games that meet your targets..."):
        try:
            # Read-only catalog query — no discovery, no Roblox requests.
            # Served from the process-wide shared cache: the first user on a
            # target preset computes it, everyone else reuses it (see
            # _shared_catalog_frame). force=True only on an explicit refresh
            # click, never on the routine initial scan.
            data = _shared_catalog_frame(
                min_visits=int(min_visits),
                min_ccu=int(min_ccu),
                discord=None,
                force=bool(sync),
            )
            # Keep only what the dashboard renders in session state (see
            # SESSION_KEEP_COLUMNS) — never the demo/DB fallback frame.
            st.session_state.data = slim_result_frame(data) if not data.empty else empty_dataframe()
            # The filter pipeline runs on the freshly synced result set so a
            # brand-new verdict becomes visible on the very next rerun.
            df = st.session_state.data
            if not df.empty and st.session_state.get("discord_filter_radio", DISCORD_FILTER_ALL) != DISCORD_FILTER_ALL:
                df = apply_filters(df, discord_filter=st.session_state.discord_filter_radio)
            st.session_state.data = df
            # Keep the exact onboarding targets attached to the result set so
            # the dashboard cannot accidentally present a previous cached scan.
            st.session_state.result_target_min_visits = int(min_visits)
            st.session_state.result_target_min_ccu = int(min_ccu)
            st.session_state.source = "live"
            st.session_state.active_run_id = None
            reset_contact_page()
            # Successful sync (plan 2.3): a toast slides up bottom-right on
            # the rerun that renders the fresh table. Only for an explicit
            # 'Sync live data' click — the automatic initial scan must not
            # greet returning scouts with a notification they did not ask for.
            if _sync_was_explicit:
                st.session_state["_sync_toast_pending"] = True
        except Exception as exc:
            st.session_state.scan_error = str(exc)
            # On failure show a clear schema-stable empty frame; do NOT drag
            # the whole tracked catalog (or demo data) into session state.
            st.session_state.data = empty_dataframe()
            st.session_state.source = "live"
            st.session_state.active_run_id = None
            reset_contact_page()

if "data" not in st.session_state:
    # Fallback only: the first scan normally populates data on the
    # post-onboarding rerun. If it ever runs first (edge case), avoid
    # serving a stale DB cache after the welcome flow selected targets.
    if st.session_state.get("welcome_scan_started"):
        st.session_state.data = empty_dataframe()
        st.session_state.source = "live"
    else:
        st.session_state.data, st.session_state.source = load_existing_or_demo(scout)
    st.session_state.result_target_min_visits = int(st.session_state.target_min_visits)
    st.session_state.result_target_min_ccu = int(st.session_state.target_min_ccu)

# A cached database is only a startup fallback. Once the welcome flow has
# selected targets, never mix that old cache into the requested result set.
if st.session_state.source == "db":
    cached_min_visits = int(st.session_state.get("result_target_min_visits", 0))
    cached_min_ccu = int(st.session_state.get("result_target_min_ccu", 0))
    if (cached_min_visits, cached_min_ccu) != (int(st.session_state.target_min_visits), int(st.session_state.target_min_ccu)):
        st.session_state.data = empty_dataframe()
        st.session_state.source = "live"

# No per-rerun .copy(): nothing in this script mutates the stored frame in
# place (update_contact_rows returns a new frame). A full-frame copy per
# rerun per session was pure memory churn under many concurrent users.
df = st.session_state.data
source = st.session_state.source
if df.empty:
    if source == "demo":
        df = demo_dataframe()
    else:
        df = empty_dataframe()

if is_watch_view:
    # Blow-up watchlist straight from the DB (blowup_flag = 1, freshest
    # signal first). No result-set caching and no thresholds apply here —
    # a flagged game must appear regardless of the user's main targets.
    df = scout.load_blowup_watch()
    source = "watch"
    if df.empty:
        # Schema-stable frame: a brand-new catalog has no flagged rows yet,
        # and the paging pipeline reads universe_id/genre columns regardless.
        df = empty_dataframe()

# The watchlist ignores the user's target thresholds entirely.
eff_min_visits = 0 if is_watch_view else int(min_visits)
eff_min_ccu = 0 if is_watch_view else int(min_ccu)

# --------------------------------------------------------------------------- #
# Filters and paginated contact loading
# --------------------------------------------------------------------------- #

if is_watch_view:
    st.sidebar.header("New & Upcoming")
    st.sidebar.caption(
        "No filters here by design — this is the raw blow-up watchlist. "
        "Filters live on the Main scout view."
    )
    search = ""
    selected_genres = []
else:
    st.sidebar.header("Scout filters")
    search = ""

    # Discord contact filter (re-added 2026-09-24). Applies AFTER metric
    # filtering so it never changes which pages get contact checks; it only
    # narrows what is displayed.
    st.sidebar.radio(
        "Discord availability",
        options=[DISCORD_FILTER_ALL, DISCORD_FILTER_TRUE, DISCORD_FILTER_FALSE],
        key="discord_filter_radio",
        help="'Discord available' keeps games with a resolved invite; "
             "'No Discord' keeps games checked and found without one. "
             "Unchecked games disappear from both narrowed views.",
    )
    selected_genres = []

# Apply metric + Discord-availability filters when deciding which contact
# page to fetch. The Discord filter MUST run before the page slice: applied
# after, it only narrowed the 20 rows already on the page (2–5 visible games
# instead of 20) and contact checks were scheduled for filtered-out rows.
discord_view = st.session_state.get("discord_filter_radio", DISCORD_FILTER_ALL)
metric_filtered = apply_filters(
    df,
    search=search,
    min_visits=eff_min_visits,
    min_ccu=eff_min_ccu,
    genres=selected_genres,
    discord_filter=discord_view,
)

signature = "|".join([
    search,
    str(min_visits),
    str(min_ccu),
    ",".join(selected_genres),
    discord_view,
])
if signature != st.session_state.get("filter_signature"):
    st.session_state.filter_signature = signature
    st.session_state.contact_page = 1

page_size = st.sidebar.selectbox("Games per page", options=[10, 20, 40], index=1, key="contact_page_size")
# Rank like atlasdev.gg: every row already meets both minimums, so the
# smallest qualifying visit counts come first, with CCU as the tiebreaker.
# This keeps the first pages focused on games closest to the target.
# The watchlist keeps DB order instead: freshest blow-up signal first.
if is_watch_view:
    metric_filtered = metric_filtered.reset_index(drop=True)
else:
    metric_filtered = metric_filtered.sort_values(
        ["visits", "ccu"],
        ascending=[True, True],
        na_position="last",
    ).reset_index(drop=True)
page_count = max(1, (len(metric_filtered) + int(page_size) - 1) // int(page_size))
if st.session_state.contact_page > page_count:
    st.session_state.contact_page = page_count
current_page = max(1, min(page_count, int(st.session_state.get("contact_page", 1))))
st.session_state.contact_page = current_page
page = st.sidebar.number_input("Page", min_value=1, max_value=page_count, step=1, key="contact_page")
page_start = (int(page) - 1) * int(page_size)
page_rows = metric_filtered.iloc[page_start:page_start + int(page_size)]
page_ids = [int(uid) for uid in page_rows["universe_id"].tolist()]

# The Discord filter already ran above, before the page slice — no second
# pass here. The visible page IS the filtered page, so 20 rows render as 20.
visible = page_rows

# Failures stay visible even without the diagnostics section: a failed sync
# or a crashed scan is surfaced as a quiet error line, everything else
# (cookie state, endpoint results, queue budgets) stays internal.
if st.session_state.get("scan_error"):
    st.sidebar.error(f"Last sync error: {st.session_state.scan_error}")

# Sync toast (plan 2.3): consume the pending flag exactly once, on the rerun
# AFTER the sync ran, so the toast appears together with the fresh table.
# The page count rounds the message ('N games on this page') without a DB hit.
if st.session_state.pop("_sync_toast_pending", False):
    _toast_count = len(visible) if hasattr(visible, "__len__") else 0
    # html.escape directly (not _esc): this block runs before the display
    # helpers are defined in module order.
    _toast_text = html.escape(
        f"Catalog synced · {_toast_count} game{'s' if _toast_count != 1 else ''} on this page"
    )
    st.html(
        _SYNC_TOAST_SCRIPT.replace("%TOAST_TEXT%", _toast_text),
        unsafe_allow_javascript=True,
    )
elif scout.last_scan and scout.last_scan.get("error"):
    st.sidebar.error(f"Scan error: {scout.last_scan['error']}")

# --------------------------------------------------------------------------- #
# Display helpers and table
# --------------------------------------------------------------------------- #


def game_url(row: pd.Series) -> str:
    place = row.get("root_place_id")
    title = truncate(str(row.get("title") or "game").strip(), 26)
    # Schema allows NULL/NaN root_place_id (demo/pipeline inserts create rows
    # independently): int() on one must never take down the whole results
    # table for the session — fall back to a keyword-search link instead.
    if place is not None and pd.notna(place):
        try:
            place_int = int(place)
        except (TypeError, ValueError):
            place_int = 0
        if place_int > 0:
            return f"https://www.roblox.com/games/{place_int}/{title}"
    # Keyword fallback goes to /discover — roblox.com/search?keyword= is a
    # DEAD route that 404s for every keyword (verified live 2026-09-23; it
    # 404s even for plain names, so emoji titles were never the problem).
    # /discover/?Keyword= is the canonical search route that roblox.com's
    # own nav uses. quote() encodes every character, so emoji titles arrive
    # intact; games renamed since ingestion still resolve by name.
    return f"https://www.roblox.com/discover/?Keyword={quote(str(row.get('title') or ''))}"



TABLE_STYLE = _MOTION_CORE + """
<style>
/* Results panel — UpScale Scouting Tool brand tokens (BRAND.md): Ink background,
   Hairline borders, UpScale Orange reserved for positive signal. Motion per
   UI_MICRO_ANIMATIONS_PLAN.md: transform/opacity only, 150-320ms, one easing
   curve; the reduced-motion guard ships inside _MOTION_CORE above. */
.ss-panel {
  border: 1px solid #262A33; border-radius: 14px; overflow: hidden;
  background: #12161E; margin-top: 4px;
  /* Panel + table ride the same entrance (plan 1.1 / 2.6): the whole
     results panel fades up 8px once per remount (page change, view switch,
     sync) — Streamlit remounts the block, which re-triggers it for free. */
  animation: ss-panel-in var(--motion-slow) var(--ease-out) backwards;
}
@keyframes ss-panel-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: none; }
}
.ss-wrap { overflow-x: auto; }
.ss-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.ss-table thead th {
  text-align: center; padding: 12px 14px; white-space: nowrap;
  background: #161B24; color: #9AA4B2; font-weight: 600;
  font-size: 0.74rem; letter-spacing: 0.08em; text-transform: uppercase;
  border-bottom: 1px solid #262A33;
  transition: box-shadow var(--motion-base) linear;
}
/* Scroll-linked header (plan 3.5): the top hairline brightens while the
   table is scrolled right — a class the wrap script toggles on scroll. */
.ss-panel.ss-scrolled .ss-table thead th {
  box-shadow: inset 0 1px 0 rgba(255, 110, 1, 0.4);
}
.ss-table thead th.ss-col-game { text-align: left; padding-left: 18px; }
.ss-table tbody td {
  padding: 9px 14px; vertical-align: middle; text-align: center;
  border-bottom: 1px solid #1B2029; color: #D7DCE4;
  transition: background var(--motion-fast) linear;
}
.ss-table tbody tr:last-child td { border-bottom: none; }
/* Row entrance stagger (plan 1.1): fade-and-rise, 18ms per row capped at
   220ms (~12 rows), so deep pages still feel snappy. Rendered as generated
   nth-child rules in _ROW_STAGGER_CSS so the row markup, which tests match
   literally, stays untouched. */
.ss-table tbody tr { opacity: 0; animation: ss-row-in var(--motion-base) var(--ease-out) forwards; }
@keyframes ss-row-in { to { opacity: 1; transform: none; } }
.ss-table tbody tr { transform: translateY(6px); }
/* Row hover (plan 1.2): a quiet lift — brighter wash, no scale on rows
   (20-row tables shear). Background transitions fast and linear. */
.ss-table tbody tr:hover td { background: rgba(255, 255, 255, 0.035); }
.ss-cell-game { text-align: left !important; padding-left: 18px !important; }
.ss-num { white-space: nowrap; font-variant-numeric: tabular-nums; }
.ss-genre {
  display: inline-block; padding: 3px 11px; border-radius: 999px;
  background: #1a1d25; border: 1px solid #262a33; color: #c3c8d1;
  font-size: 0.78rem; white-space: nowrap;
  /* Genre pill wash-in (plan 1.5): Panel -> faint orange-tint border on
     hover; color noise stays out of the resting state. */
  transition: border-color var(--motion-fast) linear,
              color var(--motion-fast) linear;
}
.ss-genre:hover { border-color: rgba(255, 110, 1, 0.45); color: #E7ECF3; }
.ss-up { color: #FF6E01; white-space: nowrap; }
.ss-down { color: #EF4444; white-space: nowrap; }
.ss-game { display: inline-flex; align-items: center; gap: 9px; text-decoration: none; color: inherit; }
.ss-game:hover .ss-name { text-decoration: underline; }
.ss-thumb {
  width: 44px; height: 44px; min-width: 44px; border-radius: 10px; object-fit: cover;
  background: rgba(128, 128, 128, 0.15);
  /* Thumb zoom-on-hover (plan 1.4): signals the link before the underline. */
  transition: transform var(--motion-base) var(--ease-out),
              box-shadow var(--motion-base) var(--ease-out);
}
.ss-game:hover .ss-thumb {
  transform: scale(1.08);
  box-shadow: 0 0 0 1px #262A33;
}
.ss-fallback {
  width: 44px; height: 44px; min-width: 44px; border-radius: 10px;
  display: inline-flex; align-items: center; justify-content: center;
  background: rgba(128, 128, 128, 0.15);
  /* Skeleton shimmer (plan 1.8): a one-time gradient sweep over the gray
     placeholder so a missing thumbnail reads as 'loading', not 'broken'. */
  background-image: linear-gradient(100deg, transparent 30%, rgba(255, 255, 255, 0.06) 50%, transparent 70%);
  background-size: 220% 100%;
  animation: ss-shimmer 900ms var(--ease-out) 1 backwards;
}
@keyframes ss-shimmer {
  from { background-position: 120% 0; }
  to { background-position: -120% 0; }
}
.ss-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 260px; font-size: 0.98rem; }
.ss-discord { display: inline-flex; align-items: center; gap: 7px; text-decoration: none; }
.ss-discord img { width: 18px; height: 18px; }
.ss-discord span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 240px; }
.ss-discord:hover span { text-decoration: underline; }
.ss-copy {
  font-size: 0.8rem; padding: 5px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(128, 128, 128, 0.5); background: transparent;
  color: inherit; white-space: nowrap; font-family: inherit;
  transition: transform var(--motion-fast) var(--ease-out),
              background var(--motion-fast) linear,
              box-shadow var(--motion-fast) linear,
              border-color var(--motion-fast) linear,
              color var(--motion-fast) linear;
}
.ss-copy:hover { background: rgba(128, 128, 128, 0.15); box-shadow: 0 0 0 1px rgba(255, 110, 1, 0.35); }
.ss-copy:active { transform: scale(0.97); }
.ss-copied { color: #FF6E01; border-color: #FF6E01; animation: ss-copy-pulse 160ms var(--ease-out) 1; }
.ss-copyfail { color: #EF4444; border-color: #EF4444; }
/* Checkmark draw (plan 2.2): the SVG path in the copy script animates its
   stroke-dashoffset through this keyframe. */
@keyframes ss-check-draw { to { stroke-dashoffset: 0; } }
/* Copy success pulse (plan 1.6): one small scale beat so 'copied' registers
   peripherally; the checkmark draw itself lives in the copy script (2.2). */
@keyframes ss-copy-pulse {
  0% { transform: scale(1); }
  60% { transform: scale(1.06); }
  100% { transform: scale(1); }
}
.ss-none { opacity: 0.55; }
/* Blow-up flag pulse (plan 2.5, watchlist only): a ONE-time two-pulse orange
   left border on entrance, then settles. The single sanctioned repeated
   pulse — it IS the product signal. Applied via the ss-blowup row class. */
tr.ss-blowup td:first-child {
  box-shadow: inset 2px 0 0 #FF6E01;
  animation: ss-blowup-pulse 700ms var(--ease-out) 1;
}
@keyframes ss-blowup-pulse {
  0%, 100% { box-shadow: inset 2px 0 0 #FF6E01; }
  30% { box-shadow: inset 3px 0 6px rgba(255, 110, 1, 0.75); }
  55% { box-shadow: inset 2px 0 1px rgba(255, 110, 1, 0.45); }
  75% { box-shadow: inset 3px 0 6px rgba(255, 110, 1, 0.55); }
}
/* Pagination direction (plan 3.1): the wrap script adds ss-page-left/right
   for one directional slide so a page change keeps its compass. */
.ss-wrap.ss-page-left { animation: ss-page-left var(--motion-slow) var(--ease-out); }
.ss-wrap.ss-page-right { animation: ss-page-right var(--motion-slow) var(--ease-out); }
@keyframes ss-page-left {
  from { opacity: 0; transform: translateX(-12px); }
  to { opacity: 1; transform: none; }
}
@keyframes ss-page-right {
  from { opacity: 0; transform: translateX(12px); }
  to { opacity: 1; transform: none; }
}
/* Sync toast (plan 2.3): slides up bottom-right after a successful sync,
   auto-dismisses. The DOM node + script live in _SYNC_TOAST_SCRIPT. */
.ss-toast {
  position: fixed; right: 18px; bottom: 18px; z-index: 999999;
  background: #12161E; border: 1px solid #262A33; border-left: 2px solid #FF6E01;
  border-radius: 10px; padding: 10px 14px; color: #E7ECF3;
  font-size: 0.85rem; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  transform: translateY(10px); opacity: 0;
  transition: transform var(--motion-slow) var(--ease-out),
              opacity var(--motion-slow) var(--ease-out);
  font-family: 'Source Sans Pro', sans-serif;
}
.ss-toast.ss-toast-in { transform: none; opacity: 1; }
.ss-toast.ss-toast-out { transform: translateY(10px); opacity: 0; }
</style>
"""

# Row-stagger delays (plan 1.1): one nth-child rule per row position with an
# 18ms increment, capped at ~220ms. Generated here (not inline styles) so
# the <tr> markup — which tests match literally — stays untouched.
_ROW_STAGGER_MAX_INDEX = 12
_ROW_STAGGER_CSS = "<style>" + "".join(
    f".ss-table tbody tr:nth-child({index}) {{ animation-delay: {min((index - 1) * 18, 220)}ms; }}"
    for index in range(1, _ROW_STAGGER_MAX_INDEX + 1)
) + "</style>"""


def _esc(value) -> str:
    """HTML-escape any value for safe embedding in the results table."""
    if value is None:
        return ""
    return html.escape(str(value))


def _text(value, fallback: str = "") -> str:
    """Stringify a cell value; None and pandas NaN count as missing."""
    try:
        if value is None or pd.isna(value):
            return fallback
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text if text else fallback


def _num_cell(value) -> str:
    """Compact number formatting that tolerates missing values."""
    try:
        if value is None or pd.isna(value):
            return "—"
    except (TypeError, ValueError):
        pass
    return compact_num(value)


def game_cell_html(row: pd.Series) -> str:
    """One cell: game thumbnail + clickable game name."""
    name = _esc(truncate(_text(row.get("title"), "game"), 40)) or "game"
    url = _esc(game_url(row))
    icon = _text(row.get("icon_url"))
    if icon.startswith("http"):
        thumb = f'<img class="ss-thumb" src="{_esc(icon)}" alt="" loading="lazy">'
    else:
        thumb = '<span class="ss-fallback">🎮</span>'
    return (
        f'<a class="ss-game" href="{url}" target="_blank" rel="noopener">'
        f'{thumb}<span class="ss-name">{name}</span></a>'
    )


def discord_cell_html(url) -> str:
    """One cell: Discord logo + clickable invite, or "No Discord Server".

    A bare dash read like a cell still loading, so a real label replaces it
    (user request): games without a resolved invite say so in plain text.
    """
    url_text = _text(url)
    if not url_text:
        return '<span class="ss-none">No Discord Server</span>'
    link = _esc(url_text)
    label = _esc(truncate(url_text, 44))
    return (
        f'<a class="ss-discord" href="{link}" target="_blank" rel="noopener">'
        f'<img src="{DISCORD_LOGO_URL}" alt="Discord" loading="lazy">'
        f'<span>{label}</span></a>'
    )


# Copy handling for the Message column, injected as a <script> by
# render_table. Inline onclick attributes are stripped by DOMPurify even
# with unsafe_allow_javascript=True, so a delegated click listener is used
# instead. Anti-spam rotation: every button carries ALL outreach variants
# (data-alts); each click picks one at random, excluding the variant copied
# on the previous click (tracked in localStorage), so two consecutive
# copies are never the same starter regardless of which game they were for.
# The handler tries the async clipboard API first and falls back to a
# hidden-textarea execCommand copy, then shows "Copied" feedback.
_COPY_SCRIPT = r"""
<script>
window.__ssCopyReady = true;
document.addEventListener('click', function (event) {
  var btn = event.target && event.target.closest ? event.target.closest('button.ss-copy') : null;
  if (!btn) { return; }
  var msg, label = btn.textContent, ok = false;
  try { msg = JSON.parse(btn.dataset.msg); } catch (err) { return; }
  // --- variant rotation: pick a random starter, never the previous one ---
  var pool = [];
  try { pool = JSON.parse(btn.dataset.alts || '[]'); } catch (err) { pool = []; }
  if (pool.length) {
    var last = null;
    try { last = window.localStorage.getItem('ss_last_variant'); } catch (e) {}
    var candidates = pool.filter(function (a) { return a.v !== last; });
    if (!candidates.length) { candidates = pool; }
    var pick = candidates[Math.floor(Math.random() * candidates.length)];
    msg = pick.t;
    try { window.localStorage.setItem('ss_last_variant', pick.v); } catch (e) {}
  }
  // Freshness override: Streamlit text_inputs only commit on blur/rerun,
  // so a name or User ID typed just before clicking Copy may not be in
  // data-msg yet. Prefer the live sidebar input values when they differ.
  var sidebar = document.querySelector('[data-testid="stSidebar"], aside');
  var nameInput = sidebar && sidebar.querySelector('input[aria-label="Discord username"]');
  var idInput = sidebar && sidebar.querySelector('input[aria-label="Discord User ID (optional)"]');
  var name = nameInput && nameInput.value.trim();
  var rawId = idInput && idInput.value.trim();
  var digits = rawId ? rawId.replace(/\D/g, '') : '';
  var validId = digits.length >= 15 && digits.length <= 21;
  if (validId) {
    // A live User ID wins: swap the tag for a real mention token, refresh
    // any stale mention from an older ID, then upgrade a resolved name.
    msg = msg.replace(/\[Your Name\]/gi, '<@' + digits + '>').replace(/\[Name\]/gi, '<@' + digits + '>');
    msg = msg.replace(/<@\d+>/g, '<@' + digits + '>');
    if (name && msg.indexOf(name) !== -1) {
      msg = msg.split(name).join('<@' + digits + '>');
    }
  } else if (name) {
    msg = msg.replace(/\[Your Name\]/gi, name).replace(/\[Name\]/gi, name);
  }
  var finish = function () {
    // Copy confirmation (plan 2.2): an inline SVG checkmark whose stroke is
    // DRAWN via stroke-dashoffset (~250ms), orange, then the label fades
    // back after 1.2s. Replaces the plain text swap — the drawn tick reads
    // as motion, the text alone read as a relabel.
    var done = ok ? '✓ Copied!' : '✗ Copy failed';
    btn.textContent = done;
    if (ok) {
      var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 16 16');
      svg.setAttribute('width', '13'); svg.setAttribute('height', '13');
      svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('style', 'vertical-align:-2px;margin-right:4px;');
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M2.5 8.5 L6.5 12.5 L13.5 4');
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', '#FF6E01');
      path.setAttribute('stroke-width', '2');
      path.setAttribute('stroke-linecap', 'round');
      path.setAttribute('stroke-linejoin', 'round');
      var len = 18;  // path length (approx, rounded up)
      path.setAttribute('stroke-dasharray', String(len));
      path.setAttribute('stroke-dashoffset', String(len));
      path.style.animation = 'ss-check-draw 250ms var(--ease-out, ease-out) forwards';
      svg.appendChild(path);
      btn.insertBefore(svg, btn.firstChild);
      btn.classList.add('ss-copied');
    } else {
      btn.classList.add('ss-copyfail');
    }
    setTimeout(function () {
      btn.textContent = label;
      btn.classList.remove('ss-copied', 'ss-copyfail');
    }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(msg).then(function () { ok = true; finish(); },
      function () { fallback(); });
  } else { fallback(); }
  function fallback() {
    var ta = document.createElement('textarea');
    ta.value = msg; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    ta.remove(); finish();
  }
});
</script>
"""

# Table feedback script (plan 3.1 + 3.5): two small listeners inside the
# table's own st.html block so they remount with it on every rerun.
#
# 3.5 scroll-linked header: while the table is scrolled right, the panel
# gets .ss-scrolled and the header hairline brightens — a functional
# 'there is more to the right' signal on narrow screens.
# 3.1 directional paging: on a page change the wrap slides 12px in the
# direction of travel. The PREVIOUS page number is stashed in localStorage
# by this same script; on the next remount a diff decides left vs right
# (a full reload or view switch has no stash and skips the slide — the
# plain stagger from _ROW_STAGGER_CSS carries the entrance instead).
_PAGE_FEEDBACK_SCRIPT = r"""
<script>
(function () {
  var wrap = document.querySelector('.ss-wrap');
  if (!wrap) { return; }
  // --- scroll-linked header hairline -------------------------------------
  var panel = document.querySelector('.ss-panel');
  var syncScrolled = function () {
    if (panel) { panel.classList.toggle('ss-scrolled', wrap.scrollLeft > 4); }
  };
  wrap.addEventListener('scroll', syncScrolled, { passive: true });
  syncScrolled();
  // --- directional page slide --------------------------------------------
  try {
    var KEY = 'ss_last_page';
    var now = wrap.closest('[data-testid="stElementContainer"], .stHtml') || wrap;
    var seq = document.querySelectorAll('.ss-wrap').length;
    var prev = parseInt(window.sessionStorage.getItem(KEY) || '0', 10) || 0;
    var pageText = '';
    var caps = document.querySelectorAll('.stCaption, [data-testid="stCaptionContainer"]');
    for (var i = 0; i < caps.length; i++) {
      var m = /Page (\d+) of (\d+)/.exec(caps[i].textContent || '');
      if (m) { pageText = m[1] + '/' + m[2]; break; }
    }
    if (pageText && prev) {
      var pNow = parseInt(pageText.split('/')[0], 10);
      if (pNow > prev) { wrap.classList.add('ss-page-right'); }
      else if (pNow < prev) { wrap.classList.add('ss-page-left'); }
    }
    window.sessionStorage.setItem(KEY, pageText);
  } catch (e) { /* storage unavailable: skip the directional slide */ }
})();
</script>
"""


def _copy_pool(row: pd.Series) -> list:
    """All outreach variants for this row, as [(variant_key, message), …].

    The pool is the 5 starters plus — when the scout customized their
    template into something none of the starters say — their own text as a
    sixth voice. Entries are rendered with THIS row's game title but keep
    the [Your Name] tag: the click script fills name/ID from the live
    sidebar inputs at copy time (so a name typed seconds earlier wins over
    the saved one, exactly like the single-message path). Keys ("s0"…"s5")
    let the client script avoid repeating the same variant back to back.
    """
    templates = list(_active_message_templates())
    current = str(st.session_state.get("message_template") or "").strip()
    if current and current not in {t.strip() for t in templates}:
        templates.append(current)  # a customized template joins the rotation
    return [
        (
            f"s{index}",
            render_outreach_message(tmpl, "", _text(row.get("title"), "this game")),
        )
        for index, tmpl in enumerate(templates)
    ]


def copy_cell_html(row: pd.Series) -> str:
    """One cell: a button that copies the outreach message for this game.

    The message is rendered from the user's editable template with their
    Discord name (welcome flow) and this row's game title, JSON-encoded into
    a data attribute so quotes and newlines survive the HTML round-trip.
    data-alts carries every rotation variant; the click script picks one at
    random (never the previous one) at copy time.
    """
    message = render_outreach_message(
        st.session_state.get("message_template", DEFAULT_MESSAGE_TEMPLATE),
        st.session_state.get("discord_name", ""),
        _text(row.get("title"), "this game"),
        discord_user_id=st.session_state.get("discord_user_id", ""),
    )
    payload = html.escape(json.dumps(message), quote=True)
    alts = html.escape(
        json.dumps([{"v": key, "t": text} for key, text in _copy_pool(row)]),
        quote=True,
    )
    return (
        '<button type="button" class="ss-copy" '
        f'data-msg="{payload}" data-alts="{alts}">'
        "📋 Copy message</button>"
    )


def _ccu_cell(value) -> str:
    """CCU-style cell: rounded integer when present, em dash otherwise."""
    try:
        if value is None or pd.isna(value):
            return "—"
    except (TypeError, ValueError):
        pass
    try:
        return compact_num(int(float(value)))
    except (TypeError, ValueError):
        return _num_cell(value)


def _momentum_cell(value) -> str:
    """Momentum cell, atlasdev.gg style: arrow + signed CCU delta vs ~24h
    ago, green rising / red falling; plain '-' when there is no baseline.
    Returns HTML (safe: only fixed spans around escaped numbers)."""
    try:
        if value is None or pd.isna(value):
            return "-"
    except (TypeError, ValueError):
        pass
    try:
        delta = int(float(value))
    except (TypeError, ValueError):
        return "-"
    shown = compact_num(abs(delta)) if abs(delta) >= 1000 else abs(delta)
    if delta > 0:
        return f"<span class='ss-up'>↑ +{shown}</span>"
    if delta < 0:
        return f"<span class='ss-down'>↓ −{shown}</span>"
    return "<span class='ss-none'>0</span>"


def _earnings_cell(row: pd.Series) -> str:
    """Earnings-rank cell (HYDRATION_SOURCES.md Phase 3): a Rotrends
    revenue proxy Roblox never exposes — '#N' with a source caption, '-'
    when the sweep hasn't reached this game yet."""
    try:
        rank = row.get("earning_rank")
        if rank is None or pd.isna(rank):
            return "-"
        return f"#{int(rank)}"
    except (TypeError, ValueError):
        return "-"


def _rank_cell(row: pd.Series) -> str:
    """Creator Exchange globalRank cell (Phase 3 enrichment)."""
    try:
        rank = row.get("cx_global_rank")
        if rank is None or pd.isna(rank):
            return "-"
        return f"#{int(rank)}"
    except (TypeError, ValueError):
        return "-"


def _momentum_flag(row: pd.Series) -> str:
    """CE momentum tag (Surging/Fading/Stable) for game detail tooltips.
    Empty string when absent — never a false signal."""
    try:
        val = row.get("momentum")
        if val is None or pd.isna(val):
            return ""
        return str(val)
    except (TypeError, ValueError):
        return ""


def _is_blowup_row(row: pd.Series) -> bool:
    """True when a watchlist row carries the blow-up signal class.

    The watchlist loads flagged games via load_blowup_watch(); the demo/live
    frames never set the column, so the check must tolerate its absence.
    """
    try:
        flag = row.get("blowup_flag")
        return bool(flag) and not pd.isna(flag)
    except (TypeError, ValueError):
        return False


def _rating_cell(row: pd.Series) -> str:
    """Rating cell: like ratio as a whole percent; '-' when no votes yet."""
    try:
        up = row.get("upvotes")
        down = row.get("downvotes")
        if up is None or down is None or pd.isna(up) or pd.isna(down):
            return "-"
        total = int(up) + int(down)
        if total <= 0:
            return "-"
        return f"{round(100.0 * int(up) / total):.0f}%"
    except (TypeError, ValueError):
        return "-"


def render_table(frame: pd.DataFrame) -> None:
    """Render the visible page as an HTML table.

    A raw HTML table is used instead of ``st.dataframe`` because dataframe
    cells render markdown as plain text, so thumbnails and the Discord logo
    could never display inline. HTML keeps the merged thumbnail+name and
    logo+invite cells working in every Streamlit version.

    Column order (user request 2026-09-23): identity → volume (visits,
    CCU) → action (Discord, Message) → trend (averages, momentum, rating).
    Favorites and Peak CCU were dropped at request — lifetime favorites
    correlate with visits, and current CCU dominates peak CCU for scouting
    decisions.
    """
    head = [
        "Game", "Genre", "Total visits", "CCU", "Discord", "Message",
        "Avg CCU (1d)", "Avg CCU (3d)", "Momentum (1d)", "Rating",
        "Earnings", "Global rank",
    ]
    rows = []
    for _, row in frame.iterrows():
        # Blow-up pulse (plan 2.5): watchlist rows carry the one-shot orange
        # border pulse on entrance — main-scout rows never do.
        row_open = '<tr class="ss-blowup">' if _is_watch_view() and _is_blowup_row(row) else "<tr>"
        rows.append(
            row_open
            + f"<td class='ss-cell-game'>{game_cell_html(row)}</td>"
            f"<td><span class='ss-genre'>{_esc(_text(row.get('genre'), 'Unknown'))}</span></td>"
            f"<td class='ss-num'>{_num_cell(row.get('visits'))}</td>"
            f"<td class='ss-num'>{_ccu_cell(row.get('ccu'))}</td>"
            f"<td>{discord_cell_html(row.get('discord_url'))}</td>"
            f"<td>{copy_cell_html(row)}</td>"
            f"<td class='ss-num'>{_ccu_cell(row.get('avg_ccu_1d'))}</td>"
            f"<td class='ss-num'>{_ccu_cell(row.get('avg_ccu_3d'))}</td>"
            f"<td class='ss-num'>{_momentum_cell(row.get('momentum_1d'))}</td>"
            f"<td class='ss-num'>{_rating_cell(row)}</td>"
            f"<td class='ss-num'>{_earnings_cell(row)}</td>"
            f"<td class='ss-num'>{_rank_cell(row)}</td>"
            + "</tr>"
        )
    # st.html with unsafe_allow_javascript=True is required for the copy
    # buttons: Streamlit's DOMPurify sanitization strips inline event
    # handlers (onclick) and st.markdown never executes scripts. All cell
    # content is escaped above; the only script is the fixed copy handler
    # in _COPY_SCRIPT.
    st.html(
        TABLE_STYLE
        + _ROW_STAGGER_CSS
        + '<div class="ss-panel"><div class="ss-wrap"><table class="ss-table"><thead><tr>'
        + f"<th class='ss-col-game'>{_esc(head[0])}</th>"
        + "".join(f"<th>{_esc(label)}</th>" for label in head[1:])
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div></div>"
        + _PAGE_FEEDBACK_SCRIPT
        + _COPY_SCRIPT,
        unsafe_allow_javascript=True,
    )

st.title("New & Upcoming" if is_watch_view else "Games matching your target")

if source == "demo":
    st.warning("Live sources were unavailable, so demo data is shown. Run Sync live data to retry.")

if visible.empty:
    if is_watch_view:
        st.info(
            "No blow-up signals yet. Keep syncing — games that climb 2+ tiers "
            "or 3x their CCU land here automatically."
        )
    elif source == "live" and metric_filtered.empty:
        st.success("The live scan finished, but no games met both target thresholds.")
    else:
        discord_view = st.session_state.get("discord_filter_radio", DISCORD_FILTER_ALL)
        if discord_view == DISCORD_FILTER_TRUE and not metric_filtered.empty:
            st.info(
                "No games with a resolved Discord invite in the filtered "
                "results yet. The overnight backfill is still working through "
                "the catalog — sync live data later to pick up new verdicts."
            )
        else:
            st.info("No games on this page match the selected filters.")
else:
    render_table(visible)
    st.download_button(
        "Export visible results (CSV)",
        visible.to_csv(index=False).encode(),
        file_name=f"upscale-scouting-export_{time.strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        width="content",
        key="export_csv_button",
    )
    # CSV export success state (plan 3.2): the browser's download bar is
    # easy to miss, so the button itself confirms. Streamlit swaps widget
    # labels only on rerun, so this runs on the NEXT rerun after the click
    # (the click itself triggers one) — label morphs, then reverts in 2s.
    if st.session_state.get("export_csv_button"):
        st.html(
            """
<script>
(function () {
  var btns = document.querySelectorAll('[data-testid="stDownloadButton"] button');
  var btn = btns[btns.length - 1];
  if (!btn || btn.dataset.ssExported) { return; }
  btn.dataset.ssExported = '1';
  var label = btn.textContent;
  btn.textContent = 'Exported ✓';
  btn.classList.add('ss-copied');
  setTimeout(function () {
    btn.textContent = label;
    btn.classList.remove('ss-copied');
    delete btn.dataset.ssExported;
  }, 2000);
})();
</script>
""",
            unsafe_allow_javascript=True,
        )

# Pagination sits under the results table, where users look for it.
nav_left, nav_center, nav_right = st.columns([1, 2, 1])
with nav_left:
    st.button(
        "← Previous page",
        disabled=int(page) <= 1,
        key="main_previous_page",
        on_click=shift_contact_page,
        args=(-1, page_count),
        width="stretch",
    )
with nav_center:
    st.caption(f"Page {int(page)} of {page_count}")
with nav_right:
    st.button(
        "Next page →",
        disabled=int(page) >= page_count,
        key="main_next_page",
        on_click=shift_contact_page,
        args=(1, page_count),
        width="stretch",
    )

st.caption(
    "Targets are applied instantly — every game already carries its Discord "
    "contact state from the shared catalog."
)
