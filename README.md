# Garmin Health Gateway

A production-oriented Garmin health gateway for 64-bit Docker hosts, with read-only data queries and on-demand synchronization. It is optimized for Raspberry Pi but also runs on Linux servers, NAS devices, mini PCs, cloud hosts, and Docker Desktop. It incrementally collects normalized health and activity data into PostgreSQL, preserves each original FIT activity file, and exposes semantic MCP tools to ChatGPT and Codex through an outbound-only OpenAI Secure MCP Tunnel.

Versioned application images are published to `ghcr.io/cara-labs/garmin-health-mcp-gateway` for `linux/amd64` and `linux/arm64`. Releases include build provenance and an SBOM. Contributors can still build the image locally with `compose.build.yaml`.

The gateway code in this repository was written independently. It uses the third-party, MIT-licensed `garminconnect` Python package as a runtime dependency; that package is installed from PyPI and its source is not copied into this repository. Our `GarminProvider` adapter is the only layer that calls it. Pinning version `0.3.11` prevents an untested update from being installed automatically, and the adapter converts failures into understandable sync errors while keeping the database and MCP layers independent of that dependency. Garmin Connect remains an unofficial and potentially changing personal-data interface.

## Architecture

```text
Garmin Connect ──HTTPS──> collector ──> PostgreSQL (internal network only)
                              │
                              └────────> FIT archive volume

ChatGPT phone <── OpenAI ── outbound Secure MCP Tunnel <── MCP queries <── PostgreSQL
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
- `request_sync()`

The 12 query tools remain read-only. `request_sync` is annotated as mutating, non-destructive, idempotent, and open-world because it requests Garmin downloads and local database updates. There is no raw SQL tool or Garmin account mutation tool.

Ask in chat: “Sync my latest Garmin health and activities, wait for completion, then summarize today's recovery.” `request_sync` immediately returns a request ID and `queued` status. Check `get_sync_status().ad_hoc` until `success` or `error`, then query the refreshed data. A request refreshes today and the preceding two calendar days in the configured timezone, including FIT files for new activities. It cannot make the watch upload to Garmin Connect.

The collector checks for requests every two seconds while idle. Requests wait behind an active scheduled sync or unfinished initial backfill. Duplicate queued/running requests return the same ID; requests within five minutes of completion/failure return a cooldown and retry interval. Status includes timestamps, per-resource counts, and error types. Interrupted jobs are marked failed after collector restart. An offline collector leaves the request queued; queued is never a success indication.

Requests use a dedicated local `sync_requests` Docker volume; the MCP database role still has only SELECT privileges and no Garmin credentials. Collector and manual CLI syncs share a process lock to prevent concurrent Garmin requests.

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

Back up the three data volumes: `postgres_data`, `fit_archive`, and `garmin_tokens`. The additional `sync_requests` volume stores only the latest refresh job and lock files; it can be recreated if losing pending job status is acceptable. Encrypt backups. The token volume grants long-lived Garmin access and is as sensitive as a password. A logical PostgreSQL dump plus a filesystem copy of the FIT archive is the most portable backup.

Do not restore PostgreSQL by copying a live data directory. Stop writes and use `pg_dump`/`pg_restore`, or use a snapshot mechanism designed for Docker volumes.

## Upgrades

Review Garmin client release notes before changing the `garminconnect` pin. Run tests and a manual sync after an upgrade because Garmin endpoints are unofficial. Pin the exact `TUNNEL_CLIENT_IMAGE` tag in `.env`; review official tunnel-client releases before changing it.

Health and training interpretations are informational, not medical advice. Seek professional care for symptoms or health concerns.

## Licensing

The original gateway code and documentation are licensed under Apache License 2.0. Third-party dependencies retain their own licenses and are not relicensed as Apache 2.0. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the release SBOM for the dependency inventory.
