"""Capacity metering tests: slots, queue, 15-minute sessions, heartbeats.

The free container dies when too many visitors run at once, so the app
meters itself. These tests pin the contract app.py relies on:

* admission honors the active-slot cap and auto-admits the queue head first;
* expired sessions release their slot and the expiring user re-queues —
  but walk straight back in when the server has room (owner's rule:
  "re-admit if slot free");
* heartbeats keep live tabs alive and release slots of dead ones;
* "Forget this device" frees the slot AND the queue place.
"""

from __future__ import annotations

import time

import capacity


def _ref(name: str) -> str:
    return f"dev-{name}"


def _fill_slots(n: int) -> list[str]:
    """Admit ``n`` sessions directly and return their refs."""
    refs = [_ref(f"active{i}") for i in range(n)]
    for ref in refs:
        assert capacity.ensure_session(ref), f"{ref} should be admitted"
    return refs


def test_admission_honors_cap_and_queues_the_rest():
    active = _fill_slots(capacity.MAX_ACTIVE)
    assert capacity.counts() == (capacity.MAX_ACTIVE, 0)

    late = _ref("late")
    assert not capacity.ensure_session(late), "full server must not admit"
    assert not capacity.join_queue(late), "queue, not admitted"
    position, total = capacity.queue_position(late)
    assert position == 1 and total == 1

    # Everyone else who arrives queues behind them in arrival order.
    second = _ref("second")
    assert not capacity.join_queue(second)
    assert capacity.queue_position(second) == (2, 2)

    # Leaving frees the slot; the queue head is admitted automatically.
    capacity.leave(active[0])
    assert capacity.remaining_seconds(late) > 0, "queue head auto-admitted"
    assert capacity.queue_position(second) == (1, 1), "everyone moves up"


def test_session_expiry_releases_slot_and_requeues():
    active = _fill_slots(capacity.MAX_ACTIVE)
    holder = _ref("holder")
    # Squeeze in by first freeing a slot, then claiming it.
    capacity.leave(active[0])
    assert capacity.ensure_session(holder)
    assert capacity.counts()[0] == capacity.MAX_ACTIVE

    # Force the session into the past: the 15 minutes are up.
    sid = capacity._sid(holder)
    with capacity._STATE_LOCK:
        sessions = capacity._load_sessions()
        for entry in sessions:
            if entry.get("sid") == sid:
                entry["expires"] = time.time() - 1
        capacity._save(capacity._sessions_path(), sessions)

    # A queue exists behind them, so expiry must send the holder back there
    # (not straight back in) — the freed slot goes to the head of the queue.
    assert not capacity.join_queue(_ref("waiter"))
    assert not capacity.ensure_session(holder), "expired session ends"
    assert capacity.remaining_seconds(holder) == 0
    assert capacity.remaining_seconds(_ref("waiter")) > 0, "queue head took the slot"

    # The queue screen then places the expired user at the BACK (FIFO):
    # exactly what render_queue_screen does for every waiting visitor.
    assert not capacity.join_queue(holder)
    assert capacity.queue_position(holder)[0] >= 1, "expiring user rejoins the queue"


def test_expired_session_reenters_when_server_has_room():
    # NOT a full server: the only other session leaves before expiry.
    _fill_slots(2)
    holder = _ref("solo")
    assert capacity.ensure_session(holder)

    sid = capacity._sid(holder)
    with capacity._STATE_LOCK:
        sessions = capacity._load_sessions()
        for entry in sessions:
            if entry.get("sid") == sid:
                entry["expires"] = time.time() - 1
        capacity._save(capacity._sessions_path(), sessions)

    capacity.leave(_ref("active0"))
    capacity.leave(_ref("active1"))
    # Expiry ran while slots were free afterwards: re-admission is instant.
    assert capacity.ensure_session(holder), "re-admit if slot free"
    fresh = capacity.remaining_seconds(holder)
    assert fresh > capacity.SESSION_SECONDS - 5, "fresh 15 minutes granted"


def test_heartbeat_keeps_live_tab_and_janitor_releases_dead_one():
    active = _fill_slots(capacity.MAX_ACTIVE)
    ref = active[0]

    # A heartbeated session survives the janitor.
    capacity.heartbeat(ref)
    assert not capacity._release_expired()
    assert capacity.remaining_seconds(ref) > 0

    # A tab that stopped pinging 2 minutes ago is dead: slot released.
    sid = capacity._sid(ref)
    # A waiter is already in line BEFORE the tab dies.
    assert not capacity.join_queue(_ref("queued"))

    # A tab that stopped pinging 2 minutes ago is dead: slot released and
    # handed straight to the queue head.
    with capacity._STATE_LOCK:
        sessions = capacity._load_sessions()
        for entry in sessions:
            if entry.get("sid") == sid:
                entry["last_seen"] = time.time() - 120
        capacity._save(capacity._sessions_path(), sessions)
    assert capacity._release_expired(), "janitor noticed the dead tab"
    assert capacity.remaining_seconds(ref) == 0
    assert capacity.remaining_seconds(_ref("queued")) > 0, "queue head auto-admitted"

    # And the server is full again, so a walk-up cannot squeeze in.
    assert not capacity.ensure_session(_ref("walkup")), "no line-jumping"


def test_stale_queue_entries_are_purged():
    _fill_slots(capacity.MAX_ACTIVE)  # full server, so joining really queues
    assert not capacity.join_queue(_ref("ghost"))
    sid = capacity._sid(_ref("ghost"))
    with capacity._STATE_LOCK:
        queue = capacity._load_queue()
        for entry in queue:
            if entry.get("sid") == sid:
                entry["last_seen"] = time.time() - 10_000
        capacity._save(capacity._queue_path(), queue)
    capacity._release_expired()
    assert capacity.queue_position(_ref("ghost")) == (0, 0), "dead queue entry gone"


def test_forget_device_releases_slot_and_queue_place():
    _fill_slots(capacity.MAX_ACTIVE)
    quitter = _ref("quitter")
    assert not capacity.join_queue(quitter), "full server → queue"
    capacity.leave(quitter)
    assert capacity.remaining_seconds(quitter) == 0
    assert capacity.queue_position(quitter) == (0, 0), "queue place released"

    # An ACTIVE quitter frees their slot for the next person.
    holder = _ref("active0")
    capacity.leave(holder)
    assert capacity.ensure_session(_ref("next")), "slot is available again"


def test_release_slot_does_not_touch_the_queue():
    _fill_slots(capacity.MAX_ACTIVE)
    holder = _ref("holder")
    assert not capacity.join_queue(holder)
    capacity.release_slot(holder)
    assert capacity.remaining_seconds(holder) == 0
    # The freed slot goes to whoever waited first — release_slot fills it.
    assert capacity.remaining_seconds(_ref("active0")) > 0
