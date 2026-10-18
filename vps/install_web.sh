#!/bin/bash
# Blackout Logger — Dashboard Install Script
#
# Installs the blackout-web systemd service (read-only dashboard on 127.0.0.1)
# and publishes it on the tailnet at http://<vps-hostname>/<path> with Tailscale Serve.
#
# Usage (run from the repo root on the VPS):
#   bash vps/install_web.sh [port] [path]
# Defaults: port 8090, path /blackouts

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CURRENT_USER="$(whoami)"
PYTHON_BIN="$(command -v python3)"
PORT="${1:-8090}"
SERVE_PATH="${2:-/blackouts}"
SERVICE_NAME="blackout-web"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

if [ ! -f "$SCRIPT_DIR/web_server.py" ]; then
    echo "Error: web_server.py not found at $SCRIPT_DIR/web_server.py"
    exit 1
fi

echo "Installing Blackout Logger dashboard..."
echo "  User:     $CURRENT_USER"
echo "  Script:   $SCRIPT_DIR/web_server.py"
echo "  Listen:   127.0.0.1:$PORT"
echo "  Tailnet:  http://<this-host>$SERVE_PATH"

$SUDO tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Blackout Logger — Dashboard
Documentation=https://github.com/JSansoP/blackout-logger
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${PYTHON_BIN} ${SCRIPT_DIR}/web_server.py --host 127.0.0.1 --port ${PORT}
Restart=always
RestartSec=5

NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=${SCRIPT_DIR}
PrivateTmp=yes

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME"
$SUDO systemctl restart "$SERVICE_NAME"

$SUDO tailscale serve --bg --http=80 --set-path="$SERVE_PATH" "http://127.0.0.1:$PORT"

echo ""
$SUDO systemctl status "$SERVICE_NAME" --no-pager -l | head -5
$SUDO tailscale serve status
