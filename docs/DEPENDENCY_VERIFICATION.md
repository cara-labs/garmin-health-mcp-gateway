# Activity-analysis dependency verification

## FIT decoder

Pin `fitdecode==0.11.0` (MIT, unmodified PyPI package). No decoder source or
fixtures are copied into this repository. The gateway will use an original
adapter and generated fixtures based on the
[Garmin FIT protocol](https://developer.garmin.com/fit/protocol/).
The [release metadata](https://pypi.org/project/fitdecode/0.11.0/) identifies
FIT SDK profile 21.171.0, Python >=3.6, and a platform-independent wheel.

The documented [reader interface](https://fitdecode.readthedocs.io/en/latest/reference/reader.html)
yields one frame at a time, including unknown messages and chained files.
Use strict CRC and parsing checks, not the warning-only defaults. Retain
field identifiers and unknown developer values/units rather than inventing
interpretations; malformed developer definitions must fail processing explicitly.
The [field interface](https://fitdecode.readthedocs.io/en/latest/reference/types.html)
exposes native and developer definitions, raw/scaled values, and units.
Streaming alone does not guarantee bounded processing: the gateway must cap
file size, frames, field payloads, and staging batches and measure real ingestion.

Compatibility evidence: a disposable ARM64 container on the Raspberry Pi,
with no production mounts and a 128 MiB memory cap, installed the released
wheel successfully and imported it on Python 3.12.13 (`aarch64`). Local
Python 3.12 installation/import also succeeded. This verifies dependency
installation, not yet the full ingestion workload or published new image.

## Garmin client contracts

The installed pinned `garminconnect==0.3.11` exposes:

- `get_activity_weather(activity_id: str) -> dict`, endpoint
  `/activity-service/activity/{id}/weather`.
- `get_heart_rate_zones() -> list[dict]`, all sport profiles.
- `get_user_profile() -> dict`, configured user settings.
- `get_userprofile_settings() -> dict`, user profile settings.

These are unofficial API calls. A method annotation is not a guarantee of
server payload completeness. Adapter tests must retain unknown payloads and
reject invalid outer shapes, and normalization must use explicit keys/units
with missing reasons. Profile scopes/methods must come from returned evidence,
not age formulas, daily measured HR, or guessed sport defaults.

## Historical weather selection

Select the [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api)
with the explicit ERA5 model for stable historical coverage. It returns hourly
modeled grid estimates from 1940 with roughly 25 km spatial resolution and a
five-day publication delay. Recent activity enrichment remains pending until
coverage is available; do not substitute a forecast as historical observation.
Temperature, humidity, dew point, wind, cloud, and shortwave radiation have
documented variables. Cloud/radiation are not personal sun exposure.

The [API terms](https://open-meteo.com/en/terms) allow free non-commercial use
subject to rate limits and require data attribution (CC BY 4.0). This personal
gateway must make lookup opt-in and document that sampled coordinates/time
are disclosed to the provider. No paid account or API key is assumed. Commercial
operators must arrange compliant access separately; the gateway license does
not grant free commercial access to the API.

Provider configuration: `GARMIN_HISTORICAL_WEATHER=true` opts into coordinate
disclosure and non-commercial historical queries; default false.
`WEATHER_REQUEST_TIMEOUT_SECONDS=10`, `WEATHER_REQUEST_DELAY_SECONDS=2`, and
`WEATHER_BATCH_SIZE=1` bound transport and per-iteration budgets. The selected
endpoint/model are fixed, not arbitrary URLs; credentials are never sent.
These settings do not yet enable collection until the enrichment worker exists.

Response-shape verification used read-only calls through the pinned installed
client on the Pi; only field names/types were returned. Tests use synthetic
values matching those captured subsets and exercise the actual pinned client
methods against mocked transport. Weather returned station metadata and native
`temp`, `dewPoint`, `relativeHumidity`, `windSpeed`, and `windDirection`, but no
units. Do not assume the user's display-unit setting defines the endpoint's
units: preserve native evidence with unknown units unless a verified source
supplies units. Zone objects returned sport, training method, configured HR
values, and five zone floors. Preserve floors, do not invent zone ceilings.
