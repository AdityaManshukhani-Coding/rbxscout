#!/usr/bin/env python3
"""Detach-and-run a python script so the terminal runner cannot kill it.

Double-fork + setsid: the intermediate parent exits immediately (the runner
command completes fast), the worker is reparented to launchd in its own
session — a SIGKILL to the runner's process group cannot reach it. Same
shape the contact backfill uses to survive Freebuff restarts.

Usage:  .venv/bin/python run_detached.py push_pending.py [args...]
Output: logs/detached_<script>.log
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))

if os.fork():          # parent exits -> runner command completes
    raise SystemExit(0)
os.setsid()            # own session, no controlling terminal
if os.fork():          # first child exits -> worker reparented to launchd
    raise SystemExit(0)

os.chdir(APP_DIR)
log_path = os.path.join(APP_DIR, "logs", "detached_" +
                        os.path.basename(sys.argv[1]) + ".log")
os.makedirs(os.path.dirname(log_path), exist_ok=True)
devnull = os.open(os.devnull, os.O_RDONLY)
log_fh = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(devnull, 0)    # stdin: nowhere
os.dup2(log_fh, 1)     # stdout: detached log
os.dup2(log_fh, 2)     # stderr: detached log
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
