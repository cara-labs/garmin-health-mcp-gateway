from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def find(source: Any, *keys: str) -> Any:
    for mapping in _walk(source):
        for key in keys:
            if key in mapping and mapping[key] is not None:
                return mapping[key]
    return None


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def find_number(source: Any, *keys: str) -> float | None:
    for mapping in _walk(source):
        for key in keys:
            if key in mapping:
                parsed = number(mapping[key])
                if parsed is not None:
                    return parsed
    return None


def integer(value: Any) -> int | None:
    parsed = number(value)
    return round(parsed) if parsed is not None else None


def minutes_from_seconds(value: Any) -> int | None:
    parsed = number(value)
    return round(parsed / 60) if parsed is not None else None


def score_value(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("value") or value.get("score")
    return integer(value)


def pace_from_speed(value: Any) -> float | None:
    speed = number(value)
    return 1000 / speed if speed and speed > 0 else None


def recovery_hours(source: Any) -> float | None:
    direct = find_number(source, "recoveryTimeHours", "recoveryHours")
    if direct is not None:
        return direct
    recovery_minutes = find_number(source, "recoveryTime")
    return recovery_minutes / 60 if recovery_minutes is not None else None


def container_metric(source: Any, container: str, *direct_keys: str) -> float | None:
    parsed = find_number(source, *direct_keys)
    if parsed is not None:
        return parsed
    if isinstance(source, dict):
        return find_number(source.get(container), "value", "metricValue", "latestValue")
    return None


def _body_battery_values(source: Any) -> list[int]:
    values: list[int] = []
    for mapping in _walk(source):
        for key in ("bodyBatteryValuesArray", "bodyBatteryValues"):
            raw = mapping.get(key)
            if not isinstance(raw, list):
                continue
            for point in raw:
                candidate = point[-1] if isinstance(point, list) and point else point
                parsed = integer(candidate)
                if parsed is not None and 0 <= parsed <= 100:
                    values.append(parsed)
    return values


def normalize_daily(target: date, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    summary = payload.get("summary") or {}
    sleep = payload.get("sleep") or {}
    hrv = payload.get("hrv") or {}
    battery = payload.get("body_battery") or {}
    stress = payload.get("stress") or {}
    readiness = payload.get("training_readiness") or {}
    status = payload.get("training_status") or {}
    metrics = payload.get("vo2max") or {}
    fitness = payload.get("fitness_age") or {}
    lactate = payload.get("lactate_threshold") or {}

    battery_values = _body_battery_values(battery)
    load_focus = find(status, "loadFocus", "trainingLoadFocus", "loadFocusData")
    if not isinstance(load_focus, (dict, list)):
        load_focus = None

    health = {
        "date": target,
        "resting_hr": integer(find(summary, "restingHeartRate", "restingHR")),
        "hrv_average": find_number(hrv, "lastNightAvg", "weeklyAvg", "hrvAverage"),
        "hrv_status": find(hrv, "status", "hrvStatus"),
        "sleep_minutes": minutes_from_seconds(find(sleep, "sleepTimeSeconds", "totalSleepSeconds")),
        "deep_sleep_minutes": minutes_from_seconds(find(sleep, "deepSleepSeconds")),
        "rem_sleep_minutes": minutes_from_seconds(find(sleep, "remSleepSeconds")),
        "light_sleep_minutes": minutes_from_seconds(find(sleep, "lightSleepSeconds")),
        "awake_minutes": minutes_from_seconds(find(sleep, "awakeSleepSeconds", "awakeSeconds")),
        "sleep_score": score_value(find(sleep, "sleepScore", "overallScore", "overall")),
        "body_battery_high": integer(find(battery, "highestValue", "bodyBatteryHigh"))
        or (max(battery_values) if battery_values else None),
        "body_battery_low": integer(find(battery, "lowestValue", "bodyBatteryLow"))
        or (min(battery_values) if battery_values else None),
        "average_stress": integer(find(stress, "avgStressLevel", "averageStressLevel")),
        "training_readiness": integer(find(readiness, "score", "trainingReadinessScore")),
        "training_readiness_level": find(readiness, "level", "scoreFeedback"),
        "training_status": find(
            status,
            "trainingStatus",
            "trainingStatusFeedbackPhrase",
            "statusLabel",
        ),
        "acute_training_load": find_number(
            status, "acuteTrainingLoad", "dailyTrainingLoadAcute", "acuteLoad"
        ),
        "load_focus": load_focus,
        "recovery_hours": recovery_hours(readiness),
        "vo2max": find_number(metrics, "vo2MaxPreciseValue", "vo2MaxValue", "vo2max"),
        "running_vo2max": find_number(
            metrics, "runningVo2Max", "runningVO2Max", "runningVo2MaxPreciseValue"
        ),
        "fitness_age": find_number(fitness, "fitnessAge"),
        "lactate_threshold_hr": integer(
            container_metric(lactate, "heart_rate", "heartRate", "lactateThresholdHeartRate")
        ),
        "lactate_threshold_pace_seconds_per_km": find_number(
            lactate, "pace", "lactateThresholdPace"
        )
        or pace_from_speed(container_metric(lactate, "speed", "speed", "lactateThresholdSpeed")),
    }
    activity = {
        "date": target,
        "steps": integer(find(summary, "totalSteps", "steps")),
        "distance_m": number(find(summary, "totalDistanceMeters", "distance")),
        "active_calories": integer(find(summary, "activeKilocalories", "activeCalories")),
        "floors": integer(find(summary, "floorsAscended", "floors")),
        "moderate_intensity_minutes": integer(find(summary, "moderateIntensityMinutes")),
        "vigorous_intensity_minutes": integer(find(summary, "vigorousIntensityMinutes")),
    }
    return health, activity


def _parse_start(value: Any, timezone: str, *, assume_utc: bool = False) -> datetime:
    if not value:
        raise ValueError("Activity is missing a start time")
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC if assume_utc else ZoneInfo(timezone))
    return parsed.astimezone(UTC)


def normalize_activity(source: dict[str, Any], *, timezone: str) -> dict[str, Any]:
    activity_id = integer(find(source, "activityId", "activityID"))
    if activity_id is None or activity_id <= 0:
        raise ValueError("Activity is missing a positive Garmin activity ID")
    kind = find(source, "typeKey", "activityTypeKey", "activityType")
    if isinstance(kind, dict):
        kind = kind.get("typeKey") or kind.get("key")
    start_gmt = source.get("startTimeGMT")
    start = start_gmt or find(source, "startTimeLocal", "startTime")
    return {
        "garmin_activity_id": activity_id,
        "activity_name": find(source, "activityName", "name"),
        "activity_type": str(kind or "unknown"),
        "start_time": _parse_start(start, timezone, assume_utc=start_gmt is not None),
        "duration_seconds": number(find(source, "duration", "elapsedDuration")),
        "moving_duration_seconds": number(find(source, "movingDuration")),
        "distance_m": number(find(source, "distance")),
        "calories": integer(find(source, "calories")),
        "average_hr": integer(find(source, "averageHR", "averageHeartRate")),
        "max_hr": integer(find(source, "maxHR", "maxHeartRate")),
        "average_pace_seconds_per_km": pace_from_speed(find(source, "averageSpeed", "avgSpeed")),
        "best_pace_seconds_per_km": pace_from_speed(find(source, "maxSpeed", "bestSpeed")),
        "average_cadence": number(
            find(source, "averageRunningCadenceInStepsPerMinute", "averageCadence")
        ),
        "elevation_gain_m": number(find(source, "elevationGain", "totalAscent")),
        "elevation_loss_m": number(find(source, "elevationLoss", "totalDescent")),
        "aerobic_training_effect": number(find(source, "aerobicTrainingEffect")),
        "anaerobic_training_effect": number(find(source, "anaerobicTrainingEffect")),
        "training_load": number(find(source, "activityTrainingLoad", "trainingLoad")),
        "recovery_hours": recovery_hours(source),
        "fit_file_path": None,
        "fit_download_status": "pending",
        "fit_download_error": None,
    }
