"""Collector-only optional observations, with last-good evidence on failed attempts."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from .contracts import AnalysisError, AnalysisResponse, EvidenceSource, Freshness, field_evidence
from .db import Database, _json_safe


def number(value: Any) -> int | float | None:
    return value if type(value) in (int, float) and math.isfinite(value) else None


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo and result.utcoffset() is not None else None
    except ValueError:
        return None


class ObservationStore:
    def __init__(self, database: Database):
        self.database = database

    def state(self, resource: str, activity_id: int | None = None) -> dict | None:
        with self.database.connection() as conn:
            return conn.execute(
                "SELECT * FROM enrichment_state WHERE resource=%s AND activity_key=%s",
                (resource, activity_id or 0),
            ).fetchone()

    def attempt(
        self,
        resource: str,
        activity_id: int | None,
        status: str,
        code: str | None = None,
        details: dict | None = None,
        retry_after: datetime | None = None,
    ) -> None:
        with self.database.connection() as conn:
            conn.execute(
                """INSERT INTO enrichment_state
                (resource,activity_key,status,error_code,details,retry_after,last_success_at)
                VALUES (%s,%s,%s,%s,%s,%s,CASE WHEN %s='success' THEN now() END)
                ON CONFLICT(resource,activity_key) DO UPDATE SET
                status=EXCLUDED.status,error_code=EXCLUDED.error_code,
                details=CASE WHEN %s THEN EXCLUDED.details ELSE enrichment_state.details END,
                retry_after=EXCLUDED.retry_after,
                last_attempt_at=clock_timestamp(),
                last_success_at=COALESCE(EXCLUDED.last_success_at,enrichment_state.last_success_at)
                """,
                (
                    resource,
                    activity_id or 0,
                    status,
                    code,
                    Jsonb(details or {}),
                    retry_after,
                    status,
                    details is not None,
                ),
            )

    def collect(self, resource: str, provider: Any, activity_id: int | None = None) -> str:
        try:
            payload = (
                provider.get_activity_weather(activity_id)
                if activity_id
                else provider.get_configured_training_profile()
            )
            if not isinstance(payload, dict):
                raise ValueError("Unexpected response shape")
            # Reject over-limit or invalid measurements; never persist credentials in error text.
            if len(json.dumps(payload, allow_nan=False).encode()) > 65536:
                raise ValueError("Observation exceeds 64 KiB")
            if not payload:
                self.attempt(resource, activity_id, "not_recorded", "empty_source_response")
                return "not_recorded"
            observed = timestamp(payload.get("issueDate")) if activity_id else None
            with self.database.connection() as conn:
                conn.execute(
                    "INSERT INTO analysis_observations(resource,activity_id,observed_at,payload) "
                    "VALUES (%s,%s,%s,%s)",
                    (resource, activity_id, observed, Jsonb(payload)),
                )
                # Same transaction as publication: never advertise success without stored evidence.
                conn.execute(
                    """INSERT INTO enrichment_state
                    (resource,activity_key,status,last_success_at)
                    VALUES (%s,%s,'success',now()) ON CONFLICT(resource,activity_key)
                    DO UPDATE SET status='success',error_code=NULL,details='{}',retry_after=NULL,
                    last_attempt_at=clock_timestamp(),last_success_at=now()""",
                    (resource, activity_id or 0),
                )
            return "success"
        except NotImplementedError:
            self.attempt(resource, activity_id, "unsupported", "provider_unsupported")
            return "unsupported"
        except Exception as error:
            self.attempt(
                resource,
                activity_id,
                "error",
                type(error).__name__,
                retry_after=datetime.now(UTC) + timedelta(hours=1),
            )
            return "error"

    def latest(self, resource: str, activity_id: int | None = None) -> dict | None:
        with self.database.connection() as conn:
            return conn.execute(
                "SELECT * FROM analysis_observations WHERE resource=%s "
                "AND activity_id IS NOT DISTINCT FROM %s "
                "ORDER BY collected_at DESC,id DESC LIMIT 1",
                (resource, activity_id),
            ).fetchone()


def freshness(state: dict | None) -> Freshness:
    return Freshness(
        last_attempt_at=state["last_attempt_at"] if state else None,
        last_success_at=state["last_success_at"] if state else None,
        stale=state["status"] != "success" if state else None,
        stale_reason=state.get("error_code") if state else "never_collected",
    )


def errors(state: dict | None) -> list[AnalysisError]:
    if not state or state["status"] not in ("error", "partial") or not state["error_code"]:
        return []
    return [
        AnalysisError(
            code=state["error_code"],
            message="Optional enrichment incomplete",
            retryable=True,
            occurred_at=state["last_attempt_at"],
        )
    ]


PROFILE_FIELDS = {
    "maximum_hr": ("maxHeartRateUsed", "bpm"),
    "configured_resting_hr": ("restingHeartRateUsed", "bpm"),
    "lactate_threshold_hr": ("lactateThresholdHeartRateUsed", "bpm"),
}


def normalize_profile(payload: dict) -> dict:
    scopes: dict[str, list] = {"running": [], "general": [], "other": []}
    for zone in payload.get("heart_rate_zones", []):
        if not isinstance(zone, dict):
            continue
        sport = zone.get("sport")
        scope = (
            "running"
            if str(sport).upper() == "RUNNING"
            else "general"
            if str(sport).upper() in ("DEFAULT", "GENERAL")
            else "other"
        )
        entry = {name: number(zone.get(key)) for name, (key, _) in PROFILE_FIELDS.items()}
        entry.update(
            sport=sport,
            calculation_method=zone.get("trainingMethod"),
            zone_floors_bpm=[number(zone.get(f"zone{i}Floor")) for i in range(1, 6)],
            zone_ceilings_bpm=None,
            effective_at=None,
        )
        scopes[scope].append(entry)
    user = payload.get("profile", {}).get("userData", {})
    scopes["unscoped_lactate_threshold_hr"] = number(user.get("lactateThresholdHeartRate"))
    return scopes


def get_training_profile(database: Database) -> dict:
    store = ObservationStore(database)
    observation = store.latest("training_profile")
    state = store.state("training_profile")
    with database.connection() as conn:
        measured = conn.execute(
            "SELECT date,resting_hr,updated_at FROM daily_health WHERE resting_hr IS NOT NULL "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
    sources = []
    fields = {}
    data = {"configured": None, "measured_resting_hr": None}
    if observation:
        source_id = "profile/" + str(observation["id"])
        sources.append(
            EvidenceSource(
                id=source_id,
                provider="Garmin Connect",
                kind="configured",
                collected_at=observation["collected_at"],
                effective_at=observation["effective_at"],
            )
        )
        data["configured"] = normalize_profile(observation["payload"])
        for scope in ("running", "general", "other"):
            for index, entry in enumerate(data["configured"][scope]):
                for name, value in entry.items():
                    unit = "bpm" if name in PROFILE_FIELDS or name.endswith("_bpm") else None
                    fields[f"configured.{scope}.{index}.{name}"] = field_evidence(
                        value,
                        unit=unit,
                        source_ids=[source_id],
                        missing_reason="not_supplied_by_configuration",
                        unit_missing_reason="categorical_or_timestamp",
                    )
                for zone, value in enumerate(entry["zone_floors_bpm"]):
                    fields[f"configured.{scope}.{index}.zone_floors_bpm.{zone}"] = field_evidence(
                        value,
                        unit="bpm",
                        source_ids=[source_id],
                        missing_reason="zone_floor_not_configured",
                    )
        fields["configured.unscoped_lactate_threshold_hr"] = field_evidence(
            data["configured"]["unscoped_lactate_threshold_hr"],
            unit="bpm",
            source_ids=[source_id],
            missing_reason="threshold_not_configured",
        )
    if measured:
        sources.append(
            EvidenceSource(
                id="daily-rhr",
                provider="Garmin daily health",
                kind="recorded",
                collected_at=measured["updated_at"],
            )
        )
        data["measured_resting_hr"] = _json_safe(measured)
    fields["measured_resting_hr.resting_hr"] = field_evidence(
        number(measured["resting_hr"]) if measured else None,
        unit="bpm",
        source_ids=["daily-rhr"] if measured else [],
        missing_reason="daily_rhr_not_recorded",
    )
    fields["configured"] = field_evidence(
        data["configured"],
        unit=None,
        unit_missing_reason="structured_scoped_configuration",
        source_ids=[sources[0].id] if observation else [],
        missing_reason="profile_not_collected",
    )
    return AnalysisResponse(
        availability=("partial" if state and state["status"] != "success" else "available")
        if observation
        else (state["status"] if state else "not_processed"),
        data=data,
        fields=fields,
        sources=sources,
        freshness=freshness(state),
        errors=errors(state),
        limitations=[
            "Retrieval time is not configuration effective time.",
            "Current settings must not be assumed to govern older activities.",
            "Zone ceilings and missing methods are not inferred.",
            "Measured resting HR has date evidence, not an exact measurement timestamp.",
        ],
    ).as_dict()
