from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from garmin_health_gateway.normalize import normalize_activity, normalize_daily


def test_normalize_daily_handles_nested_garmin_shapes() -> None:
    health, activity = normalize_daily(
        date(2026, 8, 28),
        {
            "summary": {
                "restingHeartRate": 48,
                "totalSteps": 12345,
                "totalDistanceMeters": 9876.5,
                "activeKilocalories": 650,
                "floorsAscended": 12,
                "moderateIntensityMinutes": 20,
                "vigorousIntensityMinutes": 15,
            },
            "sleep": {
                "dailySleepDTO": {
                    "sleepTimeSeconds": 27000,
                    "deepSleepSeconds": 5400,
                    "remSleepSeconds": 7200,
                    "lightSleepSeconds": 14400,
                    "awakeSleepSeconds": 1200,
                    "sleepScores": {"overall": {"value": 82}},
                }
            },
            "hrv": {"hrvSummary": {"lastNightAvg": 58, "status": "BALANCED"}},
            "body_battery": [{"bodyBatteryValuesArray": [[1, 18], [2, 79]]}],
            "stress": {"avgStressLevel": 24},
            "training_readiness": {"score": 76, "level": "HIGH", "recoveryTime": 8},
            "training_status": {"trainingStatus": "PRODUCTIVE", "acuteTrainingLoad": 512},
            "vo2max": {"vo2MaxPreciseValue": 51.4, "runningVo2Max": 52.1},
            "fitness_age": {"fitnessAge": 35.5},
            "lactate_threshold": {"heartRate": 171, "pace": 245},
        },
    )

    assert health["resting_hr"] == 48
    assert health["sleep_minutes"] == 450
    assert health["sleep_score"] == 82
    assert health["body_battery_low"] == 18
    assert health["body_battery_high"] == 79
    assert health["hrv_average"] == 58
    assert health["training_readiness"] == 76
    assert activity["steps"] == 12345
    assert activity["distance_m"] == 9876.5


def test_normalize_activity_converts_speed_to_pace_and_gmt_to_utc() -> None:
    result = normalize_activity(
        {
            "activityId": 123,
            "activityName": "Morning Run",
            "activityType": {"typeKey": "running"},
            "startTimeGMT": "2026-08-28 14:30:00",
            "duration": 1800,
            "distance": 5000,
            "averageSpeed": 2.7777777778,
            "maxSpeed": 4.1666666667,
            "averageHR": 145,
            "maxHR": 171,
        },
        timezone="America/Los_Angeles",
    )
    assert result["garmin_activity_id"] == 123
    assert result["activity_type"] == "running"
    assert result["start_time"] == datetime(2026, 8, 28, 14, 30, tzinfo=UTC)
    assert result["average_pace_seconds_per_km"] == pytest.approx(360)
    assert result["best_pace_seconds_per_km"] == pytest.approx(240)


def test_normalize_activity_rejects_missing_id() -> None:
    with pytest.raises(ValueError, match="activity ID"):
        normalize_activity({"startTimeGMT": "2026-08-28 14:30:00"}, timezone="UTC")
