"""Capacity metering for UpScale Scouting Tool (Streamlit Community Cloud).

The free container has a hard 1 GB RAM ceiling and no auto-scaling. A
thundering herd of visitors crashes the whole app for everyone, so the app
now meters itself:

* **Active slots** — at most ``SS_MAX_ACTIVE_SESSIONS`` sessions (default 15,
  measured at ~29 MB each) run the full dashboard at once.
* **Waiting queue** — everyone beyond the cap waits on a lightweight page
  that shows their live waiting position and an estimated wait. When a slot
  frees up, the head of the queue is admitted automatically (a ping every
  ``SS_CAPACITY_TICKER_SECONDS`` notices; the user never clicks anything).
* **15-minute sessions** — an active session expires after
  ``SS_SESSION_MINUTES`` (default 15). Expired sessions are re-admitted
  instantly when a slot is free, otherwise they rejoin the queue — the
  strictest they ever get is "back of the line on a full server".
* **Heartbeats** — Streamlit has no tab-close event, so liveness is tracked
  by client pings (the queue page and the sidebar countdown re-run on a
  timer). A session whose last ping is older than ``HEARTBEAT_TIMEOUT``
  seconds is considered closed and its slot is released automatically.
  This is what keeps closed tabs from squatting on slots.

State lives in two tiny JSON files next to the app (gitignored, per-container
lifetime, same trade-off as the device profiles). One user = one device ref
(the ``ss_ref`` cookie), so a refresh/back-navigation re-enters the SAME
session with its remaining time — no double-dipping from reloads.

Every call is fail-open with respect to *state integrity* (a missing or
corrupt file never crashes the app) but fail-CLOSED with respect to capacity
(when bookkeeping is unreadable the caller is treated as queued, never
admitted for free).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import streamlit as st

# --- configuration (env-overridable so the owner can tune via secrets) ----- #

MAX_ACTIVE = max(1, int(float(os.environ.get("SS_MAX_ACTIVE_SESSIONS", "15") or 15)))
SESSION_SECONDS = max(60.0, float(os.environ.get("SS_SESSION_MINUTES", "15") or 15) * 60.0)
HEARTBEAT_TIMEOUT = 90.0  # no ping for 90 s == tab closed == slot released
TICKER_SECONDS = max(5, int(os.environ.get("SS_CAPACITY_TICKER_SECONDS", "15") or 15))

# Client-side countdown: the chip re-renders every TICKER_SECONDS, so the
# displayed mm:ss used to freeze and jump a full block at a time. This script
# ticks the [data-ss-session] element every second in the browser — zero
# server traffic, and every rerun resyncs it to the server's truth.
_SESSION_TICK_SCRIPT = r"""
<script>
(function () {
  if (window.__ssSessionTick) { return; }
  var tick = function () {
    var el = document.querySelector('[data-ss-session]');
    if (!el) { return; }
    var m = /([0-9]+):([0-9]{2})/.exec(el.textContent);
    if (!m) { return; }
    var total = parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
    if (!(total > 0)) { return; }
    total -= 1;
    var mm = Math.floor(total / 60), ss = total % 60;
    el.textContent = mm + ':' + (ss < 10 ? '0' : '') + ss + ' left';
  };
  window.__ssSessionTick = window.setInterval(tick, 1000);
})();
</script>
"""

_STATE_LOCK = threading.Lock()  # serializes file read-modify-write cycles


def _dir() -> Path:
    return Path(os.environ.get("SS_CAPACITY_DIR", str(Path(__file__).resolve().parent / ".capacity")))


def _sessions_path() -> Path:
    return _dir() / "sessions.json"


def _queue_path() -> Path:
    return _dir() / "queue.json"


def _load(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(path: Path, items: list) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle)
        os.replace(tmp_name, path)
    except Exception:
        pass  # a failed write must never take the app down


def _load_sessions() -> list:
    return _load(_sessions_path())


def _load_queue() -> list:
    return _load(_queue_path())


# --- identity --------------------------------------------------------------- #


def _sid(ref: str | None) -> str:
    """Stable session id: short hash of the device ref (never the raw ref)."""
    return hashlib.sha256(str(ref or "anon").encode("utf-8")).hexdigest()[:16]


# --- janitor: expire dead sessions, admit the queue ------------------------- #


def _release_expired() -> bool:
    """Drop heartbeated-out sessions + stale queue entries; admit who fits.

    Returns True when the membership changed (callers re-render). Both files
    are saved exactly once, AFTER every mutation — saving sessions mid-way
    used to drop the queue head admitted further down (they vanished from
    both files and waited forever).
    """
    now = time.time()
    changed = False
    with _STATE_LOCK:
        sessions = [s for s in _load_sessions() if isinstance(s, dict)]
        kept = []
        for entry in sessions:
            last_seen = float(entry.get("last_seen", 0) or 0)
            if now - last_seen > HEARTBEAT_TIMEOUT:
                changed = True  # dead tab: slot released
                continue
            kept.append(entry)

        queue = [q for q in _load_queue() if isinstance(q, dict)]
        kept_q = [q for q in queue if now - float(q.get("last_seen", 0) or 0) <= HEARTBEAT_TIMEOUT * 2]
        if len(kept_q) != len(queue):
            changed = True

        free = MAX_ACTIVE - len(kept)
        while free > 0 and kept_q:
            head = kept_q.pop(0)
            kept.append(
                {
                    "sid": head.get("sid", ""),
                    "granted": now,
                    "expires": now + SESSION_SECONDS,
                    "last_seen": now,
                }
            )
            changed = True
            free -= 1
        if changed:
            _save(_sessions_path(), kept)
            _save(_queue_path(), kept_q)
    return changed


def _admit_if_room(sid: str) -> bool:
    """Grant a slot to ``sid`` when fair to do so; True when they hold one.

    Strict FIFO: when anyone is waiting, only the HEAD of the queue may
    claim a free slot — newcomers (and expired returnees behind the head)
    wait their turn, so nobody can jump the line by re-running.
    """
    now = time.time()
    with _STATE_LOCK:
        sessions = [s for s in _load_sessions() if isinstance(s, dict)]
        sessions = [s for s in sessions if now - float(s.get("last_seen", 0) or 0) <= HEARTBEAT_TIMEOUT]
        for entry in sessions:
            if entry.get("sid") == sid:
                return True  # already holding a slot
        if len(sessions) >= MAX_ACTIVE:
            return False
        queue = [q for q in _load_queue() if isinstance(q, dict)]
        if queue and queue[0].get("sid") != sid:
            return False  # someone is ahead of you
        sessions.append({"sid": sid, "granted": now, "expires": now + SESSION_SECONDS, "last_seen": now})
        _save(_sessions_path(), sessions)
        if queue and queue[0].get("sid") == sid:
            _save(_queue_path(), queue[1:])
        return True


# --- public session API (called from app.py's main flow) -------------------- #


def ensure_session(ref: str | None) -> bool:
    """Renew or grant the caller's slot; True when they hold a live one.

    Called at the top of every full app rerun (after the password gate).
    Renewing ``last_seen`` here is the rerun-driven half of the heartbeat;
    the fragment-driven half lives in :func:`render_queue_screen` and
    :func:`render_session_chip`. A session whose 15 minutes are up releases
    its slot for the queue head FIRST, then — per the owner's rule — walks
    straight back in when the server still has room, otherwise falls through
    to the queue screen.

    Lock discipline: the expiry branch must do its queue-head admission
    OUTSIDE ``_STATE_LOCK`` — the lock is non-reentrant and
    :func:`_admit_head_of_queue` takes it too (calling it under the lock
    deadlocked the very first session expiry).
    """
    _release_expired()
    sid = _sid(ref)
    now = time.time()
    expired = False
    with _STATE_LOCK:
        sessions = [s for s in _load_sessions() if isinstance(s, dict)]
        for entry in sessions:
            if entry.get("sid") == sid:
                if float(entry.get("expires", 0) or 0) <= now:
                    sessions = [s for s in sessions if s.get("sid") != sid]
                    _save(_sessions_path(), sessions)
                    expired = True
                    break
                entry["last_seen"] = now
                _save(_sessions_path(), sessions)
                return True
    if expired:
        _admit_head_of_queue()
    return _admit_if_room(sid)


def heartbeat(ref: str | None) -> None:
    """Fragment-side liveness ping: renew an ACTIVE session's last_seen only.

    Never grants, never expires, never touches the queue — those decisions
    belong to :func:`ensure_session` on full reruns. Without this, a user
    reading the dashboard (no clicks for 90 s) would look dead to the janitor
    and lose their slot mid-reading.
    """
    sid = _sid(ref)
    now = time.time()
    with _STATE_LOCK:
        sessions = [s for s in _load_sessions() if isinstance(s, dict)]
        for entry in sessions:
            if entry.get("sid") == sid and float(entry.get("expires", 0) or 0) > now:
                entry["last_seen"] = now
                _save(_sessions_path(), sessions)
                return


def _admit_head_of_queue() -> None:
    """Fill free slots from the front of the queue (call with lock held)."""
    now = time.time()
    sessions = [s for s in _load_sessions() if isinstance(s, dict)]
    sessions = [s for s in sessions if now - float(s.get("last_seen", 0) or 0) <= HEARTBEAT_TIMEOUT]
    queue = [q for q in _load_queue() if isinstance(q, dict)]
    free = MAX_ACTIVE - len(sessions)
    while free > 0 and queue:
        head = queue.pop(0)
        sessions.append(
            {
                "sid": head.get("sid", ""),
                "granted": now,
                "expires": now + SESSION_SECONDS,
                "last_seen": now,
            }
        )
        free -= 1
    _save(_sessions_path(), sessions)
    _save(_queue_path(), queue)


def join_queue(ref: str | None) -> bool:
    """Ensure queue membership; True when the caller holds a slot instead.

    Admission is deliberately routed through :func:`_admit_if_room` so a
    newcomer can never take a slot while the queue head is waiting.
    """
    sid = _sid(ref)
    if _admit_if_room(sid):
        return True
    now = time.time()
    with _STATE_LOCK:
        queue = [q for q in _load_queue() if isinstance(q, dict)]
        for entry in queue:
            if entry.get("sid") == sid:
                entry["last_seen"] = now
                _save(_queue_path(), queue)
                return False
        queue.append({"sid": sid, "joined": now, "last_seen": now})
        _save(_queue_path(), queue)
    return False


def queue_position(ref: str | None) -> tuple[int, int]:
    """(1-indexed position in queue, total waiting). (0, 0) when not queued."""
    sid = _sid(ref)
    queue = [q for q in _load_queue() if isinstance(q, dict)]
    for index, entry in enumerate(queue):
        if entry.get("sid") == sid:
            return index + 1, len(queue)
    return 0, len(queue)


def leave(ref: str | None) -> None:
    """Explicit sign-out ("Forget this device" also releases the slot/queue)."""
    sid = _sid(ref)
    with _STATE_LOCK:
        sessions = [s for s in _load_sessions() if isinstance(s, dict) and s.get("sid") != sid]
        _save(_sessions_path(), sessions)
        queue = [q for q in _load_queue() if isinstance(q, dict) and q.get("sid") != sid]
        _save(_queue_path(), queue)
    _admit_head_of_queue()


def release_slot(ref: str | None) -> None:
    """Drop the caller's slot without touching the queue (session end path)."""
    sid = _sid(ref)
    with _STATE_LOCK:
        sessions = [s for s in _load_sessions() if isinstance(s, dict) and s.get("sid") != sid]
        _save(_sessions_path(), sessions)
    _admit_head_of_queue()


def counts() -> tuple[int, int]:
    """(active sessions, waiting users) — for the sidebar chip."""
    now = time.time()
    sessions = [s for s in _load_sessions() if isinstance(s, dict) and now - float(s.get("last_seen", 0) or 0) <= HEARTBEAT_TIMEOUT]
    return len(sessions), len(_load_queue())


def remaining_seconds(ref: str | None) -> float:
    """Seconds left in the caller's active session (0 when none/queued)."""
    sid = _sid(ref)
    now = time.time()
    for entry in _load_sessions():
        if isinstance(entry, dict) and entry.get("sid") == sid:
            return max(0.0, float(entry.get("expires", 0) or 0) - now)
    return 0.0


def reset_for_tests(cache_dir: str) -> None:
    """Point the store at a fresh directory and clear it (tests only)."""
    os.environ["SS_CAPACITY_DIR"] = cache_dir
    for path in (_sessions_path(), _queue_path()):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


# --- UI fragments ------------------------------------------------------------ #
# Both fragments double as the client heartbeat: their timer-driven reruns
# refresh ``last_seen`` server-side. When they observe a change of state
# (admitted, expired) they trigger a FULL rerun so the whole app reacts.


@st.fragment(run_every=TICKER_SECONDS)
def render_queue_screen(ref: str | None) -> None:
    """The waiting page: live position, estimated wait, auto-admission."""
    sid = _sid(ref)
    _release_expired()
    if join_queue(ref):
        # A slot opened up (or was never contested) — drop straight into the
        # dashboard, no click.
        st.rerun(scope="app")
    position, total = queue_position(ref)
    with _STATE_LOCK:
        queue = [q for q in _load_queue() if isinstance(q, dict)]
        for entry in queue:
            if entry.get("sid") == sid:
                entry["last_seen"] = time.time()
                _save(_queue_path(), queue)
                break

    free_now = max(0, MAX_ACTIVE - counts()[0])
    if position <= free_now:
        wait_est = "next — a slot is opening right now"
    else:
        ahead = max(0, position - 1 - free_now)
        waves = ahead // MAX_ACTIVE + 1
        wait_est = f"~{waves * int(SESSION_SECONDS // 60)} min"

    st.markdown("<style>section[data-testid='stSidebar']{display:none}</style>", unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="gate-card" style="max-width:460px">
          <h2 style="margin:0 0 .3rem 0">⏳ You're in the waiting queue</h2>
          <p style="font-size:2.1rem;font-weight:700;margin:.4rem 0;color:#FF6E01">
            Position {position}
          </p>
          <p style="opacity:.85">{total} scout{'s' if total != 1 else ''} waiting ·
          estimated wait: <b>{wait_est}</b></p>
          <p style="opacity:.65;font-size:.9rem">
            The dashboard runs at most {MAX_ACTIVE} scouts at once to stay free
            and fast for everyone. Sessions rotate every
            {int(SESSION_SECONDS // 60)} minutes — <b>keep this tab open</b>,
            you'll be let in automatically.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("Your position updates automatically every few seconds. Do not refresh — this page holds your place.")
    if st.button("Leave the queue", width="stretch", key="queue_leave"):
        leave(ref)
        st.rerun(scope="app")


@st.fragment(run_every=TICKER_SECONDS)
def render_session_chip(ref: str | None) -> None:
    """Sidebar chip: live remaining time + occupancy, and session-expiry trigger.

    Rendered ONLY while a slot is held (``ensure_session`` returned True this
    run) — otherwise a fresh visitor would rerun-loop, since their remaining
    time is legitimately 0.
    """
    heartbeat(ref)
    _release_expired()
    if not remaining_seconds(ref):
        # Time expired while the tab sat open: full rerun re-runs the
        # admission decision — straight back in if a slot is free, else the
        # queue screen (this fragment then unmounts with the old script).
        st.rerun(scope="app")
    left = int(remaining_seconds(ref) + 0.999)
    mm, ss = divmod(left, 60)
    active, waiting = counts()
    st.sidebar.markdown(
        f"""
        <div style="
            border:1px solid rgba(250,250,250,.12); border-radius:12px;
            padding:.55rem .8rem; margin:.2rem 0 .6rem; font-size:.86rem;
            background:rgba(250,250,250,.04)">
          <span style="color:#9AA4B2">Scouting now</span>
          <b style="float:right">{active} / {MAX_ACTIVE}</b><br>
          <span style="color:#9AA4B2">Session</span>
          <b style="float:right" data-ss-session>{mm}:{ss:02d} left</b>
          {"<br><span style='color:#9AA4B2'>Queue</span><b style='float:right'>" + str(waiting) + " waiting</b>" if waiting else ""}
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.caption("Session limit: 15 minutes. When time is up you rejoin the queue and keep all your saved details.")
    st.html(_SESSION_TICK_SCRIPT, unsafe_allow_javascript=True)
