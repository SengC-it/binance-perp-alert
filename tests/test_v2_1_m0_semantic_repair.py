"""XS-LOWVOL-V2.1 M0 semantic-repair and re-freeze tests."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

import pytest

from backtest.v2_1_forward import (
    NO_SIGNAL,
    PARENT_FAILURE_STATUSES,
    SUCCESS,
    V21PairedEngine,
    V21PairedInputError,
    V2_DATA_INTEGRITY_HALT,
    validate_v2_1_temporal_inputs,
)
from backtest.v2_1_protocol import (
    V21_PROTOCOL_SHA256,
    V21_STRATEGY_ID,
    frozen_v2_1_forward_gate_policy,
    load_v2_1_protocol,
    protocol_sha256,
    validate_v2_1_protocol,
    verify_v2_1_protocol_hash,
)
from backtest.v2_protocol import load_v2_protocol
from src.xs_lowvol_v2_1_risk import (
    ControlWeeklyReturn,
    V21DataIntegrityHalt,
    V21RiskScaleResult,
    calculate_position_scale,
    completed_control_weekly_returns,
    evaluate_risk_scale,
    position_scale_from_reference_vol,
    reference_annualized_volatility,
)
from src.xs_lowvol_v2_1_spec import (
    V21_SPEC_SHA256,
    V21_STRATEGY_ID as SPEC_STRATEGY_ID,
    load_v2_1_spec,
    validate_v2_1_spec,
    verify_v2_1_spec_hash,
)
from src.xs_lowvol_v2_risk import V2RiskScaleInvalid, calculate_position_scale as old_calculate_position_scale
from src.xs_lowvol_v2_anchor import validate_v2_forward_anchor, verify_v2_forward_anchor_hash
from src.xs_lowvol_v2_1_anchor import (
    V2_1_FORWARD_ANCHOR_PATH,
    validate_v2_1_forward_anchor,
)


UTC = timezone.utc
SIGNAL_DAY = date(2020, 8, 13)
EXECUTION_DAY = date(2020, 8, 14)
SIGNAL_TIME = datetime.combine(EXECUTION_DAY, time.min, tzinfo=UTC)
TARGETS = {"BTCUSDT": 1, "ETHUSDT": -1}


def _records(
    values: list[float],
    *,
    start: date = date(2020, 5, 14),
    complete: bool = True,
    completed_at: object = "aware",
) -> list[ControlWeeklyReturn]:
    records: list[ControlWeeklyReturn] = []
    for index, value in enumerate(values):
        week_ending = start + timedelta(days=7 * index)
        if completed_at == "aware":
            marker: object = datetime.combine(
                week_ending + timedelta(days=1), time(12), tzinfo=UTC
            )
        elif completed_at == "naive":
            marker = datetime.combine(week_ending + timedelta(days=1), time(12))
        elif completed_at == "missing":
            marker = None
        else:
            marker = completed_at
        records.append(
            ControlWeeklyReturn(
                week_ending=week_ending,
                net_return=value,
                completed_at=marker,
                complete=complete,
            )
        )
    return records


def _zero_records() -> list[ControlWeeklyReturn]:
    return _records([0.0] * 13)


def test_v2_1_spec_and_protocol_are_independently_pinned():
    assert load_v2_1_spec()["strategy_id"] == SPEC_STRATEGY_ID == V21_STRATEGY_ID
    assert verify_v2_1_spec_hash() == V21_SPEC_SHA256
    validate_v2_1_spec()
    assert protocol_sha256(load_v2_1_protocol()) == V21_PROTOCOL_SHA256
    assert verify_v2_1_protocol_hash() == V21_PROTOCOL_SHA256
    validate_v2_1_protocol()
    policy = frozen_v2_1_forward_gate_policy()
    assert policy.minimum_completed_forward_weeks == 52
    assert policy.minimum_successful_rebalance_cycles == 40
    assert policy.weekly_sharpe_gt == 1.0


def test_v2_1_keeps_the_old_v2_gate_values_and_anchor_immutable():
    assert load_v2_1_protocol()["hard_gates"] == load_v2_protocol()["hard_gates"]
    validate_v2_forward_anchor()
    assert verify_v2_forward_anchor_hash()
    assert V2_1_FORWARD_ANCHOR_PATH.exists()
    validate_v2_1_forward_anchor()


def test_exact_zero_reference_volatility_is_valid_scale_one():
    result = evaluate_risk_scale(_zero_records(), signal_time=SIGNAL_TIME)
    assert isinstance(result, V21RiskScaleResult)
    assert result.status == "VALID"
    assert result.reference_vol == 0.0
    assert result.position_scale == 1.0
    assert result.selected_count == 13
    assert calculate_position_scale(_zero_records(), signal_time=SIGNAL_TIME) == 1.0


def test_tiny_positive_reference_volatility_uses_the_positive_formula():
    values = [0.0] * 12 + [1e-12]
    reference = reference_annualized_volatility(_records(values), signal_time=SIGNAL_TIME)
    assert reference > 0.0
    assert position_scale_from_reference_vol(reference) == pytest.approx(
        min(1.0, 0.15 / reference)
    )


@pytest.mark.parametrize(
    ("reference_vol", "expected_scale"),
    [(0.30, 0.50), (0.60, 0.25)],
)
def test_registered_positive_reference_volatility_values(
    reference_vol: float, expected_scale: float
):
    assert position_scale_from_reference_vol(reference_vol) == pytest.approx(expected_scale)


@pytest.mark.parametrize(
    "bad_records",
    [
        _records([0.0] * 12),
        _records([0.0] * 13, complete=False),
        _records([0.0] * 13, completed_at="missing"),
        _records([0.0] * 13, completed_at="naive"),
        _records([0.0] * 12 + [math.nan]),
        _records([0.0] * 12 + [math.inf]),
    ],
)
def test_invalid_or_incomplete_inputs_return_data_integrity_halt(
    bad_records: list[ControlWeeklyReturn],
):
    result = evaluate_risk_scale(bad_records, signal_time=SIGNAL_TIME)
    assert result.status == V2_DATA_INTEGRITY_HALT
    with pytest.raises(V21DataIntegrityHalt):
        calculate_position_scale(bad_records, signal_time=SIGNAL_TIME)


def test_negative_or_nonfinite_reference_volatility_is_a_data_integrity_halt():
    for value in (-1.0, math.nan, math.inf):
        with pytest.raises(V21DataIntegrityHalt):
            position_scale_from_reference_vol(value)


def test_future_observation_is_excluded_from_the_required_window():
    baseline = evaluate_risk_scale(_zero_records(), signal_time=SIGNAL_TIME)
    future = ControlWeeklyReturn(
        week_ending=SIGNAL_DAY + timedelta(days=7),
        net_return=0.90,
        completed_at=SIGNAL_TIME + timedelta(days=1),
    )
    with_future = evaluate_risk_scale(
        _zero_records() + [future],
        signal_time=SIGNAL_TIME,
    )
    assert with_future.status == "VALID"
    assert with_future.selected_count == 13
    assert with_future.reference_vol == baseline.reference_vol == 0.0
    assert with_future.position_scale == baseline.position_scale == 1.0
    assert completed_control_weekly_returns(
        _zero_records() + [future], signal_time=SIGNAL_TIME
    ) == tuple(_zero_records())


@pytest.mark.parametrize(
    ("flag", "value"),
    [("execution_available", False), ("transition_ok", False)],
)
def test_parent_success_cannot_be_overridden_by_v21_execution_or_transition(
    flag: str, value: bool
):
    engine = V21PairedEngine(base_notional=100.0)
    with pytest.raises(V21PairedInputError, match="V2_1_PAIRED_INPUT_INVALID"):
        engine.process_parent_attempt(
            signal_day=SIGNAL_DAY,
            execution_day=EXECUTION_DAY,
            control_targets=TARGETS,
            control_weekly_returns=_zero_records(),
            parent_status=SUCCESS,
            signal_time=SIGNAL_TIME,
            **{flag: value},
        )
    assert engine.attempts == []
    assert engine.positions == {}


def test_parent_success_with_empty_targets_is_a_paired_input_error():
    engine = V21PairedEngine(base_notional=100.0)
    with pytest.raises(V21PairedInputError, match="V2_1_PAIRED_INPUT_INVALID"):
        engine.process_parent_attempt(
            signal_day=SIGNAL_DAY,
            execution_day=EXECUTION_DAY,
            control_targets={},
            control_weekly_returns=_zero_records(),
            parent_status=SUCCESS,
            signal_time=SIGNAL_TIME,
        )
    assert engine.attempts == []


def test_v2_data_integrity_halt_is_not_a_v1_parent_status():
    assert V2_DATA_INTEGRITY_HALT not in PARENT_FAILURE_STATUSES
    engine = V21PairedEngine(base_notional=100.0)
    with pytest.raises(V21PairedInputError, match="V2_1_PAIRED_INPUT_INVALID"):
        engine.process_parent_attempt(
            signal_day=SIGNAL_DAY,
            execution_day=EXECUTION_DAY,
            control_targets=TARGETS,
            control_weekly_returns=_zero_records(),
            parent_status=V2_DATA_INTEGRITY_HALT,
            signal_time=SIGNAL_TIME,
        )
    assert engine.attempts == []


@pytest.mark.parametrize(
    ("complete", "net_return"),
    [
        (False, 0.90),
        (True, math.nan),
        (True, math.inf),
        (True, 0.90),
        (True, -0.90),
    ],
)
def test_future_row_content_cannot_change_current_risk_state(
    complete: bool, net_return: float
):
    recent = _records([0.01, -0.01] * 6 + [0.0])
    baseline = evaluate_risk_scale(recent, signal_time=SIGNAL_TIME)
    future = ControlWeeklyReturn(
        week_ending=SIGNAL_DAY + timedelta(days=7),
        net_return=net_return,
        completed_at=SIGNAL_TIME + timedelta(days=1),
        complete=complete,
    )
    result = evaluate_risk_scale(recent + [future], signal_time=SIGNAL_TIME)
    assert result.status == baseline.status == "VALID"
    assert result.reference_vol == pytest.approx(baseline.reference_vol)
    assert result.position_scale == pytest.approx(baseline.position_scale)


@pytest.mark.parametrize(
    ("complete", "net_return"),
    [(False, 0.50), (True, math.nan), (True, math.inf)],
)
def test_old_non_required_row_content_cannot_change_current_risk_state(
    complete: bool, net_return: float
):
    recent = _records([0.01, -0.01] * 6 + [0.0])
    baseline = evaluate_risk_scale(recent, signal_time=SIGNAL_TIME)
    old = ControlWeeklyReturn(
        week_ending=date(2019, 1, 1),
        net_return=net_return,
        completed_at=datetime(2019, 1, 2, 12, tzinfo=UTC),
        complete=complete,
    )
    result = evaluate_risk_scale([old, *recent], signal_time=SIGNAL_TIME)
    assert result.status == baseline.status == "VALID"
    assert result.reference_vol == pytest.approx(baseline.reference_vol)
    assert result.position_scale == pytest.approx(baseline.position_scale)


@pytest.mark.parametrize(
    ("complete", "net_return"),
    [(False, 0.01), (True, math.nan), (True, math.inf)],
)
def test_required_window_invalid_content_halts_v21(
    complete: bool, net_return: float
):
    records = _records([0.01, -0.01] * 6 + [0.0])
    last = records[-1]
    records[-1] = ControlWeeklyReturn(
        week_ending=last.week_ending,
        net_return=net_return,
        completed_at=last.completed_at,
        complete=complete,
    )
    result = evaluate_risk_scale(records, signal_time=SIGNAL_TIME)
    assert result.status == V2_DATA_INTEGRITY_HALT


def test_parent_success_zero_vol_is_paired_on_the_historical_semantic_fixture():
    engine = V21PairedEngine(base_notional=100.0)
    assert not hasattr(engine, "clock")
    result = engine.process_parent_attempt(
        signal_day=SIGNAL_DAY,
        execution_day=EXECUTION_DAY,
        control_targets=TARGETS,
        control_weekly_returns=_zero_records(),
        parent_status=SUCCESS,
        signal_time=SIGNAL_TIME,
    )
    with pytest.raises(V2RiskScaleInvalid):
        old_calculate_position_scale(_zero_records(), signal_time=SIGNAL_TIME)
    assert result.status == SUCCESS
    assert result.parent_status == SUCCESS
    assert result.paired is True
    assert result.position_scale == 1.0
    assert result.signal_day == SIGNAL_DAY
    assert result.execution_day == EXECUTION_DAY
    assert result.target_directions == (("BTCUSDT", 1), ("ETHUSDT", -1))
    assert engine.positions["BTCUSDT"].notional == 100.0
    assert engine.positions["ETHUSDT"].notional == 100.0


def test_parent_failure_is_inherited_and_next_success_is_parent_driven():
    engine = V21PairedEngine(base_notional=100.0)
    failed = engine.process_parent_attempt(
        signal_day=SIGNAL_DAY,
        execution_day=EXECUTION_DAY,
        control_targets=None,
        control_weekly_returns=None,
        parent_status=NO_SIGNAL,
        signal_time=SIGNAL_TIME,
    )
    retried = engine.process_parent_attempt(
        signal_day=EXECUTION_DAY,
        execution_day=EXECUTION_DAY + timedelta(days=1),
        control_targets=TARGETS,
        control_weekly_returns=_records([0.01, -0.01] * 6 + [0.0]),
        parent_status=SUCCESS,
        signal_time=datetime.combine(EXECUTION_DAY + timedelta(days=1), time.min, tzinfo=UTC),
    )
    assert failed.status == NO_SIGNAL
    assert failed.paired is True
    assert retried.status == SUCCESS
    assert retried.paired is True
    assert not hasattr(engine, "clock")
    assert [attempt.status for attempt in engine.attempts] == [NO_SIGNAL, SUCCESS]


def test_parent_success_with_data_integrity_failure_halts_the_entire_pair():
    engine = V21PairedEngine(base_notional=100.0)
    halted = engine.process_parent_attempt(
        signal_day=SIGNAL_DAY,
        execution_day=EXECUTION_DAY,
        control_targets=TARGETS,
        control_weekly_returns=_records([0.0] * 12),
        parent_status=SUCCESS,
        signal_time=SIGNAL_TIME,
    )
    assert halted.status == V2_DATA_INTEGRITY_HALT
    assert halted.halted is True
    assert halted.paired is False
    assert engine.halted is True
    assert engine.positions == {}

    later = engine.process_parent_attempt(
        signal_day=EXECUTION_DAY,
        execution_day=EXECUTION_DAY + timedelta(days=1),
        control_targets=TARGETS,
        control_weekly_returns=_zero_records(),
        parent_status=SUCCESS,
        signal_time=datetime.combine(EXECUTION_DAY + timedelta(days=1), time.min, tzinfo=UTC),
    )
    assert later.status == V2_DATA_INTEGRITY_HALT
    assert later.halted is True
    assert engine.positions == {}


def test_temporal_pairing_has_no_seven_day_v2_1_clock():
    assert validate_v2_1_temporal_inputs(
        signal_day=SIGNAL_DAY,
        execution_day=EXECUTION_DAY,
        signal_time=SIGNAL_TIME,
    ) == SIGNAL_TIME
