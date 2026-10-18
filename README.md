# ⚡ Blackout Logger

A lightweight, zero-dependency power outage detector. A Raspberry Pi at home acts as the sensor and a VPS acts as the monitor: the VPS polls the Pi's uptime, detects power cuts and network outages, stores them in SQLite and sends Telegram notifications.

## How It Works

```
┌─────────────────┐          ┌──────────────────────┐
│  Raspberry Pi   │◄─────────│     VPS (cron)       │
│  (at home)      │  HTTP    │                      │
│                 │  GET     │  blackout_checker.py │
│  uptime_server  │─────────►│  ┌───────────────┐   │
│  port 8080      │  JSON    │  │  SQLite DB    │   │──── Telegram
│                 │          │  │  uptime_logs  │   │     notifications
└─────────────────┘          │  │  blackouts    │   │
     Tailscale               │  └───────────────┘   │
                             └──────────────────────┘
```

1. The **Raspberry Pi** runs a tiny HTTP server that returns its uptime, read from `/proc/uptime`.
2. The **VPS** runs a cron job every 10 minutes (configurable) that polls the Pi and computes the Pi's boot time as *poll time minus uptime*, using only the VPS clock.
3. If the boot time moved by more than 30 seconds since the last poll, the Pi rebooted: a **power outage** happened between polls.
4. If the Pi is **unreachable**, a blackout is opened and the checker retries every 60 seconds (for up to 1 hour per run) to pinpoint when it comes back. When it does, the boot time tells whether it was a **power** outage (Pi rebooted) or an **internet** outage (Pi kept running).
5. If an outage lasts more than 8 hours, the Pi is assumed to be **stuck** in standby (see below) and the blackout is closed with an unknown duration.
6. **Telegram notifications** are sent for every event.
7. A small **web dashboard** on the VPS shows the current status and recent outages.

## Prerequisites

- Python 3.11+ on both machines. No third-party packages are needed.
- Both machines connected via [Tailscale](https://tailscale.com/). Disable key expiry for both nodes in the Tailscale admin console: an expired key on the VPS looks like an endless outage.
- A Telegram bot (create one via [@BotFather](https://t.me/BotFather)).

## Setup

### 1. Raspberry Pi

```bash
git clone https://github.com/JSansoP/blackout-logger.git ~/projects/blackout-logger
cd ~/projects/blackout-logger
bash pi/install.sh
```

`install.sh` detects the current user and path, generates the `blackout-uptime` systemd service and starts it. Verify it:

```bash
curl http://localhost:8080/uptime
# {"uptime_seconds": 1234.56, "boot_time": "...", "hostname": "raspberrypi", "timestamp": "..."}
```

Only `uptime_seconds` is used by the VPS. `boot_time` and `timestamp` come from the Pi's own clock, which is unreliable after a power cut, and are ignored.

To restart the service after updating the Pi code: `bash pi/restart.sh`.

### 2. VPS

```bash
git clone https://github.com/JSansoP/blackout-logger.git ~/projects/blackout-logger
cd ~/projects/blackout-logger
cp .env.example .env
nano .env
```

The `.env` file lives at the project root. Main settings:

| Variable | Description | Default |
|----------|-------------|---------|
| `PI_ADDRESS` | Tailscale IP of the Pi (required) | |
| `PI_PORT` | Port of the uptime server | `8080` |
| `POLL_TIMEOUT` | Seconds to wait for the Pi | `10` |
| `RETRY_INTERVAL` | Seconds between retries while the Pi is unreachable | `60` |
| `MAX_RETRY_DURATION` | Max seconds of retrying per cron run | `3600` |
| `MAX_BLACKOUT_DURATION` | Outages longer than this are closed as `stuck` | `28800` (8 h) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (required) | |
| `TELEGRAM_CHAT_ID` | Chat or channel ID (required) | |
| `DB_PATH` | SQLite database path | `vps/blackout.db` |
| `LOCK_FILE` | PID lock file | `/tmp/blackout_checker.lock` |

Test the checker manually, then install the cron job (optional interval in minutes, default 10):

```bash
python3 vps/blackout_checker.py
bash vps/setup_cron.sh [minutes]
```

The cron job writes its log to `vps/blackout_checker.log`.

### 3. Dashboard (optional)

A read-only dashboard shows whether the Pi is online, the last outage, and the outages of the last 7, 30 or 90 days drawn by day and time of day. It is served by `vps/web_server.py` (stdlib only) on `127.0.0.1:8090` and published on the tailnet with [Tailscale Serve](https://tailscale.com/kb/1312/serve):

```bash
bash vps/install_web.sh [port] [path]   # defaults: 8090 /blackouts
```

The script installs the `blackout-web` systemd service and runs `tailscale serve --bg --http=80 --set-path=/blackouts http://127.0.0.1:8090`, so the dashboard is at `http://<vps-hostname>/blackouts/` for devices in the tailnet only. After pulling changes to `vps/web_server.py`, run `systemctl restart blackout-web`; changes in `vps/web/` only need a browser reload.

API, used by the page:

| Endpoint | Returns |
|----------|---------|
| `GET /api/status` | `state` (`online`, `offline`, `stuck`, `no_data`), `since`, `stale` (no check in 30 min), last poll, open and latest blackout |
| `GET /api/blackouts?days=30` | Blackouts that started in the last `days` days (1 to 3650) plus any open one, newest first |

### 4. Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Copy the bot token to `.env`.
3. Create a channel or group and add the bot as an admin.
4. Send a message there, open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id` value (negative for channels) to `.env`.

## Notifications

| Event | Emoji | Description |
|-------|-------|-------------|
| Power Grid Outage Detected | ⚡ | The Pi rebooted between cron runs and is already back |
| Ongoing Outage Detected | 🔴 | The Pi is unreachable right now (type not known yet) |
| Power Restored | 🟢 | The Pi came back after being unreachable and had rebooted |
| Connectivity Restored | 🌐 | The Pi came back but never rebooted (internet/network disruption) |
| Outage Resolved | 🟢 | The Pi answered with an unusable response, type unknown |
| Pi Probably Stuck | 🟥 | Unreachable for over `MAX_BLACKOUT_DURATION` |
| Pi Back Online | 🟢 | The Pi is reachable again after being marked stuck, with its boot time |

### Stuck Pi

A Raspberry Pi 5 can stay in standby (red LED) when power returns for a split second during an outage and drops again. This is a hardware protection and cannot be configured; the Pi needs a power-cycle or a press of its power button. Blackouts that exceed `MAX_BLACKOUT_DURATION` are therefore closed with type `stuck` and no end time or duration, and no new blackout is opened until the Pi is seen again. A UPS, or a smart plug that powers on with a delay after an outage, prevents the problem.

## Database

The SQLite database has two tables:

- **`uptime_logs`**: every poll, with VPS timestamp, Pi uptime, computed boot time and reachability.
- **`blackouts`**: detected outages with start, end, duration and type (`power`, `internet`, `unknown` or `stuck`).

Outage start is the last successful poll, so durations are an upper bound that can include up to one cron interval.

```bash
# All blackouts
sqlite3 vps/blackout.db "SELECT * FROM blackouts ORDER BY started_at DESC;"

# Blackouts per month and type
sqlite3 vps/blackout.db "SELECT strftime('%Y-%m', started_at) AS month, blackout_type, COUNT(*) FROM blackouts GROUP BY month, blackout_type;"

# Average duration of power outages (stuck ones have no duration and are excluded)
sqlite3 vps/blackout.db "SELECT AVG(duration_seconds) FROM blackouts WHERE resolved = 1 AND blackout_type = 'power';"
```

To recompute type, end and duration of resolved blackouts from the stored uptime logs (dry run by default):

```bash
cd vps
python3 recompute_blackouts.py          # show changes
python3 recompute_blackouts.py --apply  # write them
```

## Development

Tests and linting run locally with `uv`; the project itself has no dependencies.

```bash
cd vps
uv run --no-project --with pytest pytest -q
cd ..
uvx ruff check vps pi
```

On a server without pytest: `cd vps && python3 -m unittest test_blackout_logic test_recompute_blackouts test_web_server`.

To preview the dashboard locally against a copy of the database: `DB_PATH=/path/to/blackout.db python3 vps/web_server.py` and open `http://127.0.0.1:8090/`.

## Project Structure

```
blackout-logger/
├── pi/
│   ├── uptime_server.py           # HTTP API exposing the Pi's uptime
│   ├── install.sh                 # Generates and installs the systemd service
│   └── restart.sh                 # Restarts the service
├── vps/
│   ├── blackout_checker.py        # Cron entry point: polling and outage detection
│   ├── config.py                  # .env loader and settings
│   ├── database.py                # SQLite schema, migrations and queries
│   ├── notifications.py           # Telegram messages
│   ├── recompute_blackouts.py     # One-off recomputation of resolved blackouts
│   ├── setup_cron.sh              # Cron installation helper
│   ├── web_server.py              # Read-only dashboard server and JSON API
│   ├── install_web.sh             # Installs the dashboard service and Tailscale Serve route
│   ├── web/                       # Dashboard frontend (HTML, CSS, JS, Archivo font)
│   ├── test_blackout_logic.py
│   ├── test_recompute_blackouts.py
│   └── test_web_server.py
├── .env.example                   # Configuration template
├── CLAUDE.md                      # Architecture and design notes
└── README.md
```

## License

MIT
