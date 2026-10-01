"""Original synthetic workload; requires an explicitly configured disposable DB.

Invoke from the checkout with PYTHONPATH=src:tests. No Garmin credentials needed.
Results include fixture creation in peak RSS, a conservative upper bound.
"""

import gc
import json
import os
import resource
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from fit_factory import BASE_TIME, FIT_EPOCH, event, fit_file, record
from psycopg import sql
from psycopg.conninfo import make_conninfo

from garmin_health_gateway.db import Database
from garmin_health_gateway.fit_decoder import FitDecoder
from garmin_health_gateway.fit_store import FitStore
from garmin_health_gateway.streams import ActivityStreams


def main():
    admin_url = os.environ["GARMIN_TEST_DATABASE_URL"]
    sample_count = int(os.getenv("BENCHMARK_FIT_SAMPLES", "50000"))
    if not 1 <= sample_count <= 1000000:
        raise ValueError("BENCHMARK_FIT_SAMPLES must be 1..1000000")
    name = "garmin_benchmark_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    db = Database(make_conninfo(admin_url, dbname=name))
    try:
        db.migrate()
        with db.connection() as conn:
            conn.execute(
                "INSERT INTO activities(garmin_activity_id,activity_type,start_time) "
                "VALUES (42,'running',%s)",
                (FIT_EPOCH + timedelta(seconds=BASE_TIME + 1000),),
            )
        with tempfile.TemporaryDirectory(prefix="garmin-fit-benchmark-") as directory:
            path = Path(directory) / "42.fit"
            path.write_bytes(
                fit_file(
                    [
                        event(1000, 0),
                        *(record(1000 + i, distance=i * 3, speed=3) for i in range(sample_count)),
                    ]
                )
            )
            gc.collect()
            started = time.monotonic()
            result = FitStore(db).process(42, path)
            duration = time.monotonic() - started
            if result["status"] != "success":
                raise RuntimeError(result)
            repeated = FitStore(db).process(42, path)
            if repeated["status"] != "unchanged":
                raise RuntimeError("unchanged-file retry failed")
            limited = FitStore(db, FitDecoder(max_messages=1)).process(42, path, reprocess=True)
            if limited["error_code"] != "message_count_limit":
                raise RuntimeError("limit error missing")
            with db.connection() as conn:
                active = conn.execute("SELECT generation_id FROM fit_active_generations").fetchone()
                if str(active["generation_id"]) != result["generation_id"]:
                    raise RuntimeError("failed limit replacement changed active generation")
                db_bytes = conn.execute(
                    "SELECT pg_database_size(current_database()) AS n"
                ).fetchone()["n"]
            # Linux reports KiB, macOS bytes; benchmark is normally run on Linux ARM64.
            import sys

            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_bytes = rss if sys.platform == "darwin" else rss * 1024
            pages = {}
            for page_name, arguments in (
                ("summary", {}),
                ("raw_window", {"start_seconds": 0, "end_seconds": 1800, "resolution_seconds": 0}),
            ):
                page = ActivityStreams(db).get(42, **arguments)
                size = len(json.dumps(page).encode())
                if size > 3 * 1024 * 1024:
                    raise RuntimeError("bounded stream response exceeded 3 MiB with metadata")
                pages[page_name] = {
                    "bytes": size,
                    "rows": len(page["data"]["streams"]),
                    "has_more": page["next_cursor"] is not None,
                }
            query_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            query_rss_bytes = query_rss if sys.platform == "darwin" else query_rss * 1024
            print(
                json.dumps(
                    {
                        "samples": sample_count,
                        "archive_bytes": path.stat().st_size,
                        "processing_seconds": round(duration, 3),
                        "peak_process_rss_bytes": rss_bytes,
                        "peak_rss_including_queries_bytes": query_rss_bytes,
                        "stream_response_pages": pages,
                        "database_bytes": db_bytes,
                        "retry": repeated["status"],
                        "limit_error": limited["error_code"],
                    }
                ),
                flush=True,
            )
    finally:
        db.close()
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
