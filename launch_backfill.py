#!/usr/bin/env python3
"""Detach-and-supervise the contact backfill so it survives everything.

Double-fork + setsid: the intermediate parent exits immediately (the runner
command completes fast), the worker is reparented to launchd and lives in
its own session — a SIGKILL to the runner's process group cannot reach it.

The detached process then SUPERVISES the run: catchup_contacts.py is
resumable (every verdict is stored as it happens), so an unexpected death
(OOM, stray signal, crash) just costs a cooldown — the supervisor relaunches
and the sweep continues where it stopped. Only a clean exit (0) or a
lock-conflict exit (3: another instance owns the sweep) ends supervision.
"""

import os
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(APP_DIR, "logs", "contact_backfill_run.out")
SHARD_LOG = os.path.join(APP_DIR, "logs", "contact_backfill_shard.out")
DONE = os.path.join(APP_DIR, "logs", "contact_backfill.done")
PID = os.path.join(APP_DIR, "logs", "contact_backfill.pid")
COOKIE2 = os.path.join(APP_DIR, "local_cookie2.txt")
MAX_ATTEMPTS = 6        # resumable run: each retry continues, none restarts
COOLDOWN_S = 300        # 5 min — lets a Roblox throttle window decay too
CLEAN_EXITS = {0, 3}    # 0 = done, 3 = another backfill owns the lock

# Two-account mode (added 2026-09-25): when a second Roblox cookie exists,
# a shard worker sweeps the DECAYED band from the far end (reversed) with
# its own cookie while the primary handles the qualified backlog + pushes.
# Disjoint pools + the shared 6h verdict cache make overlap nearly free.
# The shard is never restarted independently: it lives and dies with the
# primary attempt, and the primary's next attempt respawns it.


# First fork: the parent returns to the runner, which sees a fast exit.
if os.fork() > 0:
    print("detaching: supervised backfill continues in its own session", flush=True)
    raise SystemExit(0)

# Child: new session, no controlling terminal.
os.setsid()

# Second fork: the session leader exits; the grandchild is inherited by
# launchd and can never reacquire a controlling terminal.
if os.fork() > 0:
    os._exit(0)

# Grandchild: fully detached supervisor. stdio redirected to the log.
log_fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(log_fd, 1)
os.dup2(log_fd, 2)

with open(PID, "w") as fh:
    fh.write(str(os.getpid()))


code = -1
for attempt in range(1, MAX_ATTEMPTS + 1):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[supervisor] attempt {attempt}/{MAX_ATTEMPTS} starting ({ts})", flush=True)

    # Spawn the two-account shard BEFORE the primary so both lanes start
    # together; its output goes to its own log, and it is detached with
    # start_new_session so nothing here can kill it mid-group.
    shard = None
    if os.path.exists(COOKIE2) and os.path.getsize(COOKIE2) > 50:
        shard_log = open(SHARD_LOG, "a")
        try:
            shard = subprocess.Popen(
                [sys.executable, os.path.join(APP_DIR, "catchup_contacts.py"),
                 "--pool", "decayed", "--reverse"],
                cwd=APP_DIR, stdout=shard_log, stderr=subprocess.STDOUT,
                env={**os.environ, "SS_CONTACT_BACKFILL_ROLE": "shard",
                     "SS_CONTACT_COOKIE_FILE": "local_cookie2.txt"},
                start_new_session=True,
            )
            print(f"[supervisor] shard spawned pid={shard.pid} "
                  "(decayed band, reversed, cookie #2)", flush=True)
        finally:
            shard_log.close()

    # caffeinate holds a power assertion so closing the laptop lid (or an
    # idle sleep) cannot pause the sweep; the assertion dies with this
    # process, so normal sleep behavior resumes the moment the run ends.
    caffeinate_cmd = ["caffeinate", "-i", "-s", sys.executable,
                      os.path.join(APP_DIR, "catchup_contacts.py"), "--all"]
    try:
        code = subprocess.call(caffeinate_cmd, cwd=APP_DIR)
    except FileNotFoundError:  # caffeinate unavailable (non-macOS)
        code = subprocess.call([sys.executable, os.path.join(APP_DIR, "catchup_contacts.py"), "--all"],
                               cwd=APP_DIR)
    finally:
        if shard is not None and shard.poll() is None:
            shard.terminate()
            try:
                shard.wait(timeout=30)
            except subprocess.TimeoutExpired:
                shard.kill()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(DONE, "a") as fh:
        fh.write(f"attempt={attempt} exit={code} at={ts}\n")
    if code in CLEAN_EXITS:
        break
    if attempt < MAX_ATTEMPTS:
        print(f"[supervisor] worker exited rc={code} — resuming in {COOLDOWN_S}s "
              "(verdicts already stored are safe; the sweep resumes)", flush=True)
        time.sleep(COOLDOWN_S)
else:
    print(f"[supervisor] giving up after {MAX_ATTEMPTS} attempts (last rc={code}) — "
          "re-run launch_backfill.py to continue", flush=True)

os._exit(0 if code == 0 else 1)
