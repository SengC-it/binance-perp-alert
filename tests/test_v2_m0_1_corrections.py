"""Corrective V2-M0.1 tests for Forward engine guards and PIT inputs."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from backtest.v2_forward import (
    ForwardInputError,
    MISSING_EXECUTION_PRICE,
    NO_SIGNAL,
    NOT_DUE,
    RISK_SCALE_INVALID,
    SUCCESS,
    RebalanceNotDueError,
    V2ForwardEngine,
)
from src.xs_lowvol_v2_risk import (
    ControlWeeklyReturn,
    calculate_position_scale,
)


UTC = timezone.utc
TARGETS = {"BTCUSDT": 1, "ETHUSDT": -1}
SIGNAL_DAY = date(2026, 9, 1)
SIGNAL_TIME = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _signal_time(day: date) -> datetime:
    return datetime.combine(day, time(12), tzinfo=UTC)


def _returns(
    values: list[float] | None = None,
    *,
    start: date = date(2026, 6, 1),
) -> list[ControlWeeklyReturn]:
    returns = values if values is not None else [-0.01, 0.01] * 6 + [0.0]
    return [
        ControlWeeklyReturn(
            week_ending=start + timedelta(days=7 * index),
            net_return=value,
            completed_at=datetime.combine(
                start + timedelta(days=7 * index),
                time(12),
                tzinfo=UTC,
            ),
            complete=True,
        )
        for index, value in enumerate(returns)
    ]


def _engine() -> V2ForwardEngine:
    return V2ForwardEngine(base_notional=100.0)


def _first_success(engine: V2ForwardEngine) -> None:
    result = engine.attempt(
        signal_day=SIGNAL_DAY,
        execution_day=date(2026, 9, 2),
        control_targets=TARGETS,
        control_weekly_returns=_returns(),
        signal_time=SIGNAL_TIME,
    )
    assert result.status == SUCCESS


def test_early_attempts_fail_closed_without_ledger_or_position_changes():
    engine = _engine()
    _first_success(engine)
    positions_before = engine.positions.copy()

    for offset in range(1, 7):
        execution_day = date(2026, 9, 2) + timedelta(days=offset)
        with pytest.raises(RebalanceNotDueError, match=NOT_DUE):
            engine.attempt(
                signal_day=execution_day,
                execution_day=execution_day,
                control_targets=TARGETS,
                # Deliberately malformed inputs prove the clock guard is first.
                control_weekly_returns=[0.90] * 13,
                signal_time=None,
            )

    assert engine.positions == positions_before
    assert engine.clock.last_successful_execution_day == date(2026, 9, 2)
    assert len(engine.attempts) == 1
    assert len(engine.successful_attempts) == 1


def test_due_day_failures_retry_on_the_next_calendar_day_until_success():
    engine = _engine()
    _first_success(engine)

    no_signal = engine.attempt(
        signal_day=date(2026, 9, 8),
        execution_day=date(2026, 9, 9),
        control_targets=None,
        control_weekly_returns=None,
        signal_status=NO_SIGNAL,
        signal_time=_signal_time(date(2026, 9, 8)),
    )
    assert no_signal.status == NO_SIGNAL
    assert engine.clock.last_successful_execution_day == date(2026, 9, 2)

    missing_price = engine.attempt(
        signal_day=date(2026, 9, 9),
        execution_day=date(2026, 9, 10),
        control_targets=TARGETS,
        control_weekly_returns=None,
        execution_available=False,
        signal_time=_signal_time(date(2026, 9, 9)),
    )
    assert missing_price.status == MISSING_EXECUTION_PRICE
    assert engine.clock.last_successful_execution_day == date(2026, 9, 2)

    retry = engine.attempt(
        signal_day=date(2026, 9, 10),
        execution_day=date(2026, 9, 11),
        control_targets=TARGETS,
        control_weekly_returns=_returns(),
        signal_time=_signal_time(date(2026, 9, 10)),
    )
    assert retry.status == SUCCESS
    assert engine.clock.last_successful_execution_day == date(2026, 9, 11)
    assert [attempt.status for attempt in engine.attempts] == [
        SUCCESS,
        NO_SIGNAL,
        MISSING_EXECUTION_PRICE,
        SUCCESS,
    ]


def test_formal_forward_engine_requires_timezone_aware_signal_time():
    for signal_time in (None, date(2026, 9, 1), datetime(2026, 9, 1, 12)):
        engine = _engine()
        with pytest.raises(ForwardInputError, match="timezone-aware"):
            engine.attempt(
                signal_day=SIGNAL_DAY,
                execution_day=date(2026, 9, 2),
                control_targets=TARGETS,
                control_weekly_returns=_returns(),
                signal_time=signal_time,
            )
        assert engine.clock.last_successful_execution_day is None
        assert engine.positions == {}
        assert engine.attempts == []


def test_formal_forward_engine_rejects_numeric_only_weekly_returns():
    engine = _engine()
    result = engine.attempt(
        signal_day=SIGNAL_DAY,
        execution_day=date(2026, 9, 2),
        control_targets=TARGETS,
        control_weekly_returns=[-0.01, 0.01] * 6 + [0.0],
        signal_time=SIGNAL_TIME,
    )
    assert result.status == RISK_SCALE_INVALID
    assert "dated ControlWeeklyReturn" in result.reason
    assert engine.clock.last_successful_execution_day is None
    assert engine.positions == {}


def test_formal_forward_engine_rejects_missing_completed_at():
    engine = _engine()
    result = engine.attempt(
        signal_day=SIGNAL_DAY,
        execution_day=date(2026, 9, 2),
        control_targets=TARGETS,
        control_weekly_returns=[
            ControlWeeklyReturn(
                week_ending=date(2026, 6, 1) + timedelta(days=7 * index),
                net_return=value,
            )
            for index, value in enumerate([-0.01, 0.01] * 6 + [0.0])
        ],
        signal_time=SIGNAL_TIME,
    )
    assert result.status == RISK_SCALE_INVALID
    assert "completed_at must be explicit" in result.reason
    assert engine.clock.last_successful_execution_day is None


def test_completed_at_equal_to_signal_time_is_excluded_by_strict_cutoff():
    records = _returns()[:12]
    records.append(
        ControlWeeklyReturn(
            week_ending=SIGNAL_DAY,
            net_return=0.01,
            completed_at=SIGNAL_TIME,
        )
    )
    engine = _engine()
    result = engine.attempt(
        signal_day=SIGNAL_DAY,
        execution_day=date(2026, 9, 2),
        control_targets=TARGETS,
        control_weekly_returns=records,
        signal_time=SIGNAL_TIME,
    )
    assert result.status == RISK_SCALE_INVALID
    assert "fewer than 13" in result.reason


def test_future_plus_or_minus_90_percent_cannot_change_the_current_scale():
    future_time = SIGNAL_TIME + timedelta(days=1)
    baseline = calculate_position_scale(_returns(), signal_time=SIGNAL_TIME)
    plus_90 = calculate_position_scale(
        _returns()
        + [ControlWeeklyReturn(date(2026, 9, 8), 0.90, future_time)],
        signal_time=SIGNAL_TIME,
    )
    minus_90 = calculate_position_scale(
        _returns()
        + [ControlWeeklyReturn(date(2026, 9, 8), -0.90, future_time)],
        signal_time=SIGNAL_TIME,
    )
    assert plus_90 == pytest.approx(baseline)
    assert minus_90 == pytest.approx(baseline)


def test_incomplete_observation_outside_required_window_does_not_change_scale():
    recent = _returns()
    old_incomplete = ControlWeeklyReturn(
        week_ending=date(2025, 10, 1),
        net_return=0.50,
        completed_at=datetime(2025, 10, 1, 12, tzinfo=UTC),
        complete=False,
    )
    baseline = calculate_position_scale(recent, signal_time=SIGNAL_TIME)
    with_old = calculate_position_scale(
        [old_incomplete, *recent],
        signal_time=SIGNAL_TIME,
    )
    assert with_old == pytest.approx(baseline)

    engine = _engine()
    result = engine.attempt(
        signal_day=SIGNAL_DAY,
        execution_day=date(2026, 9, 2),
        control_targets=TARGETS,
        control_weekly_returns=[old_incomplete, *recent],
        signal_time=SIGNAL_TIME,
    )
    assert result.status == SUCCESS
    assert result.position_scale == pytest.approx(baseline)


def test_incomplete_observation_inside_required_window_fails_closed():
    records = _returns()
    last = records[-1]
    records[-1] = ControlWeeklyReturn(
        week_ending=last.week_ending,
        net_return=last.net_return,
        completed_at=last.completed_at,
        complete=False,
    )
    engine = _engine()
    result = engine.attempt(
        signal_day=SIGNAL_DAY,
        execution_day=date(2026, 9, 2),
        control_targets=TARGETS,
        control_weekly_returns=records,
        signal_time=SIGNAL_TIME,
    )
    assert result.status == RISK_SCALE_INVALID
    assert "required Control weekly return is incomplete" in result.reason
    assert engine.clock.last_successful_execution_day is None
    assert engine.positions == {}


def test_formal_mapping_requires_all_weekly_provenance_fields():
    records = [
        {
            "week_ending": record.week_ending,
            "net_return": record.net_return,
            "completed_at": record.completed_at,
        }
        for record in _returns()
    ]
    engine = _engine()
    result = engine.attempt(
        signal_day=SIGNAL_DAY,
        execution_day=date(2026, 9, 2),
        control_targets=TARGETS,
        control_weekly_returns=records,
        signal_time=SIGNAL_TIME,
    )
    assert result.status == RISK_SCALE_INVALID
    assert "missing provenance: complete" in result.reason
