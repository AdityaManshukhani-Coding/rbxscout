#!/usr/bin/env bash
# Studio Scouts — Codespace keepalive for launch events.
#
# Run this on YOUR MAC (not inside the codespace) while the launch event is
# live. It counts as editor activity, resetting GitHub's idle timer, so the
# machine stays up for exactly the hours your event runs and never a minute
# more. Kill it (Ctrl-C) when the event ends; the codespace then stops itself
# after the idle timeout, conserving your monthly 60 core-hours.
#
# One-time setup:
#   brew install gh && gh auth login
#
# Usage:
#   bash scripts/codespace_keepalive.sh            # auto-finds your running codespace
#   bash scripts/codespace_keepalive.sh 6          # keep alive for 6 hours, then exit
#
set -uo pipefail

HOURS="${1:-0}"                 # 0 = run until Ctrl-C
INTERVAL=1500                   # 25 minutes in seconds

command -v gh >/dev/null 2>&1 || { echo "gh CLI not found: brew install gh && gh auth login"; exit 1; }

echo "Finding your running codespace..."
CS_NAME="$(gh codespace list 2>/dev/null | awk '/Running/ {print $1; exit}')"
if [ -z "$CS_NAME" ]; then
  echo "No RUNNING codespace found. Start it at https://github.com/codespaces first."
  exit 1
fi
REPO_INFO="$(gh codespace list 2>/dev/null | awk -v n="$CS_NAME" '$1==n {print $2, $3}')"
echo "Keeping alive: $CS_NAME ($REPO_INFO)"
echo "Touches every 25 min. ${HOURS:+Auto-stop after ${HOURS}h.} Ctrl-C to stop keeping it awake."
echo

deadline=$(( $(date +%s) + HOURS * 3600 ))
n=0
while :; do
  if [ "$HOURS" != "0" ] && [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Time window over ($HOURS h). Stopping keepalive — the codespace will idle-stop itself."
    break
  fi
  if ! gh codespace list 2>/dev/null | grep -q "^$CS_NAME"; then
    echo "Codespace no longer exists — exiting."
    break
  fi
  # A cheap metadata read on the codespace counts as user activity for the
  # idle timer; every 25 min stays clear of the shortest (30 min) timeout.
  if gh codespace view "$CS_NAME" >/dev/null 2>&1; then
    n=$((n+1))
    echo "[$(date '+%H:%M:%S')] touch #$n ok — next in 25 min"
  else
    echo "[$(date '+%H:%M:%S')] WARNING: touch failed (network? auth?). Will retry next cycle."
  fi
  sleep "$INTERVAL"
done
