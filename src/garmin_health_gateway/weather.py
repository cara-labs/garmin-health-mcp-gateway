"""Bounded collector-side historical weather; MCP reads stored evidence only."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from urllib.request import urlopen

from psycopg.types.json import Jsonb

from .contracts import AnalysisResponse, EvidenceSource, Freshness, field_evidence
from .enrichment import ObservationStore, errors, number
from .quality import distance_m

MAX_POINTS = 10
MAX_RESPONSE_BYTES = 512 * 1024
ATTRIBUTION = "Open-Meteo / ECMWF ERA5, CC BY 4.0; https://open-meteo.com/"
VARIABLES = {
    "temperature": ("temperature_2m", "degC"),
    "relative_humidity": ("relative_humidity_2m", "%"),
    "dew_point": ("dew_point_2m", "degC"),
    "wind_speed": ("wind_speed_10m", "m/s"),
    "wind_direction": ("wind_direction_10m", "degree"),
    "cloud_cover": ("cloud_cover", "%"),
    "shortwave_radiation": ("shortwave_radiation", "W/m2"),
}
RECORDED_KEYS = {
    "temperature": "temp",
    "apparent_temperature": "apparentTemp",
    "relative_humidity": "relativeHumidity",
    "dew_point": "dewPoint",
    "wind_speed": "windSpeed",
    "wind_direction": "windDirection",
    "cloud_cover": "cloudCover",
    "shortwave_radiation": "shortwaveRadiation",
}


def circular_mean(values: list[float]) -> float | None:
    if not values:
        return None
    sine = sum(math.sin(math.radians(v)) for v in values)
    cosine = sum(math.cos(math.radians(v)) for v in values)
    if math.hypot(sine, cosine) < 1e-10:
        return None  # Opposing directions do not have an identifiable mean.
    result = math.degrees(math.atan2(sine, cosine)) % 360
    return 0.0 if math.isclose(result, 360, abs_tol=1e-10) else result


def cache_key(point: dict) -> str:
    content = json.dumps(["era5/hourly/v1", point["latitude"], point["longitude"], point["hour"]])
    return hashlib.sha256(content.encode()).hexdigest()


class HistoricalWeather:
    def __init__(
        self, *, timeout: float = 10, delay: float = 2, opener=urlopen, sleeper=time.sleep
    ):
        self.timeout, self.delay = timeout, delay
        self.opener, self.sleeper = opener, sleeper
        self.last_request = None

    def fetch(self, point: dict) -> dict:
        now = time.monotonic()
        if self.last_request is not None:
            self.sleeper(max(0, self.delay - (now - self.last_request)))
        self.last_request = time.monotonic()
        hour = datetime.fromtimestamp(point["hour"], UTC)
        params = {
            "latitude": point["latitude"],
            "longitude": point["longitude"],
            "start_date": hour.date().isoformat(),
            "end_date": hour.date().isoformat(),
            "hourly": ",".join(v[0] for v in VARIABLES.values()),
            "models": "era5",
            "timezone": "UTC",
            "timeformat": "unixtime",
            "wind_speed_unit": "ms",
        }
        url = "https://archive-api.open-meteo.com/v1/archive?" + urlencode(params)
        with self.opener(url, timeout=self.timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("historical_response_limit")
        payload = json.loads(body)
        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        if point["hour"] not in times:
            raise ValueError("historical_hour_missing")
        index = times.index(point["hour"])
        fields, units, missing = {}, {}, {}
        expected_units = {"degC": "°C", "%": "%", "m/s": "m/s", "degree": "°", "W/m2": "W/m²"}
        for name, (key, unit) in VARIABLES.items():
            values = hourly.get(key, [])
            value = number(values[index]) if index < len(values) else None
            # An unexpected unit cannot be silently relabeled.
            supplied_unit = payload.get("hourly_units", {}).get(key)
            units[name] = unit if supplied_unit == expected_units[unit] else supplied_unit
            fields[name] = value
            if value is None:
                missing[name] = "provider_field_missing"
        return {
            "values": fields,
            "units": units,
            "missing": missing,
            "requested_point": point,
            "grid_latitude": number(payload.get("latitude")),
            "grid_longitude": number(payload.get("longitude")),
            "grid_resolution_km": 25,
            "matching_method": "ten_or_fewer_elapsed_stratified_route_samples_nearest_hour",
            "attribution": ATTRIBUTION,
            "station_distance_m": None,
            "station_distance_missing_reason": "modeled_grid_not_station",
        }


class WeatherCollector:
    def __init__(self, database, *, enabled: bool = False, client: HistoricalWeather | None = None):
        self.database = database
        self.store = ObservationStore(database)
        self.enabled = enabled
        self.client = client or HistoricalWeather()

    def plan(self, activity_id: int) -> tuple[list[dict], str | None]:
        with self.database.connection() as conn:
            activity = conn.execute(
                "SELECT start_time FROM activities WHERE garmin_activity_id=%s", (activity_id,)
            ).fetchone()
            generation = conn.execute(
                "SELECT generation_id FROM fit_active_generations WHERE activity_id=%s",
                (activity_id,),
            ).fetchone()
            if not activity:
                return [], "activity_not_found"
            if not generation:
                return [], "fit_not_processed"
            rows = conn.execute(
                """WITH valid AS (
                SELECT ordinal,observed_at,data,
                row_number() OVER (ORDER BY observed_at,ordinal) AS n,
                min(observed_at) OVER () AS first_at,
                max(observed_at) OVER () AS last_at FROM fit_samples
                WHERE generation_id=%s AND observed_at IS NOT NULL
                AND jsonb_typeof(data->'latitude')='number'
                AND jsonb_typeof(data->'longitude')='number'
                AND (data->>'latitude')::double precision BETWEEN -90 AND 90
                AND (data->>'longitude')::double precision BETWEEN -180 AND 180),
                ranked AS (SELECT *,row_number() OVER
                (PARTITION BY floor(extract(epoch FROM (observed_at-first_at)) * 9 /
                greatest(extract(epoch FROM (last_at-first_at)),1)) ORDER BY n) AS pick
                FROM valid) SELECT * FROM ranked WHERE pick=1 ORDER BY n LIMIT 10""",
                (generation["generation_id"],),
            ).fetchall()
        if not rows:
            return [], "route_location_or_time_missing"
        points = []
        for row in rows:
            observed = row["observed_at"]
            if observed.year < 1940:
                return [], "outside_era5_date_coverage"
            if observed > datetime.now(UTC) - timedelta(days=5):
                return [], "era5_recent_date_delay"
            point = {
                "latitude": round(row["data"]["latitude"], 5),
                "longitude": round(row["data"]["longitude"], 5),
                "hour": int(
                    (observed + timedelta(minutes=30))
                    .replace(minute=0, second=0, microsecond=0)
                    .timestamp()
                ),
                "route_observed_at": observed.isoformat(),
                "ordinal": row["ordinal"],
                "quality_flags": row["data"].get("quality_flags", []),
            }
            points.append(point)
        return points, None

    def collect(self, activity_id: int) -> str:
        resource = "weather_historical"
        if not self.enabled:
            self.store.attempt(resource, activity_id, "unsupported", "lookup_not_opted_in")
            return "unsupported"
        points, reason = self.plan(activity_id)
        if reason:
            status = (
                "pending"
                if reason in ("era5_recent_date_delay", "fit_not_processed")
                else "unsupported"
                if reason == "outside_era5_date_coverage"
                else "not_recorded"
            )
            self.store.attempt(
                resource,
                activity_id,
                status,
                reason,
                retry_after=datetime.now(UTC) + timedelta(days=1) if status == "pending" else None,
            )
            return status
        keys = [cache_key(p) for p in points]
        completed = 0
        code = None
        for point, key in zip(points, keys, strict=True):
            with self.database.connection() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM weather_cache WHERE cache_key=%s", (key,)
                ).fetchone()
            if not existing:
                try:
                    payload = self.client.fetch(point)
                    with self.database.connection() as conn:
                        conn.execute(
                            "INSERT INTO weather_cache(cache_key,observed_at,payload) "
                            "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (key, datetime.fromtimestamp(point["hour"], UTC), Jsonb(payload)),
                        )
                except Exception as error:
                    code = type(error).__name__
                    break  # Never amplify provider failures with the remaining point requests.
            completed += 1
        status = "success" if completed == len(points) else "partial" if completed else "error"
        self.store.attempt(
            resource,
            activity_id,
            status,
            code,
            {"points": points, "cache_keys": keys, "completed": completed},
            datetime.now(UTC) + timedelta(hours=1) if code else None,
        )
        return status


def get_activity_weather(database, activity_id: int) -> dict:
    store = ObservationStore(database)
    recorded = store.latest("weather_recorded", activity_id)
    recorded_state = store.state("weather_recorded", activity_id)
    historical = store.state("weather_historical", activity_id)
    sources, fields, observations = [], {}, []
    with database.connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM activities WHERE garmin_activity_id=%s", (activity_id,)
        ).fetchone()
        cached = conn.execute(
            "SELECT * FROM weather_cache WHERE cache_key=ANY(%s) LIMIT 10",
            (historical["details"].get("cache_keys", []) if historical else [],),
        ).fetchall()
        wearable = conn.execute(
            """SELECT count(*) AS count,
            min((s.data->>'wearable_temperature_c')::double precision) AS minimum,
            max((s.data->>'wearable_temperature_c')::double precision) AS maximum,
            avg((s.data->>'wearable_temperature_c')::double precision) AS mean,
            min(s.observed_at) AS first_at,max(s.observed_at) AS last_at
            FROM fit_active_generations a JOIN fit_samples s ON s.generation_id=a.generation_id
            WHERE a.activity_id=%s AND jsonb_typeof(s.data->'wearable_temperature_c')='number'""",
            (activity_id,),
        ).fetchone()
    if not exists:
        return AnalysisResponse(availability="not_found", activity_id=activity_id).as_dict()
    if recorded:
        sid = "garmin-weather/" + str(recorded["id"])
        sources.append(
            EvidenceSource(
                id=sid,
                provider="Garmin Connect",
                kind="recorded",
                observed_at=recorded["observed_at"],
                collected_at=recorded["collected_at"],
            )
        )
        payload = recorded["payload"]
        values = {name: number(payload.get(key)) for name, key in RECORDED_KEYS.items()}
        observations.append(
            {
                "source_id": sid,
                "values": values,
                "station": payload.get("weatherStationDTO", payload.get("stationDTO")),
                "station_distance_m": None,
                "station_distance_missing_reason": "station_coordinates_not_supplied",
                "matching_method": "Garmin_activity_association",
                "units": {name: None for name in values},
            }
        )
        for name, value in values.items():
            fields[f"{sid}.{name}"] = field_evidence(
                value, unit=None, source_ids=[sid], unit_missing_reason="source_units_not_supplied"
            )
    cached_by_key = {row["cache_key"]: row for row in cached}
    matched = []
    for point in historical["details"].get("points", []) if historical else []:
        row = cached_by_key.get(cache_key(point))
        if row:
            matched.append((point, row))
    for row in cached:
        sid = "era5/" + row["cache_key"]
        sources.append(
            EvidenceSource(
                id=sid,
                provider="Open-Meteo / ERA5",
                kind="modeled",
                observed_at=row["observed_at"],
                collected_at=row["collected_at"],
                attribution=ATTRIBUTION,
            )
        )
        payload = dict(row["payload"])
        payload["matched_route_points"] = [
            p for p, r in matched if r["cache_key"] == row["cache_key"]
        ]
        lat, lon = payload.get("grid_latitude"), payload.get("grid_longitude")
        point = payload["requested_point"]
        payload["grid_distance_m"] = (
            distance_m(point["latitude"], point["longitude"], lat, lon)
            if lat is not None and lon is not None
            else None
        )
        observations.append({"source_id": sid, **payload})
        for name, value in payload["values"].items():
            fields[f"{sid}.{name}"] = field_evidence(
                value,
                unit=payload["units"].get(name),
                source_ids=[sid],
                missing_reason="provider_field_missing",
                unit_missing_reason="provider_unit_missing",
            )
    summary = {}
    for name, (_, unit) in VARIABLES.items():
        values = [
            row["payload"]["values"][name]
            for _, row in matched
            if number(row["payload"]["values"].get(name)) is not None
            and row["payload"]["units"].get(name) == unit
        ]
        summary[name] = {
            "value": circular_mean(values)
            if name == "wind_direction"
            else sum(values) / len(values)
            if values
            else None,
            "count": len(values),
            "method": "circular_mean" if name == "wind_direction" else "arithmetic_mean",
            "unit": unit,
            "missing_reason": "no_compatible_source_values"
            if not values
            else "opposing_wind_directions"
            if name == "wind_direction" and circular_mean(values) is None
            else None,
        }
        fields[f"modeled_summary.{name}.value"] = field_evidence(
            summary[name]["value"],
            unit=unit,
            source_ids=["era5/" + row["cache_key"] for row in cached],
            missing_reason=summary[name]["missing_reason"] or "not_recorded",
        )
    wearable_data = None
    if wearable["count"]:
        sources.append(
            EvidenceSource(
                id="fit-temperature",
                provider="Garmin FIT wearable sensor",
                kind="recorded",
                observed_at=wearable["first_at"],
            )
        )
        wearable_data = {k: v for k, v in wearable.items() if k not in ("first_at", "last_at")}
        wearable_data.update(
            first_at=wearable["first_at"].isoformat() if wearable["first_at"] else None,
            last_at=wearable["last_at"].isoformat() if wearable["last_at"] else None,
        )
    fields["wearable_temperature"] = field_evidence(
        wearable_data, unit="degC", source_ids=["fit-temperature"] if wearable_data else []
    )
    fields["personal_sun_exposure"] = field_evidence(
        None,
        unit=None,
        source_ids=[],
        missing_reason="personal_exposure_not_observed",
        unit_missing_reason="unknown",
    )
    state_list = [s for s in (recorded_state, historical) if s]
    partial = any(
        s["status"] in ("error", "partial", "pending")
        or (s["last_success_at"] and s["status"] != "success")
        for s in state_list
    )
    success_times = [s["last_success_at"] for s in state_list if s["last_success_at"]]
    return AnalysisResponse(
        availability=("partial" if partial else "available")
        if observations or wearable_data
        else historical["status"]
        if historical
        else recorded_state["status"]
        if recorded_state
        else "not_processed",
        activity_id=activity_id,
        data={
            "observations": observations,
            "modeled_summary": summary,
            "wearable_temperature": wearable_data,
            "personal_sun_exposure": None,
        },
        fields=fields,
        sources=sources,
        freshness=Freshness(
            last_attempt_at=max((s["last_attempt_at"] for s in state_list), default=None),
            last_success_at=max(success_times, default=None),
            stale=partial,
            stale_reason="optional_source_incomplete" if partial else None,
        ),
        errors=errors(recorded_state) + errors(historical),
        coverage={
            "requested_points": len(historical["details"].get("points", [])) if historical else 0,
            "stored_model_points": len(cached),
            "matched_route_points": len(matched),
            "temporal_resolution": "hourly",
            "spatial_resolution_km": 25,
            "provider_states": {
                s["resource"]: {"status": s["status"], "reason": s["error_code"]}
                for s in state_list
            },
        },
        limitations=[
            "Sources are not merged or silently substituted.",
            "Wearable temperature is not ambient weather.",
            "Cloud cover and radiation are not personal sun/shade exposure.",
            "Route/time sampling is not continuous spatial or temporal coverage.",
        ],
    ).as_dict()
