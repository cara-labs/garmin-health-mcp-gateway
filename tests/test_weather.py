import io
import json
from unittest.mock import Mock

import pytest

from garmin_health_gateway.enrichment import normalize_profile, number, timestamp
from garmin_health_gateway.weather import (
    MAX_RESPONSE_BYTES,
    VARIABLES,
    HistoricalWeather,
    cache_key,
    circular_mean,
)


def test_circular_wind_and_undefined_direction():
    assert circular_mean([350, 10]) == pytest.approx(0)
    assert circular_mean([90, 270]) is None
    assert circular_mean([]) is None


def test_profile_scopes_missing_zero_and_no_derived_ceilings():
    profile = normalize_profile(
        {
            "heart_rate_zones": [
                {
                    "sport": "RUNNING",
                    "maxHeartRateUsed": 190,
                    "restingHeartRateUsed": 0,
                    "zone1Floor": 90,
                    "trainingMethod": "LTHR",
                },
                {"sport": "DEFAULT", "zone1Floor": 80},
                {"sport": "CYCLING", "zone1Floor": 85},
            ],
            "profile": {"userData": {"birthDate": "1990-01-01", "lactateThresholdHeartRate": 170}},
        }
    )
    assert profile["running"][0]["configured_resting_hr"] == 0
    assert profile["general"][0]["maximum_hr"] is None
    assert profile["general"][0]["calculation_method"] is None
    assert profile["general"][0]["zone_floors_bpm"] == [80, None, None, None, None]
    assert profile["running"][0]["zone_ceilings_bpm"] is None
    assert profile["other"][0]["sport"] == "CYCLING"
    assert "birthDate" not in str(profile)


def test_finite_numeric_and_timezone_evidence():
    assert number(0) == 0
    assert number(True) is None
    assert number(float("nan")) is None
    assert timestamp("2020-01-01T12:00:00") is None
    assert timestamp("2020-01-01T12:00:00Z").utcoffset().total_seconds() == 0


def weather_payload():
    units = {
        "temperature_2m": "°C",
        "relative_humidity_2m": "%",
        "dew_point_2m": "°C",
        "wind_speed_10m": "m/s",
        "wind_direction_10m": "°",
        "cloud_cover": "%",
        "shortwave_radiation": "W/m²",
    }
    return {
        "latitude": 1.1,
        "longitude": 2.1,
        "hourly_units": units,
        "hourly": {"time": [1577836800], **{k: [0] for k, _ in VARIABLES.values()}},
    }


def test_historical_timeout_bounded_url_units_missing_and_rate():
    payload = weather_payload()
    del payload["hourly"]["dew_point_2m"]
    payload["hourly_units"]["wind_speed_10m"] = "unexpected"
    opener = Mock(side_effect=lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
    sleeper = Mock()
    client = HistoricalWeather(timeout=7, delay=2, opener=opener, sleeper=sleeper)
    point = {"latitude": 1, "longitude": 2, "hour": 1577836800}
    result = client.fetch(point)
    client.fetch(point)
    assert result["values"]["temperature"] == 0
    assert result["values"]["dew_point"] is None
    assert result["missing"]["dew_point"] == "provider_field_missing"
    assert result["units"]["wind_speed"] == "unexpected"
    assert "models=era5" in opener.call_args.args[0]
    assert opener.call_args.kwargs["timeout"] == 7
    assert sleeper.call_count == 1
    assert cache_key(point) == cache_key(dict(point))
    assert result["station_distance_m"] is None


@pytest.mark.parametrize(
    "body,match",
    [
        (b"x" * (MAX_RESPONSE_BYTES + 1), "response_limit"),
        (b'{"hourly":{"time":[]}}', "hour_missing"),
    ],
)
def test_historical_response_limits(body, match):
    client = HistoricalWeather(opener=lambda *a, **kw: io.BytesIO(body))
    with pytest.raises(ValueError, match=match):
        client.fetch({"latitude": 1, "longitude": 2, "hour": 1577836800})
