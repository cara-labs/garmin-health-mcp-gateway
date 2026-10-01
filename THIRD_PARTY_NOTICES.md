# Third-party software notices

The Garmin Health MCP Gateway source in this repository was written independently and is licensed under Apache License 2.0. It does not vendor or copy the source of the runtime libraries listed below. The published container installs those libraries from their released Python packages, and each library remains governed by its own license.

## Direct Python dependencies

| Package | Version | License | Purpose |
| --- | --- | --- | --- |
| [`fitdecode`](https://pypi.org/project/fitdecode/0.11.0/) | 0.11.0 | MIT; copyright Jean-Charles Lefebvre | Streaming FIT decoder, installed unmodified; original gateway adapter and generated test fixtures |
| [`garminconnect`](https://pypi.org/project/garminconnect/0.3.11/) | 0.3.11 | MIT; copyright 2020–2026 Ron Klinkien | Unofficial Garmin Connect client used only by the provider adapter |
| [`mcp`](https://pypi.org/project/mcp/1.29.1/) | 1.29.1 | MIT; copyright 2024 Anthropic, PBC | MCP protocol server implementation |
| [`psycopg`](https://pypi.org/project/psycopg/3.3.4/) | 3.3.4 | LGPL-3.0-only | PostgreSQL client |
| [`psycopg-binary`](https://pypi.org/project/psycopg-binary/3.3.4/) | 3.3.4 | LGPL-3.0-only | Binary PostgreSQL client components |
| [`psycopg-pool`](https://pypi.org/project/psycopg-pool/3.3.1/) | 3.3.1 | LGPL-3.0-only | PostgreSQL connection pooling |

The MIT and LGPL license texts are retained in each installed package's `.dist-info/licenses/` directory inside the image. The LGPL-covered Psycopg packages are installed as separate, unmodified libraries and are not relicensed under Apache 2.0.

## Transitive and operating-system components

The published OCI image includes transitive Python dependencies plus components from the official Python/Debian base image. Those components retain their respective licenses. The release's attached software bill of materials provides the complete machine-readable component inventory, and installed package and Debian documentation retain the corresponding license information.

PostgreSQL and OpenAI `tunnel-client` run as separate upstream container images referenced by `compose.yaml`; they are not incorporated into the Garmin Health MCP Gateway image and retain their own licenses.

Apache License 2.0 in this repository applies only to the project's original code and documentation. It does not replace or override any third-party license.

## Weather data (not software licensing)

Historical weather integration will use Open-Meteo's documented HTTP API, not
its server source code. Its data attribution and non-commercial API terms are
separate from the Apache-2.0 license on the gateway. Preserve Open-Meteo and
upstream model attribution with stored weather responses; see
[official terms](https://open-meteo.com/en/terms) and
[historical API documentation](https://open-meteo.com/en/docs/historical-weather-api).
