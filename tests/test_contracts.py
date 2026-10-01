from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from garmin_health_gateway.contracts import (
    AnalysisError,
    AnalysisResponse,
    EvidenceSource,
    FieldEvidence,
    Freshness,
    field_evidence,
)


def test_missing_is_not_zero_and_mixed_sources_keep_their_evidence():
    when = datetime(2026, 8, 29, tzinfo=UTC)
    response = AnalysisResponse(
        availability="partial",
        activity_id=42,
        data={"speed": 0, "hr": None, "temperature": 18.5, "rating": 0},
        fields={
            "speed": field_evidence(0, unit="m/s", source_ids=["fit"]),
            "hr": field_evidence(None, unit="bpm", source_ids=["fit"]),
            "temperature": field_evidence(18.5, unit="degC", source_ids=["weather"]),
            "rating": field_evidence(0, unit="0..10", source_ids=["feedback"]),
        },
        sources=[
            EvidenceSource(
                id="fit",
                provider="FIT",
                kind="recorded",
                observed_at=when,
                processed_at=when,
                processing_version="test/1",
            ),
            EvidenceSource(
                id="weather",
                provider="historical",
                kind="modeled",
                observed_at=when,
                collected_at=when,
            ),
            EvidenceSource(id="feedback", provider="user", kind="user_reported", collected_at=when),
        ],
        freshness=Freshness(last_success_at=when, last_attempt_at=when, stale=False),
    )
    result = response.as_dict()
    assert result["data"]["speed"] == result["data"]["rating"] == 0
    assert result["data"]["hr"] is None
    assert result["fields"]["hr"]["missing_reason"] == "not_recorded"
    assert result["fields"]["speed"]["missing_reason"] is None
    assert result["sources"][0]["observed_at"] == "2026-08-29T00:00:00Z"
    assert result["fields"]["temperature"]["source_ids"] == ["weather"]
    assert AnalysisResponse.model_validate_json(response.model_dump_json()) == response


@pytest.mark.parametrize("value", [False, 0, 0.0, [], {}])
def test_falsey_values_are_not_missing(value):
    metadata = field_evidence(value, unit="dimensionless", source_ids=[])
    assert metadata.availability == "available"
    assert metadata.missing_reason is None


def test_unknown_units_and_missing_reasons_are_explicit():
    metadata = field_evidence(None, unit=None, source_ids=[], missing_reason="unsupported")
    assert metadata.unit is None
    assert metadata.unit_missing_reason == "unknown"
    assert metadata.missing_reason == "unsupported"
    with pytest.raises(ValidationError):
        FieldEvidence(availability="not_recorded", unit="bpm")
    with pytest.raises(ValidationError):
        FieldEvidence(availability="available")


def test_freshness_does_not_replace_last_success_on_failed_attempt():
    success = datetime(2026, 8, 29, tzinfo=UTC)
    failed = datetime(2026, 8, 30, tzinfo=UTC)
    response = AnalysisResponse(
        availability="partial",
        data={"hr": 60},
        freshness=Freshness(
            last_success_at=success,
            last_attempt_at=failed,
            stale=True,
            stale_reason="latest_refresh_failed",
        ),
        errors=[
            AnalysisError(
                code="provider_unavailable",
                message="Refresh failed",
                retryable=True,
                occurred_at=failed,
            )
        ],
    ).as_dict()
    assert response["freshness"]["last_success_at"] != response["freshness"]["last_attempt_at"]
    assert response["data"]["hr"] == 60
    assert response["errors"][0]["retryable"] is True


def test_invalid_source_and_value_metadata_rejected():
    with pytest.raises(ValidationError, match="unknown source"):
        AnalysisResponse(
            availability="available",
            fields={
                "hr": field_evidence(60, unit="bpm", source_ids=["absent"]),
            },
        )
    with pytest.raises(ValidationError, match="null field"):
        AnalysisResponse(
            availability="available",
            data={"hr": None},
            fields={
                "hr": field_evidence(60, unit="bpm", source_ids=[]),
            },
        )
    with pytest.raises(ValidationError, match="present field"):
        AnalysisResponse(
            availability="partial",
            data={"hr": 0},
            fields={
                "hr": field_evidence(None, unit="bpm", source_ids=[]),
            },
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_nested_measurements_are_rejected(value):
    with pytest.raises(ValidationError):
        AnalysisResponse(availability="available", data={"samples": [{"hr": value}]})


def test_naive_times_and_unknown_contract_fields_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        EvidenceSource(id="fit", provider="FIT", kind="recorded", observed_at=datetime(2026, 8, 29))
    with pytest.raises(ValidationError):
        AnalysisResponse(availability="available", invented=True)
