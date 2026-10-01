from unittest.mock import Mock

import pytest
from garminconnect import Garmin

from garmin_health_gateway.config import Settings
from garmin_health_gateway.provider import GarminConnectProvider

# Field/type subsets captured read-only from the pinned client on the Pi.
# Only names/types were returned; all values below are original synthetic data.
WEATHER = {
    "issueDate": "2026-08-20T10:00:00Z",
    "temp": 0,
    "apparentTemp": None,
    "dewPoint": 0,
    "relativeHumidity": 60,
    "windSpeed": 0,
    "windDirection": 0,
    "weatherStationDTO": {"id": "SYNTHETIC", "name": "Synthetic", "timezone": None},
    "latitude": 1.0,
    "longitude": 2.0,
}
ZONES = [
    {
        "sport": "RUNNING",
        "trainingMethod": "LTHR",
        "maxHeartRateUsed": 190,
        "restingHeartRateUsed": 50,
        "lactateThresholdHeartRateUsed": 170,
        "zone1Floor": 90,
        "zone2Floor": 110,
        "zone3Floor": 130,
        "zone4Floor": 150,
        "zone5Floor": 170,
        "changeState": "UNCHANGED",
        "restingHrAutoUpdateUsed": False,
    }
]


def provider(tmp_path, payloads):
    client = Garmin()
    client.connectapi = Mock(side_effect=payloads)
    adapter = GarminConnectProvider(
        email=None, password=None, token_store=tmp_path, request_delay_seconds=0
    )
    adapter._client = client
    return adapter, client


def test_real_pinned_client_weather_call_shape_preserves_missing_and_zero(tmp_path):
    adapter, client = provider(tmp_path, [WEATHER])
    assert adapter.get_activity_weather(42) == WEATHER
    client.connectapi.assert_called_once_with("/activity-service/activity/42/weather")


def test_real_pinned_client_profile_calls_keep_sport_scopes(tmp_path):
    general = dict(ZONES[0], sport="DEFAULT", trainingMethod="HRR", zone2Floor=100)
    raw_profile = {"userData": {"lactateThresholdHeartRate": 170, "lactateThresholdSpeed": None}}
    adapter, client = provider(
        tmp_path, [raw_profile, {"measurementSystem": "METRIC"}, [general, *ZONES]]
    )
    result = adapter.get_configured_training_profile()
    assert result["heart_rate_zones"][0]["sport"] == "DEFAULT"
    assert result["heart_rate_zones"][1]["zone2Floor"] == 110
    assert result["profile"] == raw_profile
    assert client.connectapi.call_count == 3


@pytest.mark.parametrize("payload", [None, [], "bad"])
def test_weather_unexpected_outer_shape_is_explicit(tmp_path, payload):
    adapter, _ = provider(tmp_path, [payload])
    with pytest.raises(ValueError, match="response shape"):
        adapter.get_activity_weather(42)


def test_profile_bad_zones_rejected(tmp_path):
    adapter, _ = provider(tmp_path, [{}, {}, ["not a zone"]])
    with pytest.raises(ValueError, match="response shape"):
        adapter.get_configured_training_profile()


def test_weather_configuration_is_opt_in(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.delenv("GARMIN_HISTORICAL_WEATHER", raising=False)
    assert Settings.from_env().historical_weather_enabled is False
    monkeypatch.setenv("GARMIN_HISTORICAL_WEATHER", "true")
    assert Settings.from_env().historical_weather_enabled is True
