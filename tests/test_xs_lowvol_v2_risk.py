"""Synthetic V2-M0 risk-layer behavior tests."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from src.xs_lowvol_v2_risk import (
    ControlWeeklyReturn,
    FundingCoverageError,
    PositionState,
    RequiredFundingSettlement,
    V2RiskScaleInvalid,
    calculate_position_scale,
    plan_position_changes,
    position_scale_from_reference_vol,
    require_funding_coverage,
    scaled_target_positions,
)


SIGNAL_DAY = date(2026, 9, 16)


def _dated_returns(values: list[float], *, start: date = date(2026, 6, 1)) -> list[ControlWeeklyReturn]:
    return [
        ControlWeeklyReturn(
            week_ending=start + timedelta(days=7 * index),
            net_return=value,
            completed_at=start + timedelta(days=7 * index + 1),
        )
        for index, value in enumerate(values)
    ]


def _nonzero_returns() -> list[float]:
    return [-0.01, 0.01] * 6 + [0.0]


def test_reference_volatility_uses_population_std_and_frozen_annualization():
    values = _nonzero_returns()
    result = calculate_position_scale(values)
    reference = math.sqrt(52.0) * math.sqrt(sum(value * value for value in values) / len(values))
    assert result == pytest.approx(min(1.0, 0.15 / reference))


@pytest.mark.parametrize(
    ("reference_vol", "expected"),
    ((0.15, 1.0), (0.10, 1.0), (0.30, 0.5), (0.60, 0.25)),
)
def test_registered_reference_volatility_scales(reference_vol: float, expected: float):
    assert position_scale_from_reference_vol(reference_vol) == pytest.approx(expected)


def test_scale_is_always_bounded_without_leverage():
    for reference in (0.0001, 0.15, 0.3, 10.0):
        scale = position_scale_from_reference_vol(reference)
        assert 0.0 <= scale <= 1.0


def test_insufficient_weeks_zero_vol_nan_and_inf_fail_closed():
    with pytest.raises(V2RiskScaleInvalid, match="fewer than 13"):
        calculate_position_scale([0.01] * 12)
    with pytest.raises(V2RiskScaleInvalid, match="zero"):
        calculate_position_scale([0.01] * 13)
    with pytest.raises(V2RiskScaleInvalid, match="finite"):
        calculate_position_scale([0.01] * 12 + [math.nan])
    with pytest.raises(V2RiskScaleInvalid, match="finite"):
        calculate_position_scale([0.01] * 12 + [math.inf])


def test_future_weekly_return_cannot_change_the_current_scale():
    dated = _dated_returns(_nonzero_returns())
    future = ControlWeeklyReturn(
        week_ending=SIGNAL_DAY + timedelta(days=7),
        completed_at=SIGNAL_DAY + timedelta(days=7),
        net_return=0.90,
    )
    baseline = calculate_position_scale(dated, signal_time=SIGNAL_DAY)
    changed_future = calculate_position_scale(
        dated + [future], signal_time=SIGNAL_DAY
    )
    altered_future = calculate_position_scale(
        dated + [ControlWeeklyReturn(future.week_ending, -0.90, future.completed_at)],
        signal_time=SIGNAL_DAY,
    )
    assert changed_future == pytest.approx(baseline)
    assert altered_future == pytest.approx(baseline)


def test_incomplete_future_row_is_ignored_but_incomplete_required_row_fails():
    dated = _dated_returns(_nonzero_returns())
    future = ControlWeeklyReturn(
        week_ending=SIGNAL_DAY + timedelta(days=7),
        completed_at=SIGNAL_DAY + timedelta(days=7),
        net_return=0.1,
        complete=False,
    )
    assert calculate_position_scale(dated + [future], signal_time=SIGNAL_DAY) > 0
    incomplete = dated[:-1] + [ControlWeeklyReturn(dated[-1].week_ending, 0.1, dated[-1].completed_at, False)]
    with pytest.raises(V2RiskScaleInvalid, match="incomplete"):
        calculate_position_scale(incomplete, signal_time=SIGNAL_DAY)


def test_v1_targets_and_directions_are_preserved_while_notional_scales():
    targets = scaled_target_positions(
        {"BTCUSDT": 1, "ETHUSDT": -1}, base_notional=100.0, position_scale=0.5
    )
    assert {symbol: target.direction for symbol, target in targets.items()} == {
        "BTCUSDT": 1,
        "ETHUSDT": -1,
    }
    assert {target.notional for target in targets.values()} == {50.0}


def test_same_side_resize_uses_only_absolute_delta():
    old = {"BTCUSDT": PositionState("BTCUSDT", 1, 100.0)}
    new = scaled_target_positions({"BTCUSDT": 1}, base_notional=100.0, position_scale=0.6)
    changes = plan_position_changes(old, new)
    assert len(changes) == 1
    assert changes[0].action == "RESIZE"
    assert changes[0].notional_delta == pytest.approx(-40.0)
    assert changes[0].cost_notional == pytest.approx(40.0)


def test_direction_flip_is_close_then_open():
    old = {"BTCUSDT": PositionState("BTCUSDT", 1, 100.0)}
    new = scaled_target_positions({"BTCUSDT": -1}, base_notional=100.0, position_scale=0.5)
    changes = plan_position_changes(old, new)
    assert [change.action for change in changes] == ["CLOSE", "OPEN"]
    assert [change.direction for change in changes] == [1, -1]
    assert [change.cost_notional for change in changes] == pytest.approx([100.0, 50.0])


def test_missing_real_funding_settlement_fails_without_substitution():
    required = [("BTCUSDT", 1), ("BTCUSDT", 2)]
    observed = [("BTCUSDT", 1)]
    with pytest.raises(FundingCoverageError, match="missing real funding settlement"):
        require_funding_coverage(
            [RequiredFundingSettlement(symbol, timestamp) for symbol, timestamp in required],
            [RequiredFundingSettlement(symbol, timestamp) for symbol, timestamp in observed],
        )
