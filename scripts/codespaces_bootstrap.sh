#!/usr/bin/env bash
# UpScale Scouting Tool — one-command Codespaces launch.
#
#   bash scripts/codespaces_bootstrap.sh
#
# Installs deps, verifies the two secrets, starts the dashboard on 0.0.0.0:8501,
# and prints the public URL. Safe to re-run (stops an old instance first).
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

banner() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

banner "1/4 Dependencies"
pip install --no-cache-dir -q -r requirements.txt || { echo "pip install failed"; exit 1; }

banner "2/4 Secrets check"
missing=0
if [ -z "${APP_PASSWORD:-}" ]; then
  echo "  ❌ APP_PASSWORD is NOT set in this codespace."
  missing=1
else
  echo "  ✅ APP_PASSWORD present (gate will lock correctly)."
fi
if [ -z "${CONTACTS_KEY:-}" ]; then
  echo "  ⚠️  CONTACTS_KEY is NOT set — the app works but Discord links stay hidden."
  echo "     Add it at https://github.com/settings/codespaces → Codespaces secrets,"
  echo "     then stop+rebuild this codespace (Command palette: 'Codespaces: Rebuild')."
else
  echo "  ✅ CONTACTS_KEY present (links will decrypt)."
fi
if [ "$missing" = "1" ]; then
  echo
  echo "  APP_PASSWORD is required (the gate fails closed without it)."
  echo "  Set it as a Codespaces secret scoped to this repo, then rebuild:"
  echo "    https://github.com/settings/codespaces → Codespaces secrets → New secret"
  echo "    Command palette → 'Codespaces: Rebuild Container'"
  exit 1
fi

banner "3/4 Port visibility"
# Try the CLI first (gh is pre-authenticated inside Codespaces); fall back to
# the one-click manual step if the token lacks codespace scope.
gh codespace ports visibility 8501:public -c "${CODESPACE_NAME:-}" >/dev/null 2>&1 \
  && echo "  ✅ Port 8501 set to Public via gh." \
  || echo "  👉 Manual step: PORTS panel → 8501 → right-click → Port visibility → Public"

banner "4/4 Starting the dashboard"
pkill -f "streamlit run" 2>/dev/null && sleep 1
echo "  Launching Streamlit on 0.0.0.0:8501 ..."
nohup python -m streamlit run app.py \
  --server.port=8501 --server.address=0.0.0.0 --server.headless=true \
  --browser.gatherUsageStats=false \
  > /tmp/streamlit.log 2>&1 &
sleep 6
if curl -sf http://127.0.0.1:8501/_stcore/health >/dev/null; then
  echo "  ✅ App is healthy."
else
  echo "  ❌ App did not come up — last log lines:"; tail -20 /tmp/streamlit.log; exit 1
fi

echo
echo "────────────────────────────────────────────────────────────"
echo "  Share this URL with your users:"
echo
echo "  https://${CODESPACE_NAME:-<codespace>}-8501.app.github.dev"
echo
echo "  (Also always visible in the PORTS panel next to port 8501.)"
echo "────────────────────────────────────────────────────────────"
echo
echo "  Session log:  tail -f /tmp/streamlit.log"
echo "  Stop it:      pkill -f 'streamlit run'"
echo "  Idle note:    the codespace stops itself after inactivity; the URL"
echo "                stays the same when you restart it. Bump the timeout:"
echo "                https://github.com/settings/codespaces → Default idle timeout → 240 minutes"
