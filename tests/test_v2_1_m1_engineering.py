"""XS-LOWVOL V2.1-M1 contaminated engineering-replay tests."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtest.m1_b import ScheduleAttempt
from backtest.v2_1_forward import (
    MISSING_EXECUTION_PRICE,
    NO_SIGNAL,
    SUCCESS,
    V21PairedAttempt,
    validate_v2_1_temporal_inputs,
)
from backtest.v2_1_m1_engineering import (
    EXPECTED_DATASET_SHA256,
    EXPECTED_NORMALIZED_DATASET_SHA256,
    M1_DATASET_FREEZE_PATH,
    V21_M1_BASE_COMMIT,
    V21_M1_MARKDOWN_PATH,
    V21_M1_OUTPUT_DIR,
    V21_M1_REPORT_PATH,
    V21_M1_RUN_ID,
    V21_M1_TRACE_MANIFEST_PATH,
    EngineeringProducerError,
    EngineeringReplayError,
    _logical_signal_time,
    _retry_diagnostics,
    _run_no_lookahead_mutations,
    _synthetic_transition_fixture,
    _terminal_halt_diagnostics,
    _weekly_fixture,
    _weekly_inputs,
    assert_no_performance_fields,
    load_verified_dataset,
    paired_schedule_diagnostics,
    verify_frozen_identity,
)
from src.xs_lowvol_v2_1_anchor import (
    APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    V21_FORWARD_ANCHOR_STATUS,
    load_v2_1_forward_anchor,
    verify_v2_1_forward_anchor_hash,
)
from src.xs_lowvol_v2_1_risk import calculate_position_scale


UTC = timezone.utc


def test_m1_runner_is_pinned_to_the_approved_m0_2_base_and_fixed_outputs():
    assert V21_M1_BASE_COMMIT == "abf7338ff4af25e5fd0a12c1eade8a12a02667db"
    assert V21_M1_RUN_ID == "XS-LOWVOL-V2.1-M1-ENGINEERING-1"
    assert V21_M1_OUTPUT_DIR == Path("research/v2_1/m1").resolve()
    assert V21_M1_REPORT_PATH.parent == V21_M1_OUTPUT_DIR
    assert V21_M1_MARKDOWN_PATH.parent == V21_M1_OUTPUT_DIR
    assert V21_M1_TRACE_MANIFEST_PATH.parent == V21_M1_OUTPUT_DIR


def test_frozen_v1_v21_anchor_identity_is_verified():
    identity = verify_frozen_identity()
    assert identity["v1_control_sha256"] == (
        "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678"
    )
    assert identity["v2_1_spec_sha256"] == "e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c"
    assert identity["v2_1_protocol_sha256"] == "6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d"
    assert identity["forward_anchor_sha256"] == APPROVED_V2_1_FORWARD_ANCHOR_SHA256
    assert load_v2_1_forward_anchor()["status"] == V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
    assert verify_v2_1_forward_anchor_hash() == APPROVED_V2_1_FORWARD_ANCHOR_SHA256


def test_verified_dataset_loader_reuses_only_the_approved_cache():
    if not Path(M1_DATASET_FREEZE_PATH).is_file():
        pytest.skip("the ignored frozen market-data cache is unavailable in this checkout")
    dataset = load_verified_dataset()
    assert dataset["dataset_sha256"] == EXPECTED_DATASET_SHA256
    assert dataset["normalized_dataset_sha256"] == EXPECTED_NORMALIZED_DATASET_SHA256
    assert dataset["catalog"].listing_complete is True


def test_weekly_input_producer_rejects_duplicate_week_ending():
    accounting = SimpleNamespace(
        weekly_rows=(
            {"week_end": "2020-01-05", "return": 0.0},
            {"week_end": "2020-01-05", "return": 0.0},
        )
    )
    with pytest.raises(EngineeringProducerError, match="ENGINEERING PRODUCER ERROR"):
        _weekly_inputs(accounting)


def test_zero_volatility_regression_repairs_only_the_v21_semantics():
    from backtest.v2_1_m1_engineering import _zero_vol_regression

    result = _zero_vol_regression()
    assert result["status"] == "PASS"
    assert result["old_v2_zero_vol_result"] == "V2_RISK_SCALE_INVALID"
    assert result["v2_1_result"] == SUCCESS
    assert result["v2_1_position_scale"] == 1.0
    assert result["same_signal_day"] is True
    assert result["same_execution_day"] is True
    assert result["same_targets"] is True


def test_terminal_data_halt_cannot_be_recovered_by_a_later_valid_input():
    result = _terminal_halt_diagnostics()
    assert result["status"] == "PASS"
    assert result["all_required_inputs_halted"] is True
    assert result["terminal_halt_cannot_recover"] is True
    assert result["v2_specific_retry_created"] is False
    assert len(result["cases"]) == 6


def test_no_lookahead_mutations_leave_the_frozen_scale_unchanged():
    records = _weekly_fixture()
    signal_time = datetime(2020, 8, 14, tzinfo=UTC)
    baseline = calculate_position_scale(records, signal_time=signal_time)
    result = _run_no_lookahead_mutations(records, signal_time, baseline)
    assert result == {
        "mutation_count": 5,
        "old_non_required_mutation_count": 1,
        "violation_count": 0,
    }


def test_transition_fixture_covers_open_close_resize_flip_and_replacement():
    result = _synthetic_transition_fixture()
    assert result["valid"] is True
    assert result["change_count"] == 6
    assert set(result["cases"]) == {
        "open",
        "close",
        "same_side_increase_resize",
        "same_side_decrease_resize",
        "direction_flip",
        "symbol_replacement",
    }
    assert result["violations"] == []


def test_paired_schedule_detector_requires_exact_parent_dates_targets_and_statuses():
    parent = ScheduleAttempt(
        date(2020, 8, 13),
        date(2020, 8, 14),
        SUCCESS,
        target_symbols=("AAAUSDT",),
    )
    actual = V21PairedAttempt(
        signal_day=parent.signal_day,
        execution_day=parent.execution_day,
        parent_status=SUCCESS,
        status=SUCCESS,
        target_directions=(("AAAUSDT", 1),),
    )
    expected_targets = (("AAAUSDT", 1),)
    good = paired_schedule_diagnostics((parent,), (actual,), (expected_targets,), (SUCCESS,))
    assert good["paired_schedule_divergence_count"] == 0
    assert good["parent_schedule_mismatch_count"] == 0
    assert good["target_mismatch_count"] == 0

    divergent = replace(actual, execution_day=date(2020, 8, 15), target_directions=(("BBBUSD", 1),))
    bad = paired_schedule_diagnostics((parent,), (divergent,), (expected_targets,), (SUCCESS,))
    assert bad["paired_schedule_divergence_count"] == 1
    assert bad["parent_schedule_mismatch_count"] == 1
    assert bad["target_mismatch_count"] == 1


def test_parent_retry_diagnostics_never_creates_a_v21_retry_clock():
    base = date(2020, 8, 14)
    attempts = (
        ScheduleAttempt(base - timedelta(days=1), base, NO_SIGNAL),
        ScheduleAttempt(base, base + timedelta(days=1), SUCCESS),
        ScheduleAttempt(base + timedelta(days=7), base + timedelta(days=8), MISSING_EXECUTION_PRICE),
        ScheduleAttempt(base + timedelta(days=8), base + timedelta(days=9), SUCCESS),
    )
    result = _retry_diagnostics(attempts, base + timedelta(days=30))
    assert result["v21_independent_retry_count"] == 0
    assert result["parent_retry_attempt_count"] == 2
    assert result["parent_retry_boundary_violation_count"] == 0


def test_temporal_pairing_is_signal_plus_one_day_and_execution_midnight_utc():
    signal_day = date(2020, 8, 13)
    execution_day = date(2020, 8, 14)
    signal_time = _logical_signal_time(execution_day)
    assert signal_time == datetime(2020, 8, 14, tzinfo=UTC)
    assert validate_v2_1_temporal_inputs(
        signal_day=signal_day,
        execution_day=execution_day,
        signal_time=signal_time,
    ) == signal_time
    with pytest.raises(ValueError, match="exactly one UTC calendar day"):
        validate_v2_1_temporal_inputs(
            signal_day=signal_day,
            execution_day=execution_day + timedelta(days=1),
            signal_time=_logical_signal_time(execution_day + timedelta(days=1)),
        )


def test_m1_artifact_schema_rejects_formal_performance_fields_and_forwards_nothing():
    assert_no_performance_fields(
        {
            "classification": "CONTAMINATED_DEVELOPMENT_DATA",
            "scale_behavior": {"scale_min": 0.0, "scale_max": 1.0},
            "forward_evidence_created": False,
        }
    )
    with pytest.raises(EngineeringReplayError, match="forbidden historical result fields"):
        assert_no_performance_fields({"diagnostics": {"Return": 0.2}})
    with pytest.raises(EngineeringReplayError, match="forbidden historical result fields"):
        assert_no_performance_fields({"transition": {"pnl": 1.0}})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_test_fixture_nonfinite_values_are_not_accepted_as_valid_scale_inputs(value: float):
    records = list(_weekly_fixture())
    records[-1] = replace(records[-1], net_return=value)
    with pytest.raises(ValueError):
        calculate_position_scale(records, signal_time=datetime(2020, 8, 14, tzinfo=UTC))
