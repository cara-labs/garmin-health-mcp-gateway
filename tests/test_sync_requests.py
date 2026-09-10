from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_sync import FakeDatabase, FakeProvider, _settings

from garmin_health_gateway import mcp_server
from garmin_health_gateway.sync import SyncEngine
from garmin_health_gateway.sync_requests import SyncRequests


def test_concurrent_requests_coalesce_and_persist(tmp_path):
    queue = SyncRequests(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: queue.request(), range(20)))
    assert sum(result["accepted"] for result in results) == 1
    assert len({result["request_id"] for result in results}) == 1
    assert SyncRequests(tmp_path).status()["status"] == "queued"
    assert queue.claim()["status"] == "running"
    assert queue.claim() is None
    assert queue.request()["reason"] == "already_pending"
    queue.finish({"status": "success", "counts": {"activities": 1}})
    assert queue.request()["reason"] == "cooldown"
    with queue.lock():
        job = queue._read()
        job["finished_at"] = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
        queue._write(job)
    assert queue.request()["accepted"] is True


def test_restart_reports_interruption_and_retains_queued_request(tmp_path):
    queue = SyncRequests(tmp_path)
    queue.request()
    queue.recover()
    assert queue.status()["status"] == "queued"
    queue.claim()
    queue.recover()
    assert queue.status()["status"] == "error"
    assert "restarted" in queue.status()["error"]


def test_worker_lock_prevents_parallel_sync(tmp_path):
    queue = SyncRequests(tmp_path)
    with (
        queue.lock("worker"),
        pytest.raises(RuntimeError, match="already running"),
        SyncRequests(tmp_path).lock("worker", blocking=False),
    ):
        pytest.fail("Concurrent worker acquired lock")


def test_mcp_request_to_collector_to_status(tmp_path, monkeypatch):
    config = _settings(tmp_path)
    db = FakeDatabase()
    db.get_sync_state = lambda: [
        {"resource": key, "status": value} for key, value in db.states.items()
    ]
    monkeypatch.setattr(mcp_server, "settings", lambda: config)
    monkeypatch.setattr(mcp_server, "database", lambda: db)
    job = mcp_server.request_sync()
    assert job["status"] == "queued"
    queue = SyncRequests(config.sync_request_dir)
    engine = SyncEngine(config, db, FakeProvider())
    assert engine.process_request(queue)
    result = mcp_server.get_sync_status()["ad_hoc"]
    assert result["request_id"] == job["request_id"]
    assert result["status"] == "success"
    assert result["counts"] == {"daily_health": 3, "activities": 1}
    assert len(db.health) == 3
    assert engine.process_request(queue) is False


def test_failure_still_syncs_activities_and_reports_partial_result(tmp_path):
    class BrokenHealth(FakeProvider):
        def get_daily_summary(self, target):
            raise ValueError("bad response")

    config = _settings(tmp_path)
    queue = SyncRequests(config.sync_request_dir)
    queue.request()
    engine = SyncEngine(config, FakeDatabase(), BrokenHealth())
    engine.process_request(queue)
    result = queue.status()
    assert result["status"] == "error"
    assert result["counts"] == {"activities": 1}
    assert result["errors"] == {"daily_health": "ValueError"}


def test_rate_limit_stops_remaining_external_requests(tmp_path):
    class GarminConnectTooManyRequestsError(Exception):
        pass

    class RateLimited(FakeProvider):
        def get_daily_summary(self, target):
            raise GarminConnectTooManyRequestsError()

        def get_activities(self, start, end):
            pytest.fail("Must not call Garmin again after rate limit")

    config = _settings(tmp_path)
    queue = SyncRequests(config.sync_request_dir)
    queue.request()
    SyncEngine(config, FakeDatabase(), RateLimited()).process_request(queue)
    assert queue.status()["status"] == "error"
    assert queue.request()["reason"] == "cooldown"


def test_scheduler_picks_up_request_while_hourly_sync_sleeps(tmp_path, monkeypatch):
    config = _settings(tmp_path)
    queue = SyncRequests(config.sync_request_dir)
    db = FakeDatabase()
    db.states = {"daily_health": "success", "activities": "success"}
    engine = SyncEngine(config, db, FakeProvider())
    scheduled = []
    monkeypatch.setattr(engine, "run_once", lambda **kw: scheduled.append(kw) or {})

    def sleep(_):
        if queue.status() is None:
            queue.request()
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr("garmin_health_gateway.sync.time.sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        engine._schedule(queue)
    assert len(scheduled) == 1
    assert queue.status()["status"] == "success"


def test_initial_backfill_precedes_queued_refresh(tmp_path, monkeypatch):
    config = _settings(tmp_path)
    queue = SyncRequests(config.sync_request_dir)
    queue.request()
    engine = SyncEngine(config, FakeDatabase(), FakeProvider())
    order = []

    def scheduled(**kw):
        assert kw["force_backfill"] is True
        assert queue.status()["status"] == "queued"
        order.append("backfill")
        return {}

    def requested(q):
        order.append("refresh")
        raise KeyboardInterrupt

    monkeypatch.setattr(engine, "run_once", scheduled)
    monkeypatch.setattr(engine, "process_request", requested)
    monkeypatch.setattr("garmin_health_gateway.sync.time.sleep", lambda _: None)
    with pytest.raises(KeyboardInterrupt):
        engine._schedule(queue)
    assert order == ["backfill", "refresh"]


def test_requested_date_window_uses_configured_timezone(tmp_path, monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 10, 1, tzinfo=UTC).astimezone(tz)

    config = replace(_settings(tmp_path), timezone="America/Los_Angeles")
    queue = SyncRequests(config.sync_request_dir)
    queue.request()
    monkeypatch.setattr("garmin_health_gateway.sync.datetime", FrozenDatetime)
    SyncEngine(config, FakeDatabase(), FakeProvider()).process_request(queue)
    assert queue.status()["start_date"] == "2026-09-07"
    assert queue.status()["end_date"] == "2026-09-09"
