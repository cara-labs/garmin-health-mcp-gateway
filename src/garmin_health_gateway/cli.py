from __future__ import annotations

import argparse
import json

from .config import Settings
from .db import Database
from .logging import configure_logging
from .provider import GarminConnectProvider
from .sync import SyncEngine


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
    sync = subcommands.add_parser("sync", help="Run one synchronization cycle")
    sync.add_argument("--backfill", action="store_true", help="Repeat configured backfill windows")
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
            print("Database migrations and MCP reader are current.")
            return
        if args.command == "check":
            database.assert_fresh(args.max_age_hours)
            print("Synchronization is healthy.")
            return
        provider = _provider(settings)
        provider.authenticate()
        engine = SyncEngine(settings, database, provider)
        if args.command == "sync":
            print(json.dumps(engine.run_once(force_backfill=args.backfill)))
        elif args.command == "scheduler":
            engine.run_forever()
    finally:
        database.close()


if __name__ == "__main__":
    main()
