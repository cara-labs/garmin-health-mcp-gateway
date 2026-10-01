"""Streaming adapter: evidence is retained, not repaired or resampled at ingestion."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fitdecode

from .quality import DETECTOR_VERSION, quality_flags

DECODER_VERSION = f"fitdecode/{fitdecode.__version__}"
SCHEMA_VERSION = "fit-analysis/2"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_MESSAGES = 1_000_000
MAX_MESSAGE_BYTES = 64 * 1024
MAX_DEVELOPER_DEFINITIONS = 4096
MAX_CHAINED_FILES = 32
BATCH_SIZE = 128

SAMPLE_FIELDS = {
    "distance": ("distance_m", "m"),
    "speed": ("speed_mps", "m/s"),
    "heart_rate": ("heart_rate_bpm", "bpm"),
    "cadence": ("cadence_rpm", "rpm"),
    "altitude": ("altitude_m", "m"),
    "position_lat": ("latitude", "degree"),
    "position_long": ("longitude", "degree"),
    "power": ("power_w", "W"),
    "vertical_oscillation": ("vertical_oscillation_mm", "mm"),
    "stance_time": ("ground_contact_time_ms", "ms"),
    "stance_time_balance": ("ground_contact_balance_percent", "%"),
    "vertical_ratio": ("vertical_ratio_percent", "%"),
    "step_length": ("step_length_mm", "mm"),
    "temperature": ("wearable_temperature_c", "degC"),
}


class FitLimitError(ValueError):
    """Explicit limit failure: never publish a truncated generation."""


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def archive_identity(path: Path) -> tuple[str, int]:
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise FitLimitError("archive_size_limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True, slots=True)
class DecodedMessage:
    ordinal: int
    number: int
    name: str | None
    observed_at: datetime | None
    fields: list[dict[str, Any]]
    category: str | None
    data: dict[str, Any]
    elapsed_seconds: float | None = None
    timer_seconds: float | None = None


class FitDecoder:
    def __init__(
        self, *, max_messages: int = MAX_MESSAGES, max_message_bytes: int = MAX_MESSAGE_BYTES
    ):
        if max_messages <= 0 or max_message_bytes <= 0:
            raise ValueError("FIT processing limits must be positive")
        self.max_messages = max_messages
        self.max_message_bytes = max_message_bytes

    def decode(self, path: Path, start_time: datetime) -> Iterator[DecodedMessage]:
        """Elapsed axis uses the stored activity start, identified as derived evidence.

        Timer duration is known only after a recorded start event. Missing record
        timestamps stay missing, not the reader's previous timestamp. Original
        fields/raw values are retained alongside every normalized measurement.
        """
        if start_time.tzinfo is None:
            raise ValueError("activity start_time requires a timezone")
        archive_identity(path)  # also enforces file-size limit before parser allocation
        timer_start: datetime | None = None
        timer_total = 0.0
        timer_known = False
        ordinal = 0
        developer_definitions = 0
        chained_files = 0
        previous_sample = None
        with fitdecode.FitReader(
            path, check_crc=fitdecode.CrcCheck.RAISE, error_handling=fitdecode.ErrorHandling.RAISE
        ) as reader:
            for frame in reader:
                if frame.frame_type == fitdecode.FIT_FRAME_HEADER:
                    chained_files += 1
                    if chained_files > MAX_CHAINED_FILES:
                        raise FitLimitError("chained_file_limit")
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                if frame.name in ("field_description", "developer_data_id"):
                    developer_definitions += 1
                    if developer_definitions > MAX_DEVELOPER_DEFINITIONS:
                        raise FitLimitError("developer_definition_limit")
                if ordinal >= self.max_messages:
                    raise FitLimitError("message_count_limit")
                fields = []
                native = {}
                for field in frame.fields:
                    definition = field.field_def
                    developer_index = getattr(definition, "dev_data_index", None)
                    evidence = {
                        "name": str(field.name_or_num),
                        "number": field.def_num,
                        "developer_data_index": developer_index,
                        "meaning_missing_reason": "unrecognized_developer_semantics"
                        if developer_index is not None
                        else None,
                        "value": json_value(field.value),
                        "raw_value": json_value(field.raw_value),
                        "unit": field.units or None,
                        "unit_missing_reason": "unknown" if not field.units else None,
                        "expanded": field.is_expanded,
                        "missing_reason": "invalid_or_not_recorded"
                        if field.value is None
                        else None,
                    }
                    fields.append(evidence)
                    if developer_index is None:
                        native[str(field.name_or_num)] = field.value
                if len(json.dumps(fields, allow_nan=False).encode()) > self.max_message_bytes:
                    raise FitLimitError("message_payload_limit")
                timestamp = native.get("timestamp")
                if isinstance(timestamp, datetime):
                    timestamp = (
                        timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
                    )
                else:
                    timestamp = None
                elapsed = (timestamp - start_time).total_seconds() if timestamp else None
                category = None
                data = {"native": json_value(native), "fields": fields}
                if frame.name == "event" and native.get("event") == "timer":
                    category = "timer"
                    kind = native.get("event_type")
                    data["event_type"] = kind
                    if timestamp is not None:
                        if kind in ("stop", "stop_all", "stop_disable", "stop_disable_all"):
                            if timer_start is not None and timestamp >= timer_start:
                                timer_total += (timestamp - timer_start).total_seconds()
                            timer_start = None
                        elif kind == "start":
                            timer_known = True
                            if timer_start is None:
                                timer_start = timestamp
                timer = None
                if timestamp is not None and timer_known:
                    timer = timer_total
                    if timer_start is not None:
                        delta = (timestamp - timer_start).total_seconds()
                        timer = timer_total + delta if delta >= 0 else None
                if frame.name == "record":
                    category = "sample"
                    values = {}
                    missing = {}
                    source_fields = {}
                    for source, (target, _) in SAMPLE_FIELDS.items():
                        selected = source
                        if source in ("speed", "altitude"):
                            enhanced = f"enhanced_{source}"
                            if native.get(enhanced) is not None:
                                selected = enhanced
                        value = native.get(selected)
                        if source.startswith("position_") and isinstance(value, (int, float)):
                            value = value * (180 / 2**31)
                        values[target] = json_value(value)
                        source_fields[target] = selected
                        if values[target] is None:
                            missing[target] = "invalid_or_not_recorded"
                    data.update(values)
                    data.update(
                        timestamp=timestamp.isoformat() if timestamp else None,
                        elapsed_seconds=elapsed,
                        timer_seconds=timer,
                        missing=missing,
                        source_fields=source_fields,
                        quality_flags=[],
                    )
                    if timestamp is None:
                        missing.update(
                            timestamp="not_recorded", elapsed_seconds="missing_timestamp"
                        )
                    if timer is None:
                        missing["timer_seconds"] = "timer_state_unknown"
                    data["quality_flags"] = quality_flags(data, previous_sample)
                    data["quality_detector_version"] = DETECTOR_VERSION
                    previous_sample = data
                elif frame.name == "lap":
                    category = "lap"
                elif frame.name == "workout_step":
                    category = "planned_step"
                elif frame.name == "event" and native.get("event") == "workout_step":
                    category = "executed_step"
                else:
                    # All non-record structures are retained in the extra-metric
                    # collection too, including unknown message numbers.
                    category = category or "metric"
                yield DecodedMessage(
                    ordinal,
                    frame.global_mesg_num,
                    frame.name,
                    timestamp,
                    fields,
                    category,
                    data,
                    elapsed,
                    timer,
                )
                ordinal += 1
