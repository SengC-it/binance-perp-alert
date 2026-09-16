"""V2-M0.2 tests for the frozen Forward anchor and temporal boundaries."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytest

from backtest.v2_forward import (
    FORWARD_EPOCH_INVALID,
    FORWARD_TEMPORAL_INVALID,
    SUCCESS,
    ForwardEpoch,
    ForwardEpochError,
    ForwardInputError,
    ForwardTemporalError,
    V2ForwardEngine,
    validate_forward_temporal_inputs,
)
from src.xs_lowvol_v2_anchor import (
    APPROVED_V1_CONTROL_SHA256,
    APPROVED_V2_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_M0_COMMIT,
    APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC,
    APPROVED_V2_PROTOCOL_SHA256,
    APPROVED_V2_SPEC_SHA256,
    V2ForwardAnchorError,
    forward_anchor_sha256,
    load_v2_forward_anchor,
    read_v2_forward_anchor_hash,
    validate_v2_forward_anchor,
)
from src.xs_lowvol_v2_risk import ControlWeeklyReturn, validate_forward_weekly_returns


UTC = timezone.utc
UTC_PLUS_8 = timezone(timedelta(hours=8))
TARGETS = {"BTCUSDT": 1, "ETHUSDT": -1}
SIGNAL_DAY = date(2026, 9, 1)
EXECUTION_DAY = date(2026, 9, 2)
LOGICAL_SIGNAL_TIME = datetime(2026, 9, 2, tzinfo=UTC)


def _returns() -> list[ControlWeeklyReturn]:
    return [
        ControlWeeklyReturn(
            week_ending=date(2026, 5, 4) + timedelta(days=7 * index),
            net_return=value,
            completed_at=datetime.combine(
                date(2026, 5, 4) + timedelta(days=7 * index),
                time(12),
                tzinfo=UTC,
            ),
            complete=True,
        )
        for index, value in enumerate([-0.01, 0.01] * 6 + [0.0])
    ]


def _engine() -> V2ForwardEngine:
    return V2ForwardEngine(base_notional=100.0)


def _attempt(
    *,
    signal_time: Any = LOGICAL_SIGNAL_TIME,
    execution_day: date = EXECUTION_DAY,
    control_weekly_returns: Any = None,
) -> Any:
    engine = _engine()
    return engine.attempt(
        signal_day=SIGNAL_DAY,
        execution_day=execution_day,
        control_targets=TARGETS,
        control_weekly_returns=_returns()
        if control_weekly_returns is None
        else control_weekly_returns,
        signal_time=signal_time,
    )


def test_forward_anchor_is_canonically_pinned_and_not_started():
    anchor = load_v2_forward_anchor()
    validate_v2_forward_anchor()
    assert anchor["status"] == "FROZEN_NOT_YET_STARTED"
    assert forward_anchor_sha256(anchor) == APPROVED_V2_FORWARD_ANCHOR_SHA256
    assert read_v2_forward_anchor_hash() == APPROVED_V2_FORWARD_ANCHOR_SHA256
    assert anchor["approved_v2_m0_commit"] == APPROVED_V2_M0_COMMIT
    assert anchor["v1_control_sha256"] == APPROVED_V1_CONTROL_SHA256
    assert anchor["v2_spec_sha256"] == APPROVED_V2_SPEC_SHA256
    assert anchor["v2_protocol_sha256"] == APPROVED_V2_PROTOCOL_SHA256


def test_forward_anchor_tampering_is_rejected():
    altered = load_v2_forward_anchor()
    altered["status"] = "STARTED"
    with pytest.raises(V2ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        validate_v2_forward_anchor(
            altered,
            require_approved_hash=False,
            require_current_identities=False,
        )


def test_forward_epoch_requires_the_approved_m0_commit():
    with pytest.raises(ForwardEpochError, match=FORWARD_EPOCH_INVALID):
        ForwardEpoch("0" * 40, date(2026, 9, 17))


def test_forward_epoch_binds_all_frozen_identities():
    for field_name in (
        "v1_control_sha256",
        "v2_spec_sha256",
        "v2_protocol_sha256",
        "forward_anchor_sha256",
    ):
        with pytest.raises(ForwardEpochError, match=FORWARD_EPOCH_INVALID):
            ForwardEpoch(
                APPROVED_V2_M0_COMMIT,
                date(2026, 9, 17),
                **{field_name: "0" * 64},
            )


def test_execution_same_day_is_rejected():
    with pytest.raises(ForwardTemporalError, match=FORWARD_TEMPORAL_INVALID):
        _attempt(
            execution_day=SIGNAL_DAY,
            signal_time=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_execution_two_days_after_signal_is_rejected():
    with pytest.raises(ForwardTemporalError, match=FORWARD_TEMPORAL_INVALID):
        _attempt(
            execution_day=date(2026, 9, 3),
            signal_time=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_execution_exactly_one_day_after_signal_and_midnight_signal_time_passes():
    result = _attempt(signal_time=LOGICAL_SIGNAL_TIME)
    assert result.status == SUCCESS


def test_signal_day_noon_is_not_the_logical_signal_time():
    with pytest.raises(ForwardTemporalError, match=FORWARD_TEMPORAL_INVALID):
        _attempt(signal_time=datetime(2026, 9, 1, 12, tzinfo=UTC))


def test_execution_day_noon_is_not_the_logical_signal_time():
    with pytest.raises(ForwardTemporalError, match=FORWARD_TEMPORAL_INVALID):
        _attempt(signal_time=datetime(2026, 9, 2, 12, tzinfo=UTC))


def test_naive_signal_time_fails_closed():
    with pytest.raises(ForwardInputError, match="timezone-aware"):
        _attempt(signal_time=datetime(2026, 9, 2))


def test_formal_completed_at_naive_datetime_and_naive_string_fail_closed():
    naive_records = _returns()
    naive_records[-1] = ControlWeeklyReturn(
        week_ending=naive_records[-1].week_ending,
        net_return=naive_records[-1].net_return,
        completed_at=datetime(2026, 7, 27, 12),
    )
    result = _attempt(control_weekly_returns=naive_records)
    assert result.status != SUCCESS
    assert "timezone-aware datetime" in result.reason

    mapping_records = [
        {
            "week_ending": record.week_ending,
            "net_return": record.net_return,
            "completed_at": "2026-07-27T12:00:00",
            "complete": True,
        }
        if index == len(_returns()) - 1
        else {
            "week_ending": record.week_ending,
            "net_return": record.net_return,
            "completed_at": record.completed_at,
            "complete": True,
        }
        for index, record in enumerate(_returns())
    ]
    mapping_result = _attempt(control_weekly_returns=mapping_records)
    assert mapping_result.status != SUCCESS
    assert "timezone-aware datetime" in mapping_result.reason


def test_formal_completed_at_date_only_value_fails_closed():
    records = _returns()
    records[-1] = ControlWeeklyReturn(
        week_ending=records[-1].week_ending,
        net_return=records[-1].net_return,
        completed_at=date(2026, 7, 27),
    )
    result = _attempt(control_weekly_returns=records)
    assert result.status != SUCCESS
    assert "date-only" in result.reason


def test_aware_plus_eight_completion_time_normalizes_before_cutoff():
    records = _returns()
    records.append(
        ControlWeeklyReturn(
            week_ending=date(2026, 9, 1),
            net_return=0.02,
            completed_at=datetime(2026, 9, 2, 7, 30, tzinfo=UTC_PLUS_8),
        )
    )
    normalized = validate_forward_weekly_returns(records)
    assert normalized[-1].completed_at == datetime(2026, 9, 1, 23, 30, tzinfo=UTC)
    assert _attempt(control_weekly_returns=records).status == SUCCESS


def test_completion_exactly_at_cutoff_is_excluded():
    records = _returns()[:12]
    records.append(
        ControlWeeklyReturn(
            week_ending=date(2026, 9, 1),
            net_return=0.02,
            completed_at=datetime(2026, 9, 2, 8, tzinfo=UTC_PLUS_8),
        )
    )
    result = _attempt(control_weekly_returns=records)
    assert result.status != SUCCESS
    assert "fewer than 13" in result.reason


def test_completion_strictly_before_cutoff_is_eligible():
    records = _returns()[:12]
    records.append(
        ControlWeeklyReturn(
            week_ending=date(2026, 9, 1),
            net_return=0.02,
            completed_at=datetime(2026, 9, 2, 7, 59, tzinfo=UTC_PLUS_8),
        )
    )
    assert _attempt(control_weekly_returns=records).status == SUCCESS


def test_forward_observation_logical_time_must_be_strictly_after_m0_freeze():
    epoch = ForwardEpoch(APPROVED_V2_M0_COMMIT, date(2026, 9, 17))
    with pytest.raises(ForwardEpochError, match=FORWARD_EPOCH_INVALID):
        epoch.validate_logical_signal_time(APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC)
    with pytest.raises(ForwardEpochError, match=FORWARD_EPOCH_INVALID):
        epoch.validate_logical_signal_time(
            APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC - timedelta(seconds=1)
        )
    assert epoch.validate_logical_signal_time(
        APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC + timedelta(seconds=1)
    ) == APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC + timedelta(seconds=1)


def test_temporal_helper_returns_the_frozen_utc_logical_instant():
    assert validate_forward_temporal_inputs(
        signal_day=SIGNAL_DAY,
        execution_day=EXECUTION_DAY,
        signal_time=LOGICAL_SIGNAL_TIME,
    ) == LOGICAL_SIGNAL_TIME
