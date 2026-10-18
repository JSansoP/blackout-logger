# Blackout Logger — Notes for Claude

Power outage detector: a Raspberry Pi 5 at home exposes its uptime, a VPS polls it from cron, records outages in SQLite and notifies via Telegram. Keep this file and `README.md` updated whenever architecture, behavior, configuration or deployment changes.

## Architecture

- `pi/uptime_server.py`: stdlib `http.server` with `GET /uptime` and `GET /health`. Stateless sensor; runs as the `blackout-uptime` systemd service.
- `vps/blackout_checker.py`: cron entry point (every 10 min). `main()` polls once and routes to one of:
  - `handle_reachable(uptime_seconds)`: logs the poll, resolves an open blackout, or detects a reboot between polls.
  - `handle_unreachable()`: opens a blackout (or continues the open one) and runs a retry loop every `RETRY_INTERVAL` s for at most `MAX_RETRY_DURATION` s.
  - `handle_invalid_response(error)`: the Pi answered but the data is unusable.
- `vps/database.py`: SQLite (WAL), tables `uptime_logs` and `blackouts`, schema plus migrations in `init_db()`.
- `vps/notifications.py`: Telegram Bot API via `urllib`.
- `vps/config.py`: reads `.env` from the project root (not `vps/`); existing environment variables win.
- `vps/recompute_blackouts.py`: one-off recomputation of resolved blackouts; dry run unless `--apply`.

## Design decisions

- **Zero dependencies.** Stdlib only on both machines, so deployment is `git pull`. No `pyproject.toml`; `uv` is used only to run pytest/ruff locally. Minimum Python is 3.11 (`datetime.UTC`).
- **Only the VPS clock is trusted.** The Pi has no RTC battery and `fake-hwclock` restores a stale time after power cuts, so the Pi's `boot_time`/`timestamp` fields are ignored. Boot time is always `VPS poll time - uptime_seconds` (`_calculate_boot_time`). Mixing Pi and VPS clocks caused the historical "0 seconds" and wrong-timestamp bugs.
- **Reboot detection.** One helper, `_pi_rebooted`: boot time moved by more than `REBOOT_THRESHOLD_SECONDS` (30). `_determine_blackout_type` builds on it: `power` (rebooted), `internet` (not rebooted), `unknown` (missing data). Do not add parallel heuristics in individual code paths.
- **Read previous state before writing.** In `handle_reachable`, the open blackout and last successful log are fetched before inserting the current poll; otherwise the comparison is against itself.
- **Outage timing.** Start is the last successful poll (an upper bound; can overstate by up to one cron interval). End is the Pi's boot time for `power` and the recovery poll time otherwise. `_outage_end` computes both; if the end would precede the start it logs a warning and falls back to the poll time, never a silent clamp to 0.
- **Three poll outcomes.** `poll_pi()` returns uptime (float), `None` for unreachable (`URLError`/`OSError`, including timeouts), or raises `InvalidPiResponse` for HTTP errors and bad payloads (missing, null, non-numeric, negative, NaN uptime). An invalid response means the Pi is online: it never opens a blackout and resolves an open one as `unknown`.
- **Stuck Pi.** A Pi 5 can stay in standby (red LED) after a sub-second power blip during an outage; hardware behavior, not configurable. Once an open blackout exceeds `MAX_BLACKOUT_DURATION` (default 8 h) it is resolved as type `stuck` with `ended_at` and `duration_seconds` NULL. While the latest blackout is `stuck` and no successful poll exists after its `started_at`, unreachable runs only log the poll (no new blackout, no retry loop), and the first successful poll sends a single "back online" notification instead of creating a between-crons power record.
- **Concurrency.** Cron runs can overlap with a long retry loop; a PID lock file (`LOCK_FILE`) makes later runs exit. A run that dies leaves the blackout open and the next run continues it.
- **Migrations.** Column additions are idempotent `ALTER TABLE` statements with `OperationalError` suppressed. Data migrations are versioned with `PRAGMA user_version` (v1: recompute all boot times from the VPS clock). Add new data migrations as the next version.
- **History rewrites go through scripts** with a dry-run default (`recompute_blackouts.py`), never silent migrations. The recompute skips `stuck` blackouts.

## Conventions

- Tests: `unittest`-style classes run with pytest (`cd vps && uv run --no-project --with pytest pytest -q`). `DatabaseTestCase` gives each test a temp DB. Tests patch `blackout_checker._now_iso`, `blackout_checker.poll_pi`, `time.sleep` and `notifications.*`; never hit the network.
- Lint: `uvx ruff check vps pi` (user-level ruff config, stricter than defaults). The two catch-all `except Exception` blocks (Telegram send, Pi HTTP handler) are deliberate and marked `# noqa: BLE001`.
- Telegram messages use emojis by design; logs, comments and docstrings do not.
- The repo uses `core.autocrlf`; LF/CRLF warnings on commit are expected.

## Deployment

- VPS: `root@personal-vps`, repo at `/root/projects/blackout-logger`, `.env` at the repo root, DB `vps/blackout.db`, log `vps/blackout_checker.log`. Cron: `*/10 * * * * cd .../vps && /usr/bin/python3 .../vps/blackout_checker.py >> .../vps/blackout_checker.log 2>&1`. Deploy with `git pull`; the next cron run picks it up. Back up `blackout.db` before data changes.
- Pi: `sanso@pi-nas`, repo at `/home/sanso/projects/blackout-logger`, service `blackout-uptime`. Restart only when `pi/` changes: a poll during the restart opens a short false `internet` blackout.

## Known pitfalls

- **Tailscale key expiry on the VPS** shows up as `[Errno 101] Network is unreachable` for every poll and looks like a never-ending outage (Sep 23 to Oct 12, 2026). Key expiry is disabled on both nodes; check `tailscale status` first when every poll fails that way.
- **Pi logs do not survive power cuts.** OpenMediaVault's `folder2ram` keeps `/var/log` in RAM, so the journal of a boot that lost power ends a few seconds in. Use the VPS data, or the speedtest-logger DB on the Pi, to reconstruct what happened.
- **The VPS is IPv6-only**; outbound access to IPv4-only hosts goes through a NAT64-style workaround.
