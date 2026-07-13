#!/usr/bin/env bash
# Mobile Farming — agent one-click setup (Raspberry Pi or any Debian/Ubuntu
# host with phones attached over USB and adb on PATH).
#
# Run from this directory on the target machine:
#   sudo bash setup.sh [agent-id]
#
# Installs every prerequisite (Python, adb, Tailscale), copies this agent to
# /opt/mobilefarm-agent, sets up a venv, and registers + starts a systemd
# service in --listen (server) mode so the main dashboard can dial in.
#
# Safe to re-run: stops the service, re-syncs files, re-installs deps, and
# restarts — use the same command to upgrade an existing install. Whatever's
# in data/ (connection tokens, admin password, ban list) is never touched.
#
# Optional: set TS_AUTHKEY=tskey-... before running to log Tailscale in
# non-interactively (e.g. `TS_AUTHKEY=tskey-xxx sudo -E bash setup.sh`).
# See DEPLOY_EDGE_DEVICE.md for the full story.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo: sudo bash setup.sh" >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer targets Debian/Ubuntu-based systems (apt-get not found)." >&2
  echo "Raspberry Pi OS, Debian, and Ubuntu are supported." >&2
  exit 1
fi

INSTALL_DIR="/opt/mobilefarm-agent"
SERVICE_USER="${SUDO_USER:-pi}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_ID="${1:-$(hostname)}"
FRESH_INSTALL=1
[ -d "$INSTALL_DIR" ] && FRESH_INSTALL=0

echo "==> [1/6] Checking prerequisites..."
NEED_PKGS=()
command -v python3 >/dev/null 2>&1 || NEED_PKGS+=(python3)
python3 -c "import venv" >/dev/null 2>&1 || NEED_PKGS+=(python3-venv)
command -v pip3 >/dev/null 2>&1 || NEED_PKGS+=(python3-pip)
command -v adb >/dev/null 2>&1 || NEED_PKGS+=(android-tools-adb)
command -v lsusb >/dev/null 2>&1 || NEED_PKGS+=(usbutils)
command -v curl >/dev/null 2>&1 || NEED_PKGS+=(curl)
command -v unzip >/dev/null 2>&1 || NEED_PKGS+=(unzip)

if [ "${#NEED_PKGS[@]}" -gt 0 ]; then
  echo "    installing: ${NEED_PKGS[*]}"
  apt-get update -qq
  apt-get install -y "${NEED_PKGS[@]}" >/dev/null
else
  echo "    python3, adb, and friends are already present."
fi

echo "==> [2/6] Checking Tailscale (fixed address for remote access)..."
if ! command -v tailscale >/dev/null 2>&1; then
  echo "    installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1
else
  echo "    already installed."
fi
TS_UP=0
if command -v tailscale >/dev/null 2>&1; then
  tailscale ip -4 >/dev/null 2>&1 && TS_UP=1
fi
if [ "$TS_UP" -eq 0 ]; then
  if [ -n "${TS_AUTHKEY:-}" ]; then
    echo "    logging in with TS_AUTHKEY..."
    tailscale up --authkey="$TS_AUTHKEY" --hostname="$AGENT_ID" || echo "    tailscale login failed — you can retry manually with: sudo tailscale up"
  else
    echo "    not logged in yet — this needs a one-time interactive step."
    echo "    after this script finishes, run: sudo tailscale up"
  fi
fi

if systemctl is-active --quiet mobilefarm-agent 2>/dev/null; then
  echo "==> [3/6] Existing install found — stopping the service before updating..."
  systemctl stop mobilefarm-agent
elif [ "$FRESH_INSTALL" -eq 0 ]; then
  echo "==> [3/6] Existing install found (service wasn't running)."
else
  echo "==> [3/6] Fresh install."
fi

echo "==> [4/6] Copying files to $INSTALL_DIR (data/ is preserved)..."
mkdir -p "$INSTALL_DIR"
shopt -s dotglob
for item in "$SCRIPT_DIR"/*; do
  name="$(basename "$item")"
  [ "$name" = "data" ] && continue
  [ "$name" = ".venv" ] && continue
  [ "$name" = "__pycache__" ] && continue
  rm -rf "${INSTALL_DIR:?}/$name"
  cp -r "$item" "$INSTALL_DIR/$name"
done
shopt -u dotglob
find "$INSTALL_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$INSTALL_DIR/data"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

echo "==> [5/6] Setting up the virtualenv + dependencies (first run can take a few minutes)..."
# A venv's bin/ scripts embed an ABSOLUTE shebang path — if this directory was
# ever created elsewhere and moved/copied here (or the zip staging path
# differs from the install path), pip silently breaks with "cannot execute:
# required file not found". Verify it actually runs, not just that it exists.
if [ ! -x "$INSTALL_DIR/.venv/bin/pip" ] || ! "$INSTALL_DIR/.venv/bin/pip" --version >/dev/null 2>&1; then
  [ -d "$INSTALL_DIR/.venv" ] && echo "    existing venv is broken (stale path?) — recreating it..."
  rm -rf "$INSTALL_DIR/.venv"
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR/.venv"

echo "==> [6/6] Installing systemd service (agent id: $AGENT_ID)..."
sed -e "s#/opt/mobilefarm-agent#$INSTALL_DIR#g" -e "s/User=pi/User=$SERVICE_USER/" \
    -e "s/--id CHANGE_ME/--id $AGENT_ID/" \
  "$SCRIPT_DIR/mobilefarm-agent.service" > /etc/systemd/system/mobilefarm-agent.service
systemctl daemon-reload
systemctl enable --now mobilefarm-agent
sleep 2

echo
echo "============================================================"
if systemctl is-active --quiet mobilefarm-agent; then
  echo " Mobile Farming agent installed and running."
else
  echo " Service didn't come up — check: journalctl -u mobilefarm-agent -n 50"
fi
echo
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo " Admin panel (LAN)  : http://${LAN_IP:-<this-host-ip>}:8090"
echo " Logs               : journalctl -u mobilefarm-agent -f"
echo " (the admin panel's first-run password is printed there, or in"
echo "  $INSTALL_DIR/data/admin_password.txt)"
echo
if command -v tailscale >/dev/null 2>&1; then
  TS_IP="$(tailscale ip -4 2>/dev/null || true)"
  if [ -n "$TS_IP" ]; then
    echo " Admin panel (Tailscale) : http://$TS_IP:8090"
    echo " Once you create a connection there, its dashboard address is:"
    echo "   ws://$TS_IP:8091"
  else
    echo " Tailscale is installed but not logged in yet — run: sudo tailscale up"
    echo " Then re-check your address with: tailscale ip -4"
  fi
else
  echo " Tailscale install seems to have failed — retry manually:"
  echo "   curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
fi
echo "============================================================"
