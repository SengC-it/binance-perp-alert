"""V2-M1 engineering-replay parity and safety tests."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from backtest.m1_b import ScheduleAttempt
from backtest.v2_forward import MISSING_EXECUTION_PRICE, NO_SIGNAL, SUCCESS, RebalanceAttempt
from backtest.v2_m1_engineering import (
    EXPECTED_DATASET_SHA256,
    EXPECTED_NORMALIZED_DATASET_SHA256,
    EngineeringReplayError,
    M1_DATASET_FREEZE_PATH,
    _logical_signal_time,
    _retry_diagnostics,
    _run_no_lookahead_mutation,
    _synthetic_transition_fixture,
    _transition_diagnostics,
    assert_no_performance_fields,
    load_verified_dataset,
    paired_schedule_diagnostics,
)
from src.xs_lowvol_v2_anchor import (
    APPROVED_V2_FORWARD_ANCHOR_SHA256,
    FORWARD_ANCHOR_STATUS,
    load_v2_forward_anchor,
    validate_v2_forward_anchor,
    verify_v2_forward_anchor_hash,
)
from src.xs_lowvol_v2_risk import (
    ControlWeeklyReturn,
    PositionState,
    calculate_position_scale,
    plan_position_changes,
    reference_annualized_volatility,
)


UTC = timezone.utc


def _weekly_fixture() -> tuple[ControlWeeklyReturn, ...]:
    start = date(2026, 1, 4)
    values = (-0.012, 0.006, 0.019, -0.004, 0.011, -0.009, 0.003) * 2
    return tuple(
        ControlWeeklyReturn(
            week_ending=start + timedelta(days=7 * index),
            net_return=value,
            completed_at=datetime.combine(
                start + timedelta(days=7 * index),
                time.max,
                tzinfo=UTC,
            ),
            complete=True,
        )
        for index, value in enumerate(values)
    )


def test_historical_v1_target_parity_fixture_has_zero_divergence():
    first = date(2020, 8, 14)
    parents = (
        ScheduleAttempt(date(2020, 8, 13), first, SUCCESS, target_symbols=("AAAUSDT", "BBBUSDT")),
        ScheduleAttempt(date(2020, 8, 20), first + timedelta(days=7), SUCCESS, target_symbols=("AAAUSDT", "CCCUSDT")),
    )
    targets = (
        (("AAAUSDT", 1), ("BBBUSDT", -1)),
        (("AAAUSDT", 1), ("CCCUSDT", -1)),
    )
    v2 = (
        RebalanceAttempt(
            signal_day=parents[0].signal_day,
            execution_day=parents[0].execution_day,
            status=SUCCESS,
            target_directions=targets[0],
        ),
        RebalanceAttempt(
            signal_day=parents[1].signal_day,
            execution_day=parents[1].execution_day,
            status=SUCCESS,
            target_directions=targets[1],
        ),
    )
    result = paired_schedule_diagnostics(
        parents,
        v2,
        targets,
        (SUCCESS, SUCCESS),
    )
    assert result["paired_schedule_divergence_count"] == 0
    assert result["parent_schedule_mismatch_count"] == 0
    assert result["target_mismatch_count"] == 0


def test_historical_retry_parity_fixture_retries_on_next_calendar_day():
    base = date(2020, 8, 14)
    attempts = (
        ScheduleAttempt(base - timedelta(days=1), base, NO_SIGNAL),
        ScheduleAttempt(base, base + timedelta(days=1), SUCCESS),
        ScheduleAttempt(base + timedelta(days=7), base + timedelta(days=8), SUCCESS),
        ScheduleAttempt(base + timedelta(days=8), base + timedelta(days=9), MISSING_EXECUTION_PRICE),
        ScheduleAttempt(base + timedelta(days=9), base + timedelta(days=10), SUCCESS),
        ScheduleAttempt(base + timedelta(days=19), base + timedelta(days=20), NO_SIGNAL),
        ScheduleAttempt(base + timedelta(days=21), base + timedelta(days=22), SUCCESS),
    )
    result = _retry_diagnostics(attempts, base + timedelta(days=30))
    assert result == {
        "retry_count": 2,
        "retry_semantic_violation_count": 1,
        "eventually_recovered_count": 2,
    }


def test_real_frozen_dataset_risk_scale_recomputation_fixture():
    """Recompute the frozen scale from 13 weeks of actual cached bars."""
    if not Path(M1_DATASET_FREEZE_PATH).is_file():
        pytest.skip("the ignored frozen market-data cache is unavailable in this checkout")
    dataset = load_verified_dataset()
    assert dataset["dataset_sha256"] == EXPECTED_DATASET_SHA256
    assert dataset["normalized_dataset_sha256"] == EXPECTED_NORMALIZED_DATASET_SHA256
    history = next(item for item in dataset["usable_histories"] if item.symbol == "BTCUSDT")
    bars = sorted(history.daily_bars, key=lambda item: item.day)
    window = bars[:92]
    assert len(window) == 92
    assert all(right.day - left.day == timedelta(days=1) for left, right in zip(window, window[1:]))
    weekly = tuple(
        ControlWeeklyReturn(
            week_ending=window[(index + 1) * 7].day,
            net_return=window[(index + 1) * 7].close / window[index * 7].close - 1.0,
            completed_at=datetime.combine(window[(index + 1) * 7].day, time.max, tzinfo=UTC),
            complete=True,
        )
        for index in range(13)
    )
    signal_time = datetime.combine(window[91].day + timedelta(days=1), time.min, tzinfo=UTC)
    reference = reference_annualized_volatility(weekly, signal_time=signal_time)
    scale = calculate_position_scale(weekly, signal_time=signal_time)
    assert scale == pytest.approx(min(1.0, 0.15 / reference), rel=0.0, abs=1e-15)
    assert 0.0 <= scale <= 1.0
    assert all(record.completion_time < signal_time for record in weekly)


def test_future_mutation_no_lookahead_keeps_scale_unchanged():
    weekly = _weekly_fixture()
    signal_time = datetime(2026, 4, 7, tzinfo=UTC)
    baseline = calculate_position_scale(weekly, signal_time=signal_time)
    assert _run_no_lookahead_mutation(weekly, signal_time, baseline)


def test_same_side_resize_accounting_has_no_fake_close_open():
    old = {"AAAUSDT": PositionState("AAAUSDT", 1, 0.4)}
    from src.xs_lowvol_v2_risk import scaled_target_positions

    new = scaled_target_positions({"AAAUSDT": 1}, base_notional=1.0, position_scale=0.7)
    changes = plan_position_changes(old, new)
    cases, violations = _transition_diagnostics(old, {"AAAUSDT": 1}, changes)
    assert [change.action for change in changes] == ["RESIZE"]
    assert changes[0].cost_notional == pytest.approx(abs(changes[0].notional_delta))
    assert "increase_resize" in cases
    assert "direction_flip" not in cases
    assert violations == []


def test_transition_fixture_covers_all_frozen_cases():
    fixture = _synthetic_transition_fixture()
    assert fixture["valid"] is True
    assert fixture["change_count"] == 6
    assert all(fixture["cases"].values())


def test_paired_schedule_divergence_detector_reports_first_cause():
    parent = ScheduleAttempt(
        date(2020, 8, 13),
        date(2020, 8, 14),
        SUCCESS,
        target_symbols=("AAAUSDT",),
    )
    divergent = RebalanceAttempt(
        signal_day=parent.signal_day,
        execution_day=parent.execution_day + timedelta(days=1),
        status=SUCCESS,
        target_directions=(("BBBUSD", 1),),
    )
    result = paired_schedule_diagnostics(
        (parent,),
        (divergent,),
        ((("AAAUSDT", 1),),),
        (SUCCESS,),
    )
    assert result["paired_schedule_divergence_count"] == 1
    assert result["parent_schedule_mismatch_count"] == 1
    assert result["target_mismatch_count"] == 1
    assert result["first_divergence"]["reason"] == "signal/execution day mismatch; target direction mismatch"


def test_no_performance_field_schema_is_fail_closed():
    assert_no_performance_fields(
        {
            "scale_behavior": {"scale_min": 0.2, "scale_max": 1.0},
            "classification": "NOT_PERFORMANCE_EVIDENCE",
        }
    )
    with pytest.raises(EngineeringReplayError, match="forbidden performance fields"):
        assert_no_performance_fields({"return": 0.1})


def test_forward_anchor_remains_frozen_not_started():
    anchor = load_v2_forward_anchor()
    validate_v2_forward_anchor()
    assert anchor["status"] == FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
    assert verify_v2_forward_anchor_hash() == APPROVED_V2_FORWARD_ANCHOR_SHA256


def test_logical_signal_time_uses_execution_day_midnight_utc():
    assert _logical_signal_time(date(2020, 8, 14)) == datetime(2020, 8, 14, tzinfo=UTC)
