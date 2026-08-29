from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

DAILY_HEALTH_COLUMNS = (
    "date",
    "resting_hr",
    "hrv_average",
    "hrv_status",
    "sleep_minutes",
    "deep_sleep_minutes",
    "rem_sleep_minutes",
    "light_sleep_minutes",
    "awake_minutes",
    "sleep_score",
    "body_battery_high",
    "body_battery_low",
    "average_stress",
    "training_readiness",
    "training_readiness_level",
    "training_status",
    "acute_training_load",
    "load_focus",
    "recovery_hours",
    "vo2max",
    "running_vo2max",
    "fitness_age",
    "lactate_threshold_hr",
    "lactate_threshold_pace_seconds_per_km",
)

DAILY_ACTIVITY_COLUMNS = (
    "date",
    "steps",
    "distance_m",
    "active_calories",
    "floors",
    "moderate_intensity_minutes",
    "vigorous_intensity_minutes",
)

ACTIVITY_COLUMNS = (
    "garmin_activity_id",
    "activity_name",
    "activity_type",
    "start_time",
    "duration_seconds",
    "moving_duration_seconds",
    "distance_m",
    "calories",
    "average_hr",
    "max_hr",
    "average_pace_seconds_per_km",
    "best_pace_seconds_per_km",
    "average_cadence",
    "elevation_gain_m",
    "elevation_loss_m",
    "aerobic_training_effect",
    "anaerobic_training_effect",
    "training_load",
    "recovery_hours",
    "fit_file_path",
    "fit_download_status",
    "fit_download_error",
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


class Database:
    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 5):
        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
            timeout=30,
        )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self) -> Iterator[Connection[Any]]:
        with self.pool.connection() as connection:
            yield connection

    def migrate(self, migrations_dir: Path | None = None) -> None:
        if migrations_dir is None:
            configured = os.getenv("MIGRATIONS_DIR", "/app/migrations")
            migrations_dir = Path(configured)
            if not migrations_dir.exists():
                migrations_dir = Path(__file__).parents[2] / "migrations"
        files = sorted(migrations_dir.glob("*.sql"))
        if not files:
            raise RuntimeError(f"No database migrations found in {migrations_dir}")
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (719042881,))
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cursor.execute("SELECT version FROM schema_migrations")
            applied = {row["version"] for row in cursor.fetchall()}
            for path in files:
                if path.name in applied:
                    continue
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute(
                    "INSERT INTO schema_migrations(version) VALUES (%s)",
                    (path.name,),
                )

    def provision_readonly_user(self, password: str, role_name: str = "garmin_mcp_reader") -> None:
        if not password:
            raise ValueError("The MCP reader password must not be empty")
        role = sql.Identifier(role_name)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_database() AS name")
            database_name = cursor.fetchone()["name"]
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
            if cursor.fetchone() is None:
                cursor.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
            cursor.execute(sql.SQL("ALTER ROLE {} PASSWORD %s").format(role), (password,))
            cursor.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role)
            )
            cursor.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '15s'").format(role))
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database_name), role
                )
            )
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            cursor.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role)
            )
            cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}"
                ).format(role)
            )

    def _upsert(self, table: str, columns: tuple[str, ...], values: Mapping[str, Any]) -> None:
        missing = set(columns) - set(values)
        if missing:
            raise ValueError(f"Missing {table} values: {sorted(missing)}")
        prepared = [
            Jsonb(values[key]) if key == "load_focus" and values[key] is not None else values[key]
            for key in columns
        ]
        primary = columns[0]
        updates = [key for key in columns if key != primary]
        query = sql.SQL(
            "INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT ({primary}) DO UPDATE SET {updates}, updated_at = now()"
        ).format(
            table=sql.Identifier(table),
            columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
            placeholders=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            primary=sql.Identifier(primary),
            updates=sql.SQL(", ").join(
                sql.SQL("{column} = EXCLUDED.{column}").format(column=sql.Identifier(key))
                for key in updates
            ),
        )
        with self.connection() as connection:
            connection.execute(query, prepared)

    def upsert_daily_health(self, values: Mapping[str, Any]) -> None:
        self._upsert("daily_health", DAILY_HEALTH_COLUMNS, values)

    def upsert_daily_activity(self, values: Mapping[str, Any]) -> None:
        self._upsert("daily_activity", DAILY_ACTIVITY_COLUMNS, values)

    def upsert_activity(self, values: Mapping[str, Any]) -> None:
        self._upsert("activities", ACTIVITY_COLUMNS, values)

    def start_sync(self, resource: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO sync_state(resource, last_attempt, status, error_message)
                VALUES (%s, now(), 'running', NULL)
                ON CONFLICT (resource) DO UPDATE SET
                    last_attempt = now(), status = 'running', error_message = NULL,
                    updated_at = now()
                """,
                (resource,),
            )

    def finish_sync(self, resource: str, error: str | None = None) -> None:
        with self.connection() as connection:
            if error is None:
                connection.execute(
                    """
                    UPDATE sync_state SET last_successful_sync = now(), status = 'success',
                        error_message = NULL, updated_at = now() WHERE resource = %s
                    """,
                    (resource,),
                )
            else:
                connection.execute(
                    """
                    UPDATE sync_state SET status = 'error', error_message = %s,
                        updated_at = now() WHERE resource = %s
                    """,
                    (error[:2000], resource),
                )

    def get_sync_state(self) -> list[dict[str, Any]]:
        return self._fetch_all("SELECT * FROM sync_state ORDER BY resource")

    def has_successful_sync(self, resource: str) -> bool:
        rows = self._fetch_all(
            "SELECT 1 AS found FROM sync_state WHERE resource = %s "
            "AND last_successful_sync IS NOT NULL",
            (resource,),
        )
        return bool(rows)

    def _fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_json_safe(dict(row)) for row in rows]

    def get_daily_health(self, target: date) -> dict[str, Any] | None:
        rows = self._fetch_all("SELECT * FROM daily_health WHERE date = %s", (target,))
        return rows[0] if rows else None

    def get_health_range(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._fetch_all(
            "SELECT * FROM daily_health WHERE date BETWEEN %s AND %s ORDER BY date",
            (start, end),
        )

    def get_recent_activities(
        self, since: datetime, activity_type: str | None = None
    ) -> list[dict[str, Any]]:
        if activity_type:
            return self._fetch_all(
                "SELECT * FROM activities WHERE start_time >= %s "
                "AND lower(activity_type) = lower(%s) ORDER BY start_time DESC",
                (since, activity_type),
            )
        return self._fetch_all(
            "SELECT * FROM activities WHERE start_time >= %s ORDER BY start_time DESC",
            (since,),
        )

    def get_activity(self, activity_id: int) -> dict[str, Any] | None:
        rows = self._fetch_all(
            "SELECT * FROM activities WHERE garmin_activity_id = %s", (activity_id,)
        )
        return rows[0] if rows else None

    def get_metric_history(self, column: str, days: int) -> list[dict[str, Any]]:
        allowed = {
            "vo2max",
            "running_vo2max",
            "hrv_average",
            "hrv_status",
            "sleep_minutes",
            "sleep_score",
            "resting_hr",
        }
        if column not in allowed:
            raise ValueError("Unsupported metric")
        start = date.today() - timedelta(days=days - 1)
        query = sql.SQL(
            "SELECT date, {column} AS value FROM daily_health "
            "WHERE date >= %s AND {column} IS NOT NULL ORDER BY date"
        ).format(column=sql.Identifier(column))
        with self.connection() as connection:
            rows = connection.execute(query, (start,)).fetchall()
        return [_json_safe(dict(row)) for row in rows]

    def get_sleep_history(self, days: int) -> list[dict[str, Any]]:
        start = date.today() - timedelta(days=days - 1)
        return self._fetch_all(
            """
            SELECT date, sleep_minutes, deep_sleep_minutes, rem_sleep_minutes,
                   light_sleep_minutes, awake_minutes, sleep_score
            FROM daily_health WHERE date >= %s AND sleep_minutes IS NOT NULL
            ORDER BY date
            """,
            (start,),
        )

    def get_training_load(self, days: int) -> dict[str, Any]:
        start = datetime.now(UTC) - timedelta(days=days)
        activities = self._fetch_all(
            """
            SELECT garmin_activity_id, activity_type, start_time, duration_seconds,
                   distance_m, training_load, aerobic_training_effect,
                   anaerobic_training_effect
            FROM activities WHERE start_time >= %s ORDER BY start_time
            """,
            (start,),
        )
        daily = self._fetch_all(
            """
            SELECT date, acute_training_load, training_status, load_focus,
                   training_readiness, recovery_hours
            FROM daily_health WHERE date >= %s ORDER BY date
            """,
            (start.date(),),
        )
        return {"days": days, "daily": daily, "activities": activities}

    def assert_fresh(self, max_age_hours: int) -> None:
        states = self._fetch_all("SELECT resource, last_successful_sync, status FROM sync_state")
        if len(states) < 2:
            raise RuntimeError("Synchronization has not initialized")
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        stale = []
        for state in states:
            stamp = state["last_successful_sync"]
            parsed = datetime.fromisoformat(stamp) if stamp else None
            if state["status"] == "error" or parsed is None or parsed < cutoff:
                stale.append(state["resource"])
        if stale:
            raise RuntimeError(f"Stale or failed resources: {', '.join(stale)}")
