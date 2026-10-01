"""One bounded, resumable local batch at a time, sharing the collector worker lock."""

from pathlib import Path

from .enrichment import ObservationStore
from .fit_decoder import DECODER_VERSION, SCHEMA_VERSION
from .fit_store import FitStore
from .sync_requests import SyncRequests


def archive_batch(
    database,
    requests: SyncRequests,
    *,
    activity_ids: list[int] | None = None,
    limit: int = 10,
    after_activity_id: int = 0,
    reprocess: bool = False,
) -> dict:
    if not 1 <= limit <= 100 or after_activity_id < 0:
        raise ValueError("Batch limit must be 1..100 and after activity id must be nonnegative")
    if activity_ids and (
        len(activity_ids) > 100 or any(type(v) is not int or v <= 0 for v in activity_ids)
    ):
        raise ValueError("Select at most 100 positive activity ids")
    with requests.lock("worker", blocking=False):
        pending = requests.status()
        if pending and pending["status"] in ("queued", "running"):
            return {
                "status": "yielded_to_sync",
                "results": [],
                "next_after_activity_id": after_activity_id,
            }
        with database.connection() as conn:
            rows = conn.execute(
                """SELECT a.garmin_activity_id,a.fit_file_path FROM activities a
                LEFT JOIN fit_active_generations f ON f.activity_id=a.garmin_activity_id
                LEFT JOIN fit_generations g ON g.id=f.generation_id
                WHERE a.fit_file_path IS NOT NULL AND a.garmin_activity_id>%s
                AND (%s::bigint[] IS NULL OR a.garmin_activity_id=ANY(%s::bigint[]))
                AND (%s OR g.id IS NULL OR g.decoder_version<>%s OR g.schema_version<>%s)
                ORDER BY a.garmin_activity_id LIMIT %s""",
                (
                    after_activity_id,
                    activity_ids,
                    activity_ids,
                    reprocess,
                    DECODER_VERSION,
                    SCHEMA_VERSION,
                    limit + 1,
                ),
            ).fetchall()
        results = []
        cursor = after_activity_id
        for row in rows[:limit]:
            pending = requests.status()
            if pending and pending["status"] in ("queued", "running"):
                return {
                    "status": "yielded_to_sync",
                    "results": results,
                    "next_after_activity_id": cursor,
                }
            cursor = row["garmin_activity_id"]
            result = FitStore(database).process(
                cursor, Path(row["fit_file_path"]), reprocess=reprocess
            )
            results.append(result)
            if result["status"] == "success":
                ObservationStore(database).attempt(
                    "weather_historical", cursor, "pending", "fit_generation_changed"
                )
        return {
            "status": "batch_complete",
            "results": results,
            "has_more": len(rows) > limit,
            "next_after_activity_id": cursor,
        }


def weather_batch(
    database,
    requests: SyncRequests,
    collector,
    *,
    limit: int = 1,
    activity_ids: list[int] | None = None,
) -> dict:
    if not 1 <= limit <= 10:
        raise ValueError("Weather batch limit must be 1..10")
    if activity_ids and (
        len(activity_ids) > 100 or any(type(v) is not int or v <= 0 for v in activity_ids)
    ):
        raise ValueError("Select at most 100 positive activity ids")
    with requests.lock("worker", blocking=False):
        pending = requests.status()
        if pending and pending["status"] in ("queued", "running"):
            return {"status": "yielded_to_sync", "results": []}
        with database.connection() as conn:
            rows = conn.execute(
                """SELECT a.garmin_activity_id FROM activities a
                JOIN fit_active_generations f ON f.activity_id=a.garmin_activity_id
                LEFT JOIN enrichment_state e ON e.resource='weather_historical'
                AND e.activity_key=a.garmin_activity_id
                WHERE (%s::bigint[] IS NULL OR a.garmin_activity_id=ANY(%s::bigint[]))
                AND (e.resource IS NULL OR (e.status IN ('pending','partial','error')
                AND (e.retry_after IS NULL OR e.retry_after<=now()))
                OR e.error_code='lookup_not_opted_in')
                ORDER BY e.last_attempt_at NULLS FIRST,a.garmin_activity_id LIMIT %s""",
                (activity_ids, activity_ids, limit),
            ).fetchall()
        results = []
        for row in rows:
            pending = requests.status()
            if pending and pending["status"] in ("queued", "running"):
                return {"status": "yielded_to_sync", "results": results}
            activity_id = row["garmin_activity_id"]
            results.append({"activity_id": activity_id, "status": collector.collect(activity_id)})
        return {"status": "batch_complete", "results": results}
