import math

import pytest
from pydantic import ValidationError

from garmin_health_gateway.streams import Bucket, StreamCursor, StreamWindow, summarize


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_seconds": -1},
        {"start_seconds": 5, "end_seconds": 5},
        {"end_seconds": -1},
        {"end_seconds": math.inf},
        {"start_seconds": math.nan},
        {"resolution_seconds": -1},
        {"resolution_seconds": 1.5},
        {"resolution_seconds": True},
        {"resolution_seconds": 3601},
        {"resolution_seconds": 0},
        {"resolution_seconds": 0, "end_seconds": 10},
        {"resolution_seconds": 0, "start_seconds": 0, "end_seconds": 1801},
    ],
)
def test_invalid_windows_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        StreamWindow(**kwargs)


def test_cursor_round_trip_and_invalid_encoding():
    cursor = StreamCursor(
        activity_id=42, generation_id="generation", start=0, end=10, resolution=5, position=[1]
    )
    assert StreamCursor.decode(cursor.encode()) == cursor
    for value in ("!!", "a" * 2049, "e30", "bnVsbA"):
        with pytest.raises(ValueError, match="invalid stream cursor"):
            StreamCursor.decode(value)


def row(ordinal, elapsed, **fields):
    return {
        "ordinal": ordinal,
        "elapsed_seconds": elapsed,
        "data": dict(elapsed_seconds=elapsed, **fields),
    }


def test_sparse_half_open_buckets_preserve_gaps_zero_and_quality_contribution():
    rows = iter(
        [
            row(0, 0, heart_rate_bpm=None, speed_mps=0, distance_m=0, latitude=1, longitude=2),
            row(1, 4, heart_rate_bpm=100, speed_mps=2, distance_m=8, latitude=2, longitude=3),
            row(
                2,
                10,
                heart_rate_bpm=200,
                speed_mps=4,
                distance_m=40,
                quality_flags=[{"reason": "hr_abrupt_change"}],
            ),
        ]
    )
    results = [result for _, result in summarize(rows, 0, 15, 5)]
    first, gap, flagged = results
    assert first["sample_count"] == 2
    assert first["fields"]["speed_mps"]["value"] == 1
    assert first["fields"]["speed_mps"]["min"] == 0
    assert first["fields"]["heart_rate_bpm"]["count"] == 1
    assert first["fields"]["distance_m"]["value"] == 8
    assert first["fields"]["distance_m"]["first"] == 0
    assert first["fields"]["latitude"]["value"] is None
    assert first["fields"]["latitude"]["first"] == 1
    assert first["fields"]["latitude"]["last"] == 2
    assert gap["gap"] and gap["sample_count"] == 0
    assert gap["fields"]["heart_rate_bpm"]["value"] is None
    assert flagged["fields"]["heart_rate_bpm"]["value"] == 200
    assert flagged["quality"]["flagged_samples_included"]
    assert flagged["quality"]["reason_counts"] == {"hr_abrupt_change": 1}


def test_clipped_window_is_anchored_at_zero_and_end_exclusive():
    rows = iter([row(1, 3, heart_rate_bpm=100), row(2, 5, heart_rate_bpm=200)])
    result = list(summarize(rows, 3, 5, 5))
    assert len(result) == 1
    assert result[0][1]["bucket_start_seconds"] == 3
    assert result[0][1]["sample_count"] == 1


def test_flag_summaries_are_bounded_for_large_buckets():
    bucket = Bucket(0, 3600)
    for i in range(10000):
        bucket.add(row(i, 0, quality_flags=[{"reason": "test"}]))
    result = bucket.result()
    assert result["quality"]["reason_counts"] == {"test": 10000}
    assert len(result["quality"]["example_flagged_ordinals"]) == 10
