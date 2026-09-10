# Release notes

## 1.1.0 — 2026-09-09

- Add `request_sync` so chats can request the latest three calendar days of Garmin health and activity data, including new FIT archives.
- Return immediately with a durable job ID. Extend `get_sync_status` with queued/running/success/error status, timestamps, counts, and error types.
- Serialize scheduled, manual, and requested syncs; coalesce duplicate requests; apply a five-minute cooldown after completion or failure. Recover interrupted requests as errors after collector restart.
- Preserve the MCP database reader role and collector-only Garmin credentials. Add a separate `sync_requests` volume and correctly mark the new tool as mutating.
- Use the configured timezone for sync date boundaries. Preserve scheduled historical backfill before processing refresh requests.

Upgrade: update the repository and Compose configuration, set the existing `.env` image to `ghcr.io/cara-labs/garmin-health-mcp-gateway:1.1.0`, then run `docker compose pull` and `docker compose up -d`. Refresh ChatGPT's tool discovery (13 tools total). No database migration or credential reset is needed. Initial backfill must finish before queued requests run; the tool cannot force a wearable to upload data to Garmin Connect.
