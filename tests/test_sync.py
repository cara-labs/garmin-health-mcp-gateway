from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from garmin_health_gateway.config import Settings
from garmin_health_gateway.sync import SyncEngine, date_range


class FakeDatabase:
    def __init__(self) -> None:
        self.health = []
        self.daily = []
        self.activities = []
        self.states = {}

    def start_sync(self, resource):
        self.states[resource] = "running"

    def finish_sync(self, resource, error=None):
        self.states[resource] = "error" if error else "success"

    def upsert_daily_health(self, value):
        self.health.append(value)

    def upsert_daily_activity(self, value):
        self.daily.append(value)

    def upsert_activity(self, value):
        self.activities.append(value)

    def has_successful_sync(self, resource):
        return self.states.get(resource) == "success"


class FakeProvider:
    def get_daily_summary(self, target):
        return {"totalSteps": 1000, "restingHeartRate": 50}

    def get_sleep(self, target):
        return {"sleepTimeSeconds": 28800}

    def get_hrv(self, target):
        return {"lastNightAvg": 55}

    def get_body_battery(self, target):
        return []

    def get_stress(self, target):
        return {"avgStressLevel": 20}

    def get_training_readiness(self, target):
        return {"score": 80, "recoveryTime": 120}

    def get_training_status(self, target):
        return {"trainingStatus": "PRODUCTIVE"}

    def get_vo2max(self, target):
        return {"vo2MaxPreciseValue": 50}

    def get_fitness_age(self, target):
        return {"fitnessAge": 35}

    def get_lactate_threshold(self, target):
        return {"speed": [{"value": 4.0}], "heart_rate": [{"value": 170}]}

    def get_activities(self, start, end):
        return [
            {
                "activityId": 99,
                "activityType": {"typeKey": "running"},
                "startTimeGMT": "2026-08-28 12:00:00",
                "duration": 1200,
                "distance": 4000,
            }
        ]

    def download_fit(self, activity_id):
        return b"fit-data"


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url="postgresql://unused",
        fit_archive=tmp_path / "fit",
        token_store=tmp_path / "tokens",
        timezone="UTC",
        daily_backfill_days=2,
        activity_backfill_days=2,
        recent_days=1,
        sync_interval_seconds=3600,
        request_delay_seconds=0,
        mcp_host="127.0.0.1",
        mcp_port=8000,
        log_level="INFO",
        garmin_email=None,
        garmin_password=None,
        mcp_reader_password=None,
        sync_request_dir=tmp_path / "requests",
    )


def test_date_range_is_inclusive() -> None:
    assert list(date_range(date(2026, 8, 27), date(2026, 8, 28))) == [
        date(2026, 8, 27),
        date(2026, 8, 28),
    ]


def test_sync_writes_normalized_rows_and_fit_archive(tmp_path) -> None:
    database = FakeDatabase()
    engine = SyncEngine(_settings(tmp_path), database, FakeProvider())

    assert engine.sync_daily(date(2026, 8, 28), date(2026, 8, 28)) == 1
    assert engine.sync_activities(date(2026, 8, 28), date(2026, 8, 28)) == 1
    assert database.health[0]["recovery_hours"] == 2
    assert database.health[0]["lactate_threshold_hr"] == 170
    assert database.health[0]["lactate_threshold_pace_seconds_per_km"] == 250
    assert database.activities[0]["start_time"] == datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    assert database.activities[0]["fit_download_status"] == "archived"
    fit_path = tmp_path / "fit" / "2026" / "08" / "99.fit"
    assert fit_path.read_bytes() == b"fit-data"


def test_authentication_failures_are_not_hidden_as_optional_data(tmp_path) -> None:
    class GarminConnectAuthenticationError(Exception):
        pass

    engine = SyncEngine(_settings(tmp_path), FakeDatabase(), FakeProvider())
    with pytest.raises(GarminConnectAuthenticationError):
        engine._optional(
            "sleep",
            date(2026, 8, 28),
            lambda: (_ for _ in ()).throw(GarminConnectAuthenticationError()),
        )


def test_missing_fit_is_recorded_without_blocking_other_activity_data(tmp_path) -> None:
    class ProviderWithoutFit(FakeProvider):
        def download_fit(self, activity_id):
            raise FileNotFoundError("Garmin has no FIT for this manual activity")

    database = FakeDatabase()
    engine = SyncEngine(_settings(tmp_path), database, ProviderWithoutFit())

    assert engine.sync_activities(date(2026, 8, 28), date(2026, 8, 28)) == 1
    assert database.activities[0]["fit_file_path"] is None
    assert database.activities[0]["fit_download_status"] == "error"
    assert "FileNotFoundError" in database.activities[0]["fit_download_error"]
