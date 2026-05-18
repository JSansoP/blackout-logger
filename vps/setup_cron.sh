#!/bin/bash
# Blackout Logger — Cron Setup Script
#
# Installs a cron job that runs the blackout checker.
# Usage: bash vps/setup_cron.sh [minutes]  (run from the project root)
# Default: 10 minutes

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECKER_SCRIPT="$SCRIPT_DIR/blackout_checker.py"
# Log file lives inside the project directory — no sudo needed
LOG_FILE="$SCRIPT_DIR/blackout_checker.log"

# Get interval from parameter (default: 10)
INTERVAL="${1:-10}"

# Validate interval
if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [ "$INTERVAL" -le 0 ] || [ "$INTERVAL" -ge 60 ]; then
    echo "Error: Interval must be a positive integer between 1 and 59"
    exit 1
fi

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
CRON_LINE="*/$INTERVAL * * * * cd $SCRIPT_DIR && $PYTHON_PATH $CHECKER_SCRIPT >> $LOG_FILE 2>&1"

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -q "blackout_checker.py"; then
    echo "Cron job already exists. Current entry:"
    crontab -l | grep "blackout_checker.py"
    echo ""
    read -p "Replace it? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        (crontab -l 2>/dev/null | grep -v "blackout_checker.py"; echo "$CRON_LINE") | crontab -
        echo "Cron job updated."
    else
        echo "No changes made."
        exit 0
    fi
else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "Cron job installed."
fi

echo ""
echo "Cron job: $CRON_LINE"
echo "Log file: $LOG_FILE"
echo ""
echo "Verify with: crontab -l"
echo "View logs:   tail -f $LOG_FILE"
