from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from .activity_details import ActivityDetails
from .config import Settings
from .db import Database
from .enrichment import get_training_profile as read_training_profile
from .feedback import ActivityFeedback, get_feedback, save_feedback
from .streams import ActivityStreams
from .sync_requests import SyncRequests
from .weather import get_activity_weather as read_activity_weather

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
INSTRUCTIONS = (
    "Query one user's locally stored Garmin health and activity data. "
    "When asked to refresh or sync, call request_sync, then check get_sync_status. "
    "A queued request is not completion; wait for ad_hoc.status=success before claiming freshness. "
    "Use readiness, sleep, HRV, resting heart rate, training load, and recent activity "
    "context together. Do not present training advice as medical diagnosis. Empty fields "
    "usually mean the wearable or Garmin did not supply that metric."
)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def database() -> Database:
    return Database(settings().database_url, min_size=1, max_size=8)


@lru_cache(maxsize=1)
def feedback_writer() -> Database:
    if not settings().feedback_database_url:
        raise RuntimeError(
            "Configure the separate MCP feedback writer secret and provision its role"
        )
    return Database(settings().feedback_database_url, min_size=1, max_size=2)


mcp = FastMCP(
    "Garmin Health Gateway",
    instructions=INSTRUCTIONS,
    host="0.0.0.0",
    port=8000,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["mcp:8000", "localhost:*", "127.0.0.1:*"],
        allowed_origins=["http://mcp:8000", "http://localhost:*", "http://127.0.0.1:*"],
    ),
)


def _days(value: int, maximum: int = 730) -> int:
    if not 1 <= value <= maximum:
        raise ValueError(f"days must be between 1 and {maximum}")
    return value


@mcp.tool(title="Get activity streams", annotations=READ_ONLY)
def get_activity_streams(
    activity_id: Annotated[int, Field(gt=0, strict=True)],
    start_seconds: Annotated[float | None, Field(ge=0, allow_inf_nan=False)] = None,
    end_seconds: Annotated[float | None, Field(gt=0, allow_inf_nan=False)] = None,
    resolution_seconds: Annotated[int, Field(ge=0, le=3600, strict=True)] = 5,
    cursor: Annotated[str | None, Field(max_length=2048)] = None,
) -> dict[str, Any]:
    """Read stored FIT streams, defaulting to five-second evidence summaries.

    Bounds use elapsed seconds [start,end). Resolution 0 returns original samples
    for an explicit window of at most 1800 seconds. Follow next_cursor for all
    pages, including timer events and unplaced samples. Flagged values are not
    filtered. Missing FIT processing is not evidence of no activity measurements.
    This query never downloads, decodes, or calls Garmin/weather services.
    """
    return ActivityStreams(database()).get(
        activity_id,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        resolution_seconds=resolution_seconds,
        cursor=cursor,
    )


@mcp.tool(title="Get activity laps and splits", annotations=READ_ONLY)
def get_activity_laps(
    activity_id: Annotated[int, Field(gt=0, strict=True)],
    cursor: Annotated[str | None, Field(max_length=2048)] = None,
) -> dict[str, Any]:
    """Read recorded laps, planned/executed steps, and separately calculated kilometer splits.

    Unknown triggers/alignment remain unknown. Derived boundary interpolation,
    gaps, resets and partial splits are labeled. Follow next_cursor for all pages.
    """
    return ActivityDetails(database()).get(activity_id, tool="laps", cursor=cursor)


@mcp.tool(title="Get activity FIT metrics", annotations=READ_ONLY)
def get_activity_fit_metrics(
    activity_id: Annotated[int, Field(gt=0, strict=True)],
    cursor: Annotated[str | None, Field(max_length=2048)] = None,
) -> dict[str, Any]:
    """Read additional session/sensor/developer FIT evidence with original units.

    Recorded recovery-HR event value is numeric and unchanged; its meaning,
    baseline/final readings and interval remain unknown unless explicitly recorded.
    Never substitute recovery hours or inferred HR decline. Follow next_cursor.
    """
    return ActivityDetails(database()).get(activity_id, tool="metrics", cursor=cursor)


@mcp.tool(
    title="Save activity feedback revision",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
def save_activity_feedback(
    activity_id: Annotated[int, Field(gt=0, strict=True)],
    feedback: ActivityFeedback,
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
    expected_revision: Annotated[int | None, Field(ge=0, strict=True)] = None,
) -> dict[str, Any]:
    """Append an immutable complete subjective snapshot; does not write Garmin account data.

    Effort/RPE/pain severity scales are 0..10. Heaviness onset is elapsed seconds.
    Omitted fields are unset, not inherited. Reuse a key only for the identical
    payload; corrections use a new key and preferably the last expected_revision.
    """
    return save_feedback(
        feedback_writer(), activity_id, feedback, idempotency_key, expected_revision
    )


@mcp.tool(title="Get activity feedback revisions", annotations=READ_ONLY)
def get_activity_feedback(
    activity_id: Annotated[int, Field(gt=0, strict=True)],
    revision: Annotated[int | None, Field(gt=0, strict=True)] = None,
    include_history: bool = False,
    cursor: Annotated[str | None, Field(max_length=1024)] = None,
) -> dict[str, Any]:
    """Read latest feedback by default, a selected immutable revision, or paginated history.

    History is bounded to 50 revisions per page and bound to its initial snapshot.
    User-reported recovery-HR protocol is separate from recorded FIT evidence.
    """
    return get_feedback(
        database(), activity_id, revision=revision, include_history=include_history, cursor=cursor
    )


@mcp.tool(title="Get stored activity weather", annotations=READ_ONLY)
def get_activity_weather(activity_id: Annotated[int, Field(gt=0, strict=True)]) -> dict[str, Any]:
    """Read stored recorded weather, modeled historical conditions, and wearable temperature.

    No network lookup is triggered. Station observations and model grids remain
    separate. Missing units are unknown, not guessed. Radiation/cloud cover is
    not evidence of personal sun/shade exposure.
    """
    return read_activity_weather(database(), activity_id)


@mcp.tool(title="Get configured training profile", annotations=READ_ONLY)
def get_training_profile() -> dict[str, Any]:
    """Read latest configured running/general HR settings and separate measured daily resting HR.

    Snapshot collection time is not effective time. Do not assume these settings
    applied to older activities; missing thresholds and zone methods stay unknown.
    """
    return read_training_profile(database())


@mcp.tool(title="Get daily health", annotations=READ_ONLY)
def get_daily_health(target_date: str) -> dict[str, Any]:
    """Get normalized health and recovery metrics for one YYYY-MM-DD date."""
    target = date.fromisoformat(target_date)
    result = database().get_daily_health(target)
    return {"date": target_date, "found": result is not None, "health": result}


@mcp.tool(title="Get health range", annotations=READ_ONLY)
def get_health_range(start_date: str, end_date: str) -> dict[str, Any]:
    """Get chronological daily health metrics for an inclusive date range."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    if (end - start).days > 730:
        raise ValueError("Date range cannot exceed 731 days")
    rows = database().get_health_range(start, end)
    return {"start_date": start_date, "end_date": end_date, "count": len(rows), "days": rows}


@mcp.tool(title="Get today's readiness", annotations=READ_ONLY)
def get_today_readiness() -> dict[str, Any]:
    """Get today's readiness, recovery, HRV, sleep, stress, and training-status context."""
    today = date.today()
    row = database().get_daily_health(today)
    return {"date": today.isoformat(), "found": row is not None, "readiness": row}


@mcp.tool(title="Get recent runs", annotations=READ_ONLY)
def get_recent_runs(days: Annotated[int, Field(ge=1, le=365)] = 30) -> dict[str, Any]:
    """Get recent running activities, including trail and treadmill runs."""
    days = _days(days, 365)
    since = datetime.now(UTC) - timedelta(days=days)
    rows = database().get_recent_activities(since)
    runs = [row for row in rows if "run" in row["activity_type"].lower()]
    return {"days": days, "count": len(runs), "activities": runs}


@mcp.tool(title="Get recent activities", annotations=READ_ONLY)
def get_recent_activities(
    days: Annotated[int, Field(ge=1, le=365)] = 30,
    activity_type: str | None = None,
) -> dict[str, Any]:
    """Get recent activities, optionally filtered by Garmin activity type."""
    days = _days(days, 365)
    since = datetime.now(UTC) - timedelta(days=days)
    rows = database().get_recent_activities(since, activity_type)
    return {"days": days, "activity_type": activity_type, "count": len(rows), "activities": rows}


@mcp.tool(title="Get activity", annotations=READ_ONLY)
def get_activity(activity_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
    """Get one normalized activity by its stable Garmin activity ID."""
    if activity_id <= 0:
        raise ValueError("activity_id must be positive")
    row = database().get_activity(activity_id)
    return {"activity_id": activity_id, "found": row is not None, "activity": row}


@mcp.tool(title="Get VO2 max history", annotations=READ_ONLY)
def get_vo2max_history(days: Annotated[int, Field(ge=1, le=730)] = 180) -> dict[str, Any]:
    """Get chronological running VO2 max history, falling back to generic VO2 max."""
    days = _days(days)
    running = database().get_metric_history("running_vo2max", days)
    generic = database().get_metric_history("vo2max", days)
    return {"days": days, "running_vo2max": running, "vo2max": generic}


@mcp.tool(title="Get HRV history", annotations=READ_ONLY)
def get_hrv_history(days: Annotated[int, Field(ge=1, le=730)] = 90) -> dict[str, Any]:
    """Get chronological nightly HRV averages for recovery trend analysis."""
    days = _days(days)
    return {"days": days, "history": database().get_metric_history("hrv_average", days)}


@mcp.tool(title="Get sleep history", annotations=READ_ONLY)
def get_sleep_history(days: Annotated[int, Field(ge=1, le=365)] = 30) -> dict[str, Any]:
    """Get sleep duration, stages, awake time, and score history."""
    days = _days(days, 365)
    return {"days": days, "history": database().get_sleep_history(days)}


@mcp.tool(title="Get resting heart-rate history", annotations=READ_ONLY)
def get_resting_hr_history(days: Annotated[int, Field(ge=1, le=730)] = 90) -> dict[str, Any]:
    """Get chronological resting heart-rate history."""
    days = _days(days)
    return {"days": days, "history": database().get_metric_history("resting_hr", days)}


@mcp.tool(title="Get training load", annotations=READ_ONLY)
def get_training_load(days: Annotated[int, Field(ge=1, le=365)] = 28) -> dict[str, Any]:
    """Get daily acute load/status and contributing activity loads for a period."""
    return database().get_training_load(_days(days, 365))


@mcp.tool(title="Get synchronization status", annotations=READ_ONLY)
def get_sync_status() -> dict[str, Any]:
    """Get collector freshness and the last understandable synchronization errors."""
    rows = database().get_sync_state()
    return {
        "resources": rows,
        "ad_hoc": SyncRequests(settings().sync_request_dir).status(),
        "analysis": database().get_analysis_state(),
    }


@mcp.tool(
    title="Sync latest Garmin data",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def request_sync() -> dict[str, Any]:
    """Queue a refresh of today's and the preceding two days' health and activities.

    Returns immediately. Poll get_sync_status for ad_hoc completion/error before
    reading refreshed data. Pending requests are coalesced; a five-minute cooldown
    follows completion or failure. A busy collector finishes its current cycle first.
    This fetches data already uploaded to Garmin Connect; it cannot sync the watch.
    """
    return SyncRequests(settings().sync_request_dir).request()


def run() -> None:
    current = settings()
    mcp.settings.host = current.mcp_host
    mcp.settings.port = current.mcp_port
    mcp.run(transport="streamable-http")
