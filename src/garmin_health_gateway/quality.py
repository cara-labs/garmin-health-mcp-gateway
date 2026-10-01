"""Conservative suspicion rules; never mutate, exclude, or repair measurements."""

from __future__ import annotations

import math
from typing import Any

DETECTOR_VERSION = "hr-gps/1"


def distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    delta_p = p2 - p1
    delta_l = math.radians(b_lon - a_lon)
    term = math.sin(delta_p / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(delta_l / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, term))))


def _valid_position(row: dict[str, Any]) -> bool:
    lat, lon = row.get("latitude"), row.get("longitude")
    return (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def quality_flags(current: dict[str, Any], previous: dict[str, Any] | None) -> list[dict]:
    flags = []

    def flag(reason: str, **evidence):
        flags.append(
            {
                "reason": reason,
                "detector_version": DETECTOR_VERSION,
                "suspected": True,
                "evidence": evidence,
            }
        )

    hr = current.get("heart_rate_bpm")
    if hr is not None and (not isinstance(hr, (int, float)) or not 20 <= hr <= 250):
        flag("hr_outside_conservative_range", minimum_bpm=20, maximum_bpm=250)
    lat, lon = current.get("latitude"), current.get("longitude")
    if (lat is not None or lon is not None) and not _valid_position(current):
        flag("gps_invalid_or_incomplete_coordinate")
    elapsed = current.get("elapsed_seconds")
    if elapsed is None:
        flag("timing_missing")
    elif elapsed < 0:
        flag("timestamp_before_activity_start")
    if previous is None:
        return flags
    prior_elapsed = previous.get("elapsed_seconds")
    if elapsed is None or prior_elapsed is None:
        return flags
    delta = elapsed - prior_elapsed
    if delta < 0:
        flag("timestamp_out_of_order", delta_seconds=delta)
        return flags
    prior_hr = previous.get("heart_rate_bpm")
    if (
        isinstance(hr, (int, float))
        and isinstance(prior_hr, (int, float))
        and 0 < delta <= 5
        and abs(hr - prior_hr) > 35
    ):
        flag("hr_abrupt_change", change_bpm=hr - prior_hr, interval_seconds=delta)
    if _valid_position(current) and _valid_position(previous):
        displacement = distance_m(previous["latitude"], previous["longitude"], lat, lon)
        if 0 < delta <= 30 and displacement / delta > 50:
            flag(
                "gps_implausible_jump",
                displacement_m=displacement,
                interval_seconds=delta,
                threshold_mps=50,
            )
        elif delta == 0 and displacement > 100:
            flag("gps_duplicate_time_displacement", displacement_m=displacement, threshold_m=100)
    return flags
