"""V2-M0 protocol, epoch, paired-schedule, and retry tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta

import pytest

from backtest.v2_forward import (
    INSUFFICIENT_UNIVERSE,
    MISSING_EXECUTION_PRICE,
    NO_SIGNAL,
    RISK_SCALE_INVALID,
    SUCCESS,
    TRANSITION_FAILURE,
    ForwardEpoch,
    ForwardEpochError,
    ForwardEvidenceLedger,
    RebalanceAttempt,
    V2ForwardEngine,
    assert_paired_schedule,
    assert_same_targets_and_directions,
)
from backtest.v2_protocol import (
    F_GATE_NAMES,
    V2_PROTOCOL_ID,
    V2_PROTOCOL_SHA256,
    V2ProtocolError,
    frozen_v2_forward_gate_policy,
    load_v2_protocol,
    protocol_sha256,
    validate_v2_protocol,
    verify_v2_protocol_hash,
)
from src.xs_lowvol_v2_spec import V2_SPEC_SHA256


def test_protocol_and_sidecar_are_frozen_without_data_access():
    protocol = load_v2_protocol()
    assert protocol["protocol_id"] == V2_PROTOCOL_ID
    assert protocol["v2_spec_sha256"] == V2_SPEC_SHA256
    assert protocol_sha256(protocol) == V2_PROTOCOL_SHA256
    assert verify_v2_protocol_hash() == V2_PROTOCOL_SHA256
    validate_v2_protocol(protocol)
    policy = frozen_v2_forward_gate_policy(protocol)
    assert policy.minimum_completed_forward_weeks == 52
    assert policy.minimum_successful_rebalance_cycles == 40
    assert policy.net_return_pct_gt == 8.0
    assert policy.cost_2x_net_return_pct_gt == 0.0
    assert policy.weekly_sharpe_gt == 1.0
    assert policy.max_drawdown_pct_lt == 20.0
    assert policy.weekly_profit_factor_gt == 1.2
    assert policy.bootstrap_probability_gte == 0.95
    assert policy.paired_drawdown_ratio_lte == 0.8


def test_protocol_gate_tampering_is_rejected():
    altered = deepcopy(load_v2_protocol())
    altered["hard_gates"]["F4_sharpe"]["weekly_sharpe_gt"] = 0.9
    with pytest.raises(V2ProtocolError, match="F4_sharpe"):
        validate_v2_protocol(
            altered,
            require_approved_hash=False,
            require_current_v1=False,
        )
    assert set(F_GATE_NAMES) == {
        "F0_data_integrity",
        "F1_forward_sample",
        "F2_net_return",
        "F3_cost_robustness",
        "F4_sharpe",
        "F5_max_drawdown",
        "F6_profit_factor",
        "F7_block_bootstrap",
        "F8_best_week_robustness",
        "F9_paired_risk_improvement",
        "F10_paired_sharpe",
    }


def test_forward_epoch_excludes_pre_freeze_observations():
    epoch = ForwardEpoch("2066f0856575146e917e9dc7c30e17a225f8927d", date(2026, 9, 17))
    ledger = ForwardEvidenceLedger(epoch)
    assert ledger.record(date(2026, 9, 16), {"net_return": 99.0}) is False
    assert ledger.record(date(2026, 9, 17), {"net_return": 0.01}) is True
    assert len(ledger.pre_epoch_observations) == 1
    assert len(ledger.forward_observations) == 1
    assert ledger.forward_observations[0].payload["net_return"] == 0.01
    with pytest.raises(ForwardEpochError):
        ForwardEpoch("not-a-commit", date(2026, 9, 17))


def _returns() -> list[float]:
    return [-0.01, 0.01] * 6 + [0.0]


def _engine() -> V2ForwardEngine:
    return V2ForwardEngine(base_notional=100.0)


def test_only_complete_success_advances_the_stateful_clock():
    engine = _engine()
    targets = {"BTCUSDT": 1, "ETHUSDT": -1}
    first = engine.attempt(
        signal_day=date(2026, 9, 1),
        execution_day=date(2026, 9, 2),
        control_targets=targets,
        control_weekly_returns=_returns(),
    )
    assert first.status == SUCCESS
    assert engine.clock.last_successful_execution_day == date(2026, 9, 2)

    no_signal = engine.attempt(
        signal_day=date(2026, 9, 3),
        execution_day=date(2026, 9, 4),
        control_targets=None,
        control_weekly_returns=None,
        signal_status=NO_SIGNAL,
    )
    assert no_signal.status == NO_SIGNAL
    assert engine.clock.last_successful_execution_day == date(2026, 9, 2)

    insufficient = engine.attempt(
        signal_day=date(2026, 9, 5),
        execution_day=date(2026, 9, 6),
        control_targets=targets,
        control_weekly_returns=_returns(),
        signal_status=INSUFFICIENT_UNIVERSE,
    )
    assert insufficient.status == INSUFFICIENT_UNIVERSE
    assert engine.clock.last_successful_execution_day == date(2026, 9, 2)

    missing_price = engine.attempt(
        signal_day=date(2026, 9, 8),
        execution_day=date(2026, 9, 9),
        control_targets=targets,
        control_weekly_returns=_returns(),
        execution_available=False,
    )
    assert missing_price.status == MISSING_EXECUTION_PRICE
    assert engine.clock.last_successful_execution_day == date(2026, 9, 2)

    invalid_risk = engine.attempt(
        signal_day=date(2026, 9, 10),
        execution_day=date(2026, 9, 11),
        control_targets=targets,
        control_weekly_returns=[0.01] * 13,
    )
    assert invalid_risk.status == RISK_SCALE_INVALID
    assert engine.clock.last_successful_execution_day == date(2026, 9, 2)

    transition = engine.attempt(
        signal_day=date(2026, 9, 12),
        execution_day=date(2026, 9, 13),
        control_targets=targets,
        control_weekly_returns=_returns(),
        transition_ok=False,
    )
    assert transition.status == TRANSITION_FAILURE
    assert engine.clock.last_successful_execution_day == date(2026, 9, 2)


def test_control_and_v2_schedule_and_targets_must_match():
    targets = {"BTCUSDT": 1, "ETHUSDT": -1}
    control = RebalanceAttempt(date(2026, 9, 17), date(2026, 9, 18), SUCCESS, (("BTCUSDT", 1), ("ETHUSDT", -1)))
    v2 = RebalanceAttempt(date(2026, 9, 17), date(2026, 9, 18), SUCCESS, (("BTCUSDT", 1), ("ETHUSDT", -1)))
    assert_paired_schedule([control], [v2])
    assert_same_targets_and_directions(targets, targets.copy())
    with pytest.raises(ValueError, match="directions"):
        assert_same_targets_and_directions(targets, {"BTCUSDT": -1, "ETHUSDT": -1})
    with pytest.raises(ValueError, match="diverge"):
        assert_paired_schedule([control], [RebalanceAttempt(date(2026, 9, 17), date(2026, 9, 19), SUCCESS, control.target_directions)])
