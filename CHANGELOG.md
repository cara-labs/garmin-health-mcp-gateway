# Release notes

## 1.2.0-rc.1 — activity analysis prerelease

- Add seven tools (20 total): stored streams, laps/splits, FIT metrics, feedback
  save/read, weather, and configured training profiles. Existing 13 tools remain.
- Preserve original FIT files/native/developer fields, duplicate samples, recorded
  zeros and explicit missing values. Default streams summarize five seconds;
  selected windows support original resolution and generation-bound pagination.
- Flag suspected HR/GPS errors without deleting values. Keep recorded laps and
  planned/executed steps separate from derived kilometer splits.
- Expose recorded recovery-HR payloads numerically and unchanged, with timestamp;
  do not invent units/meaning, baseline/final readings, intervals or protocols.
- Add immutable feedback revisions, idempotent saves and optimistic conflicts
  using an execution-only writer role/secret. The query role remains read-only.
- Store separate recorded weather, opt-in historical ERA5 estimates and wearable
  temperature; report cache coverage, attribution, circular wind summaries and
  missing personal exposure. Keep running/general profile snapshots separate from
  measured daily resting HR and retain last good evidence after refresh failure.
- Integrate optional enrichment independently of core sync; add no-login bounded
  archive/weather backfill commands, queue priority and additive analysis status.
- Add migrations 002–004 and isolated protocol/privilege/ARM64 tests. The default
  production image remains `1.1.0`. The approved candidate publishes as
  `ghcr.io/cara-labs/garmin-health-mcp-gateway:1.2.0-rc.1` for ARM64/AMD64 without
  changing stable/`latest` tags. Live deployment requires separate approval.
  See `INSTALL.md` for new-secret setup,
  migration/backfill, tool discovery and non-destructive rollback.

## 1.1.0 — 2026-09-09

- Add `request_sync` so chats can request the latest three calendar days of Garmin health and activity data, including new FIT archives.
- Return immediately with a durable job ID. Extend `get_sync_status` with queued/running/success/error status, timestamps, counts, and error types.
- Serialize scheduled, manual, and requested syncs; coalesce duplicate requests; apply a five-minute cooldown after completion or failure. Recover interrupted requests as errors after collector restart.
- Preserve the MCP database reader role and collector-only Garmin credentials. Add a separate `sync_requests` volume and correctly mark the new tool as mutating.
- Use the configured timezone for sync date boundaries. Preserve scheduled historical backfill before processing refresh requests.

Upgrade: update the repository and Compose configuration, set the existing `.env` image to `ghcr.io/cara-labs/garmin-health-mcp-gateway:1.1.0`, then run `docker compose pull` and `docker compose up -d`. Refresh ChatGPT's tool discovery (13 tools total). No database migration or credential reset is needed. Initial backfill must finish before queued requests run; the tool cannot force a wearable to upload data to Garmin Connect.
