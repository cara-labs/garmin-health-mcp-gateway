# Activity analysis data contract

The new activity-analysis tools use the typed `activity-analysis/1` envelope.
Existing health/activity tools keep their response shapes. This document describes
the contract; individual tools become available as their implementation completes.

`availability` distinguishes available/partial evidence from not-found,
not-recorded, not-processed, unsupported, pending, and error states. `data` holds
measurements; absent measurements are explicit JSON nulls, never substituted with
zero. A recorded zero, false, or empty collection remains recorded evidence.

`fields` maps field paths (including documented repeated-sample paths) to units,
source IDs, availability, missing reasons, observation timestamps, and limitations.
Unknown units remain null with `unit_missing_reason`; use `dimensionless` only
when that is actually known. Repeated samples retain per-row missing information.
Mixed-source objects identify the source of each field rather than suggesting one
provider measured everything.

`sources` identifies recorded, modeled, derived, configured, or user-reported
evidence. Observation, collection, processing, and effective times are separate,
timezone-aware ISO-8601 timestamps, null when unknown. Collection time does not
establish when a setting became effective. `processing_version` identifies the
decoder/derivation used. Source IDs must be unique and field references valid.

`freshness.last_success_at` retains the last successful dataset even when
`last_attempt_at` is later and the refresh failed. `stale` may be null if staleness
cannot be determined; do not claim fresh evidence merely from a query timestamp.
`errors` contains machine-readable codes, safe messages, retryability, and attempt
timestamps. Messages must not expose credentials, precise locations, or feedback.

`coverage` and `limitations` disclose partial spatial/temporal/field evidence.
`next_cursor` signals another bounded page; null means no further page. NaN and
infinity are invalid JSON measurements: normalization must return null with an
explicit reason rather than silently create a zero.

For example, a recorded speed of `0` m/s with missing heart rate produces:

```json
{
  "data": {"speed": 0, "hr": null},
  "fields": {
    "speed": {"unit": "m/s", "source_ids": ["fit"], "availability": "available",
              "missing_reason": null},
    "hr": {"unit": "bpm", "source_ids": ["fit"], "availability": "not_recorded",
           "missing_reason": "not_recorded"}
  }
}
```

This abbreviated example omits the rest of the envelope and nullable metadata.

## FIT ingestion and archive preservation

The collector-side adapter reads the original archive without writing it. Every
decoded data message has an original ordinal, message identity, native/developer
field identifiers, raw/scaled decoded values, units, and null/missing information.
Duplicate timestamps do not share a primary key and remain distinct. Unknown
fields/messages retain their identifiers. Semicircle coordinates are additionally
normalized to degrees; original raw values remain in the field evidence. Enhanced
speed/altitude take precedence only in normalized fields, with the selected source
field named explicitly. Wearable temperature is not ambient weather.

Elapsed time is derived from recorded UTC timestamps and the stored activity
start. Missing timestamps remain unplaced; a prior sample's time is not reused.
Timer values require recorded start/stop evidence; absent state means unknown,
not elapsed time relabeled as active time. Planned FIT workout-step messages are
not evidence of executed step alignment.

Processing limits are 100 MiB per archive, 1,000,000 decoded data messages,
64 KiB serialized field evidence per message, 4,096 developer definition messages,
32 chained files, and 128 messages per staging transaction. Limit errors are
explicit (`archive_size_limit`, `message_count_limit`, `message_payload_limit`,
`developer_definition_limit`, `chained_file_limit`). CRC/parse failures also fail
processing; none publishes a truncated dataset.

Each generation records SHA-256, archive size, decoder/normalization versions,
attempt/completion times, status, count, and safe error code. Batches stage under a
per-activity database lock. The active-generation pointer is switched atomically
with the completed status only after strict decoding and a final archive identity
check. Failed replacements retain the previous successful generation. Repeating
an unchanged checksum/version is a no-op; explicit reprocessing creates a new
generation. Interrupted staging is marked as interrupted on restart and reread
from the original archive, never exposed as completed. Old/error generations are
retained as evidence; database storage grows and must be monitored.

Real PostgreSQL tests are opt-in via `GARMIN_TEST_DATABASE_URL` pointing to a
disposable instance, not production. Tests create/drop their own random databases.
The workload script `scripts/benchmark-fit-processing.py` generates original
synthetic data and measures actual staging runtime, peak RSS, and database size,
including retry and failed-limit replacement checks. Run with
`PYTHONPATH=src:tests` and the same disposable database URL.

## Suspected sensor/timing errors

Detector `hr-gps/1` labels suspicion, not diagnosed sensor failure. It flags HR
outside 20–250 bpm, changes greater than 35 bpm between adjacent original
samples 0–5 seconds apart (excluding zero time), incomplete/out-of-range GPS,
GPS displacement greater than 50 m/s over 0–30 seconds, and displacement over
100 m between identical timestamps. Longer gaps do not prove jumps. Missing,
pre-start, and decreasing timestamps are flagged as timing limitations.
Legitimate sport/motion may cross these conservative thresholds and stable but
wrong readings can evade them. There is no automatic filtering or repair:
original values remain in raw records and flagged samples contribute to default
summaries. Responses must identify that contribution and detector version.

## Stream queries and aggregation

`get_activity_streams(activity_id, start_seconds?, end_seconds?,
resolution_seconds=5, cursor?)` queries stored data only. Bounds are elapsed seconds
`[start,end)`; start is inclusive, end exclusive. Raw resolution 0 requires both
bounds and a window no longer than 1,800 seconds. Integer summary resolutions
1–3,600 seconds are supported. Default start is zero and end covers the last
placed sample. Buckets are anchored at elapsed zero and clipped to requested
bounds, not realigned to each request's start.

Each summary contains total samples, per-field valid counts/min/max/first/last,
methods, missing reasons, and quality reason counts. Physiological/speed/cadence/
power/dynamics values use arithmetic valid-record means (not time-weighted).
Distance/timer use last values with first/last evidence. Coordinates/altitude have
first/last evidence with no invented mean position. Empty buckets remain gaps;
there is no interpolation or forward fill across gaps/pauses. Suspected values
still contribute, with bounded example ordinals and reason counts.

Pages share a 2,000-item / 2 MiB serialized-data budget across streams, timer
events, and unplaced samples, plus a small metadata envelope. Continue with the
same activity/bounds/resolution and `next_cursor`; pages progress through streams,
then events, then samples with missing/pre-start timing. Unplaced samples cannot
be attributed to the selected time window and are explicitly separate. Timer
boundary evidence identifies the last event before the window; absence is unknown.
No events are silently discarded when the sample budget is exhausted.

Cursors are bound to activity, active processing generation, window, resolution,
and position. A changed generation produces a stale-cursor validation error;
restart from the first page. Unknown activity and not-processed/error states are
structured and do not turn absent datasets into zero measurements.

## Isolated ARM64 ingestion measurement

In a disposable Python 3.12.13/ARM64 container with a 512 MiB memory limit and an
isolated PostgreSQL 17 database, 50,000 original generated one-second record
samples (1,850,038 archive bytes) processed in 87.747 seconds. Peak process RSS,
including fixture generation, was 48,820,224 bytes (46.6 MiB). The database was
141,831,315 bytes including indexes and PostgreSQL overhead. This is a synthetic
workload, not a claim of uniform performance for every watch/file/device. Fifty-
eight tests passed in that snapshot. Unchanged-file retry returned `unchanged`;
an explicit record-limit failure retained the active successful generation.
Later changes still require the final full installation/regression verification.

## Laps, workout steps, and calculated splits

`get_activity_laps(activity_id, cursor?)` returns separate `recorded_laps`,
`planned_steps`, `executed_steps`, and `calculated_splits` collections. Manual,
time, and distance triggers identify manual/automatic evidence; other or missing
triggers remain unknown rather than guessed. Executed step associations use a
recorded `wkt_step_index` only; the presence of a plan does not prove execution.
Pace is derived from a positive recorded average speed, never from a missing zero.

`km-splits/1` walks cumulative distance in original order. Kilometer boundary
times are linearly interpolated only between adjacent usable distance/time
samples; derived boundaries are labeled. Timer duration and pause seconds require
timer evidence on both endpoints. A final shorter segment is labeled partial.
Missing distance/time, resets, identical timestamps with changing distance, or
unobserved movement gaps over 30 seconds end reliable derivation with an explicit
limitation record; no unsupported continuity is invented. Leading unrecorded
distance is disclosed. Calculated splits are never relabeled as recorded laps.

Both detail tools share bounded 2,000-item / 2 MiB data pages with generation-
bound cursors. Follow `next_cursor` with the same activity/tool. Metadata is
additional to the data budget. Changed generations invalidate old cursors.

## Additional FIT metrics and numeric recovery HR

`get_activity_fit_metrics(activity_id, cursor?)` returns additional recorded
session, device/sensor, event and developer evidence. Original field numbers,
native/developer identity, raw/scaled values, units, and decoder version remain
visible. Unknown developer meanings/units stay unknown. Unnecessary serials,
personal device names, user IDs and credential fields are omitted from responses.

`recorded_recovery_hr` is null with a not-recorded reason when no recovery-HR
event exists; recovery-hours recommendations are never substituted. When a FIT
event stores recovery HR, its `value` is numeric and unchanged (including a
recorded zero), with the original event timestamp when present. The number's
interpretation/units remain unknown if the decoder supplies no semantics. A
baseline, final HR, interval, or two-minute protocol is not inferred. For example:

```json
{"value":120,"observed_at":"2026-08-20T12:00:00Z",
 "meaning":"uninterpreted_recorded_recovery_hr_payload","unit":null,
 "baseline_bpm":null,"recovery_bpm":null,"interval_seconds":null}
```

This confirmed behavior exposes recorded evidence as-is, not a medical conclusion.
User-reported protocol belongs to feedback provenance, separately from FIT data.

## Revisioned user feedback

`save_activity_feedback(activity_id, feedback, idempotency_key, expected_revision?)`
appends a complete immutable subjective snapshot. Breathing effort, leg effort,
overall RPE, and pain severity are independently rated 0–10. Heaviness onset uses
elapsed seconds. Fueling carbohydrate and hydration amounts use grams and mL;
optional narrative fields retain context. Recovery protocol readings/timing are
explicitly user-reported and never substituted for FIT evidence.

Every optional field is unknown if omitted, not zero/no pain and not inherited
from an earlier revision. A correction starts by reading the latest snapshot,
then submits its complete corrected payload with a new idempotency key and the
last `expected_revision`. Identical key/payload retries return the original
revision even if other revisions were added. Different payloads under the same
key conflict; a stale expected revision conflicts. For example, after revision 1:

```json
{"activity_id":42,"feedback":{"breathing_effort":3,"leg_effort":6},
 "idempotency_key":"correction-2","expected_revision":1}
```

`get_activity_feedback` returns latest by default, accepts `revision`, and offers
50-revision ascending history pages with `include_history=true`. History cursors
bind the activity and initial upper revision, so later appends do not shift pages.
Unknown activity and no feedback are separate availability states.

The MCP query role remains transaction-read-only. A separate writer role has
execution permission only on `append_activity_feedback`, not table access. The
security-definer function fixes its search path to `pg_catalog`, fully qualifies
tables, revokes public execution, validates input/activity, and serializes revision
allocation transactionally. MCP gets no collector secrets or archive mounts.

New installations generate `secrets/mcp_feedback_password.txt` through
`scripts/setup-secrets.sh`. For an upgrade, create only that new secret without
changing the existing database/reader passwords:

```sh
./scripts/setup-secrets.sh --feedback-only
docker compose run --rm migrate
```

The feedback-only mode preserves existing credentials and an existing feedback
secret. Use a tested application
image containing this migration; publishing/deployment is a separate operation.
`garmin-health provision-feedback` separately provisions the execution-only role;
normal `provision-reader` also provisions it when the new secret is supplied.

## Weather and training settings

`get_activity_weather` is a stored-data query: it never calls Garmin or a weather
provider. Recorded Garmin observations, ERA5 modeled estimates, and FIT wearable
temperature stay separate. The pinned Garmin weather endpoint does not supply
units in its native payload; numeric values remain unchanged and units explicitly
unknown. A station identifier does not establish station coordinates/distance.
Wearable temperature is not ambient weather. Neither cloud cover nor shortwave
radiation measures personal sun/shade exposure.

Historical lookup is opt-in (`GARMIN_HISTORICAL_WEATHER=true`). This discloses
sampled coordinates and activity hours to Open-Meteo, not user identity, account
credentials, or the complete route. The selected free endpoint is for
non-commercial use; consult provider terms before any commercial deployment.
Data attribution: Open-Meteo / ECMWF ERA5, CC BY 4.0. ERA5 coverage begins in 1940,
is hourly and approximately 25 km, and recent activity dates wait five days.
These are coarse modeled conditions, not observations at every route location.

Each activity selects start/end and up to eight additional elapsed-time-stratified
valid coordinate samples (ten total). Times match the nearest UTC hour. Requested
coordinates (rounded to five decimals), original route timestamps/ordinals and
quality flags, returned grid coordinates/distance and provider timestamps remain
visible. No coordinate averaging is used. Indoor/no-route activities and invalid
coordinates have explicit missing-location evidence; original FIT data is intact.
The cache key includes ERA5 integration version, location and hour. `requested_point`
is the original cache request; `matched_route_points` identifies the querying
activity's own samples (including reuse across activities). Successful
point results survive later failures and are reused across retries/activities.
Provider failures stop remaining requests, record partial/error status and defer
retry for one hour. Unsupported dates remain explicit; recent dates retry daily.
Model summaries use means of compatible-unit, non-null values over matched route
points, not continuous-time weighting. Wind direction uses a circular mean;
opposing directions have no defined mean. Missing/unexpected units are never
silently converted. Nulls stay null and recorded zeros contribute normally.

`get_training_profile` returns stored immutable snapshots with separate running,
general, and other sport scopes. Zone floors, configured maximum/resting/threshold
HR and methods are returned only as supplied; ceilings, unknown methods and
age/activity-derived maxima are not invented. Daily measured resting HR is a
separate source with date evidence. Unscoped profile thresholds are labeled
unscoped. Retrieval time is not configuration effective time, so the latest
settings must not be assumed to apply to older runs. Failed/unsupported refreshes
retain the last good snapshot with stale/error evidence. Raw source payloads stay
in collector storage; the tool returns only training-related fields.

## Local backfill and independent sync

Scheduled and chat-requested sync refresh optional profile settings and decode
stored activity FIT files after core ingestion, then collect recorded weather.
Enrichment exceptions cannot turn successful core health/activity sync into a
failure. Optional auth/rate-limit errors suspend further optional Garmin calls in
that cycle. `get_sync_status.analysis` adds grouped FIT/enrichment states; existing
`resources` and `ad_hoc` contracts are unchanged.

These commands use local storage and do **not** authenticate with Garmin:

```sh
docker compose run --rm collector process-archives --limit 10
docker compose run --rm collector process-archives --limit 10 --after-activity-id 42
docker compose run --rm collector process-archives --activity-id 42 --reprocess
docker compose run --rm collector backfill-weather --limit 1
docker compose run --rm collector backfill-weather --activity-id 42 --limit 1
```

Archive batches accept up to 100 selected IDs/100 files. JSON output includes
per-file progress, `has_more`, and the next ID cursor. Resume with the cursor;
rerun without a cursor to retry failed files. Current successful decoder/schema
generations are skipped unless `--reprocess` is specified, which also handles an
explicitly replaced archive. Restarted processing republishes atomically and never
duplicates active records. Originals are not changed. Weather batches allow 1..10
activities; the default/scheduler budget is one, at most ten point requests, ten
seconds per request and two seconds spacing (roughly two minutes worst-case per
activity). Batches release the existing worker lock and yield to queued sync before
each next activity/file. One in-flight activity is not interrupted. No extra
scheduler/worker concurrency is introduced. Archive publication makes historical
weather pending again while retaining earlier cached evidence.

## Final isolated ARM64 validation

On 2026-10-01 UTC, the activity-analysis Docker build passed real Streamable HTTP
initialization, exact 20-tool discovery and every tool call using synthetic data.
The fixture's recorded recovery-HR value remained numeric `120` after protocol
serialization. Feedback retry preserved its revision, and weather/profile missing
values remained distinct from zeros. The same migrated database then passed all
13 tool calls using the old published `1.1.0` image, without removing analysis data.
Both installation smoke runs cleaned their own test containers/volumes.

All 112 tests passed on ARM64 (14.52 seconds). The complete suite uses an
independent PostgreSQL 17.6 instance and random
disposable databases/roles. Tests cover fresh/upgrade/repeated migrations, writer
privilege denial, concurrent revisions, last-good evidence, corrupt/interrupted
FIT generations, route/date coverage, queue cooldown/serialization, and bounded
backfill. Production Garmin credentials, sessions and health data are not used.

A final 50,000-sample original synthetic fixture (1,850,038 bytes) processed in
92.161 seconds with one CPU and a 512 MiB container limit. Peak RSS for processing
was 56,807,424 bytes (54.2 MiB); including bounded stream queries it was 76,099,584
bytes (72.6 MiB). PostgreSQL occupied 155,667,603 bytes including indexes/overhead.
The default summary page held 715 rows/2,102,490 bytes including metadata; the
selected original-resolution page held 686 rows/2,102,008 bytes. Both returned
cursors rather than silently truncating the result. Unchanged-file retry was a
no-op; an explicit record-limit failure retained the active successful generation.
The benchmark database was removed successfully. These are synthetic workload
measurements, not a promise of runtime or database size for every activity.

The new code has **not** been published or deployed to production. The currently
pinned public `1.1.0` image lacks the new tools, so the updated published-image
installation path remains pending an approved release-candidate publication.
