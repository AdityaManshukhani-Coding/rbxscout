#!/usr/bin/env bash
# UpScale Scouting Tool — "open for business": wake a stopped codespace and bring the
# dashboard back up in one command from the Mac.
#
#   bash scripts/codespace_resume.sh          # find + start + relaunch app
#   bash scripts/codespace_keepalive.sh 4     # then keep it alive for the event
#
# Why this exists: a STOPPED codespace does NOT auto-wake when a user opens
# the URL — visitors just get a connection error. Restarting is a manual
# step (this script), which is also why stopped hours never burn: the meter
# only runs while the machine is up.
#
# Requires: gh CLI authenticated once (brew install gh && gh auth login).
set -uo pipefail

CS_NAME="$(gh codespace list 2>/dev/null | awk '{print $1; exit}')"
if [ -z "$CS_NAME" ]; then
  echo "No codespace found. Create one at https://github.com/codespaces first."
  exit 1
fi

STATE="$(gh codespace list 2>/dev/null | awk -v n="$CS_NAME" '$1==n {print $4}')"
echo "Codespace: $CS_NAME (state: $STATE)"

if [ "$STATE" != "Running" ]; then
  echo "Starting machine..."
  gh codespace start -c "$CS_NAME" >/dev/null 2>&1 || gh codespace stop -c "$CS_NAME" >/dev/null 2>&1 && gh codespace start -c "$CS_NAME" >/dev/null
  for i in $(seq 1 24); do
    STATE="$(gh codespace list 2>/dev/null | awk -v n="$CS_NAME" '$1==n {print $4}')"
    [ "$STATE" = "Running" ] && break
    sleep 5
  done
  [ "$STATE" = "Running" ] || { echo "Codespace did not reach Running in 2 min — try https://github.com/codespaces"; exit 1; }
  echo "Machine is up (filesystem persists — no reinstall needed)."
fi

# Relaunch the dashboard inside the codespace (the old process died with the
# last stop). ssh-exec from here; the port stays Public from the first
# bootstrap, so no visibility step is needed again.
echo "Relaunching Streamlit inside the codespace..."
gh codespace ssh -c "$CS_NAME" -- '
  pkill -f "streamlit run" 2>/dev/null; sleep 1
  cd /workspaces/rbxscout 2>/dev/null \
    || cd "$(find /workspaces -maxdepth 2 -name app.py -printf "%h\n" 2>/dev/null | head -1)" \
    || cd ~
  nohup python -m streamlit run app.py --server.port=8501 --server.address=0.0.0.0 \
    --server.headless=true --browser.gatherUsageStats=false \
    > /tmp/streamlit.log 2>&1 &
  for i in $(seq 1 20); do
    curl -sf http://127.0.0.1:8501/_stcore/health >/dev/null && { echo APP_HEALTHY; exit 0; }
    sleep 3
  done
  echo APP_FAILED; tail -20 /tmp/streamlit.log
' && echo "✅ Dashboard is live." || { echo "❌ App failed to start — open the codespace terminal and run scripts/codespaces_bootstrap.sh manually."; exit 1; }

echo
echo "────────────────────────────────────────────────────────────"
echo "  Same URL as always:"
echo "  https://${CS_NAME}-8501.app.github.dev"
echo
echo "  Now start the keepalive for your event window:"
echo "    bash scripts/codespace_keepalive.sh <hours>"
echo "  When the event ends (Ctrl-C), the machine stops itself within"
echo "  the 60-min idle timeout and the meter pauses."
echo "────────────────────────────────────────────────────────────"
