"""Opt-in real PostgreSQL checks. Always create/drop an isolated database."""

import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fit_factory import (
    BASE_TIME,
    FIT_EPOCH,
    developer_definition,
    event,
    fit_file,
    message,
    record,
    u32,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo

from garmin_health_gateway.activity_details import ActivityDetails
from garmin_health_gateway.db import Database
from garmin_health_gateway.feedback import ActivityFeedback, get_feedback, save_feedback
from garmin_health_gateway.fit_decoder import FitDecoder
from garmin_health_gateway.fit_store import FitStore
from garmin_health_gateway.streams import ActivityStreams

ADMIN_URL = os.getenv("GARMIN_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="isolated PostgreSQL URL not configured")


@pytest.fixture
def isolated_db():
    name = "garmin_test_" + uuid4().hex
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(make_conninfo(ADMIN_URL, dbname=name))
    database.test_roles = []
    try:
        yield database
    finally:
        database.close()
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in database.test_roles:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def count(db, table):
    with db.connection() as conn:
        return conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
        ).fetchone()["n"]


def test_profile_snapshots_and_recorded_weather_last_good(isolated_db):
    from unittest.mock import Mock

    from garmin_health_gateway.enrichment import ObservationStore, get_training_profile
    from garmin_health_gateway.weather import get_activity_weather

    db = isolated_db
    db.migrate()
    add_activity(db)
    store = ObservationStore(db)
    provider = Mock()
    provider.get_configured_training_profile.return_value = {
        "heart_rate_zones": [
            {"sport": "RUNNING", "maxHeartRateUsed": 190},
            {"sport": "DEFAULT", "maxHeartRateUsed": 180},
        ]
    }
    assert store.collect("training_profile", provider) == "success"
    provider.get_configured_training_profile.return_value = {
        "heart_rate_zones": [{"sport": "RUNNING", "maxHeartRateUsed": 191}]
    }
    assert store.collect("training_profile", provider) == "success"
    provider.get_configured_training_profile.side_effect = RuntimeError("secret must not leak")
    assert store.collect("training_profile", provider) == "error"
    result = get_training_profile(db)
    assert result["availability"] == "partial"
    assert result["data"]["configured"]["running"][0]["maximum_hr"] == 191
    assert result["data"]["measured_resting_hr"] is None
    assert result["sources"][0]["effective_at"] is None
    assert count(db, "analysis_observations") == 2
    assert "secret" not in str(result)
    with db.connection() as conn:
        conn.execute("INSERT INTO daily_health(date,resting_hr) VALUES ('2020-01-01',55)")
    assert get_training_profile(db)["data"]["measured_resting_hr"]["resting_hr"] == 55
    provider.get_activity_weather.return_value = {
        "temp": 0,
        "windSpeed": 0,
        "issueDate": "2020-01-01T10:00:00Z",
        "weatherStationDTO": {"id": "synthetic"},
    }
    assert store.collect("weather_recorded", provider, 42) == "success"
    provider.get_activity_weather.side_effect = NotImplementedError()
    assert store.collect("weather_recorded", provider, 42) == "unsupported"
    result = get_activity_weather(db, 42)
    assert result["data"]["observations"][0]["values"]["temperature"] == 0
    assert result["data"]["observations"][0]["station"]["id"] == "synthetic"
    assert result["sources"][0]["kind"] == "recorded"
    assert next(iter(result["fields"].values()))["unit"] is None
    assert result["data"]["personal_sun_exposure"] is None
    assert get_activity_weather(db, 999)["availability"] == "not_found"


def test_historical_weather_cache_resume_bounded_route_and_missing(isolated_db, tmp_path):
    from unittest.mock import Mock

    from psycopg.types.json import Jsonb

    from garmin_health_gateway.enrichment import ObservationStore
    from garmin_health_gateway.weather import WeatherCollector, get_activity_weather

    db = isolated_db
    db.migrate()
    add_activity(db)
    client = Mock()
    collector = WeatherCollector(db, enabled=True, client=client)
    assert collector.collect(42) == "pending"
    path = tmp_path / "42.fit"
    path.write_bytes(fit_file([record(1000 + i) for i in range(20)]))
    FitStore(db).process(42, path)
    assert collector.collect(42) == "not_recorded"
    with db.connection() as conn:
        conn.execute(
            "UPDATE fit_samples SET data=data || jsonb_build_object("
            "'latitude',ordinal::double precision / 100,'longitude',2)"
        )
    points, reason = collector.plan(42)
    assert reason is None and len(points) == 10
    assert points[0]["ordinal"] == 0 and points[-1]["ordinal"] == 19

    def fetch(point):
        return {
            "values": {"temperature": 0, "wind_direction": 350},
            "units": {"temperature": "degC", "wind_direction": "degree"},
            "requested_point": point,
            "grid_latitude": 0,
            "grid_longitude": 2,
        }

    client.fetch.side_effect = [fetch(points[0]), RuntimeError("private provider error")]
    assert collector.collect(42) == "partial"
    assert count(db, "weather_cache") == 1
    result = get_activity_weather(db, 42)
    assert result["availability"] == "partial"
    assert result["data"]["modeled_summary"]["temperature"]["value"] == 0
    assert result["sources"][0]["kind"] == "modeled"
    assert "private" not in str(result)
    client.fetch.side_effect = fetch
    assert collector.collect(42) == "success"
    assert count(db, "weather_cache") == 10
    client.reset_mock()
    assert collector.collect(42) == "success"
    client.fetch.assert_not_called()
    # Queries never invoke the client. A failed later plan keeps successful evidence.
    with db.connection() as conn:
        conn.execute("UPDATE fit_samples SET observed_at=now()")
    assert collector.collect(42) == "pending"
    result = get_activity_weather(db, 42)
    assert result["coverage"]["stored_model_points"] == 10
    assert result["freshness"]["last_success_at"] is not None
    with db.connection() as conn:
        conn.execute("UPDATE fit_samples SET observed_at='1939-01-01T00:00:00Z'")
    assert collector.collect(42) == "unsupported"
    assert WeatherCollector(db).collect(42) == "unsupported"
    store = ObservationStore(db)
    store.attempt(
        "weather_historical", 42, "error", "outage", details={"points": points, "cache_keys": []}
    )
    assert get_activity_weather(db, 42)["availability"] == "error"
    # Null wearable values do not become zero.
    with db.connection() as conn:
        conn.execute(
            "UPDATE fit_samples SET data=data || %s", (Jsonb({"wearable_temperature_c": 0}),)
        )
    assert get_activity_weather(db, 42)["data"]["wearable_temperature"]["mean"] == 0


def add_activity(db, activity_id=42):
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO activities(garmin_activity_id,activity_type,start_time) "
            "VALUES (%s,'running',%s)",
            (activity_id, FIT_EPOCH + timedelta(seconds=BASE_TIME + 1000)),
        )


def test_fresh_upgrade_and_repeat_migrations(isolated_db, tmp_path):
    db = isolated_db
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    source = Path(__file__).parents[1] / "migrations" / "001_initial.sql"
    (old_dir / source.name).write_text(source.read_text())
    (old_dir / "._001_initial.sql").write_bytes(b"\x00\xffMac metadata, not SQL")
    db.migrate(old_dir)
    add_activity(db)
    db.migrate()
    db.migrate()
    assert count(db, "activities") == 1
    assert count(db, "schema_migrations") == len(list(source.parent.glob("[0-9]*.sql")))
    assert count(db, "fit_samples") == 0


def test_archive_resume_and_weather_batch_yields_to_queue(isolated_db, tmp_path):
    from unittest.mock import Mock

    from garmin_health_gateway.backfill import archive_batch, weather_batch
    from garmin_health_gateway.sync_requests import SyncRequests
    from garmin_health_gateway.weather import WeatherCollector

    db = isolated_db
    db.migrate()
    queue = SyncRequests(tmp_path / "queue")
    for activity_id in (42, 43):
        add_activity(db, activity_id)
        path = tmp_path / f"{activity_id}.fit"
        path.write_bytes(fit_file([record(1000), record(1005)]))
        with db.connection() as conn:
            conn.execute(
                "UPDATE activities SET fit_file_path=%s WHERE garmin_activity_id=%s",
                (str(path), activity_id),
            )
    first = archive_batch(db, queue, limit=1)
    assert first["has_more"] is True
    assert first["results"][0]["status"] == "success"
    second = archive_batch(db, queue, after_activity_id=first["next_after_activity_id"], limit=1)
    assert second["results"][0]["activity_id"] == 43
    assert second["has_more"] is False
    assert archive_batch(db, queue)["results"] == []
    assert count(db, "fit_generations") == 2
    client = Mock()

    def collect(activity_id):
        queue.request()
        return "partial"

    client.collect.side_effect = collect
    result = weather_batch(db, queue, client, limit=2)
    assert result["status"] == "yielded_to_sync"
    assert len(result["results"]) == 1
    assert weather_batch(db, queue, client)["status"] == "yielded_to_sync"
    assert archive_batch(db, queue)["status"] == "yielded_to_sync"
    queue.claim()
    queue.finish({"status": "success"})
    with queue.lock("worker", blocking=False), pytest.raises(RuntimeError, match="already running"):
        weather_batch(db, queue, client)
    actual = WeatherCollector(db, enabled=True, client=Mock())
    assert weather_batch(db, queue, actual, limit=1)["results"][0]["status"] == "not_recorded"
    assert (
        archive_batch(db, queue, activity_ids=[42], reprocess=True)["results"][0]["status"]
        == "success"
    )
    assert count(db, "fit_generations") == 3
    with pytest.raises(ValueError):
        archive_batch(db, queue, limit=101)
    with pytest.raises(ValueError):
        weather_batch(db, queue, actual, limit=11)


def test_atomic_success_unchanged_and_failed_replacement(isolated_db, tmp_path):
    db = isolated_db
    db.migrate()
    add_activity(db)
    path = tmp_path / "42.fit"
    original = fit_file([event(1000, 0), record(1000), record(1000), record(1005)])
    path.write_bytes(original)
    store = FitStore(db)
    success = store.process(42, path)
    assert success["status"] == "success"
    assert count(db, "fit_samples") == 3
    assert store.process(42, path)["status"] == "unchanged"
    assert count(db, "fit_samples") == 3
    path.write_bytes(original[:-2] + b"\x00\x00")
    failed = store.process(42, path)
    assert failed["status"] == "error" and failed["error_code"] == "FitCRCError"
    with db.connection() as conn:
        active = conn.execute("SELECT generation_id FROM fit_active_generations").fetchone()
    assert str(active["generation_id"]) == success["generation_id"]
    path.write_bytes(original)
    replacement = store.process(42, path, reprocess=True)
    assert replacement["status"] == "success"
    assert replacement["generation_id"] != success["generation_id"]
    assert count(db, "fit_samples") == 6  # old immutable generation retained


def test_interruption_after_committed_batch_leaves_active_generation_untouched(
    isolated_db, tmp_path
):
    db = isolated_db
    db.migrate()
    add_activity(db)
    path = tmp_path / "42.fit"
    path.write_bytes(fit_file([record(1000)]))
    previous = FitStore(db).process(42, path)
    path.write_bytes(fit_file([record(1000 + i) for i in range(260)]))

    class Interrupted(FitDecoder):
        def decode(self, path, start):
            for i, row in enumerate(super().decode(path, start)):
                if i == 150:
                    raise KeyboardInterrupt()
                yield row

    with pytest.raises(KeyboardInterrupt):
        FitStore(db, Interrupted()).process(42, path)
    with db.connection() as conn:
        assert (
            str(
                conn.execute("SELECT generation_id FROM fit_active_generations").fetchone()[
                    "generation_id"
                ]
            )
            == previous["generation_id"]
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM fit_generations WHERE status='error'"
            ).fetchone()["n"]
            == 1
        )
    resumed = FitStore(db).process(42, path)
    assert resumed["status"] == "success"
    with db.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM fit_samples WHERE generation_id=%s",
                (resumed["generation_id"],),
            ).fetchone()["n"]
            == 260
        )
    assert FitStore(db).process(42, path)["status"] == "unchanged"


def test_limit_failure_and_unknown_activity_are_explicit(isolated_db, tmp_path):
    db = isolated_db
    db.migrate()
    add_activity(db)
    path = tmp_path / "42.fit"
    path.write_bytes(fit_file([record(1000), record(1001)]))
    result = FitStore(db, FitDecoder(max_messages=1)).process(42, path)
    assert result["error_code"] == "message_count_limit"
    assert count(db, "fit_active_generations") == 0
    assert FitStore(db).process(999, path)["status"] == "not_found"


def test_crashed_staging_generation_is_restarted_not_published(isolated_db, tmp_path):
    db = isolated_db
    db.migrate()
    add_activity(db)
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO fit_generations(activity_id,archive_sha256,decoder_version, "
            "schema_version,status,archive_bytes) VALUES (42,%s,'old','old','staging',0)",
            ("0" * 64,),
        )
    path = tmp_path / "42.fit"
    path.write_bytes(fit_file([record(1000)]))
    assert FitStore(db).process(42, path)["status"] == "success"
    with db.connection() as conn:
        row = conn.execute(
            "SELECT status,error_code FROM fit_generations WHERE decoder_version='old'"
        ).fetchone()
    assert row == {"status": "error", "error_code": "interrupted"}


def test_stream_windows_duplicates_pauses_unplaced_and_readonly(isolated_db, tmp_path):
    db = isolated_db
    db.migrate()
    add_activity(db)
    path = tmp_path / "42.fit"
    path.write_bytes(
        fit_file(
            [
                event(1000, 0),
                record(1000, speed=0, hr=255),
                record(1000, hr=100),
                event(1005, 1),
                record(1010, hr=200),
                event(1015, 0),
                record(1020),
                record(),
            ]
        )
    )
    assert FitStore(db).process(42, path)["status"] == "success"
    stream = ActivityStreams(db)
    response = stream.get(42, start_seconds=0, end_seconds=20, resolution_seconds=0)
    assert [r["elapsed_seconds"] for r in response["data"]["streams"]] == [0, 0, 10]
    assert response["data"]["streams"][0]["heart_rate_bpm"] is None
    assert response["data"]["streams"][0]["speed_mps"] == 0
    assert [e["elapsed_seconds"] for e in response["data"]["timer_events"]] == [0, 5, 15]
    assert len(response["data"]["unplaced_samples"]) == 1
    assert response["next_cursor"] is None
    summary = stream.get(42, end_seconds=20)
    assert len(summary["data"]["streams"]) == 4
    assert summary["data"]["streams"][1]["gap"] is True
    assert stream.get(999)["availability"] == "not_found"
    add_activity(db, 43)
    assert stream.get(43)["availability"] == "not_processed"
    # A DB-enforced read-only transaction must support every stream query.
    with db.connection() as conn:
        conn.execute("SET default_transaction_read_only=on")
    assert stream.get(42)["availability"] == "available"


def test_stream_page_limits_mismatch_and_stale_cursors(isolated_db, tmp_path, monkeypatch):
    import garmin_health_gateway.streams as streams

    db = isolated_db
    db.migrate()
    add_activity(db)
    path = tmp_path / "42.fit"
    path.write_bytes(fit_file([record(1000 + i) for i in range(8)]))
    assert FitStore(db).process(42, path)["status"] == "success"
    monkeypatch.setattr(streams, "PAGE_ROWS", 3)
    query = {"start_seconds": 0, "end_seconds": 8, "resolution_seconds": 0}
    first = ActivityStreams(db).get(42, **query)
    assert len(first["data"]["streams"]) == 3
    cursor = first["next_cursor"]
    second = ActivityStreams(db).get(42, **query, cursor=cursor)
    third = ActivityStreams(db).get(42, **query, cursor=second["next_cursor"])
    assert [
        r["ordinal"] for page in (first, second, third) for r in page["data"]["streams"]
    ] == list(range(8))
    assert third["next_cursor"] is None
    with pytest.raises(ValueError, match="match"):
        ActivityStreams(db).get(
            42, start_seconds=1, end_seconds=8, resolution_seconds=0, cursor=cursor
        )
    assert FitStore(db).process(42, path, reprocess=True)["status"] == "success"
    with pytest.raises(ValueError, match="stale"):
        ActivityStreams(db).get(42, **query, cursor=cursor)


def test_summary_and_event_pagination_do_not_skip_evidence(isolated_db, tmp_path, monkeypatch):
    import garmin_health_gateway.streams as streams

    db = isolated_db
    db.migrate()
    add_activity(db)
    path = tmp_path / "42.fit"
    path.write_bytes(fit_file([event(1000, 0), record(1000), event(1005, 1), record(1010)]))
    assert FitStore(db).process(42, path)["status"] == "success"
    monkeypatch.setattr(streams, "PAGE_ROWS", 2)
    pages, cursor = [], None
    for _ in range(10):
        page = ActivityStreams(db).get(42, end_seconds=15, cursor=cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert [r["bucket_start_seconds"] for p in pages for r in p["data"]["streams"]] == [0, 5, 10]
    assert [r["elapsed_seconds"] for p in pages for r in p["data"]["timer_events"]] == [0, 5]


def test_laps_steps_splits_and_numeric_recovery_end_to_end(isolated_db, tmp_path, monkeypatch):
    import asyncio
    import json
    import struct

    import garmin_health_gateway.activity_details as details
    import garmin_health_gateway.mcp_server as server

    db = isolated_db
    db.migrate()
    add_activity(db)

    def lap(trigger):
        return message(19, [(9, 0x86, u32(120000)), (24, 0, bytes([trigger]))])

    step = message(27, [(254, 0x84, struct.pack("<H", 0))])
    recovery = message(
        21,
        [(253, 0x86, u32(BASE_TIME + 1080)), (0, 0, b"\x15"), (1, 0, b"\x03"), (3, 0x86, u32(120))],
    )
    path = tmp_path / "42.fit"
    path.write_bytes(
        fit_file(
            [
                event(1000, 0),
                *developer_definition(),
                step,
                *(
                    record(1000 + i * 20, distance=distance, developer_fields=[(1, 0, b"\x2a\x00")])
                    for i, distance in enumerate([0, 600, 1200, 1800, 2400])
                ),
                lap(0),
                lap(2),
                recovery,
            ]
        )
    )
    assert FitStore(db).process(42, path)["status"] == "success"
    lap_result = ActivityDetails(db).get(42, tool="laps")["data"]
    assert [r["trigger_classification"] for r in lap_result["recorded_laps"]] == [
        "manual",
        "automatic",
    ]
    assert len(lap_result["planned_steps"]) == 1
    assert lap_result["executed_steps"] == []
    assert lap_result["planned_steps"][0]["association_index"] is None
    assert [r["distance_m"] for r in lap_result["calculated_splits"]] == [1000, 1000, 400]
    monkeypatch.setattr(server, "database", lambda: db)
    protocol = asyncio.run(server.mcp.call_tool("get_activity_fit_metrics", {"activity_id": 42}))
    blocks = protocol[0] if isinstance(protocol, tuple) else protocol
    payload = json.loads(next(block.text for block in blocks if block.type == "text"))
    hr = payload["data"]["recorded_recovery_hr"][0]
    assert hr["value"] == 120 and isinstance(hr["value"], int)
    assert hr["observed_at"] is not None
    assert hr["baseline_bpm"] is hr["recovery_bpm"] is hr["interval_seconds"] is None
    unknowns = [
        f
        for metric in payload["data"]["metrics"]
        for f in metric["evidence"]["fields"]
        if f["developer_data_index"] is not None
    ]
    assert unknowns and unknowns[0]["value"] == 42 and unknowns[0]["unit"] is None
    monkeypatch.setattr(details, "PAGE_ROWS", 2)
    cursor, collected = None, []
    for _ in range(20):
        page = ActivityDetails(db).get(42, tool="metrics", cursor=cursor)
        collected.extend(page["data"]["metrics"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert len({r["ordinal"] for r in collected}) == len(payload["data"]["metrics"])
    add_activity(db, 43)
    second = tmp_path / "43.fit"
    second.write_bytes(fit_file([record(1000)]))
    assert FitStore(db).process(43, second)["status"] == "success"
    assert ActivityDetails(db).get(43, tool="metrics")["data"]["recorded_recovery_hr"] is None


def test_feedback_revisions_idempotency_concurrency_and_restricted_privileges(
    isolated_db, monkeypatch
):
    import asyncio
    import json
    from concurrent.futures import ThreadPoolExecutor

    from psycopg.errors import InsufficientPrivilege, InvalidParameterValue
    from psycopg.types.json import Jsonb

    import garmin_health_gateway.mcp_server as server

    db = isolated_db
    db.migrate()
    add_activity(db)
    writer_role = "feedback_" + uuid4().hex
    reader_role = "reader_" + uuid4().hex
    db.test_roles.extend([writer_role, reader_role])
    db.provision_feedback_user("test-only-password", writer_role)
    db.provision_feedback_user("test-only-password", writer_role)
    db.provision_readonly_user("test-only-password", reader_role)
    with db.connection() as conn:
        name = conn.execute("SELECT current_database() AS n").fetchone()["n"]
        properties = conn.execute(
            "SELECT proconfig,prosecdef FROM pg_proc WHERE proname='append_activity_feedback'"
        ).fetchone()
    assert properties["prosecdef"] is True
    assert properties["proconfig"] == ["search_path=pg_catalog"]
    writer = Database(
        make_conninfo(ADMIN_URL, dbname=name, user=writer_role, password="test-only-password")
    )
    reader = Database(
        make_conninfo(ADMIN_URL, dbname=name, user=reader_role, password="test-only-password")
    )
    try:
        assert get_feedback(reader, 42)["fields"]["feedback"]["missing_reason"] == "no_feedback"
        initial = save_feedback(
            writer, 42, ActivityFeedback(breathing_effort=3, leg_effort=7), "first", 0
        )
        assert initial["data"]["revision"]["revision"] == 1
        retried = save_feedback(
            writer, 42, ActivityFeedback(breathing_effort=3, leg_effort=7), "first", 0
        )
        assert retried["data"]["revision"]["id"] == initial["data"]["revision"]["id"]
        with pytest.raises(ValueError, match="idempotency_payload_conflict"):
            save_feedback(writer, 42, ActivityFeedback(leg_effort=9), "first")
        with pytest.raises(ValueError, match="feedback_revision_conflict"):
            save_feedback(writer, 42, ActivityFeedback(leg_effort=9), "second", 0)
        with ThreadPoolExecutor(max_workers=4) as executor:
            saved = list(
                executor.map(
                    lambda i: save_feedback(
                        writer, 42, ActivityFeedback(overall_rpe=i), "parallel-" + str(i)
                    ),
                    range(8),
                )
            )
        assert sorted(s["data"]["revision"]["revision"] for s in saved) == list(range(2, 10))
        latest = get_feedback(reader, 42, include_history=True)
        assert latest["data"]["revision"]["revision"] == 9
        assert len(latest["data"]["history"]) == 9
        assert get_feedback(reader, 42, revision=1)["data"]["feedback"]["leg_effort"] == 7
        assert latest["data"]["feedback"]["leg_effort"] is None
        for i in range(50):
            save_feedback(writer, 42, ActivityFeedback(overall_rpe=0), "history-" + str(i))
        first_page = get_feedback(reader, 42, include_history=True)
        assert len(first_page["data"]["history"]) == 50
        assert first_page["next_cursor"] is not None
        save_feedback(writer, 42, ActivityFeedback(overall_rpe=1), "after-snapshot")
        second_page = get_feedback(
            reader, 42, include_history=True, cursor=first_page["next_cursor"]
        )
        assert [r["revision"] for r in second_page["data"]["history"]] == list(range(51, 60))
        assert second_page["next_cursor"] is None
        assert get_feedback(reader, 999)["availability"] == "not_found"
        assert (
            get_feedback(reader, 42, revision=999)["fields"]["feedback"]["missing_reason"]
            == "revision_not_found"
        )
        monkeypatch.setattr(server, "database", lambda: reader)
        monkeypatch.setattr(server, "feedback_writer", lambda: writer)
        result = asyncio.run(
            server.mcp.call_tool(
                "save_activity_feedback",
                {
                    "activity_id": 42,
                    "feedback": {"breathing_effort": 0, "hydration_ml": 0},
                    "idempotency_key": "via-mcp",
                },
            )
        )
        blocks = result[0] if isinstance(result, tuple) else result
        payload = json.loads(next(b.text for b in blocks if b.type == "text"))
        assert payload["data"]["feedback"]["breathing_effort"] == 0
        assert payload["data"]["feedback"]["hydration_ml"] == 0
        assert payload["data"]["feedback"]["pain"] is None
        assert (
            save_feedback(writer, 999, ActivityFeedback(), "absent")["availability"] == "not_found"
        )
        for query in (
            "SELECT * FROM activities",
            "SELECT * FROM activity_feedback_revisions",
            "UPDATE activities SET activity_name='bad'",
            "DELETE FROM activities",
            "DELETE FROM activity_feedback_revisions",
            "UPDATE activity_feedback_revisions SET revision=99",
            "INSERT INTO activity_feedback_revisions(activity_id,revision,"
            "payload,payload_sha256,idempotency_key) VALUES (42,99,'{}','x','x')",
        ):
            with pytest.raises(InsufficientPrivilege), writer.connection() as conn:
                conn.execute(query)
        with pytest.raises(InsufficientPrivilege), reader.connection() as conn:
            conn.execute("SELECT append_activity_feedback(42,'{}','forbidden',NULL)")
        with pytest.raises(InvalidParameterValue), writer.connection() as conn:
            conn.execute(
                "SELECT append_activity_feedback(42,%s,'invalid',NULL)",
                (Jsonb({"leg_effort": 11}),),
            )
    finally:
        writer.close()
        reader.close()


def test_mcp_stream_input_output_and_original_round_trip(isolated_db, tmp_path, monkeypatch):
    import asyncio
    import json

    import garmin_health_gateway.mcp_server as server

    db = isolated_db
    db.migrate()
    add_activity(db)
    path = tmp_path / "42.fit"
    path.write_bytes(fit_file([record(1000, hr=0, speed=0)]))
    assert FitStore(db).process(42, path)["status"] == "success"
    monkeypatch.setattr(server, "database", lambda: db)
    result = asyncio.run(
        server.mcp.call_tool(
            "get_activity_streams",
            {
                "activity_id": 42,
                "start_seconds": 0,
                "end_seconds": 10,
                "resolution_seconds": 0,
            },
        )
    )
    # FastMCP returns both protocol text content and structured content for dict tools.
    text_blocks = result[0] if isinstance(result, tuple) else result
    payload = json.loads(next(block.text for block in text_blocks if block.type == "text"))
    original = payload["data"]["streams"][0]
    assert original["heart_rate_bpm"] == original["speed_mps"] == 0
    assert next(f for f in original["fields"] if f["name"] == "heart_rate")["raw_value"] == 0
    assert original["quality_flags"][0]["reason"] == "hr_outside_conservative_range"
    with pytest.raises(Exception, match="raw resolution requires"):
        asyncio.run(
            server.mcp.call_tool(
                "get_activity_streams",
                {
                    "activity_id": 42,
                    "resolution_seconds": 0,
                },
            )
        )
