"""Complete subjective snapshots; only the save path receives an execution-only writer."""

import base64
import json
from typing import Annotated

from psycopg.errors import NoDataFound, SerializationFailure
from psycopg.types.json import Jsonb
from pydantic import Field

from .contracts import AnalysisResponse, ContractModel, EvidenceSource, Freshness, field_evidence
from .db import Database

Rating = Annotated[float, Field(ge=0, le=10, strict=True)]
Amount = Annotated[float, Field(ge=0, strict=True)]
Text = Annotated[str, Field(max_length=2000, strict=True)]


class RecoveryProtocol(ContractModel):
    baseline_hr_bpm: Annotated[float, Field(ge=0, le=300, strict=True)] | None = None
    recovery_hr_bpm: Annotated[float, Field(ge=0, le=300, strict=True)] | None = None
    interval_seconds: Amount | None = None
    posture: Text | None = None
    method: Text | None = None
    notes: Text | None = None


class ActivityFeedback(ContractModel):
    breathing_effort: Rating | None = None
    leg_effort: Rating | None = None
    overall_rpe: Rating | None = None
    heaviness_onset_seconds: Amount | None = None
    heaviness_location: Annotated[str, Field(max_length=200, strict=True)] | None = None
    pain: Text | None = None
    pain_severity: Rating | None = None
    walking_reasons: (
        Annotated[list[Annotated[str, Field(max_length=200, strict=True)]], Field(max_length=20)]
        | None
    ) = None
    fueling: Text | None = None
    fueling_carbohydrate_g: Amount | None = None
    hydration: Text | None = None
    hydration_ml: Amount | None = None
    strap_problems: Text | None = None
    recovery_hr_protocol: RecoveryProtocol | None = None


UNITS = {
    "breathing_effort": "0..10",
    "leg_effort": "0..10",
    "overall_rpe": "0..10",
    "pain_severity": "0..10",
    "heaviness_onset_seconds": "s elapsed",
    "fueling_carbohydrate_g": "g",
    "hydration_ml": "mL",
}

PROTOCOL_UNITS = {
    "baseline_hr_bpm": "bpm",
    "recovery_hr_bpm": "bpm",
    "interval_seconds": "s",
    "posture": "text",
    "method": "text",
    "notes": "text",
}


class FeedbackCursor(ContractModel):
    activity_id: int = Field(gt=0)
    upper_revision: int = Field(ge=0)
    after_revision: int = Field(ge=0)

    def encode(self):
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value):
        if len(value) > 1024:
            raise ValueError("invalid feedback cursor")
        try:
            return cls.model_validate_json(
                base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            )
        except ValueError as error:
            raise ValueError("invalid feedback cursor") from error


def _response(activity_id, row, *, history=None, next_cursor=None, missing_reason="no_feedback"):
    source = EvidenceSource(
        id="feedback",
        provider="local user",
        kind="user_reported",
        collected_at=row["recorded_at"] if row else None,
    )
    payload = row["payload"] if row else None
    fields = {
        "feedback." + name: field_evidence(
            value,
            unit=UNITS.get(name, "text"),
            source_ids=["feedback"],
            missing_reason="not_reported",
        )
        for name, value in (payload or {}).items()
    }
    fields["feedback"] = field_evidence(
        payload,
        unit=None,
        source_ids=["feedback"],
        missing_reason=missing_reason,
        unit_missing_reason="per_field_units",
    )
    if payload and payload.get("recovery_hr_protocol") is not None:
        fields.update(
            {
                "feedback.recovery_hr_protocol." + name: field_evidence(
                    value,
                    unit=PROTOCOL_UNITS[name],
                    source_ids=["feedback"],
                    missing_reason="not_reported",
                )
                for name, value in payload["recovery_hr_protocol"].items()
            }
        )
    return AnalysisResponse(
        availability="available" if row else "not_recorded",
        activity_id=activity_id,
        data={
            "revision": row,
            "feedback": payload,
            "history": history,
            "units": UNITS,
            "recovery_protocol_units": PROTOCOL_UNITS,
        },
        fields=fields,
        sources=[source],
        freshness=Freshness(last_success_at=row["recorded_at"] if row else None),
        next_cursor=next_cursor,
        limitations=[
            "Feedback is user-reported, not Garmin/FIT measurement evidence.",
            "Each revision is a complete snapshot; omitted fields are unset.",
        ],
    ).as_dict()


def save_feedback(
    writer: Database,
    activity_id: int,
    feedback: ActivityFeedback,
    idempotency_key: str,
    expected_revision: int | None = None,
):
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or any(ord(c) < 32 or ord(c) == 127 for c in idempotency_key)
    ):
        raise ValueError("idempotency_key must be 1..128 non-control characters")
    payload = feedback.model_dump(mode="json")
    if len(json.dumps(payload).encode()) > 16384:
        raise ValueError("feedback payload exceeds 16 KiB")
    try:
        with writer.connection() as conn:
            row = conn.execute(
                "SELECT public.append_activity_feedback(%s,%s,%s,%s) AS revision",
                (activity_id, Jsonb(payload), idempotency_key, expected_revision),
            ).fetchone()["revision"]
    except NoDataFound:
        return AnalysisResponse(availability="not_found", activity_id=activity_id).as_dict()
    except SerializationFailure as error:
        # Do not echo subjective content or any credentials from SQL diagnostics.
        code = (
            "idempotency_payload_conflict"
            if "idempotency_payload_conflict" in str(error)
            else "feedback_revision_conflict"
        )
        raise ValueError(code) from None
    return _response(activity_id, row)


def get_feedback(
    reader: Database, activity_id: int, *, revision=None, include_history=False, cursor=None
):
    if cursor and not include_history:
        raise ValueError("feedback cursor requires include_history=true")
    with reader.connection() as conn:
        if not conn.execute(
            "SELECT 1 FROM activities WHERE garmin_activity_id=%s", (activity_id,)
        ).fetchone():
            return AnalysisResponse(availability="not_found", activity_id=activity_id).as_dict()
        latest = conn.execute(
            "SELECT coalesce(max(revision),0) AS n "
            "FROM activity_feedback_revisions WHERE activity_id=%s",
            (activity_id,),
        ).fetchone()["n"]
        row = conn.execute(
            "SELECT to_jsonb(f) AS data FROM activity_feedback_revisions f "
            "WHERE activity_id=%s AND revision=%s",
            (activity_id, revision if revision is not None else latest),
        ).fetchone()
        history, next_cursor = None, None
        if include_history:
            position = FeedbackCursor(
                activity_id=activity_id, upper_revision=latest, after_revision=0
            )
            if cursor:
                position = FeedbackCursor.decode(cursor)
                if (
                    position.activity_id != activity_id
                    or position.after_revision > position.upper_revision
                ):
                    raise ValueError("feedback cursor does not match activity/history")
            rows = conn.execute(
                "SELECT to_jsonb(f) AS data FROM activity_feedback_revisions f "
                "WHERE activity_id=%s AND revision>%s AND revision<=%s "
                "ORDER BY revision LIMIT 51",
                (activity_id, position.after_revision, position.upper_revision),
            ).fetchall()
            history = [r["data"] for r in rows[:50]]
            if len(rows) > 50:
                position.after_revision = history[-1]["revision"]
                next_cursor = position.encode()
        return _response(
            activity_id,
            row["data"] if row else None,
            history=history,
            next_cursor=next_cursor,
            missing_reason="revision_not_found"
            if revision is not None and latest
            else "no_feedback",
        )
