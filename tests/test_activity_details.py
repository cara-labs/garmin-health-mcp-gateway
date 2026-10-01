from garmin_health_gateway.activity_details import recorded_lap, recovery_evidence, sanitize_metric
from garmin_health_gateway.splits import calculated_splits


def test_recorded_trigger_classification_does_not_invent_manual_or_auto():
    for trigger, expected in [
        ("manual", "manual"),
        ("time", "automatic"),
        ("distance", "automatic"),
        (None, "unknown"),
        ("session_end", "unknown"),
    ]:
        result = recorded_lap({"native": {"lap_trigger": trigger}, "fields": []}, 1)
        assert result["trigger_classification"] == expected
        assert result["fields"]["average_hr_bpm"]["value"] is None
        assert result["fields"]["distance_m"]["missing_reason"] == "not_recorded"
        assert result["workout_step_index"] is None


def test_metric_identifiers_are_omitted_and_unknown_fields_retained():
    data = {
        "native": {"serial_number": 123, "product": 42, "250": 9},
        "fields": [
            {"name": "serial_number", "value": 123},
            {"name": "250", "number": 250, "value": 9, "unit": None},
        ],
    }
    result = sanitize_metric(data)
    assert "serial_number" not in result["native"]
    assert result["native"]["product"] == 42
    assert result["fields"][0]["number"] == 250


def test_numeric_recovery_event_is_unchanged_without_inferred_readings_or_protocol():
    evidence = recovery_evidence(
        {"native": {"event": "recovery_hr", "data": 120, "timestamp": "2026-08-20T12:00:00Z"}}
    )
    assert evidence["stored_event_data"] == 120
    assert evidence["value"] == 120
    assert isinstance(evidence["value"], int)
    assert evidence["baseline_bpm"] is evidence["recovery_bpm"] is None
    assert evidence["interval_seconds"] is None
    assert evidence["unit"] is None
    assert recovery_evidence({"native": {"recovery_hours": 24}}) is None


def test_missing_and_zero_recovery_values_are_distinct():
    zero = recovery_evidence({"native": {"event": "recovery_hr", "data": 0}})
    missing = recovery_evidence({"native": {"event": "recovery_hr"}})
    assert zero["value"] == 0 and zero["availability"] == "available"
    assert zero["observed_at"] is None
    assert missing["value"] is None and missing["availability"] == "not_recorded"


def test_calculated_2_point_4_km_splits_and_partial_are_separate():
    samples = [
        {"distance_m": distance, "elapsed_seconds": i * 20, "timer_seconds": i * 20}
        for i, distance in enumerate([0, 600, 1200, 1800, 2400])
    ]
    splits = list(calculated_splits(samples))
    assert [row["distance_m"] for row in splits] == [1000, 1000, 400]
    assert [row["partial"] for row in splits] == [False, False, True]
    assert splits[0]["boundary_interpolated"] is True
    assert splits[0]["kind"] == "calculated_kilometer_split"


def test_calculated_splits_handle_pause_reset_gap_and_missing_evidence():
    pause = list(
        calculated_splits(
            [
                {"distance_m": 0, "elapsed_seconds": 0, "timer_seconds": 0},
                {"distance_m": 0, "elapsed_seconds": 20, "timer_seconds": 0},
                {"distance_m": 1000, "elapsed_seconds": 40, "timer_seconds": 20},
            ]
        )
    )
    assert pause[0]["elapsed_duration_seconds"] == 40
    assert pause[0]["timer_duration_seconds"] == 20
    assert pause[0]["pause_seconds"] == 20
    for samples, expected in [
        (
            [{"distance_m": 500, "elapsed_seconds": 0}, {"distance_m": 0, "elapsed_seconds": 1}],
            "distance_or_timing_reset",
        ),
        (
            [{"distance_m": 0, "elapsed_seconds": 0}, {"distance_m": 1000, "elapsed_seconds": 60}],
            "unresolvable_distance_gap",
        ),
        (
            [{"distance_m": 0, "elapsed_seconds": 0}, {"distance_m": None, "elapsed_seconds": 1}],
            "missing_distance_or_timing",
        ),
        ([], "no_usable_distance"),
    ]:
        assert list(calculated_splits(samples))[-1]["reason"] == expected
