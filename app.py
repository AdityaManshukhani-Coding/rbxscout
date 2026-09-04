"""
RbxScout - Automated Roblox Scouting & Contact Identification Dashboard.

Run: streamlit run app.py
"""

from __future__ import annotations

import html
import logging
import time
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

from scout_core import (
    DEFAULT_CANDIDATE_LIMIT,
    DISCORD_FILTER_ALL,
    DISCORD_FILTER_FALSE,
    DISCORD_FILTER_TRUE,
    DISCORD_LOGO_URL,
    RobloxPlatformScout,
    apply_filters,
    compact_num,
    truncate,
)

logging.basicConfig(level=logging.INFO)

APP_DIR = Path(__file__).resolve().parent
DB_PATH = str(APP_DIR / "rbx_scout.db")
PAGE_SIZE = 20
DEFAULT_MIN_VISITS = 20_000
DEFAULT_MIN_CCU = 25
EMPTY_DATA_COLUMNS = [
    "universe_id", "root_place_id", "title", "ccu", "peak_ccu", "visits", "favorites",
    "genre", "creator_name", "creator_type", "creator_id", "description", "icon_url",
    "has_discord", "discord_url", "status", "found_via", "has_social_links",
    "avg_ccu_1d", "momentum_1d", "contacts_checked_at",
]
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
            "momentum_1d": int(game["ccu"] * 0.07),
            "contacts_checked_at": None,
        }
        for game in DEMO_GAMES
    ])


# --------------------------------------------------------------------------- #
# Onboarding
# --------------------------------------------------------------------------- #


def initialize_session() -> None:
    defaults = {
        "onboarding_step": 0,
        "onboarding_complete": False,
        "target_min_visits": DEFAULT_MIN_VISITS,
        "target_min_ccu": DEFAULT_MIN_CCU,
        "onboarding_cookie": "",
        "discord_name": "",  # reserved for a future workflow; intentionally hidden
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
        if st.button("Next", type="primary", width="stretch"):
            st.session_state.onboarding_step = 1
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
        if st.button("Next", type="primary", width="stretch"):
            if int(st.session_state.target_min_visits) or int(st.session_state.target_min_ccu):
                st.session_state.onboarding_step = 2
                st.session_state.guide_step = 1
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
    next_label = "Start my first scan" if guide_step == 4 else "Next"
    if next_column.button(next_label, type="primary", width="stretch"):
        if guide_step < 4:
            st.session_state.guide_step = guide_step + 1
        else:
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
            # Run the real first scan on the post-onboarding rerun with the
            # captured targets. The dashboard paints as soon as it finishes;
            # speed hardening in scout_core keeps that well under a minute.
            st.session_state.pending_initial_scan = True
            st.session_state.contact_page = 1
            st.session_state.contact_loaded = set()
            st.session_state.contact_signature = ""
        st.rerun()
    if guide_step == 4 and not st.session_state.onboarding_cookie:
        st.caption("You can continue without a cookie; public descriptions will still be checked.")
    return False


initialize_session()
if not st.session_state.onboarding_complete:
    render_onboarding()
    st.stop()


# --------------------------------------------------------------------------- #
# Session and scan helpers
# --------------------------------------------------------------------------- #


def get_scout() -> RobloxPlatformScout:
    if "scout" not in st.session_state:
        st.session_state.scout = RobloxPlatformScout(
            db_path=DB_PATH,
            roblox_cookie=st.session_state.get("onboarding_cookie") or None,
        )
    return st.session_state.scout


def run_metric_scan(
    scout: RobloxPlatformScout,
    min_visits: int,
    min_ccu: int,
    deep: bool,
    force: bool,
) -> pd.DataFrame:
    progress = st.progress(0.0, text="Starting targeted scan...")
    status = st.empty()

    def callback(percent: float, message: str) -> None:
        progress.progress(min(1.0, percent), text=message)
        status.caption(message)

    try:
        # The page renderer performs the first contact lookup after metrics
        # are ready. Keeping this pass metric-only prevents duplicate requests.
        return scout.scan(
            limit=None,
            deep_contacts=False,
            force_contacts=force,
            min_visits=min_visits,
            min_ccu=min_ccu,
            candidate_limit=DEFAULT_CANDIDATE_LIMIT,
            initial_contact_limit=PAGE_SIZE,
            progress_cb=callback,
        )
    finally:
        progress.empty()
        status.empty()


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
        return scout.scan_contacts(
            page_ids,
            force=force,
            run_id=run_id,
            progress_cb=callback,
        )
    finally:
        progress.empty()
        status.empty()


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
    existing = scout.load_table()
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


# --------------------------------------------------------------------------- #
# Main sidebar controls
# --------------------------------------------------------------------------- #

scout = get_scout()

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
    st.caption("Targets apply to the next live sync. Existing results are not expanded until you sync.")

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
        help="Session-only credential. It is scoped to Roblox requests and never saved in SQLite.",
    )
    apply_cookie = st.button("💾 Apply cookie", width="stretch", disabled=not cookie)
    if apply_cookie:
        scout.set_cookie(cookie)
        st.toast("Cookie applied to the active Roblox session.")

sync = st.sidebar.button("🔄 Sync live data", type="primary", width="stretch")
check_contacts = st.sidebar.button(
    "🔎 Check Discord servers",
    width="stretch",
    disabled=not deep,
)
if check_contacts:
    st.session_state.check_contacts_requested = True

if sync or st.session_state.pending_initial_scan:
    st.session_state.pending_initial_scan = False
    st.session_state.welcome_scan_started = True
    st.session_state.scan_error = ""
    with st.spinner("Scanning games that meet your targets..."):
        try:
            data = run_metric_scan(
                scout,
                min_visits=int(min_visits),
                min_ccu=int(min_ccu),
                deep=deep,
                force=force,
            )
            st.session_state.data = data if not data.empty else empty_dataframe()
            # Keep the exact onboarding targets attached to the result set so
            # the dashboard cannot accidentally present a previous cached scan.
            st.session_state.result_target_min_visits = int(min_visits)
            st.session_state.result_target_min_ccu = int(min_ccu)
            st.session_state.source = "live"
            st.session_state.active_run_id = scout.last_scan.get("run_id")
            reset_contact_page()
        except Exception as exc:
            st.session_state.scan_error = str(exc)
            existing, source = load_existing_or_demo(scout)
            st.session_state.data = existing
            st.session_state.source = source
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

df = st.session_state.data.copy()
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
    discord_filter = DISCORD_FILTER_ALL
    selected_genres = []
else:
    st.sidebar.header("🎯 Scout filters")
    search = st.sidebar.text_input("🔎 Search game or creator", placeholder="e.g. blox, tycoon...")

    discord_filter = st.sidebar.radio(
        "Discord server",
        options=[DISCORD_FILTER_ALL, DISCORD_FILTER_TRUE, DISCORD_FILTER_FALSE],
        index=0,
    )
    # Genre is a metric filter, so it is applied before contact requests.
    genres = sorted(g for g in df["genre"].dropna().unique() if g and g != "Unknown")
    selected_genres = st.sidebar.multiselect("Genre", options=genres)

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
                    df = st.session_state.data.copy()
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
    discord_filter=discord_filter,
    genres=selected_genres,
)

# Diagnostics are rendered after the page request completes so the current
# Streamlit run displays the newly collected endpoint results immediately.
st.sidebar.divider()
st.sidebar.header("🔍 Scan diagnostics")
st.sidebar.caption(f"Cookie configured: {'yes' if scout.has_cookie else 'no'}")
st.sidebar.caption(f"Database: `{Path(scout.db_path)}`")
if scout.last_scan:
    scan_status = scout.last_scan.get("status", "unknown")
    scan_time = scout.last_scan.get("finished_at") or scout.last_scan.get("started_at") or "-"
    st.sidebar.caption(f"Last scan: {scan_status} · {scan_time}")
    st.sidebar.caption(
        f"Candidates {scout.last_scan.get('candidate_count', 0)} · "
        f"Matches {scout.last_scan.get('matched_count', scout.last_scan.get('metrics_count', 0))} · "
        f"Contacts {scout.last_scan.get('contacts_completed', 0)}/"
        f"{scout.last_scan.get('contacts_attempted', 0)} · "
        f"Errors {scout.last_scan.get('contact_errors', 0)}"
    )
    if scout.last_scan.get("error"):
        st.sidebar.error(f"Scan error: {scout.last_scan['error']}")
    tier_schedule = scout.last_scan.get("tier_schedule") or {}
    if tier_schedule:
        st.sidebar.caption(
            f"Refresh queue: T1–2 {tier_schedule.get('t1_t2', 0):,} · "
            f"T3 {tier_schedule.get('t3', 0):,} · T4 {tier_schedule.get('t4', 0):,} · "
            f"weekly {tier_schedule.get('weekly', 0):,} · T8 {tier_schedule.get('t8', 0):,}"
        )
    hydration = scout.last_scan.get("hydration_budget") or {}
    if hydration:
        st.sidebar.caption(
            f"Hydration: {hydration.get('hydrated', 0):,} hydrated · "
            f"{hydration.get('deferred', 0):,} rolled to next sync · "
            f"sync #{scout.last_scan.get('sync_number', 0)}"
        )
    if scout.last_scan.get("blowup_watch_count"):
        st.sidebar.success(
            f"🚀 {scout.last_scan['blowup_watch_count']} game(s) flagged for the "
            "blow-up watch — see New and Upcoming."
        )
if scout.source_diagnostics:
    place_details = scout.source_diagnostics.get("place_details", {})
    if place_details:
        st.sidebar.caption(
            f"Place batches {place_details.get('successful_batches', 0)}/"
            f"{place_details.get('batches', 0)} · "
            f"resolved {place_details.get('resolved', 0):,}"
        )
if st.session_state.get("scan_error"):
    st.sidebar.error(f"Last sync error: {st.session_state.scan_error}")

if scout.last_contact_diagnostics:
    st.sidebar.caption("Latest endpoint results")
    labels = {
        "game_social_links": "Game social links",
        "group_profile": "Community profile",
        "group_social_links": "Community social links",
        "owner_profile": "Owner profile",
        "owner_social_links": "Owner social links",
    }
    for uid, diagnostic in list(scout.last_contact_diagnostics.items())[-5:]:
        with st.sidebar.expander(f"Game {uid}", expanded=False):
            for key, value in diagnostic.items():
                if key in ("selected_source", "cached", "checked_at"):
                    continue
                label = labels.get(key, key.replace("_", " ").title())
                meaning = (
                    "OK" if value == 200
                    else "Sign-in required (check the cookie)" if value in (401, 403)
                    else "No links configured" if value == 404
                    else "Request failed"
                )
                st.write(f"**{label}:** {value} - {meaning}")
            st.write(f"**Selected source:** {diagnostic.get('selected_source') or 'None'}")
            if diagnostic.get("cached"):
                st.caption("This result came from the contact cache.")
else:
    st.sidebar.info("No contact checks yet. Page one will be checked after the metric scan.")

# --------------------------------------------------------------------------- #
# Display helpers and table
# --------------------------------------------------------------------------- #


def game_url(row: pd.Series) -> str:
    place = row.get("root_place_id")
    title = truncate(str(row.get("title") or "game").strip(), 26)
    if place and int(place) > 0:
        return f"https://www.roblox.com/games/{int(place)}/{title}"
    return f"https://www.roblox.com/search?keyword={quote(str(row.get('title') or ''))}"



TABLE_STYLE = """
<style>
.ss-wrap { overflow-x: auto; }
.ss-table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
.ss-table thead th {
  text-align: left; padding: 9px 12px; white-space: nowrap;
  border-bottom: 1px solid rgba(128, 128, 128, 0.45); font-weight: 700;
}
.ss-table tbody td {
  padding: 7px 12px; vertical-align: middle;
  border-bottom: 1px solid rgba(128, 128, 128, 0.18);
}
.ss-table tbody tr:hover td { background: rgba(128, 128, 128, 0.08); }
.ss-num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.ss-game { display: inline-flex; align-items: center; gap: 9px; text-decoration: none; color: inherit; }
.ss-game:hover .ss-name { text-decoration: underline; }
.ss-thumb { width: 52px; height: 52px; min-width: 52px; border-radius: 10px; object-fit: cover; background: rgba(128, 128, 128, 0.15); }
.ss-fallback {
  width: 52px; height: 52px; min-width: 52px; border-radius: 10px;
  display: inline-flex; align-items: center; justify-content: center;
  background: rgba(128, 128, 128, 0.15);
}
.ss-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 260px; }
.ss-discord { display: inline-flex; align-items: center; gap: 7px; text-decoration: none; }
.ss-discord img { width: 18px; height: 18px; }
.ss-discord span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 240px; }
.ss-discord:hover span { text-decoration: underline; }
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
    """One cell: Discord logo + clickable invite, or an em dash."""
    url_text = _text(url)
    if not url_text:
        return '<span class="ss-none">—</span>'
    link = _esc(url_text)
    label = _esc(truncate(url_text, 44))
    return (
        f'<a class="ss-discord" href="{link}" target="_blank" rel="noopener">'
        f'<img src="{DISCORD_LOGO_URL}" alt="Discord" loading="lazy">'
        f'<span>{label}</span></a>'
    )


def render_table(frame: pd.DataFrame) -> None:
    """Render the visible page as an HTML table.

    A raw HTML table is used instead of ``st.dataframe`` because dataframe
    cells render markdown as plain text, so thumbnails and the Discord logo
    could never display inline. HTML keeps the merged thumbnail+name and
    logo+invite cells working in every Streamlit version.
    """
    head = ["Game", "Genre", "Total visits", "CCU", "Peak CCU", "Favorites", "Discord", "Creator"]
    rows = []
    for _, row in frame.iterrows():
        rows.append(
            "<tr>"
            f"<td>{game_cell_html(row)}</td>"
            f"<td>{_esc(_text(row.get('genre'), 'Unknown'))}</td>"
            f"<td class='ss-num'>{_num_cell(row.get('visits'))}</td>"
            f"<td class='ss-num'>{_num_cell(row.get('ccu'))}</td>"
            f"<td class='ss-num'>{_num_cell(row.get('peak_ccu'))}</td>"
            f"<td class='ss-num'>{_num_cell(row.get('favorites'))}</td>"
            f"<td>{discord_cell_html(row.get('discord_url'))}</td>"
            f"<td>{_esc(_text(row.get('creator_name'), '-'))}</td>"
            "</tr>"
        )
    st.markdown(
        TABLE_STYLE
        + '<div class="ss-wrap"><table class="ss-table"><thead><tr>'
        + "".join(f"<th>{_esc(label)}</th>" for label in head)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>",
        unsafe_allow_html=True,
    )

st.title("🚀 New and Upcoming" if is_watch_view else "Games matching your target")
if is_watch_view:
    st.caption(
        "Games that climbed 2+ tiers or tripled their CCU between syncs. "
        "Same table, same Discord checks — no filters by design."
    )
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
    st.caption(f"Page {int(page)} of {page_count} · {len(metric_filtered):,} matching games")
with nav_right:
    st.button(
        "Next page →",
        disabled=int(page) >= page_count,
        key="main_next_page",
        on_click=shift_contact_page,
        args=(1, page_count),
        width="stretch",
    )
meta_bits = [
    f"**{len(visible):,}** shown",
    f"**{len(metric_filtered):,}** target matches",
    f"Page **{int(page)}/{page_count}**",
    f"Σ CCU **{compact_num(visible['ccu'].sum()) if not visible.empty else '0'}**",
]
badge = {"live": "🟢 live", "db": "💾 cached", "demo": "🛰️ demo", "watch": "🚀 watch"}.get(source, source)
st.caption("  ·  ".join(meta_bits) + f"  ·  {badge}")

if source == "demo":
    st.warning("Live sources were unavailable, so demo data is shown. Run Sync live data to retry.")
if DEFAULT_CANDIDATE_LIMIT <= 10_000:
    st.caption(
        f"Metric scan checks up to {DEFAULT_CANDIDATE_LIMIT:,} ranked source candidates. "
        "Discord lookups are requested only for the visible page."
    )
if discord_filter != DISCORD_FILTER_ALL:
    st.info("Discord filters apply to the currently checked page; advancing pages checks more games.")

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

st.caption(
    "Targets are applied before contact lookup. Page navigation checks only the selected page, "
    "so the first useful results arrive without waiting for the entire catalog."
)
