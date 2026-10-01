"""Seed an owned isolated installation only; never use on a real account database."""

import sys
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, "/testfixtures")
from fit_factory import BASE_TIME, FIT_EPOCH, fit_file, message, record, u32

from garmin_health_gateway.config import Settings
from garmin_health_gateway.db import Database
from garmin_health_gateway.enrichment import ObservationStore

settings = Settings.from_env()
database = Database(settings.database_url)
try:
    with database.connection() as conn:
        # Refuse to seed a non-empty database; accidental use cannot overwrite real activities.
        assert conn.execute("SELECT count(*) AS n FROM activities").fetchone()["n"] == 0
        path = settings.fit_archive / "synthetic-install-42.fit"
        conn.execute(
            "INSERT INTO activities(garmin_activity_id,activity_type,start_time,fit_file_path) "
            "VALUES (42,'running',%s,%s)",
            (FIT_EPOCH + timedelta(seconds=BASE_TIME + 1000), str(path)),
        )
    path.write_bytes(
        fit_file(
            [
                record(1000),
                record(1005, hr=0),
                message(
                    21,
                    [(253, 0x86, u32(BASE_TIME + 1010)), (0, 0, bytes([21])), (3, 0x86, u32(120))],
                ),
            ]
        )
    )
    provider = SimpleNamespace(
        get_activity_weather=lambda activity_id: {"temp": 0, "issueDate": "2020-01-01T10:00:00Z"},
        get_configured_training_profile=lambda: {
            "heart_rate_zones": [{"sport": "RUNNING", "maxHeartRateUsed": 190}]
        },
    )
    store = ObservationStore(database)
    assert store.collect("weather_recorded", provider, 42) == "success"
    assert store.collect("training_profile", provider) == "success"
finally:
    database.close()
