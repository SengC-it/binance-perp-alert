"""Synthetic and cache-optional tests for the M1.4 state bridge."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest import v2_1_m1_4_prestart_bridge as bridge
from backtest.v2_1_m1_engineering import M1_DATASET_FREEZE_PATH, load_verified_dataset
from backtest.xs_history import DailyBar


UTC = timezone.utc


def _labels() -> dict[str, object]:
    return {"classification": list(bridge._PRESTART_LABELS), "scope": "STATE"}


def _checkpoint() -> dict[str, object]:
    return {
        "scheduler_clock_state": {
            "last_successful_rebalance_execution_day": "2026-08-28",
            "last_successful_signal_day": "2026-08-27",
            "rebalance_interval_days": 7,
            "next_rebalance_due_day": "2026-09-04",
            "pending_retry": False,
            "scheduler_initialized_from_checkpoint": True,
        },
        "current_control_positions": [
            {
                "symbol": "AAAUSDT",
                "direction": "LONG",
                "direction_code": 1,
                "target_notional": 0.1,
                "entry_timestamp_utc": "2026-08-28T23:59:59.999Z",
                "last_mark_timestamp_utc": "2026-08-31T23:59:59.999Z",
                "last_mark_price": 100.0,
            }
        ],
        "historical_full_replay_invoked": False,
        "m1_3_result_commit": bridge.M13_RESULT_COMMIT,
        "m1_3_overlay_sha256": bridge.M13_OVERLAY_SHA256,
        "accounting_state_continuity_hash": "a" * 64,
    }


def _week_rows(count: int = 15) -> list[dict[str, object]]:
    start = date(2026, 6, 1)
    rows: list[dict[str, object]] = []
    for index in range(count):
        week_start = start + timedelta(days=7 * index)
        week_end = week_start + timedelta(days=6)
        rows.append(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "completed_at_utc": datetime.combine(
                    week_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC
                ).isoformat().replace("+00:00", "Z"),
                "source": "M1_3_CORRECTED_STATE" if index < 13 else "M1_4_PRESTART_BRIDGE",
                "price_component_complete": True,
                "funding_component_complete": True,
                "cost_component_complete": True,
                "complete": True,
                "weekly_return": (index + 1) / 1000.0,
            }
        )
    return rows


def test_start_before_extension_is_rejected():
    with pytest.raises(bridge.M14DataError):
        bridge.validate_extension_window(
            data_start=date(2026, 8, 31), data_end=date(2026, 9, 18)
        )


def test_historical_full_replay_invocation_is_rejected():
    with pytest.raises(bridge.M14DataError):
        bridge.assert_no_historical_full_replay_invoked(
            data_start=bridge.EXTENSION_START, historical_full_replay_invoked=True
        )


def test_cutoff_excludes_2026_09_19():
    bridge.validate_extension_window(
        data_start=date(2026, 9, 1), data_end=date(2026, 9, 18)
    )
    with pytest.raises(bridge.M14DataError):
        bridge.validate_extension_window(
            data_start=date(2026, 9, 1), data_end=date(2026, 9, 19)
        )


def test_checkpoint_binds_accepted_m13_state_and_terminal_control_signal():
    if not Path(M1_DATASET_FREEZE_PATH).is_file():
        pytest.skip("verified frozen cache unavailable")
    checkpoint, _, _ = bridge.build_m13_checkpoint(dataset=load_verified_dataset())
    assert checkpoint["m1_3_result_commit"] == bridge.M13_RESULT_COMMIT
    assert checkpoint["m1_3_code_commit"] == bridge.M13_CODE_COMMIT
    assert checkpoint["m1_3_overlay_sha256"] == bridge.M13_OVERLAY_SHA256
    assert checkpoint["last_successful_signal_day"] == "2026-08-27"
    assert checkpoint["last_successful_rebalance_execution_day"] == "2026-08-28"
    assert checkpoint["historical_full_replay_invoked"] is False
    assert len(checkpoint["current_control_positions"]) == 10


def test_scheduler_clock_carries_forward_without_reset():
    clock = bridge.carry_scheduler_state(_checkpoint())
    assert clock["next_rebalance_due_day"] == "2026-09-04"
    assert clock["scheduler_initialized_from_checkpoint"] is True


def test_positions_carry_forward_without_reopen():
    positions = bridge.carry_positions_without_reopen(_checkpoint())
    assert positions["AAAUSDT"].entry_timestamp_ms < positions["AAAUSDT"].mark_timestamp_ms
    assert positions["AAAUSDT"].mark_price == 100.0


def test_current_exchange_info_alone_is_not_pit_proof():
    assert bridge.candidate_has_pit_proof(onboard_timestamp_ms=1, first_completed_bar=None) is False
    bar = DailyBar(
        symbol="NEWUSDT",
        day=date(2026, 9, 2),
        open=1.0,
        high=1.1,
        low=0.9,
        close=1.05,
        quote_volume=100.0,
        open_time_ms=int(datetime(2026, 9, 2, tzinfo=UTC).timestamp() * 1000),
        close_time_ms=int(datetime(2026, 9, 2, 23, 59, 59, 999000, tzinfo=UTC).timestamp() * 1000),
    )
    assert bridge.candidate_has_pit_proof(onboard_timestamp_ms=bar.open_time_ms - 1, first_completed_bar=bar)


def test_latest_13_selection_is_temporal_and_excludes_old_2026_06_07():
    selected = bridge.select_latest_current_weeks(_week_rows())
    endings = [row["week_end"] for row in selected]
    assert len(endings) == 13
    assert len(set(endings)) == 13
    assert "2026-09-06" in endings
    assert "2026-09-13" in endings
    assert "2026-06-07" not in endings


def test_weekly_return_uses_equity_continuity():
    assert bridge.compute_weekly_return(prior_equity=2.0, week_end_equity=2.2) == pytest.approx(0.1)


def test_complete_13_week_input_computes_diagnostic_risk_only():
    result = bridge.build_risk_state(_week_rows())
    assert result["status"] == "VALID"
    assert result["selected_count"] == 13
    assert result["risk_diagnostic_status"] == "INITIAL_STATE_DIAGNOSTIC_ONLY"
    assert result["evidence_status"] == "NOT_VALIDATION_EVIDENCE"


def test_incomplete_week_halts_risk_input():
    rows = _week_rows()
    rows[-1]["complete"] = False
    result = bridge.build_risk_state(rows)
    assert result["status"] == "V2_DATA_INTEGRITY_HALT"
    assert result["risk_diagnostic_status"] == "HALTED_INCOMPLETE_INPUT"
    assert "reference_vol" not in result


def test_prestart_aggregate_schema_is_rejected():
    payload = _labels()
    payload["price_component_total"] = 1.0
    with pytest.raises(Exception):
        bridge.assert_prestart_state_schema(payload)


def test_quarantined_weekly_return_is_allowed_only_with_labels():
    payload = _labels()
    payload["weekly_return"] = 0.01
    bridge.assert_prestart_state_schema(payload)


def test_anchor_remains_frozen_not_started():
    assert bridge.V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
    assert bridge.verify_v2_1_forward_anchor_hash() == bridge.FORWARD_ANCHOR_SHA256


def test_no_forward_artifact_path_is_created_by_code_phase():
    assert "forward" not in bridge.M14_REPORT_PATH.name.lower()
    assert bridge.M14_REPORT_PATH.name == "V2_1_M1_4_PRESTART_BRIDGE.json"
