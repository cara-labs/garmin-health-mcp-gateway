import pytest
from pydantic import ValidationError

from garmin_health_gateway.feedback import ActivityFeedback, RecoveryProtocol


@pytest.mark.parametrize(
    "data",
    [
        {"breathing_effort": -1},
        {"leg_effort": 11},
        {"overall_rpe": float("nan")},
        {"hydration_ml": float("inf")},
        {"heaviness_onset_seconds": -1},
        {"pain_severity": "5"},
        {"invented": 1},
        {"walking_reasons": ["x"] * 21},
        {"recovery_hr_protocol": {"interval_seconds": -1}},
        {"recovery_hr_protocol": {"recovery_hr_bpm": 301}},
    ],
)
def test_feedback_validation(data):
    with pytest.raises(ValidationError):
        ActivityFeedback.model_validate(data)


def test_optional_complete_snapshot_keeps_zero_and_unknown_separate():
    payload = ActivityFeedback(breathing_effort=0, leg_effort=7, hydration_ml=0).model_dump(
        mode="json"
    )
    assert payload["breathing_effort"] == 0
    assert payload["leg_effort"] == 7
    assert payload["hydration_ml"] == 0
    assert payload["pain"] is None
    assert RecoveryProtocol(interval_seconds=120).model_dump()["baseline_hr_bpm"] is None
