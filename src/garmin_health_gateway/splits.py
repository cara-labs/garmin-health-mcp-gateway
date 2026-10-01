"""Separately labeled kilometer splits from adjacent recorded distance samples."""

from collections.abc import Iterable
from typing import Any

SPLIT_VERSION = "km-splits/1"
MAX_DISTANCE_GAP_SECONDS = 30


def calculated_splits(rows: Iterable[dict[str, Any]]):
    """Stream split evidence; unsupported continuity ends reliable derivation.

    Split boundary time is linearly interpolated between adjacent usable samples.
    Timer interpolation is available only when both samples carry timer evidence.
    Never bridge a reset, missing distance/time, or long unobserved movement gap.
    """
    previous = None
    origin = None
    boundary = 1000.0
    split_index = 0
    pending_limitations = []

    def result(end_distance, end_elapsed, end_timer, partial, interpolation):
        nonlocal split_index, origin
        split_index += 1
        distance, elapsed, timer = origin
        timer_duration = None if timer is None or end_timer is None else end_timer - timer
        row = {
            "split_number": split_index,
            "kind": "calculated_kilometer_split",
            "derivation_version": SPLIT_VERSION,
            "distance_source": "FIT cumulative distance",
            "start_distance_m": distance,
            "end_distance_m": end_distance,
            "distance_m": end_distance - distance,
            "start_elapsed_seconds": elapsed,
            "end_elapsed_seconds": end_elapsed,
            "elapsed_duration_seconds": end_elapsed - elapsed,
            "timer_duration_seconds": timer_duration,
            "pause_seconds": None
            if timer_duration is None
            else end_elapsed - elapsed - timer_duration,
            "partial": partial,
            "boundary_interpolated": interpolation,
            "interpolation_method": "linear_adjacent_distance" if interpolation else None,
            "availability": "partial" if pending_limitations else "available",
            "limitations": list(pending_limitations),
            "missing": {"timer_duration_seconds": "timer_state_unknown"}
            if timer_duration is None
            else {},
        }
        origin = (end_distance, end_elapsed, end_timer)
        return row

    for sample in rows:
        data = sample.get("data", sample)
        distance, elapsed, timer = (
            data.get("distance_m"),
            data.get("elapsed_seconds"),
            data.get("timer_seconds"),
        )
        if distance is None or elapsed is None or distance < 0 or elapsed < 0:
            if previous is not None:
                yield {
                    "kind": "calculated_split_limit",
                    "availability": "partial",
                    "reason": "missing_distance_or_timing",
                    "derivation_version": SPLIT_VERSION,
                }
                return
            pending_limitations.append("leading_samples_missing_distance_or_timing")
            continue
        if previous is None:
            previous = (distance, elapsed, timer)
            origin = previous
            boundary = (int(distance // 1000) + 1) * 1000.0
            if distance != 0:
                pending_limitations.append("leading_distance_unrecorded")
            continue
        pd, pe, pt = previous
        if distance < pd or elapsed < pe or (timer is not None and pt is not None and timer < pt):
            yield {
                "kind": "calculated_split_limit",
                "availability": "partial",
                "reason": "distance_or_timing_reset",
                "derivation_version": SPLIT_VERSION,
            }
            return
        if distance > pd and (elapsed == pe or elapsed - pe > MAX_DISTANCE_GAP_SECONDS):
            yield {
                "kind": "calculated_split_limit",
                "availability": "partial",
                "reason": "unresolvable_distance_gap",
                "derivation_version": SPLIT_VERSION,
            }
            return
        while distance >= boundary:
            ratio = (boundary - pd) / (distance - pd)
            at_elapsed = pe + ratio * (elapsed - pe)
            at_timer = None if pt is None or timer is None else pt + ratio * (timer - pt)
            yield result(
                boundary, at_elapsed, at_timer, boundary - origin[0] < 1000, ratio not in (0, 1)
            )
            boundary += 1000
        previous = (distance, elapsed, timer)
    if previous is None:
        yield {
            "kind": "calculated_split_limit",
            "availability": "not_recorded",
            "reason": "no_usable_distance",
            "derivation_version": SPLIT_VERSION,
        }
    elif previous[0] > origin[0]:
        yield result(*previous, partial=True, interpolation=False)
