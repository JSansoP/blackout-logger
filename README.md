# ⚡ Blackout Logger

A lightweight, zero-dependency power outage detection system using a Raspberry Pi as a sensor and a VPS as the monitor.

## How It Works

```
┌─────────────────┐          ┌─────────────────────┐
│  Raspberry Pi   │◄─────────│     VPS (cron)       │
│  (at home)      │  HTTP    │                      │
│                 │  GET     │  blackout_checker.py  │
│  uptime_server  │─────────►│  ┌──────────────┐    │
│  port 8080      │  JSON    │  │  SQLite DB    │    │──── Telegram
│                 │          │  │  ┌──────────┐ │    │     Notifications
└─────────────────┘          │  │  │ uptimes  │ │    │
     Tailscale               │  │  │ blackouts│ │    │
                             │  │  └──────────┘ │    │
                             │  └──────────────┘    │
                             └─────────────────────┘
```

1. **Raspberry Pi** runs a tiny HTTP server that returns the system's uptime (read from `/proc/uptime`)
2. **VPS** runs a cron job every 10 minutes (default, configurable) that polls the Pi
3. If the Pi's uptime is lower than expected (it rebooted), a **blackout** is recorded
4. If the Pi is **unreachable**, the script enters a fast retry loop (every 60s) to pinpoint when power returns
5. **Telegram notifications** are sent for all blackout events

## Prerequisites

- Python 3.7+ on both machines (no pip packages needed!)
- Both machines connected via [Tailscale](https://tailscale.com/)
- A Telegram bot (create one via [@BotFather](https://t.me/BotFather))

## Setup

### 1. Raspberry Pi

Clone the repo on the Pi:

```bash
git clone https://github.com/JSansoP/blackout-logger.git ~/projects/blackout-logger
cd ~/projects/blackout-logger
```

Run the install script — it auto-detects your user and project path, generates the systemd service, and starts it:

```bash
bash pi/install.sh
```

Verify it's running:

```bash
curl http://localhost:8080/uptime
# {"uptime_seconds": 1234.56, "boot_time": "2026-05-18T...", "hostname": "raspberrypi", ...}
```

### 2. VPS

Copy the `vps/` directory to your VPS:

```bash
scp -r vps/ user@<VPS_IP>:~/projects/blackout-logger/
```

Create the configuration:

```bash
cd ~/projects/blackout-logger/vps
cp ../.env.example .env
# Edit .env with your values:
nano .env
```

Required `.env` values:

| Variable | Description | Example |
|----------|-------------|---------|
| `PI_ADDRESS` | Tailscale IP of your Pi | `100.64.0.5` |
| `PI_PORT` | Port the uptime server listens on | `8080` |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | `123456:ABC-DEF...` |
| `TELEGRAM_CHAT_ID` | Your chat/channel ID | `-1001234567890` |

Test the checker manually:

```bash
python3 blackout_checker.py
```

Install the cron job (accepts an optional argument for interval in minutes, default is 10):

```bash
bash setup_cron.sh [minutes]
```

Or manually:

```bash
crontab -e
# Add this line (runs every 10 minutes):
*/10 * * * * cd /path/to/blackout-logger && python3 blackout_checker.py >> /var/log/blackout_checker.log 2>&1
```

### 3. Telegram Bot Setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token to your `.env` file
4. Create a new channel or group for notifications
5. Add your bot as an admin to the channel
6. Get the chat ID:
   - Send a message in the channel
   - Visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
   - Find the `chat.id` value (it will be negative for channels)
7. Add the chat ID to your `.env` file

## Notifications

The system distinguishes between actual power outages (Pi rebooted) and network issues (Pi unreachable but uptime didn't reset). There are four types of Telegram notifications:

| Event | Emoji | Description |
|-------|-------|-------------|
| Power Outage Detected | ⚡ | Blackout happened between cron runs, Pi is already back |
| Outage In Progress | 🔴 | Pi is currently unreachable, monitoring every 60s (type unknown yet) |
| Power Restored | 🟢 | Pi came back online after being unreachable and had rebooted |
| Connectivity Restored | 🌐 | Pi came back online but never rebooted (internet/network disruption) |

## Database

The SQLite database (`blackout.db`) has two tables:

- **`uptime_logs`** — Every poll attempt with timestamp, uptime, and reachability
- **`blackouts`** — Detected power outages with start/end times and duration

Query examples:

```bash
# View all blackouts
sqlite3 blackout.db "SELECT * FROM blackouts ORDER BY started_at DESC;"

# View blackouts from the last 30 days
sqlite3 blackout.db "SELECT * FROM blackouts WHERE started_at > datetime('now', '-30 days') ORDER BY started_at DESC;"

# Count blackouts per month
sqlite3 blackout.db "SELECT strftime('%Y-%m', started_at) as month, COUNT(*) as count FROM blackouts GROUP BY month;"

# Average blackout duration
sqlite3 blackout.db "SELECT AVG(duration_seconds) as avg_seconds FROM blackouts WHERE resolved = 1;"
```

## Project Structure

```
blackout-logger/
├── pi/
│   ├── uptime_server.py          # Lightweight HTTP API for the Pi
│   └── install.sh                # Generates & installs the systemd service
├── vps/
│   ├── blackout_checker.py       # Main cron script
│   ├── config.py                 # Configuration loader
│   ├── database.py               # SQLite database layer
│   ├── notifications.py          # Telegram notifications
│   └── setup_cron.sh             # Cron installation helper
├── .env.example                  # Configuration template
└── README.md
```

## License

MIT
