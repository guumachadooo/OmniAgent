#!/usr/bin/env bash
# OmniAgent v4 — One-Command Termux / Debian PRoot Installer & Launcher ("Antigravity 2")
# Usage:
#   bash .agents/agent/install_termux_agent.sh
#   # or non-interactive:
#   CAVERN_RELAY_URL="https://..." CAVERN_BRIDGE_PAIR_SECRET="CAVPAIR-..." bash .agents/agent/install_termux_agent.sh --bg
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CFG_DIR="${HOME}/.omni_agent"
CFG_FILE="${CFG_DIR}/config.json"
LOG_FILE="${CFG_DIR}/daemon.log"
PID_FILE="${CFG_DIR}/daemon.pid"

mkdir -p "$CFG_DIR"
chmod 700 "$CFG_DIR"

BG_MODE=0
for arg in "$@"; do
  case "$arg" in
    --bg|--background) BG_MODE=1 ;;
    --stop)
      if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE"
        echo "[OmniAgent] Stopped background daemon."
      else
        echo "[OmniAgent] No running daemon found."
      fi
      exit 0
      ;;
  esac
done

RELAY_URL="${CAVERN_RELAY_URL:-}"
PAIR_SECRET="${CAVERN_BRIDGE_PAIR_SECRET:-}"
SECURITY_MODE="${OMNI_SECURITY_MODE:-smart}"
CHANNEL="${OMNI_CHANNEL:-termux}"
DEFAULT_REPO="${OMNI_REPO:-$REPO_ROOT}"

if [ -z "$RELAY_URL" ] && [ -f "$CFG_FILE" ]; then
  RELAY_URL="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("relay_url",""))' "$CFG_FILE" 2>/dev/null || true)"
fi
if [ -z "$PAIR_SECRET" ] && [ -f "$CFG_FILE" ]; then
  PAIR_SECRET="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("pair_secret",""))' "$CFG_FILE" 2>/dev/null || true)"
fi

if [ -z "$RELAY_URL" ]; then
  read -r -p "Cloudflare Worker URL (https://...workers.dev): " RELAY_URL
fi
if [ -z "$PAIR_SECRET" ]; then
  read -r -s -p "Pairing Key (same 64-hex CAVPAIR key from Cavernize): " PAIR_SECRET
  echo ""
fi

python3 - "$CFG_FILE" "$RELAY_URL" "$PAIR_SECRET" "$DEFAULT_REPO" "$CHANNEL" "$SECURITY_MODE" << 'PY'
import json, os, sys
cfg_path, url, secret, repo, channel, mode = sys.argv[1:7]
data = {
    "relay_url": url.strip().rstrip("/"),
    "pair_secret": secret.strip(),
    "repo": repo.strip(),
    "channel": channel.strip().lower() or "termux",
    "security_mode": mode.strip().lower() or "smart",
}
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
os.chmod(cfg_path, 0o600)
print(f"[OmniAgent] Saved secured config -> {cfg_path} (chmod 600)")
PY

# If running directly inside Termux, auto-enable allow-external-apps=true for OmniAgent Wake Termux
if [ -n "${TERMUX_VERSION:-}" ] || [ -d "/data/data/com.termux/files/home" ]; then
  TERMUX_PROPS="${HOME}/.termux/termux.properties"
  if [ -d "${HOME}" ] && [ "${HOME}" = "/data/data/com.termux/files/home" ]; then
    mkdir -p "${HOME}/.termux"
    if ! grep -qxF "allow-external-apps=true" "$TERMUX_PROPS" 2>/dev/null; then
      echo "allow-external-apps=true" >> "$TERMUX_PROPS"
      echo "[OmniAgent] Enabled allow-external-apps=true in $TERMUX_PROPS"
      command -v termux-reload-settings >/dev/null 2>&1 && termux-reload-settings || true
    fi
  fi
fi

# Create convenient shortcut launcher in ~/.omni_agent/start.sh (supports --bg cleanly)
cat > "${CFG_DIR}/start.sh" << EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "--bg" ] || [ "\${1:-}" = "--background" ]; then
  if [ -f "${PID_FILE}" ] && kill -0 "\$(cat "${PID_FILE}")" 2>/dev/null; then
    exit 0
  fi
  nohup python3 -B "${SCRIPT_DIR}/omni_agent_daemon.py" --config "${CFG_FILE}" > "${LOG_FILE}" 2>&1 &
  echo \$! > "${PID_FILE}"
  exit 0
fi
exec python3 -B "${SCRIPT_DIR}/omni_agent_daemon.py" --config "${CFG_FILE}" "\$@"
EOF
chmod +x "${CFG_DIR}/start.sh"

# Self-test environment detection
echo "[OmniAgent] Environment probe:"
python3 -B "${SCRIPT_DIR}/omni_agent_daemon.py" --config "$CFG_FILE" --once-self-test

if [ "$BG_MODE" -eq 1 ]; then
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")" || true
    sleep 1
  fi
  nohup python3 -B "${SCRIPT_DIR}/omni_agent_daemon.py" --config "$CFG_FILE" > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  echo "[OmniAgent] Running in background (PID $(cat "$PID_FILE")). Log: $LOG_FILE"
  echo "[OmniAgent] Stop anytime with: bash ${SCRIPT_DIR}/install_termux_agent.sh --stop"
else
  echo "[OmniAgent] Starting foreground daemon (Ctrl+C to stop, or pass --bg for background)..."
  exec python3 -B "${SCRIPT_DIR}/omni_agent_daemon.py" --config "$CFG_FILE"
fi
