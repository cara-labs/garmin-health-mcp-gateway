"""Read-only bounded streams, preserving evidence and disclosing aggregation."""

from __future__ import annotations

import base64
import json
import math
from collections import Counter
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .contracts import AnalysisResponse, EvidenceSource, FieldEvidence, Freshness
from .db import Database
from .fit_decoder import SAMPLE_FIELDS
from .quality import DETECTOR_VERSION

PAGE_ROWS = 2000
PAGE_BYTES = 2 * 1024 * 1024
RAW_WINDOW_SECONDS = 1800
UNIT_MAP = {target: unit for target, unit in SAMPLE_FIELDS.values()}
UNIT_MAP.update(elapsed_seconds="s", timer_seconds="s", timestamp="ISO-8601 UTC")
CUMULATIVE = {"distance_m", "timer_seconds"}
POSITION = {"latitude", "longitude", "altitude_m"}


class StreamWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)
    resolution_seconds: StrictInt = Field(default=5, ge=0, le=3600)

    @model_validator(mode="after")
    def interval(self):
        if self.end_seconds is not None and self.end_seconds <= (self.start_seconds or 0):
            raise ValueError("end_seconds must be greater than start_seconds")
        if self.resolution_seconds == 0:
            if self.start_seconds is None or self.end_seconds is None:
                raise ValueError("raw resolution requires explicit start_seconds and end_seconds")
            if self.end_seconds - self.start_seconds > RAW_WINDOW_SECONDS:
                raise ValueError("raw windows cannot exceed 1800 seconds")
        return self


class StreamCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version: Literal[1] = 1
    activity_id: int
    generation_id: str
    start: float
    end: float
    resolution: int
    phase: Literal["streams", "events", "unplaced"] = "streams"
    position: list[float | int] = Field(default_factory=list, max_length=2)

    def encode(self):
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, text):
        if not isinstance(text, str) or len(text) > 2048:
            raise ValueError("invalid stream cursor")
        try:
            raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
            return cls.model_validate_json(raw)
        except (ValueError, UnicodeError) as error:
            raise ValueError("invalid stream cursor") from error


class Bucket:
    """Constant-memory summary, including evidence for first/last semantics."""

    def __init__(self, start: float, end: float):
        self.start, self.end, self.count = start, end, 0
        self.stats = {}
        self.flags = Counter()
        self.flagged_ordinals = []

    def add(self, row):
        self.count += 1
        for name in UNIT_MAP:
            if name == "timestamp":
                continue
            value = row["data"].get(name)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            state = self.stats.setdefault(
                name,
                {"count": 0, "sum": 0.0, "min": value, "max": value, "first": value, "last": value},
            )
            state["count"] += 1
            state["sum"] += value
            state["min"], state["max"] = min(state["min"], value), max(state["max"], value)
            state["last"] = value
        flags = row["data"].get("quality_flags", [])
        self.flags.update(flag["reason"] for flag in flags)
        if flags and len(self.flagged_ordinals) < 10:
            self.flagged_ordinals.append(row["ordinal"])

    def result(self):
        fields = {}
        for name in UNIT_MAP:
            if name == "timestamp":
                continue
            state = self.stats.get(name)
            method = (
                "first_last"
                if name in POSITION
                else ("last_with_first_last" if name in CUMULATIVE else "arithmetic_mean")
            )
            if state is None:
                fields[name] = {
                    "value": None,
                    "count": 0,
                    "min": None,
                    "max": None,
                    "first": None,
                    "last": None,
                    "method": method,
                    "missing_reason": "no_recorded_samples",
                }
            else:
                value = (
                    None
                    if name in POSITION
                    else state["last"]
                    if name in CUMULATIVE
                    else state["sum"] / state["count"]
                )
                fields[name] = {k: v for k, v in state.items() if k != "sum"}
                fields[name].update(value=value, method=method, missing_reason=None)
        return {
            "bucket_start_seconds": self.start,
            "bucket_end_seconds": self.end,
            "sample_count": self.count,
            "gap": self.count == 0,
            "fields": fields,
            "quality": {
                "detector_version": DETECTOR_VERSION,
                "reason_counts": dict(self.flags),
                "example_flagged_ordinals": self.flagged_ordinals,
                "flagged_samples_included": True,
            },
        }


def summarize(
    rows: Iterator[dict], start: float, end: float, resolution: int, first_bucket: int | None = None
):
    """Half-open clipping; buckets remain anchored at activity elapsed zero."""
    current = next(rows, None)
    first = math.floor(start / resolution) if first_bucket is None else first_bucket
    for index in range(first, math.ceil(end / resolution)):
        low, high = max(start, index * resolution), min(end, (index + 1) * resolution)
        bucket = Bucket(low, high)
        while current is not None and current["elapsed_seconds"] < high:
            if current["elapsed_seconds"] >= low:
                bucket.add(current)
            current = next(rows, None)
        yield index, bucket.result()


class ActivityStreams:
    def __init__(self, database: Database):
        self.database = database

    def get(
        self,
        activity_id: int,
        *,
        start_seconds=None,
        end_seconds=None,
        resolution_seconds=5,
        cursor=None,
    ) -> dict[str, Any]:
        if isinstance(activity_id, bool) or activity_id <= 0:
            raise ValueError("activity_id must be positive")
        window = StreamWindow(
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            resolution_seconds=resolution_seconds,
        )
        with self.database.connection() as conn:
            activity = conn.execute(
                "SELECT start_time FROM activities WHERE garmin_activity_id=%s", (activity_id,)
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
                    data={
                        "processing_status": state["status"] if state else "not_processed",
                        "error_code": state["error_code"] if state else None,
                    },
                ).as_dict()
            gid = str(generation["id"])
            bounds = conn.execute(
                "SELECT max(elapsed_seconds) AS last, count(*) FILTER "
                "(WHERE elapsed_seconds IS NULL OR elapsed_seconds<0) AS unplaced "
                "FROM fit_samples WHERE generation_id=%s",
                (gid,),
            ).fetchone()
            start = window.start_seconds or 0.0
            end = window.end_seconds
            if end is None:
                end = max(0.0, bounds["last"] or 0.0) + max(1, window.resolution_seconds)
            position = StreamCursor(
                activity_id=activity_id,
                generation_id=gid,
                start=start,
                end=end,
                resolution=window.resolution_seconds,
            )
            if cursor:
                received = StreamCursor.decode(cursor)
                if received.generation_id != gid:
                    raise ValueError("stale cursor: activity processing generation changed")
                keys = ("activity_id", "generation_id", "start", "end", "resolution")
                if any(getattr(received, k) != getattr(position, k) for k in keys):
                    raise ValueError("cursor does not match activity/window/resolution")
                position = received
            collections = {"streams": [], "events": [], "unplaced": []}
            used_bytes, used_rows = 0, 0

            def append(phase, value):
                nonlocal used_bytes, used_rows
                size = len(json.dumps(value, allow_nan=False).encode())
                if used_rows >= PAGE_ROWS or used_bytes + size > PAGE_BYTES:
                    return False
                collections[phase].append(value)
                used_bytes += size
                used_rows += 1
                return True

            for phase in ("streams", "events", "unplaced"):
                if position.phase != phase:
                    continue
                complete = True
                if phase == "streams":
                    complete = self._streams(conn, gid, position, append)
                    if complete:
                        position.phase, position.position = "events", []
                elif phase == "events":
                    complete = self._events(conn, gid, position, append)
                    if complete:
                        position.phase, position.position = "unplaced", []
                else:
                    complete = self._unplaced(conn, gid, position, append)
                if not complete:
                    break
            boundary = conn.execute(
                "SELECT event_type,elapsed_seconds,ordinal FROM fit_timer_events "
                "WHERE generation_id=%s AND elapsed_seconds<%s "
                "ORDER BY elapsed_seconds DESC,ordinal DESC LIMIT 1",
                (gid, start),
            ).fetchone()
            source = EvidenceSource(
                id="fit",
                provider="FIT archive",
                kind="recorded",
                collected_at=None,
                processed_at=generation["completed_at"],
                processing_version=generation["decoder_version"]
                + ";"
                + generation["schema_version"],
            )
            return AnalysisResponse(
                availability="partial" if state and state["status"] == "error" else "available",
                activity_id=activity_id,
                data={
                    "generation_id": gid,
                    "archive_sha256": generation["archive_sha256"],
                    "start_seconds": start,
                    "end_seconds": end,
                    "resolution_seconds": window.resolution_seconds,
                    "streams": collections["streams"],
                    "timer_events": collections["events"],
                    "unplaced_samples": collections["unplaced"],
                    "timer_boundary_state": dict(boundary) if boundary else None,
                    "flagged_samples_included": True,
                },
                fields={
                    "streams.*." + name: FieldEvidence(
                        unit=unit,
                        source_ids=["fit", "normalization"]
                        if name in ("elapsed_seconds", "timer_seconds", "latitude", "longitude")
                        else ["fit"],
                        availability="partial",
                        limitations=["Per-sample missing reasons or bucket valid counts apply."],
                    )
                    for name, unit in UNIT_MAP.items()
                },
                sources=[
                    source,
                    EvidenceSource(
                        id="normalization",
                        provider="gateway",
                        kind="derived",
                        processed_at=generation["completed_at"],
                        processing_version=generation["schema_version"],
                    ),
                ],
                freshness=Freshness(
                    last_success_at=generation["completed_at"],
                    last_attempt_at=state["last_attempt_at"] if state else None,
                    stale=True if state and state["status"] == "error" else None,
                    stale_reason="latest_processing_failed"
                    if state and state["status"] == "error"
                    else None,
                ),
                coverage={
                    "unplaced_sample_count": bounds["unplaced"],
                    "page_rows": used_rows,
                    "page_data_bytes": used_bytes,
                    "page_row_limit": PAGE_ROWS,
                    "page_data_byte_limit": PAGE_BYTES,
                },
                limitations=[
                    "Collection time is unknown for previously archived FIT files.",
                    "Timer values require recorded event evidence; gaps are not filled.",
                    "Pages contain streams, then events, then unplaced samples; follow "
                    "next_cursor to retain all evidence.",
                ],
                next_cursor=None if complete else position.encode(),
            ).as_dict()

    @staticmethod
    def _streams(conn, gid, position, append):
        raw = position.resolution == 0
        low = position.start
        extra, params = "", [gid, low, position.end]
        if position.position:
            if raw:
                if len(position.position) != 2:
                    raise ValueError("invalid raw cursor position")
                extra = " AND (elapsed_seconds,ordinal)>(%s,%s)"
                params.extend(position.position)
            else:
                if len(position.position) != 1 or position.position[0] != int(position.position[0]):
                    raise ValueError("invalid summary cursor position")
                params[1] = max(low, (int(position.position[0]) + 1) * position.resolution)
        with conn.cursor(name="activity_streams", withhold=False) as rows:
            rows.itersize = 128
            rows.execute(
                "SELECT ordinal,elapsed_seconds,data FROM fit_samples "
                "WHERE generation_id=%s AND elapsed_seconds>=%s AND elapsed_seconds<%s"
                + extra
                + " ORDER BY elapsed_seconds,ordinal",
                params,
            )
            if raw:
                for row in rows:
                    result = dict(row["data"], ordinal=row["ordinal"])
                    if not append("streams", result):
                        return False
                    position.position = [row["elapsed_seconds"], row["ordinal"]]
            else:
                first = int(position.position[0]) + 1 if position.position else None
                for index, result in summarize(
                    iter(rows), low, position.end, position.resolution, first_bucket=first
                ):
                    if not append("streams", result):
                        return False
                    position.position = [index]
        return True

    @staticmethod
    def _events(conn, gid, position, append):
        params = [gid, position.start, position.end]
        extra = ""
        if position.position:
            if len(position.position) != 2:
                raise ValueError("invalid event cursor position")
            extra = " AND (elapsed_seconds,ordinal)>(%s,%s)"
            params.extend(position.position)
        with conn.cursor(name="activity_events") as rows:
            rows.itersize = 128
            rows.execute(
                "SELECT ordinal,elapsed_seconds,event_type,data FROM fit_timer_events "
                "WHERE generation_id=%s AND elapsed_seconds>=%s AND elapsed_seconds<%s"
                + extra
                + " ORDER BY elapsed_seconds,ordinal",
                params,
            )
            for row in rows:
                if not append("events", dict(row)):
                    return False
                position.position = [row["elapsed_seconds"], row["ordinal"]]
        return True

    @staticmethod
    def _unplaced(conn, gid, position, append):
        if len(position.position) > 1:
            raise ValueError("invalid unplaced cursor position")
        after = position.position[0] if position.position else -1
        with conn.cursor(name="activity_unplaced") as rows:
            rows.itersize = 128
            rows.execute(
                "SELECT ordinal,data FROM fit_samples WHERE generation_id=%s "
                "AND (elapsed_seconds IS NULL OR elapsed_seconds<0) AND ordinal>%s "
                "ORDER BY ordinal",
                (gid, after),
            )
            for row in rows:
                if not append("unplaced", dict(row["data"], ordinal=row["ordinal"])):
                    return False
                position.position = [row["ordinal"]]
        return True
