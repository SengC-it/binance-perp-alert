from __future__ import annotations

from datetime import datetime, timezone

from backtest.v2_1_m1_2_ton_root_cause import (
    CONTRACT_SETTLEMENT_MS,
    classify_funding_lifecycle_root_cause,
    run_boundary_diagnostic_cases,
)


def test_case_1_does_not_require_post_lifetime_funding():
    result = classify_funding_lifecycle_root_cause(
        last_funding_ms=CONTRACT_SETTLEMENT_MS - 3_600_000,
        funding_interval_hours=4.0,
        contract_termination_ms=CONTRACT_SETTLEMENT_MS,
        holding_end_ms=CONTRACT_SETTLEMENT_MS,
    )
    assert result["classification"] is None
    assert result["required_funding_missing"] is False
    assert result["next_scheduled_funding_utc"] == "2026-06-23T12:00:00Z"


def test_case_2_detects_real_missing_funding_gap_while_active():
    result = classify_funding_lifecycle_root_cause(
        last_funding_ms=CONTRACT_SETTLEMENT_MS - 3_600_000,
        funding_interval_hours=4.0,
        contract_termination_ms=CONTRACT_SETTLEMENT_MS + 4 * 3_600_000,
        holding_end_ms=CONTRACT_SETTLEMENT_MS + 5 * 3_600_000,
        actual_funding_times_ms=(),
    )
    assert result["classification"] == "REAL_MISSING_REQUIRED_FUNDING_SETTLEMENT"
    assert result["required_funding_missing"] is True


def test_case_3_unknown_termination_fails_closed():
    result = classify_funding_lifecycle_root_cause(
        last_funding_ms=CONTRACT_SETTLEMENT_MS - 3_600_000,
        funding_interval_hours=4.0,
        contract_termination_ms=None,
        holding_end_ms=CONTRACT_SETTLEMENT_MS + 5 * 3_600_000,
    )
    assert result["classification"] == "ROOT_CAUSE_UNRESOLVED"
    assert result["required_funding_missing"] is False


def test_case_4_holding_ends_before_next_scheduled_settlement():
    result = classify_funding_lifecycle_root_cause(
        last_funding_ms=CONTRACT_SETTLEMENT_MS - 3_600_000,
        funding_interval_hours=4.0,
        contract_termination_ms=CONTRACT_SETTLEMENT_MS + 5 * 3_600_000,
        holding_end_ms=CONTRACT_SETTLEMENT_MS + 1 * 3_600_000,
    )
    assert result["classification"] is None
    assert result["required_funding_missing"] is False


def test_all_registered_boundary_cases_pass():
    results = run_boundary_diagnostic_cases()
    assert len(results) == 4
    assert all(row["passed"] for row in results)
