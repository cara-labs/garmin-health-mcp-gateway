"""Stored lap/step and additional FIT evidence; no provider calls or archive reads."""

import base64
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import AnalysisResponse, EvidenceSource, Freshness, field_evidence
from .db import Database
from .splits import calculated_splits
from .streams import PAGE_BYTES, PAGE_ROWS

LAP_FIELDS = {
    "elapsed_duration_seconds": ("total_elapsed_time", "s"),
    "timer_duration_seconds": ("total_timer_time", "s"),
    "distance_m": ("total_distance", "m"),
    "average_hr_bpm": ("avg_heart_rate", "bpm"),
    "maximum_hr_bpm": ("max_heart_rate", "bpm"),
    "average_cadence_rpm": ("avg_cadence", "rpm"),
    "ascent_m": ("total_ascent", "m"),
    "descent_m": ("total_descent", "m"),
}
PRIVATE_FIELDS = {
    "serial_number",
    "friendly_name",
    "application_id",
    "developer_id",
    "user_id",
    "email",
    "password",
    "access_token",
    "refresh_token",
}


class DetailCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activity_id: int
    generation_id: str
    tool: Literal["laps", "metrics"]
    phase: int = Field(default=0, ge=0, le=3)
    after: int = Field(default=-1, ge=-1)

    def encode(self):
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value):
        if len(value) > 2048:
            raise ValueError("invalid detail cursor")
        try:
            return cls.model_validate_json(
                base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            )
        except (ValueError, UnicodeError) as error:
            raise ValueError("invalid detail cursor") from error


def recorded_lap(data, ordinal):
    native = data["native"]
    fields = {}
    for target, (source, unit) in LAP_FIELDS.items():
        value = native.get(source)
        fields[target] = {
            "value": value,
            "unit": unit,
            "source_field": source,
            "missing_reason": "not_recorded" if value is None else None,
        }
    speed = native.get("enhanced_avg_speed", native.get("avg_speed"))
    fields["average_pace_seconds_per_km"] = {
        "value": 1000 / speed if isinstance(speed, (int, float)) and speed > 0 else None,
        "unit": "s/km",
        "source_field": "average_speed",
        "kind": "derived",
        "missing_reason": None
        if isinstance(speed, (int, float)) and speed > 0
        else "missing_or_zero_speed",
    }
    trigger = native.get("lap_trigger")
    classification = (
        "manual"
        if trigger == "manual"
        else ("automatic" if trigger in ("time", "distance") else "unknown")
    )
    return {
        "ordinal": ordinal,
        "kind": "recorded_lap",
        "fields": fields,
        "trigger": trigger,
        "trigger_classification": classification,
        "trigger_missing_reason": "not_recorded" if trigger is None else None,
        "workout_step_index": native.get("wkt_step_index"),
        "recorded_evidence": data,
    }


def sanitize_metric(data):
    """Remove unnecessary personal/device identifiers, not measurement evidence."""
    native = {
        key: value
        for key, value in data.get("native", {}).items()
        if key.lower() not in PRIVATE_FIELDS
    }
    fields = [
        field for field in data.get("fields", []) if field["name"].lower() not in PRIVATE_FIELDS
    ]
    return {
        "native": native,
        "fields": fields,
        "omitted_field_names": sorted(set(data.get("native", {})) - set(native)),
    }


def recovery_evidence(data):
    native = data.get("native", {})
    if native.get("event") != "recovery_hr":
        return None
    # The FIT profile names the event but the selected decoder exposes its data
    # as an untyped integer. Preserve it; do not assume it is a baseline, final HR,
    # or HR decline, nor invent a two-minute protocol.
    value = native.get("data")
    numeric = (
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    )
    return {
        "source_event": "recovery_hr",
        "observed_at": native.get("timestamp"),
        "value": value if numeric else None,
        "stored_event_data": value,
        "availability": "available" if numeric else "not_recorded",
        "value_missing_reason": None if numeric else "numeric_payload_not_recorded",
        "meaning": "uninterpreted_recorded_recovery_hr_payload",
        "unit": None,
        "unit_missing_reason": "event_payload_semantics_not_supplied",
        "baseline_bpm": None,
        "recovery_bpm": None,
        "interval_seconds": None,
        "protocol": None,
        "missing": {
            "baseline_bpm": "not_explicitly_recorded",
            "recovery_bpm": "payload_semantics_unknown",
            "interval_seconds": "not_recorded",
            "protocol": "not_recorded",
        },
    }


class ActivityDetails:
    def __init__(self, database: Database):
        self.database = database

    def get(self, activity_id: int, *, tool: Literal["laps", "metrics"], cursor=None):
        with self.database.connection() as conn:
            activity = conn.execute(
                "SELECT 1 FROM activities WHERE garmin_activity_id=%s", (activity_id,)
            ).fetchone()
            if activity is None:
                return AnalysisResponse(availability="not_found", activity_id=activity_id).as_dict()
            generation = conn.execute(
                "SELECT g.* FROM fit_active_generations a JOIN fit_generations g "
                "ON a.generation_id=g.id WHERE a.activity_id=%s",
                (activity_id,),
            ).fetchone()
            state = conn.execute(
                "SELECT * FROM fit_processing_state WHERE activity_id=%s", (activity_id,)
            ).fetchone()
            if generation is None:
                return AnalysisResponse(
                    availability="error"
                    if state and state["status"] == "error"
                    else "not_processed",
                    activity_id=activity_id,
                    data={"error_code": state["error_code"] if state else None},
                ).as_dict()
            gid = str(generation["id"])
            position = DetailCursor(activity_id=activity_id, generation_id=gid, tool=tool)
            if cursor:
                received = DetailCursor.decode(cursor)
                if received.generation_id != gid:
                    raise ValueError("stale detail cursor")
                if received.activity_id != activity_id or received.tool != tool:
                    raise ValueError("detail cursor does not match activity/tool")
                position = received
            names = (
                ["recorded_laps", "planned_steps", "executed_steps", "calculated_splits"]
                if tool == "laps"
                else ["metrics"]
            )
            if position.phase >= len(names):
                raise ValueError("invalid detail cursor phase")
            collections = {name: [] for name in names}
            byte_count, row_count, complete = 0, 0, True
            recovery_count = (
                conn.execute(
                    "SELECT count(*) AS n FROM fit_extra_metrics WHERE generation_id=%s "
                    "AND data->'native'->>'event'='recovery_hr'",
                    (gid,),
                ).fetchone()["n"]
                if tool == "metrics"
                else 0
            )
            for phase in range(position.phase, len(names)):
                name = names[phase]
                with conn.cursor(name="activity_details") as rows:
                    rows.itersize = 128
                    if name == "calculated_splits":
                        rows.execute(
                            "SELECT data FROM fit_samples WHERE generation_id=%s ORDER BY ordinal",
                            (gid,),
                        )
                        results = (
                            (i, value)
                            for i, value in enumerate(calculated_splits(rows))
                            if i > position.after
                        )
                    else:
                        table = {
                            "recorded_laps": "fit_laps",
                            "planned_steps": "fit_workout_steps",
                            "executed_steps": "fit_workout_steps",
                            "metrics": "fit_extra_metrics",
                        }[name]
                        extra = " AND kind=%s" if name.endswith("steps") else ""
                        params = [gid, position.after]
                        if extra:
                            params.append("planned" if name == "planned_steps" else "executed")
                        rows.execute(
                            "SELECT ordinal,data FROM "
                            + table
                            + " WHERE generation_id=%s AND ordinal>%s"
                            + extra
                            + " ORDER BY ordinal",
                            params,
                        )

                        def transform(row, name=name):
                            data = row["data"]
                            if name == "recorded_laps":
                                return recorded_lap(data, row["ordinal"])
                            if name == "metrics":
                                return {
                                    "ordinal": row["ordinal"],
                                    "evidence": sanitize_metric(data),
                                    "recorded_recovery_hr": recovery_evidence(data),
                                }
                            return {
                                "ordinal": row["ordinal"],
                                "kind": name,
                                "association_index": data["native"].get("wkt_step_index"),
                                "association_missing_reason": "not_recorded"
                                if data["native"].get("wkt_step_index") is None
                                else None,
                                "recorded_evidence": data,
                            }

                        results = ((row["ordinal"], transform(row)) for row in rows)
                    for ordinal, value in results:
                        size = len(json.dumps(value).encode())
                        if row_count >= PAGE_ROWS or byte_count + size > PAGE_BYTES:
                            complete = False
                            break
                        collections[name].append(value)
                        row_count += 1
                        byte_count += size
                        position.after = ordinal
                if not complete:
                    break
                position.phase, position.after = phase + 1, -1
            return AnalysisResponse(
                availability="partial" if state and state["status"] == "error" else "available",
                activity_id=activity_id,
                data={
                    "generation_id": gid,
                    **collections,
                    **(
                        {
                            "recorded_recovery_hr": [
                                r["recorded_recovery_hr"]
                                for r in collections["metrics"]
                                if r["recorded_recovery_hr"] is not None
                            ]
                            if recovery_count
                            else None,
                            "recorded_recovery_hr_event_count": recovery_count,
                        }
                        if tool == "metrics"
                        else {}
                    ),
                },
                fields={
                    "recorded_recovery_hr": field_evidence(
                        [] if recovery_count else None,
                        unit=None,
                        source_ids=["fit"],
                        unit_missing_reason="event_payload_semantics_not_supplied",
                    )
                }
                if tool == "metrics"
                else {},
                sources=[
                    EvidenceSource(
                        id="fit",
                        provider="FIT archive",
                        kind="recorded",
                        processed_at=generation["completed_at"],
                        processing_version=generation["decoder_version"]
                        + ";"
                        + generation["schema_version"],
                    )
                ],
                freshness=Freshness(
                    last_success_at=generation["completed_at"],
                    last_attempt_at=state["last_attempt_at"] if state else None,
                    stale=True if state and state["status"] == "error" else None,
                    stale_reason="latest_processing_failed"
                    if state and state["status"] == "error"
                    else None,
                ),
                coverage={"page_rows": row_count, "page_data_bytes": byte_count},
                limitations=[
                    "FIT recovery events are not recovery-hours recommendations.",
                    "Current settings or inferred HR changes are not recorded recovery HR.",
                    "Field evidence retains original units and explicit unknown units.",
                    "Calculated splits are derived, never recorded laps.",
                ],
                next_cursor=None if complete else position.encode(),
            ).as_dict()
