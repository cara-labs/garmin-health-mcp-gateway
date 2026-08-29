# Garmin Health Gateway

A production-oriented, read-only Garmin health gateway for 64-bit Docker hosts. It is optimized for Raspberry Pi but also runs on Linux servers, NAS devices, mini PCs, cloud hosts, and Docker Desktop. It incrementally collects normalized health and activity data into PostgreSQL, preserves each original FIT activity file, and exposes semantic MCP tools to ChatGPT and Codex through an outbound-only OpenAI Secure MCP Tunnel.

Garmin Connect is not a stable official personal-data API. This project isolates the current `garminconnect` client behind `GarminProvider`, pins the tested client release, retains understandable sync errors, and keeps database/MCP code independent of that library.

## Architecture

```text
Garmin Connect ──HTTPS──> collector ──> PostgreSQL (internal network only)
                              │
                              └────────> FIT archive volume

ChatGPT phone <── OpenAI ── outbound Secure MCP Tunnel <── read-only MCP <── PostgreSQL
```

The MCP server never receives Garmin credentials, Garmin tokens, or FIT-file access. Its separate PostgreSQL role has only `SELECT` privileges and database-enforced read-only transactions. PostgreSQL has no published port. The Docker host needs outbound HTTPS but no inbound firewall or router port.

## Docker host requirements

- A 64-bit `arm64` or `amd64` host. Raspberry Pi deployments should use a 64-bit OS.
- Docker Engine with Compose v2
- At least 2 GB RAM and sufficient durable storage for PostgreSQL plus FIT files
- A ChatGPT account/workspace with developer-mode app access and OpenAI Platform Secure MCP Tunnel permissions

Docker Desktop on macOS or Windows is suitable for development. On Windows, run the POSIX setup script through WSL or Git Bash. A native Linux Docker host is recommended for continuous production operation.

## Installation

Follow the complete [installation guide](INSTALL.md). It covers Docker prerequisites, OpenAI tunnel permissions, local secret creation, Garmin MFA, service verification, ChatGPT web setup, phone access, upgrades, and troubleshooting.

The first run backfills 90 days of daily health and 365 days of activities. With the default request delay this can take a while by design. Later cycles re-fetch the latest three days every hour to pick up overnight and post-activity Garmin corrections.

If Garmin has no original FIT file for a manual/imported activity, the activity is still stored with `fit_download_status=error` and an understandable error instead of blocking the entire collector. A manual backfill retries it.

## MCP tools

- `get_daily_health(date)`
- `get_health_range(start_date, end_date)`
- `get_today_readiness()`
- `get_recent_runs(days=30)`
- `get_recent_activities(days=30, activity_type=null)`
- `get_activity(activity_id)`
- `get_vo2max_history(days=180)`
- `get_hrv_history(days=90)`
- `get_sleep_history(days=30)`
- `get_resting_hr_history(days=90)`
- `get_training_load(days=28)`
- `get_sync_status()`

Every tool is annotated `readOnlyHint=true`, `destructiveHint=false`, and `openWorldHint=false`. There is no SQL tool and no mutation tool.

## Operations

Run an immediate incremental sync:

```bash
docker compose exec collector garmin-health sync
```

Repeat the configured historical windows manually:

```bash
docker compose exec collector garmin-health sync --backfill
```

Inspect status and understandable errors:

```bash
docker compose logs --since=2h collector
docker compose exec collector garmin-health check --max-age-hours 3
```

Test locally with MCP Inspector without publishing MCP on the LAN:

```bash
docker compose -f compose.yaml -f compose.local.yaml up -d mcp
npx @modelcontextprotocol/inspector@latest
```

Choose Streamable HTTP and `http://127.0.0.1:8000/mcp`. Stop using the local override after testing.

## Backups and restore

Back up all three persistent volumes: `postgres_data`, `fit_archive`, and `garmin_tokens`. Encrypt backups. The token volume grants long-lived Garmin access and is as sensitive as a password. A logical PostgreSQL dump plus a filesystem copy of the FIT archive is the most portable backup.

Do not restore PostgreSQL by copying a live data directory. Stop writes and use `pg_dump`/`pg_restore`, or use a snapshot mechanism designed for Docker volumes.

## Upgrades

Review Garmin client release notes before changing the `garminconnect` pin. Run tests and a manual sync after an upgrade because Garmin endpoints are unofficial. Pin the exact `TUNNEL_CLIENT_IMAGE` tag in `.env`; review official tunnel-client releases before changing it.

Health and training interpretations are informational, not medical advice. Seek professional care for symptoms or health concerns.
