from unittest.mock import Mock

from test_sync import FakeDatabase, FakeProvider, _settings

from garmin_health_gateway import cli
from garmin_health_gateway.sync import SyncEngine


def test_optional_analysis_failures_do_not_change_core_success(tmp_path):
    from datetime import date

    db = FakeDatabase()
    analysis = Mock()
    analysis.profile.side_effect = RuntimeError("profile unavailable")
    analysis.activity.side_effect = RuntimeError("FIT/weather unavailable")
    engine = SyncEngine(_settings(tmp_path), db, FakeProvider(), analysis=analysis)
    assert engine.sync_daily(date(2020, 1, 1), date(2020, 1, 1)) == 1
    assert engine.sync_activities(date(2020, 1, 1), date(2020, 1, 1)) == 1
    assert db.states == {"daily_health": "success", "activities": "success"}
    analysis.profile.assert_called_once()
    analysis.activity.assert_called_once()


def test_archive_cli_does_not_construct_or_authenticate_garmin(tmp_path, monkeypatch, capsys):
    from garmin_health_gateway import backfill

    db = Mock()
    batch = Mock(return_value={"status": "batch_complete", "results": []})
    provider = Mock(side_effect=AssertionError("Garmin must not be touched"))
    monkeypatch.setattr(cli.Settings, "from_env", lambda: _settings(tmp_path))
    monkeypatch.setattr(cli, "Database", lambda *a, **kw: db)
    monkeypatch.setattr(cli, "_provider", provider)
    monkeypatch.setattr(backfill, "archive_batch", batch)
    monkeypatch.setattr(
        "sys.argv",
        ["garmin-health", "process-archives", "--activity-id", "42", "--limit", "1", "--reprocess"],
    )
    cli.main()
    provider.assert_not_called()
    assert batch.call_args.kwargs["activity_ids"] == [42]
    assert batch.call_args.kwargs["reprocess"] is True
    assert "batch_complete" in capsys.readouterr().out
