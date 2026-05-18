#!/bin/bash
# Blackout Logger — Cron Setup Script
#
# Installs a cron job that runs the blackout checker every 5 minutes.
# Usage: bash setup_cron.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECKER_SCRIPT="$SCRIPT_DIR/blackout_checker.py"
LOG_FILE="/var/log/blackout_checker.log"

# Verify the checker script exists
if [ ! -f "$CHECKER_SCRIPT" ]; then
    echo "Error: blackout_checker.py not found at $CHECKER_SCRIPT"
    exit 1
fi

# Verify Python 3 is available
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found in PATH"
    exit 1
fi

PYTHON_PATH="$(which python3)"
CRON_LINE="*/5 * * * * cd $SCRIPT_DIR && $PYTHON_PATH $CHECKER_SCRIPT >> $LOG_FILE 2>&1"

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -q "blackout_checker.py"; then
    echo "Cron job already exists. Current entry:"
    crontab -l | grep "blackout_checker.py"
    echo ""
    read -p "Replace it? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # Remove existing entry and add new one
        (crontab -l 2>/dev/null | grep -v "blackout_checker.py"; echo "$CRON_LINE") | crontab -
        echo "Cron job updated."
    else
        echo "No changes made."
        exit 0
    fi
else
    # Add new cron job
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "Cron job installed."
fi

# Create log file if it doesn't exist
sudo touch "$LOG_FILE"
sudo chown "$(whoami)" "$LOG_FILE"

echo ""
echo "Cron job: $CRON_LINE"
echo "Log file: $LOG_FILE"
echo ""
echo "Verify with: crontab -l"
echo "View logs:   tail -f $LOG_FILE"
