#!/bin/bash
# Blackout Logger — Raspberry Pi Install Script
#
# Generates and installs the systemd service using the CURRENT user
# and directory. No hardcoded paths — works for any user/location.
#
# Usage (run from the repo root on the Pi):
#   bash pi/install.sh

set -e

# ── Resolve paths ────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"   # one level up from pi/
CURRENT_USER="$(whoami)"
PYTHON_BIN="$(which python3)"
SERVICE_NAME="blackout-uptime"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# ── Sanity checks ────────────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/uptime_server.py" ]; then
    echo "Error: uptime_server.py not found at $SCRIPT_DIR/uptime_server.py"
    exit 1
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "Error: python3 not found in PATH"
    exit 1
fi

echo "Installing Blackout Logger uptime server..."
echo "  User:       $CURRENT_USER"
echo "  Project:    $PROJECT_DIR"
echo "  Script:     $SCRIPT_DIR/uptime_server.py"
echo "  Python:     $PYTHON_BIN"
echo "  Service:    $SERVICE_FILE"
echo ""

# ── Generate and install the service file ────────────────────
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Blackout Logger — Uptime Server
Documentation=https://github.com/JSansoP/blackout-logger
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
Group=${CURRENT_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${PYTHON_BIN} ${SCRIPT_DIR}/uptime_server.py --port 8080
Restart=always
RestartSec=5

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

echo "Service file written to $SERVICE_FILE"

# ── Enable and start the service ─────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "Done! Service status:"
sudo systemctl status "$SERVICE_NAME" --no-pager -l

echo ""
echo "Verify the endpoint:"
echo "  curl http://localhost:8080/uptime"
