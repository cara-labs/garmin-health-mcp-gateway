from __future__ import annotations

import argparse
import json

from .collector_analysis import CollectorAnalysis
from .config import Settings
from .db import Database
from .logging import configure_logging
from .provider import GarminConnectProvider
from .sync import SyncEngine
from .sync_requests import SyncRequests
from .weather import HistoricalWeather, WeatherCollector


def _provider(settings: Settings) -> GarminConnectProvider:
    return GarminConnectProvider(
        email=settings.garmin_email,
        password=settings.garmin_password,
        token_store=settings.token_store,
        request_delay_seconds=settings.request_delay_seconds,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="garmin-health")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("auth", help="Interactively create or refresh Garmin tokens")
    subcommands.add_parser("migrate", help="Apply database migrations")
    subcommands.add_parser(
        "provision-reader", help="Migrate and provision the database-enforced MCP reader"
    )
    subcommands.add_parser(
        "provision-feedback", help="Provision the execution-only feedback writer"
    )
    sync = subcommands.add_parser("sync", help="Run one synchronization cycle")
    sync.add_argument("--backfill", action="store_true", help="Repeat configured backfill windows")
    archives = subcommands.add_parser(
        "process-archives", help="Decode local FIT files without Garmin login"
    )
    archives.add_argument("--activity-id", type=int, action="append")
    archives.add_argument("--limit", type=int, default=10)
    archives.add_argument("--after-activity-id", type=int, default=0)
    archives.add_argument("--reprocess", action="store_true")
    weather = subcommands.add_parser(
        "backfill-weather", help="One bounded historical-weather batch"
    )
    weather.add_argument("--activity-id", type=int, action="append")
    weather.add_argument("--limit", type=int, default=1)
    subcommands.add_parser("scheduler", help="Run synchronization every configured interval")
    check = subcommands.add_parser("check", help="Fail if synchronization is stale")
    check.add_argument("--max-age-hours", type=int, default=3)
    subcommands.add_parser("mcp", help="Run the Streamable HTTP MCP server")
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    if args.command == "mcp":
        from .mcp_server import run

        run()
        return

    if args.command == "auth":
        provider = _provider(settings)
        provider.authenticate(interactive=True)
        print("Garmin session saved successfully.")
        return

    database = Database(settings.database_url)
    try:
        database.migrate()
        if args.command == "migrate":
            print("Database migrations are current.")
            return
        if args.command == "provision-reader":
            if not settings.mcp_reader_password:
                raise ValueError("MCP_READER_PASSWORD(_FILE) is required")
            database.provision_readonly_user(settings.mcp_reader_password)
            if settings.feedback_writer_password:
                database.provision_feedback_user(settings.feedback_writer_password)
            print("Database migrations and MCP reader are current.")
            return
        if args.command == "provision-feedback":
            if not settings.feedback_writer_password:
                raise ValueError("MCP_FEEDBACK_PASSWORD(_FILE) is required")
            database.provision_feedback_user(settings.feedback_writer_password)
            print("Execution-only feedback writer is current.")
            return
        if args.command == "check":
            database.assert_fresh(args.max_age_hours)
            print("Synchronization is healthy.")
            return
        queue = SyncRequests(settings.sync_request_dir)
        historical = WeatherCollector(
            database,
            enabled=settings.historical_weather_enabled,
            client=HistoricalWeather(
                timeout=settings.weather_request_timeout_seconds,
                delay=settings.weather_request_delay_seconds,
            ),
        )
        if args.command == "process-archives":
            from .backfill import archive_batch

            print(
                json.dumps(
                    archive_batch(
                        database,
                        queue,
                        activity_ids=args.activity_id,
                        limit=args.limit,
                        after_activity_id=args.after_activity_id,
                        reprocess=args.reprocess,
                    )
                )
            )
            return
        if args.command == "backfill-weather":
            from .backfill import weather_batch

            if not settings.historical_weather_enabled:
                raise ValueError(
                    "Opt in to coordinate/time disclosure with GARMIN_HISTORICAL_WEATHER=true"
                )
            print(
                json.dumps(
                    weather_batch(
                        database, queue, historical, limit=args.limit, activity_ids=args.activity_id
                    )
                )
            )
            return
        provider = _provider(settings)
        provider.authenticate()
        engine = SyncEngine(
            settings,
            database,
            provider,
            analysis=CollectorAnalysis(database, provider),
            historical_weather=historical,
        )
        if args.command == "sync":
            print(json.dumps(engine.run_once(force_backfill=args.backfill)))
        elif args.command == "scheduler":
            engine.run_forever()
    finally:
        database.close()


if __name__ == "__main__":
    main()
