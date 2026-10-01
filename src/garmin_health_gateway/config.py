from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from psycopg.conninfo import make_conninfo


def _read_secret(name: str, default_file: str | None = None) -> str | None:
    value = os.getenv(name)
    if value:
        return value.strip()
    path_value = os.getenv(f"{name}_FILE", default_file)
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    fit_archive: Path
    token_store: Path
    timezone: str
    daily_backfill_days: int
    activity_backfill_days: int
    recent_days: int
    sync_interval_seconds: int
    request_delay_seconds: float
    mcp_host: str
    mcp_port: int
    log_level: str
    garmin_email: str | None
    garmin_password: str | None
    mcp_reader_password: str | None
    sync_request_dir: Path = Path("/var/lib/garmin-sync")
    historical_weather_enabled: bool = False
    weather_request_timeout_seconds: int = 10
    weather_request_delay_seconds: float = 2.0
    weather_batch_size: int = 1
    feedback_writer_password: str | None = None
    feedback_database_url: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            db_password = _read_secret("POSTGRES_PASSWORD", "/run/secrets/postgres_password")
            if not db_password:
                raise ValueError("Set DATABASE_URL or provide POSTGRES_PASSWORD(_FILE)")
            db_user = os.getenv("POSTGRES_USER", "garmin")
            db_name = os.getenv("POSTGRES_DB", "garmin_health")
            db_host = os.getenv("POSTGRES_HOST", "postgres")
            db_port = int(os.getenv("POSTGRES_PORT", "5432"))
            database_url = (
                f"postgresql://{quote(db_user, safe='')}:{quote(db_password, safe='')}"
                f"@{db_host}:{db_port}/{quote(db_name, safe='')}"
            )

        feedback_password = _read_secret(
            "MCP_FEEDBACK_PASSWORD", "/run/secrets/mcp_feedback_password"
        )
        feedback_url = os.getenv("MCP_FEEDBACK_DATABASE_URL")
        if not feedback_url and feedback_password:
            feedback_url = make_conninfo(
                database_url, user="garmin_mcp_feedback_writer", password=feedback_password
            )
        weather_batch_size = _positive_int("WEATHER_BATCH_SIZE", 1)
        if weather_batch_size > 10:
            raise ValueError("WEATHER_BATCH_SIZE must be between 1 and 10")
        return cls(
            feedback_writer_password=feedback_password,
            feedback_database_url=feedback_url,
            historical_weather_enabled=os.getenv("GARMIN_HISTORICAL_WEATHER", "false").lower()
            == "true",
            weather_request_timeout_seconds=_positive_int("WEATHER_REQUEST_TIMEOUT_SECONDS", 10),
            weather_request_delay_seconds=_nonnegative_float("WEATHER_REQUEST_DELAY_SECONDS", 2.0),
            weather_batch_size=weather_batch_size,
            sync_request_dir=Path(os.getenv("GARMIN_SYNC_REQUEST_DIR", "/var/lib/garmin-sync")),
            database_url=database_url,
            fit_archive=Path(os.getenv("GARMIN_FIT_ARCHIVE", "/data/garmin/fit")),
            token_store=Path(os.getenv("GARMIN_TOKEN_STORE", "/var/lib/garmin-auth")),
            timezone=os.getenv("TZ", "UTC"),
            daily_backfill_days=_positive_int("GARMIN_DAILY_BACKFILL_DAYS", 90),
            activity_backfill_days=_positive_int("GARMIN_ACTIVITY_BACKFILL_DAYS", 365),
            recent_days=_positive_int("GARMIN_RECENT_DAYS", 3),
            sync_interval_seconds=_positive_int("GARMIN_SYNC_INTERVAL_SECONDS", 3600),
            request_delay_seconds=_nonnegative_float("GARMIN_REQUEST_DELAY_SECONDS", 1.25),
            mcp_host=os.getenv("MCP_HOST", "0.0.0.0"),
            mcp_port=_positive_int("MCP_PORT", 8000),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            garmin_email=_read_secret("GARMIN_EMAIL", "/run/secrets/garmin_email"),
            garmin_password=_read_secret("GARMIN_PASSWORD", "/run/secrets/garmin_password"),
            mcp_reader_password=_read_secret(
                "MCP_READER_PASSWORD", "/run/secrets/mcp_reader_password"
            ),
        )
