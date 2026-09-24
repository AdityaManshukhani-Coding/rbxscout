"""
RbxScout - Automated Roblox Scouting & Contact Identification Dashboard.

Run: streamlit run app.py
Deploy marker: table reorder + No-Discord label + literal search + genre-✕ fix (2026-09-23).
"""

from __future__ import annotations

import html
import json
import logging
import os
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

from scout_core import (
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_MESSAGE_TEMPLATE,
    DISCORD_LOGO_URL,
    THROTTLED_STATUS,
    RobloxPlatformScout,
    apply_filters,
    compact_num,
    normalize_discord_user_id,
    render_outreach_message,
    truncate,
)
import catalog_fetch
import gate
import profile_store

logging.basicConfig(level=logging.INFO)

APP_DIR = Path(__file__).resolve().parent


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
DESKTOP_DIR = Path.home() / "Desktop"
GUIDE_IMAGE_CANDIDATES = {
    number: [
        APP_DIR / "assets" / f"Step {number} SS.png",
        APP_DIR / f"Step {number} SS.png",
        DESKTOP_DIR / f"Step {number} SS.png",
    ]
    for number in range(1, 5)
}

st.set_page_config(
    page_title="Studio Scouts - Roblox Scouting Dashboard",
    page_icon="🕹️",
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

_PROFILE_FIELDS = (        "discord_name",
    "discord_user_id",
    "message_template",
    "message_variant",
    "target_min_visits",
    "target_min_ccu",
    "onboarding_cookie",
    "onboarding_step",
    "guide_step",
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
    return True


def _render_gate() -> None:
    """Password screen in front of the whole app; ``st.stop()``s when locked."""
    if os.environ.get("SS_TEST_BYPASS_GATE") == "1":
        return  # automated tests exercise the app itself, not the lock
    ref = _device_ref()
    if gate.is_unlocked(ref):
        return

    st.markdown("<style>section[data-testid='stSidebar']{display:none}</style>", unsafe_allow_html=True)
    st.markdown(
        """
        <style>
           .gate-card {
                max-width: 420px; margin: 9vh auto 0 auto; padding: 2.2rem 2.4rem;
                border: 1px solid rgba(250, 250, 250, 0.12); border-radius: 18px;
                background: rgba(250, 250, 250, 0.04); text-align: center;
            }
            .gate-lock { font-size: 2.6rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="gate-card"><div class="gate-lock">🔒</div>'
        '<h2 style="margin-bottom:0.2rem">Studio Scouts</h2>'
        '<p style="opacity:0.75;margin-top:0">This site is private. Enter the access password to continue.</p>',
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
    st.html(_INPUT_GUARD_SCRIPT, unsafe_allow_javascript=True)  # no save-password prompt here either
    st.stop()


def initialize_session() -> bool:
    """Seed defaults, then overlay the device profile; True when one applied."""
    restored = _restore_from_profile()  # first, so defaults only fill gaps
    defaults = {
        "onboarding_step": 0,
        "onboarding_complete": False,
        "target_min_visits": DEFAULT_MIN_VISITS,
        "target_min_ccu": DEFAULT_MIN_CCU,
        "onboarding_cookie": "",
        "discord_name": "",  # asked in the welcome flow; auto-fills the outreach message
        "discord_user_id": "",  # optional; turns [Your Name] into a real <@ID> mention
        "message_template": DEFAULT_MESSAGE_TEMPLATE,
        "message_variant": 0,  # which of the 5 starter templates is active
        "guide_step": 1,
        "pending_initial_scan": False,
    "welcome_scan_started": False,
        "active_run_id": None,
        "check_contacts_requested": False,
        "contact_page": 1,
        "contact_page_size": PAGE_SIZE,
        "contact_loaded": set(),
        "contact_signature": "",
        "scan_error": "",
        "source": "demo",
        "watch_contact_loaded": set(),
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
    st.session_state.target_min_visits = min_visits
    st.session_state.target_min_ccu = min_ccu
    st.session_state.onboard_visits = min_visits
    st.session_state.onboard_ccu = min_ccu


def _save_onboarding_targets() -> None:
    """Persist the number_input values so they survive when the widgets
    disappear from the render tree on the next rerun."""
    st.session_state.target_min_visits = st.session_state["onboard_visits"]
    st.session_state.target_min_ccu = st.session_state["onboard_ccu"]
    _profile_save()


def _save_profile_identity() -> None:
    """Snapshot identity fields (name / ID / template / cookie) on change."""
    _profile_save()


def _message_variant_options() -> list:
    """Labels for the 5 starter templates ('Starter 1 — original default' …)."""
    return [
        ("Starter 1 — original" if i == 0 else f"Starter {i + 1} — variation")
        for i in range(len(DEFAULT_MESSAGE_TEMPLATES))
    ]


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
    st.session_state.message_template = DEFAULT_MESSAGE_TEMPLATES[index]
    _profile_save()


def guide_image(step: int):
    for path in GUIDE_IMAGE_CANDIDATES.get(step, []):
        if path.exists():
            return path
    return None


def render_onboarding() -> bool:
    """Render the first-run flow; return True when it has completed."""
    step = int(st.session_state.onboarding_step)
    st.markdown("<style>section[data-testid='stSidebar']{display:none}</style>", unsafe_allow_html=True)

    if step == 0:
        st.title("Welcome Fellow Scout")
        st.subheader("to the Studio Scouts Website")
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
            min_value=0,
            step=1_000,
            key="onboard_visits",
            on_change=_save_onboarding_targets,
            persist_state="session",
            help="Games below this lifetime visit count are excluded from the first scan.",
        )
        right.number_input(
            "Minimum CCU",
            min_value=0,
            step=25,
            key="onboard_ccu",
            on_change=_save_onboarding_targets,
            persist_state="session",
            help="Games below this current player count are excluded from the first scan.",
        )
        if int(st.session_state.target_min_visits) == 0 and int(st.session_state.target_min_ccu) == 0:
            st.warning("Set at least one target before continuing.")
        if st.button("Next", type="primary", width="stretch", key="onb1_next"):
            if int(st.session_state.target_min_visits) or int(st.session_state.target_min_ccu):
                st.session_state.onboarding_step = 2
                st.session_state.guide_step = 1
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
            st.session_state.onboarding_step = 2
            st.session_state.guide_step = 4
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
            st.session_state.target_min_visits = int(
                st.session_state.get("onboard_visits", st.session_state.target_min_visits)
            )
            st.session_state.target_min_ccu = int(
                st.session_state.get("onboard_ccu", st.session_state.target_min_ccu)
            )
            st.session_state.onboarding_complete = True
            _profile_save()  # completion + targets survive refreshes now
            # Run the real first scan on the post-onboarding rerun with the
            # captured targets. The dashboard paints as soon as it finishes;
            # speed hardening in scout_core keeps that well under a minute.
            st.session_state.pending_initial_scan = True
            st.session_state.contact_page = 1
            st.session_state.contact_loaded = set()
            st.session_state.contact_signature = ""
            st.rerun()
        return False

    st.title("Connect your Roblox session")
    st.caption("Follow the steps below to copy the cookie used for Roblox social-link checks.")
    st.warning(
        "A .ROBLOSECURITY cookie is a live account credential. Never share it in chat, screenshots, "
        "or source files. Use a test account and revoke it immediately if it is exposed."
    )

    guide_steps = [
        (1, "Open Roblox in Chrome", "Log in to your Roblox account, open the Roblox home page, right-click the page, and choose Inspect."),
        (2, "Open Application", "In DevTools, select the Application tab."),
        (3, "Expand Cookies", "In the left panel, expand Cookies, then select the Roblox website entry."),
        (4, "Copy .ROBLOSECURITY", "Select .ROBLOSECURITY in the table and copy the complete value from the lower panel. Do not copy any other cookie."),
    ]
    guide_step = max(1, min(4, int(st.session_state.guide_step)))
    st.progress(guide_step / 4, text=f"Cookie guide: step {guide_step} of 4")
    number, title, instructions = guide_steps[guide_step - 1]
    st.subheader(f"Step {number}: {title}")
    st.write(instructions)
    image = guide_image(number)
    if image:
        st.image(image, use_container_width=True)
    else:
        st.caption("The step image is not available in this checkout; the written instructions still apply.")

    if guide_step == 4:
        st.text_input(
            ".ROBLOSECURITY cookie",
            type="password",
            key="onboarding_cookie",
            help="Stored in this Streamlit session only and never written to SQLite.",
            persist_state="session",
            on_change=_save_profile_identity,
        )
        st.caption("Cookie access can vary with Roblox account age, verification, privacy settings, region, and endpoint policy.")

    back, next_column = st.columns(2)
    if back.button("Back", width="stretch"):
        if guide_step == 1:
            st.session_state.onboarding_step = 1
            st.session_state.guide_step = 1
        else:
            st.session_state.guide_step = guide_step - 1
        st.rerun()
    if next_column.button("Next", type="primary", width="stretch", key="onb2_next"):
        if guide_step < 4:
            st.session_state.guide_step = guide_step + 1
        else:
            st.session_state.onboarding_step = 3
        _profile_save()  # cookie-guide progress + cookie survive refreshes
        st.rerun()
    if guide_step == 4 and not st.session_state.onboarding_cookie:
        st.caption(
            "You can continue without a cookie, but Roblox hides social links from "
            "signed-out requests — Discord invites can only be resolved with one."
        )
    return False


_restored = initialize_session()
_render_gate()  # private site: nothing below renders without the password
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
            roblox_cookie=st.session_state.get("onboarding_cookie") or None,
            # Per-session worker pool: every session gets its own, so keep it
            # small — the process-wide ROBLOX_CALL_GATE (scout_core) is what
            # actually bounds total outbound Roblox concurrency across all
            # users; 8 workers per session just multiplied queue pressure.
            max_workers=2,
        )
    return st.session_state.scout


def run_contact_scan(
    scout: RobloxPlatformScout,
    page_ids: list[int],
    force: bool,
    run_id: int | None = None,
) -> pd.DataFrame:
    progress = st.progress(0.0, text="Checking Discord contacts...")
    status = st.empty()

    def callback(percent: float, message: str) -> None:
        progress.progress(min(1.0, percent), text=message)
        status.caption(message)

    try:
        refreshed = scout.scan_contacts(
            page_ids,
            force=force,
            run_id=run_id,
            progress_cb=callback,
        )
    finally:
        progress.empty()
        status.empty()
    # Roblox was throttling during this check: pages of lookups were skipped
    # (NOT stored as "No Contact Found"), so say so instead of letting users
    # read a transient verdict as ground truth.
    if scout.throttle_window_active() and not refreshed.empty:
        if "status" in refreshed.columns and (refreshed["status"] == THROTTLED_STATUS).any():
            st.warning(
                "⚠️ Roblox briefly rate-limited us — some games on this page "
                "could not be checked and show “Throttled”. Re-run the check "
                "on this page in a few minutes."
            )
    # Protect the verdicts from catalog asset swaps: the hosted cache copy of
    # the catalog is fully replaced on every pipeline sync, which would
    # otherwise erase exactly the contact state the user just resolved. The
    # overlay store is replayed onto every fresh download.
    try:
        if not refreshed.empty and "universe_id" in refreshed.columns:
            records = {}
            for rec in refreshed.to_dict("records"):
                try:
                    records[int(rec["universe_id"])] = rec
                except (KeyError, TypeError, ValueError):
                    continue
            catalog_fetch.record_contacts(records)
    except Exception:
        pass  # overlay is best effort; the authoritative write already happened
    return refreshed


def update_contact_rows(base: pd.DataFrame, refreshed: pd.DataFrame) -> pd.DataFrame:
    if refreshed.empty or "universe_id" not in refreshed.columns:
        return base
    merged = base.set_index("universe_id").copy()
    replacement = refreshed.set_index("universe_id")
    for column in ("has_discord", "discord_url", "status", "found_via", "has_social_links", "contacts_checked_at"):
        if column in replacement.columns:
            merged.loc[replacement.index, column] = replacement[column]
    return merged.reset_index()


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
    st.session_state.contact_loaded = set()
    st.session_state.contact_signature = ""
    # A stale run id from a previous page/targets would attribute the next
    # check's diagnostics to the wrong run — start clean each time.
    st.session_state.active_run_id = None


# --------------------------------------------------------------------------- #
# Main sidebar controls
# --------------------------------------------------------------------------- #

scout = get_scout()

# The cookie widget can render before the scout exists (welcome flow) and its
# value lives in session state; push the latest value into the live Roblox
# session on every run so contact lookups never run unauthenticated by accident.
_cookie_value = str(st.session_state.get("onboarding_cookie") or "").strip()
if _cookie_value and not scout.has_cookie:
    scout.set_cookie(_cookie_value)
elif not _cookie_value and scout.has_cookie:
    scout.set_cookie(None)

st.sidebar.title("🕹️ Studio Scouts")
st.sidebar.caption("Roblox game scouting and Discord contact finder")

# Workspace switch: the New and Upcoming view reuses the exact same paging,
# Discord-check and table pipeline as the main view — only the data source
# (blow-up watchlist) and the absence of filters differ.
view = st.sidebar.radio(
    "Workspace",
    options=["🎮 Main scout", "🚀 New and Upcoming"],
    key="workspace_view",
)
is_watch_view = str(view).startswith("🚀")
# Switching workspaces lands you on page 1 of the new view; contact state
# stays per-view so neither side loses its checked pages.
if st.session_state.get("last_workspace_view") != view:
    st.session_state.last_workspace_view = view
    st.session_state.contact_page = 1

# Stop Chrome's "save your password?" bubble on the cookie / Discord fields.
# Page-level flag inside the script makes repeated mounts harmless.
st.html(_INPUT_GUARD_SCRIPT, unsafe_allow_javascript=True)

with st.sidebar.expander("🎯 Current target", expanded=True):
    min_visits = st.number_input(
        "Minimum visits",
        min_value=0,
        step=1_000,
        value=st.session_state.get("target_min_visits", DEFAULT_MIN_VISITS),
        key="target_min_visits",
        persist_state="session",
    )
    min_ccu = st.number_input(
        "Minimum CCU",
        min_value=0,
        step=25,
        value=st.session_state.get("target_min_ccu", DEFAULT_MIN_CCU),
        key="target_min_ccu",
        persist_state="session",
    )
    st.caption("Targets filter your results the moment you sync. New games appear as the 24/7 pipeline discovers them.")

with st.sidebar.expander("⚙️ Scan settings", expanded=False):
    deep = st.toggle("Check Discord contacts", value=True, key="deep_contacts")
    force = st.button(
        "↻ Force re-check current page",
        key="force_contacts_now",
        width="stretch",
        help="Run the contact lookup again for the page currently displayed. This is a one-time action.",
    )
    cookie = st.text_input(
        ".ROBLOSECURITY cookie",
        type="password",
        key="onboarding_cookie",
        persist_state="session",
        on_change=_save_profile_identity,
        help="Remembered on this device so refreshes keep you signed in. Never written to SQLite.",
    )
    apply_cookie = st.button("💾 Apply cookie", width="stretch", disabled=not cookie)
    if apply_cookie:
        scout.set_cookie(cookie)
        _profile_save()
        st.toast("Cookie applied — remembered on this device.")
    with st.sidebar.expander("This device", expanded=False):
        st.caption(
            "Your details (name, User ID, targets, template, cookie, progress) are "
            "remembered on this device, so refreshes and back-navigation keep you "
            "where you were."
        )
        if st.button("🧠 Forget this device", width="stretch", key="forget_device"):
            ref = _device_ref()
            if ref:
                profile_store.clear_profile(ref)
                gate.forget_unlock(ref)
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

with st.sidebar.expander("✉️ Outreach message", expanded=False):
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
        "Each 📋 Copy click picks a random starter (never the same one twice "
        "in a row) and auto-fills [Your Name] / [Game Name]. Edit the text "
        "above to customize what gets rotated."
    )

sync = st.sidebar.button("🔄 Sync live data", type="primary", width="stretch", key="sync_live_data")
check_contacts = st.sidebar.button(
    "🔎 Check Discord servers",
    width="stretch",
    disabled=not deep,
)
if check_contacts:
    st.session_state.check_contacts_requested = True

if sync or st.session_state.pending_initial_scan:
    _profile_save()  # targets/identity snapshot rides along with the scan
    st.session_state.pending_initial_scan = False
    st.session_state.welcome_scan_started = True
    st.session_state.scan_error = ""
    with st.spinner("Loading games that meet your targets..."):
        try:
            # Read-only catalog query — no discovery, no Roblox requests.
            # The 24/7 pipeline (Cloudflare cron → hydrator and expander
            # workflows) owns discovery and hydration; this
            # button only pulls what it already stored. Results page one is
            # ready instantly; contacts still load page by page below.
            data = _read_catalog(
                min_visits=int(min_visits),
                min_ccu=int(min_ccu),
            )
            # Keep only what the dashboard renders in session state (see
            # SESSION_KEEP_COLUMNS) — never the demo/DB fallback frame.
            st.session_state.data = slim_result_frame(data) if not data.empty else empty_dataframe()
            # Keep the exact onboarding targets attached to the result set so
            # the dashboard cannot accidentally present a previous cached scan.
            st.session_state.result_target_min_visits = int(min_visits)
            st.session_state.result_target_min_ccu = int(min_ccu)
            st.session_state.source = "live"
            st.session_state.active_run_id = None
            reset_contact_page()
        except Exception as exc:
            st.session_state.scan_error = str(exc)
            # On failure show a clear schema-stable empty frame; do NOT drag
            # the whole tracked catalog (or demo data) into session state.
            st.session_state.data = empty_dataframe()
            st.session_state.source = "live"
            st.session_state.active_run_id = None
            reset_contact_page()
    # Defer the page-1 contact check to the NEXT rerun: running it inline
    # kept the pre-dashboard frame (the welcome flow) visible behind the
    # spinner for the whole slow lookup. Skipping this run lets the
    # dashboard paint its rows first; the check then runs over it.
    st.session_state.defer_contact_check = True

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
    st.sidebar.header("🚀 New and Upcoming")
    st.sidebar.caption(
        "No filters here by design — this is the raw blow-up watchlist. "
        "Filters live on the Main scout view."
    )
    search = ""
    selected_genres = []
else:
    st.sidebar.header("🎯 Scout filters")
    search = st.sidebar.text_input("🔎 Search game or creator", placeholder="e.g. blox, tycoon...")

    # Genre is a metric filter, so it is applied before contact requests.
    genres = sorted(
        str(g) for g in df["genre"].dropna().unique() if g and str(g) != "Unknown"
    )
    # The genre multiselect carries an explicit key so its selections can be
    # sanitized BEFORE the widget renders. Streamlit raises
    # StreamlitAPIException the instant a multiselect's session value names
    # an option missing from the current options list — which happens when
    # the picked genre no longer exists in the freshly loaded data (sync
    # with different targets, demo/live data swap). That exception aborts
    # the whole script and the page goes grey and unclickable. Dropping
    # stale labels first makes the ✕ click (and any stale selection) fall
    # back to the remaining valid genres with the visits/CCU targets
    # untouched.
    GENRE_KEY = "genre_multiselect"
    picked_genres = st.session_state.get(GENRE_KEY) or []
    if picked_genres and not set(picked_genres).issubset(set(genres)):
        st.session_state[GENRE_KEY] = [g for g in picked_genres if g in set(genres)]
    selected_genres = st.sidebar.multiselect("Genre", options=genres, key=GENRE_KEY)

# Apply only metric-known filters when deciding which contact page to fetch.
metric_filtered = apply_filters(
    df,
    search=search,
    min_visits=eff_min_visits,
    min_ccu=eff_min_ccu,
    genres=selected_genres,
)

signature = "|".join([
    search,
    str(min_visits),
    str(min_ccu),
    ",".join(selected_genres),
])
if signature != st.session_state.contact_signature:
    st.session_state.contact_signature = signature
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
requested_contact_check = bool(st.session_state.pop("check_contacts_requested", False))

if deep and page_ids:
    # The watchlist tracks its own checked-page set so main-view contact
    # state never suppresses (or leaks into) watch-view lookups.
    loaded_key = "watch_contact_loaded" if is_watch_view else "contact_loaded"
    if st.session_state.pop("defer_contact_check", False) and not (force or requested_contact_check):
        # Set by the sync above: let this run paint the results table now;
        # the page-1 contact check runs on the next rerun instead.
        needs_contact_check = False
        if page_ids:
            st.session_state.contact_check_scheduled = True
    else:
        needs_contact_check = (
            requested_contact_check
            or force
            or not set(page_ids).issubset(st.session_state[loaded_key])
        )
    if needs_contact_check:
        with st.spinner(f"Checking Discord contacts for page {page}..."):
            try:
                refreshed = run_contact_scan(
                    scout,
                    page_ids,
                    force=force,
                    run_id=st.session_state.get("active_run_id"),
                )
                st.session_state.active_run_id = scout.last_scan.get("run_id")
                st.session_state[loaded_key].update(page_ids)
                if is_watch_view:
                    # The watchlist re-reads from the DB: contact results are
                    # persisted there, and watch rows must not leak into the
                    # main result set cached in session state.
                    df = update_contact_rows(scout.load_blowup_watch(), refreshed)
                    metric_filtered = df.reset_index(drop=True)
                else:
                    st.session_state.data = update_contact_rows(st.session_state.data, refreshed)
                    df = st.session_state.data
                    metric_filtered = apply_filters(
                        df,
                        search=search,
                        min_visits=eff_min_visits,
                        min_ccu=eff_min_ccu,
                        genres=selected_genres,
                    ).sort_values(
                        ["visits", "ccu"],
                        ascending=[True, True],
                        na_position="last",
                    ).reset_index(drop=True)
                page_rows = metric_filtered.iloc[page_start:page_start + int(page_size)]
            except Exception as exc:
                scout.mark_scan_failed(exc)
                st.session_state.scan_error = str(exc)
                st.sidebar.error(f"Contact page failed: {exc}")

# Apply contact filters only after the current page has had a chance to resolve.
visible = apply_filters(
    page_rows,
    search="",
    min_visits=0,
    min_ccu=0,
    genres=selected_genres,
)

# Failures stay visible even without the diagnostics section: a failed sync
# or a crashed scan is surfaced as a quiet error line, everything else
# (cookie state, endpoint results, queue budgets) stays internal.
if st.session_state.get("scan_error"):
    st.sidebar.error(f"Last sync error: {st.session_state.scan_error}")
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



TABLE_STYLE = """
<style>
/* Results panel: atlasdev.gg-style card — near-black body, hairline border,
   raised header strip, generous row padding. */
.ss-panel {
  border: 1px solid #23262e; border-radius: 14px; overflow: hidden;
  background: #101218; margin-top: 4px;
}
.ss-wrap { overflow-x: auto; }
.ss-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.ss-table thead th {
  text-align: center; padding: 12px 14px; white-space: nowrap;
  background: #16181f; color: #b6bcc7; font-weight: 600;
  font-size: 0.76rem; letter-spacing: 0.05em; text-transform: uppercase;
  border-bottom: 1px solid #23262e;
}
.ss-table thead th.ss-col-game { text-align: left; padding-left: 18px; }
.ss-table tbody td {
  padding: 9px 14px; vertical-align: middle; text-align: center;
  border-bottom: 1px solid #1b1e25; color: #d7dbe2;
}
.ss-table tbody tr:last-child td { border-bottom: none; }
.ss-table tbody tr:hover td { background: rgba(255, 255, 255, 0.028); }
.ss-cell-game { text-align: left !important; padding-left: 18px !important; }
.ss-num { white-space: nowrap; font-variant-numeric: tabular-nums; }
.ss-genre {
  display: inline-block; padding: 3px 11px; border-radius: 999px;
  background: #1a1d25; border: 1px solid #262a33; color: #c3c8d1;
  font-size: 0.78rem; white-space: nowrap;
}
.ss-up { color: #22c55e; white-space: nowrap; }
.ss-down { color: #ef4444; white-space: nowrap; }
.ss-game { display: inline-flex; align-items: center; gap: 9px; text-decoration: none; color: inherit; }
.ss-game:hover .ss-name { text-decoration: underline; }
.ss-thumb { width: 44px; height: 44px; min-width: 44px; border-radius: 10px; object-fit: cover; background: rgba(128, 128, 128, 0.15); }
.ss-fallback {
  width: 44px; height: 44px; min-width: 44px; border-radius: 10px;
  display: inline-flex; align-items: center; justify-content: center;
  background: rgba(128, 128, 128, 0.15);
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
}
.ss-copy:hover { background: rgba(128, 128, 128, 0.15); }
.ss-copied { color: #22c55e; border-color: #22c55e; }
.ss-copyfail { color: #ef4444; border-color: #ef4444; }
.ss-none { opacity: 0.55; }
</style>
"""


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
    btn.textContent = ok ? '✓ Copied!' : '✗ Copy failed';
    btn.classList.add(ok ? 'ss-copied' : 'ss-copyfail');
    setTimeout(function () {
      btn.textContent = label;
      btn.classList.remove('ss-copied', 'ss-copyfail');
    }, 1600);
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
    templates = list(DEFAULT_MESSAGE_TEMPLATES)
    current = str(st.session_state.get("message_template") or "").strip()
    if current and current not in {t.strip() for t in DEFAULT_MESSAGE_TEMPLATES}:
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
    ]
    rows = []
    for _, row in frame.iterrows():
        rows.append(
            "<tr>"
            f"<td class='ss-cell-game'>{game_cell_html(row)}</td>"
            f"<td><span class='ss-genre'>{_esc(_text(row.get('genre'), 'Unknown'))}</span></td>"
            f"<td class='ss-num'>{_num_cell(row.get('visits'))}</td>"
            f"<td class='ss-num'>{_ccu_cell(row.get('ccu'))}</td>"
            f"<td>{discord_cell_html(row.get('discord_url'))}</td>"
            f"<td>{copy_cell_html(row)}</td>"
            f"<td class='ss-num'>{_ccu_cell(row.get('avg_ccu_1d'))}</td>"
            f"<td class='ss-num'>{_ccu_cell(row.get('avg_ccu_3d'))}</td>"
            f"<td class='ss-num'>{_momentum_cell(row.get('momentum_1d'))}</td>"
            f"<td class='ss-num'>{_rating_cell(row)}</td>"
            "</tr>"
        )
    # st.html with unsafe_allow_javascript=True is required for the copy
    # buttons: Streamlit's DOMPurify sanitization strips inline event
    # handlers (onclick) and st.markdown never executes scripts. All cell
    # content is escaped above; the only script is the fixed copy handler
    # in _COPY_SCRIPT.
    st.html(
        TABLE_STYLE
        + '<div class="ss-panel"><div class="ss-wrap"><table class="ss-table"><thead><tr>'
        + f"<th class='ss-col-game'>{_esc(head[0])}</th>"
        + "".join(f"<th>{_esc(label)}</th>" for label in head[1:])
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div></div>"
        + _COPY_SCRIPT,
        unsafe_allow_javascript=True,
    )

st.title("🚀 New and Upcoming" if is_watch_view else "Games matching your target")

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
        st.info("No games on this page match the selected filters.")
else:
    render_table(visible)
    st.download_button(
        "⬇️ Export visible results (CSV)",
        visible.to_csv(index=False).encode(),
        file_name=f"studioscout_export_{time.strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        width="content",
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
    "Targets are applied before contact lookup. Page navigation checks only the selected page, "
    "so the first useful results arrive without waiting for the entire catalog."
)

# The deferred page-1 contact check (scheduled by the sync above) reruns the
# script now: the dashboard is already painted, so the slow lookup shows its
# spinner over real results instead of the stale welcome-flow frame.
if st.session_state.pop("contact_check_scheduled", False):
    st.rerun()
