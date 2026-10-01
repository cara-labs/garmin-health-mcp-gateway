from copy import deepcopy

from garmin_health_gateway.quality import DETECTOR_VERSION, quality_flags


def sample(elapsed=0, hr=140, latitude=40.0, longitude=20.0):
    return {
        "elapsed_seconds": elapsed,
        "heart_rate_bpm": hr,
        "latitude": latitude,
        "longitude": longitude,
    }


def reasons(current, previous=None):
    flags = quality_flags(current, previous)
    assert all(f["suspected"] and f["detector_version"] == DETECTOR_VERSION for f in flags)
    return {f["reason"] for f in flags}


def test_normal_running_examples_are_not_flagged():
    assert reasons(sample()) == set()
    assert reasons(sample(1, 142, 40.00001, 20.00001), sample()) == set()
    assert reasons(sample(1, None, None, None), sample()) == set()


def test_flags_do_not_delete_or_replace_original_values():
    bad = sample(1, 230, 45, 25)
    original = deepcopy(bad)
    found = reasons(bad, sample())
    assert found == {"hr_abrupt_change", "gps_implausible_jump"}
    assert bad == original
    assert reasons(sample(hr=0)) == {"hr_outside_conservative_range"}


def test_invalid_coordinates_and_time_evidence():
    assert reasons(sample(latitude=95)) == {"gps_invalid_or_incomplete_coordinate"}
    assert reasons(sample(latitude=None)) == {"gps_invalid_or_incomplete_coordinate"}
    assert "timing_missing" in reasons(sample(elapsed=None))
    assert "timestamp_before_activity_start" in reasons(sample(elapsed=-1))
    assert reasons(sample(2), sample(3)) == {"timestamp_out_of_order"}
    assert reasons(sample(0, longitude=30), sample()) == {"gps_duplicate_time_displacement"}


def test_long_gaps_are_not_claimed_to_be_abrupt_hr_or_gps_errors():
    assert reasons(sample(3600, 230, 45, 25), sample()) == set()
