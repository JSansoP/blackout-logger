#!/usr/bin/env python3
"""
Blackout Logger — Configuration

Loads configuration from a .env file (no dependencies required).
"""

import os

# _BASE_DIR: the vps/ directory (where this script lives)
# _PROJECT_DIR: the project root (one level up, where .env lives)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_BASE_DIR)


def _load_env_file(path):
    """Parse a .env file and load values into os.environ (if not already set)."""
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Remove surrounding quotes if present
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            # Don't override existing environment variables
            if key not in os.environ:
                os.environ[key] = value


# Load .env from the project root (alongside .env.example and README.md)
_load_env_file(os.path.join(_PROJECT_DIR, ".env"))

# --- Configuration values ---

# Raspberry Pi connection
PI_ADDRESS = os.environ.get("PI_ADDRESS", "")
PI_PORT = int(os.environ.get("PI_PORT", "8080"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "10"))

# Retry loop settings
RETRY_INTERVAL = int(os.environ.get("RETRY_INTERVAL", "60"))
MAX_RETRY_DURATION = int(os.environ.get("MAX_RETRY_DURATION", "3600"))

# Database
DB_PATH = os.environ.get("DB_PATH", os.path.join(_BASE_DIR, "blackout.db"))

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Lock file
LOCK_FILE = os.environ.get("LOCK_FILE", "/tmp/blackout_checker.lock")


def validate():
    """Validate that required configuration values are set."""
    errors = []
    if not PI_ADDRESS:
        errors.append("PI_ADDRESS is required (Tailscale IP of the Raspberry Pi)")
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is required")
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID is required")
    if errors:
        raise ValueError("Configuration errors:\n  - " + "\n  - ".join(errors))
