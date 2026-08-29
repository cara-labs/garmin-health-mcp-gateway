from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Settings
from .db import Database

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
INSTRUCTIONS = (
    "Read-only access to one user's locally stored Garmin health and activity data. "
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
    return {"resources": rows}


def run() -> None:
    current = settings()
    mcp.settings.host = current.mcp_host
    mcp.settings.port = current.mcp_port
    mcp.run(transport="streamable-http")
