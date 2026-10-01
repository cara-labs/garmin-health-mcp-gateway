import hashlib
import struct
from datetime import timedelta

import fitdecode
import pytest
from fit_factory import BASE_TIME, FIT_EPOCH, developer_definition, event, fit_file, message, record

from garmin_health_gateway.fit_decoder import FitDecoder, FitLimitError, archive_identity


def test_generated_real_fit_pause_duplicate_timestamp_missing_and_unknown_fields(tmp_path):
    path = tmp_path / "42.fit"
    path.write_bytes(
        fit_file(
            [
                event(1000, 0),
                record(1000, hr=255),
                record(1000, distance=1),
                record(1005, distance=5),
                event(1005, 1),
                record(1010, distance=5),
                event(1015, 0),
                record(1020, distance=10),
                record(),
                message(65000, [(1, 0x86, struct.pack("<I", 999))]),
            ]
        )
    )
    decoded = list(FitDecoder().decode(path, FIT_EPOCH + timedelta(seconds=BASE_TIME + 1000)))
    assert [row.ordinal for row in decoded] == list(range(10))
    samples = [row for row in decoded if row.category == "sample"]
    assert samples[0].data["heart_rate_bpm"] is None
    assert samples[0].data["speed_mps"] == 0
    assert samples[0].observed_at == samples[1].observed_at
    assert samples[2].elapsed_seconds == samples[2].timer_seconds == 5
    assert samples[3].elapsed_seconds == 10 and samples[3].timer_seconds == 5
    assert samples[4].elapsed_seconds == 20 and samples[4].timer_seconds == 10
    assert samples[5].observed_at is None and samples[5].elapsed_seconds is None
    unknown = next(field for field in samples[0].fields if field["number"] == 250)
    assert unknown["value"] == unknown["raw_value"] == 123
    assert unknown["unit"] is None
    assert decoded[-1].number == 65000 and decoded[-1].fields[0]["value"] == 999
    assert archive_identity(path) == (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_size,
    )


def test_supported_developer_field_with_unknown_units_round_trips(tmp_path):
    path = tmp_path / "developer.fit"
    path.write_bytes(
        fit_file([*developer_definition(), record(1000, developer_fields=[(1, 0, b"\x2a\x00")])])
    )
    decoded = list(FitDecoder().decode(path, FIT_EPOCH + timedelta(seconds=BASE_TIME + 1000)))
    field = next(f for f in decoded[-1].fields if f["developer_data_index"] == 0)
    assert field["name"] == "mystery" and field["number"] == 1
    assert field["value"] == field["raw_value"] == 42
    assert field["unit"] in (None, "")


def test_explicit_limits_do_not_silently_truncate(tmp_path):
    path = tmp_path / "many.fit"
    path.write_bytes(fit_file([record(1000), record(1001)]))
    with pytest.raises(FitLimitError, match="message_count_limit"):
        list(FitDecoder(max_messages=1).decode(path, FIT_EPOCH))
    with pytest.raises(FitLimitError, match="message_payload_limit"):
        list(FitDecoder(max_message_bytes=10).decode(path, FIT_EPOCH))


def test_corrupt_crc_is_an_error_and_archive_untouched(tmp_path):
    path = tmp_path / "corrupt.fit"
    payload = fit_file([record(1000)])
    corrupted = payload[:-2] + b"\x00\x00"
    path.write_bytes(corrupted)
    with pytest.raises(fitdecode.FitCRCError):
        list(FitDecoder().decode(path, FIT_EPOCH))
    assert path.read_bytes() == corrupted


def test_no_timer_events_does_not_invent_timer_time(tmp_path):
    path = tmp_path / "no-events.fit"
    path.write_bytes(fit_file([record(1000)]))
    sample = next(FitDecoder().decode(path, FIT_EPOCH))
    assert sample.timer_seconds is None
    assert sample.data["missing"]["timer_seconds"] == "timer_state_unknown"
