"""Deterministic tests for the V2.1-M1.1 warm-start audit."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from backtest.m1_b import ScheduleAttempt
from backtest.v2_1_m1_1_warm_start_audit import (
    V21_M1_1_AUDIT_ID,
    V21_M1_1_BASE_COMMIT,
    V21_M1_1_CUTOFF_UTC,
    V21_M1_1_EXTENSION_END,
    V21_M1_1_EXTENSION_START,
    V21_M1_1_OUTPUT_DIR,
    V21_M1_1_PRESTART_MANIFEST_PATH,
    V21_M1_1_REPORT_PATH,
    V21_M1_1_FROZEN_DATA_END,
    V21_M1_1_LAST_ELIGIBLE_MS,
    V21_M1_1_MARKDOWN_PATH,
    V2_DATA_INTEGRITY_HALT,
    _accounting_issue_days,
    _week_component_provenance,
    _warm_start_gates,
    assert_no_formal_performance_fields,
    build_risk_input,
    load_frozen_dataset,
    select_latest_completed_weeks,
    weekly_funding_audit,
    WarmStartAuditError,
    WarmStartProducerError,
)
from backtest.v2_1_m1_engineering import M1_DATASET_FREEZE_PATH
from backtest.xs_data_quality import HoldingInterval
from backtest.xs_history import FundingEvent, LifecycleRecord, SymbolHistory
from src.xs_lowvol_v2_1_anchor import (
    V21_FORWARD_ANCHOR_STATUS,
    verify_v2_1_forward_anchor_hash,
)


UTC = timezone.utc


def _weekly_rows(count: int = 14) -> list[dict[str, object]]:
    first_start = date(2026, 6, 15)
    rows: list[dict[str, object]] = []
    for index in range(count):
        week_start = first_start + timedelta(days=7 * index)
        week_end = week_start + timedelta(days=6)
        rows.append(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "return": (index - 5) / 1000.0,
            }
        )
    return rows


def _audited_rows(*, complete: bool = True) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(13):
        week_start = date(2026, 6, 15) + timedelta(days=7 * index)
        week_end = week_start + timedelta(days=6)
        complete_value = complete
        rows.append(
            {
                "week_ending": week_end.isoformat(),
                "completed_at": datetime.combine(week_end, time.max, tzinfo=UTC).isoformat(),
                "weekly_net_return": (index - 6) / 1000.0,
                "price_component_complete": complete_value,
                "funding_component_complete": complete_value,
                "transaction_cost_component_complete": complete_value,
                "weekly_return_complete": complete_value,
            }
        )
    return rows


def _component_accounting(*, missing: str | None = None) -> SimpleNamespace:
    week_start = date(2026, 8, 3)
    dates = tuple(week_start + timedelta(days=index) for index in range(7))
    price = [0.001] * 7
    funding = [-0.0002] * 7
    cost = [-0.0001] * 7
    if missing == "price":
        price[3] = None
    elif missing == "funding":
        funding[3] = None
    elif missing == "cost":
        cost[3] = None
    equity = []
    value = 1.0
    equity_price = [0.001] * 7
    equity_funding = [-0.0002] * 7
    equity_cost = [-0.0001] * 7
    for index in range(7):
        value += equity_price[index] + equity_funding[index] + equity_cost[index]
        equity.append(value)
    return SimpleNamespace(
        dates=dates,
        capital=1.0,
        daily_price_pnl=price,
        daily_funding_pnl=funding,
        daily_cost_pnl=cost,
        equity_by_day=lambda: dict(zip(dates, equity)),
    )


def _funding_history(events: tuple[FundingEvent, ...]) -> SymbolHistory:
    lifecycle = LifecycleRecord(
        symbol="SYNUSDT",
        first_available_day=date(2021, 1, 1),
        last_available_day=date(2021, 1, 12),
        listed_from=date(2021, 1, 1),
        delisted_at=None,
        currently_active=True,
        lifecycle_source="test",
        lifecycle_confidence="high",
    )
    return SymbolHistory(
        symbol="SYNUSDT",
        daily_bars=(),
        funding_events=events,
        lifecycle=lifecycle,
    )


def _ms(day: date, hour: int) -> int:
    return int(datetime.combine(day, time.min, tzinfo=UTC).timestamp() * 1000) + hour * 3_600_000


def _funding_week(*, omit_hour: int | None = None) -> tuple[tuple[SymbolHistory, ...], tuple[HoldingInterval, ...]]:
    start = date(2021, 1, 3)
    timestamps = []
    for offset in range(0, 9 * 24, 8):
        day = start + timedelta(days=offset // 24)
        hour = offset % 24
        if omit_hour is not None and offset == omit_hour:
            continue
        timestamps.append(FundingEvent("SYNUSDT", _ms(day, hour), 0.0001, 8.0))
    history = _funding_history(tuple(timestamps))
    hold = HoldingInterval("SYNUSDT", _ms(date(2021, 1, 4), 0), _ms(date(2021, 1, 10), 23) + 3_599_999)
    return (history,), (hold,)


def _minimal_extension_manifest() -> dict[str, object]:
    request_names = {
        "exchange_info",
        "funding_info",
        "SYNUSDT.daily_1d",
        "SYNUSDT.funding_rate",
    }
    return {
        "request_window": {
            "extension_start_utc": V21_M1_1_EXTENSION_START.isoformat(),
            "extension_end_utc": V21_M1_1_EXTENSION_END.isoformat(),
            "audit_cutoff_utc": V21_M1_1_CUTOFF_UTC.isoformat(),
        },
        "requests": [
            {
                "name": name,
                "status_code": 200,
                "error": None,
                "content_sha256": "a" * 64,
                "method": "GET",
            }
            for name in sorted(request_names)
        ],
        "symbol_coverage": {
            "symbols": [
                {
                    "symbol": "SYNUSDT",
                    "scope": "FROZEN_USABLE_HISTORY_CURRENTLY_ACTIVE",
                    "daily_missing_days": [],
                    "price_coverage_pass": True,
                }
            ]
        },
    }


def test_m1_1_identity_and_output_are_isolated_from_forward_and_frozen_m1():
    assert V21_M1_1_BASE_COMMIT == "76412b9cfa3ac102ff2b8db8ee5242971bfa1480"
    assert V21_M1_1_AUDIT_ID == "XS-LOWVOL-V2.1-M1.1-WARM-START-1"
    assert V21_M1_1_CUTOFF_UTC == datetime(2026, 9, 16, tzinfo=UTC)
    assert V21_M1_1_EXTENSION_START == date(2026, 9, 1)
    assert V21_M1_1_EXTENSION_END == date(2026, 9, 15)
    assert V21_M1_1_FROZEN_DATA_END == date(2026, 8, 31)
    assert V21_M1_1_OUTPUT_DIR.name == "m1_1"
    assert V21_M1_1_REPORT_PATH.name == "V2_1_M1_1_WARM_START_AUDIT.json"
    assert V21_M1_1_MARKDOWN_PATH.name == "V2_1_M1_1_WARM_START_AUDIT.md"
    assert V21_M1_1_PRESTART_MANIFEST_PATH.name == "PRESTART_EXTENSION_MANIFEST.json"
    assert "forward" not in V21_M1_1_REPORT_PATH.name.lower()


def test_frozen_cache_boundary_is_explicit_and_cannot_masquerade_as_current():
    if not M1_DATASET_FREEZE_PATH.is_file():
        pytest.skip("ignored frozen market-data cache is unavailable in this checkout")
    dataset = load_frozen_dataset()
    assert dataset["manifest"]["last_available_date"] == "2026-08-31"
    assert dataset["manifest"]["last_available_date"] != V21_M1_1_CUTOFF_UTC.date().isoformat()


def test_latest_thirteen_selection_includes_post_august_weeks_and_excludes_cutoff_after_rows():
    selected = select_latest_completed_weeks(_weekly_rows(), cutoff=V21_M1_1_CUTOFF_UTC)
    endings = [row["week_end"] for row in selected]
    assert len(endings) == 13
    assert len(set(endings)) == 13
    assert date(2026, 9, 6) in endings
    assert date(2026, 9, 13) in endings
    assert date(2026, 9, 20) not in endings
    assert all(row["completed_at"] < V21_M1_1_CUTOFF_UTC for row in selected)
    assert any(item > V21_M1_1_FROZEN_DATA_END for item in endings)


def test_extension_request_upper_bound_is_strictly_before_audit_cutoff():
    assert V21_M1_1_LAST_ELIGIBLE_MS < int(V21_M1_1_CUTOFF_UTC.timestamp() * 1000)
    assert V21_M1_1_LAST_ELIGIBLE_MS + 1 == int(V21_M1_1_CUTOFF_UTC.timestamp() * 1000)


def test_duplicate_week_is_rejected_even_before_risk_layer():
    rows = _weekly_rows()
    rows.append(dict(rows[-1]))
    with pytest.raises(WarmStartProducerError, match="duplicate week_ending"):
        select_latest_completed_weeks(rows, cutoff=V21_M1_1_CUTOFF_UTC)


@pytest.mark.parametrize("missing", ["price", "funding", "cost"])
def test_missing_weekly_component_is_serialized_as_incomplete(missing: str):
    week_start = date(2026, 8, 3)
    week = {
        "week_start": week_start,
        "week_end": week_start + timedelta(days=6),
        "completed_at": datetime(2026, 8, 9, 23, 59, 59, 999999, tzinfo=UTC),
        "producer_weekly_net_return": 0.001,
    }
    funding = {
        "overlapping_holding_interval_count": 1,
        "held_symbols": ["SYNUSDT"],
        "funding_coverage_pass": True,
        "funding_issue_count": 0,
        "funding_issue_symbols": [],
        "funding_issues": [],
        "funding_coverage_status": "PASS_ACTUAL_HELD_INTERVALS",
    }
    row = _week_component_provenance(
        accounting=_component_accounting(missing=missing),
        week=week,
        funding=funding,
        price_issue_days=set(),
        unknown_price_issue=False,
    )
    assert row["weekly_return_complete"] is False
    component_key = (
        "transaction_cost_component_complete" if missing == "cost" else f"{missing}_component_complete"
    )
    assert row[component_key] is False


def test_funding_gap_in_required_week_blocks_but_unrelated_structural_gap_does_not():
    complete_histories, intervals = _funding_week()
    complete = weekly_funding_audit(
        complete_histories, intervals, date(2021, 1, 4), date(2021, 1, 10)
    )
    assert complete["funding_coverage_pass"] is True

    required_gap_histories, required_gap_intervals = _funding_week(omit_hour=4 * 24 + 0)
    required_gap = weekly_funding_audit(
        required_gap_histories,
        required_gap_intervals,
        date(2021, 1, 4),
        date(2021, 1, 10),
    )
    assert required_gap["funding_coverage_pass"] is False
    assert required_gap["funding_issue_symbols"] == ["SYNUSDT"]

    outside_gap_histories, outside_gap_intervals = _funding_week(omit_hour=8 * 24 + 0)
    outside_gap = weekly_funding_audit(
        outside_gap_histories,
        outside_gap_intervals,
        date(2021, 1, 4),
        date(2021, 1, 10),
    )
    assert outside_gap["funding_coverage_pass"] is True


def test_funding_only_mark_failure_is_not_mislabeled_as_price_failure():
    issue = "DATA_INVALID: missing funding coverage SYNUSDT 2026-09-07 -> 2026-09-08"
    accounting = SimpleNamespace(issues=(issue,))
    schedule = SimpleNamespace(
        attempts=(
            ScheduleAttempt(
                date(2026, 9, 7),
                date(2026, 9, 8),
                "MARK_FAILURE",
                issue,
            ),
        )
    )
    assert _accounting_issue_days(accounting, schedule) == (set(), False)


def test_complete_rows_feed_risk_and_incomplete_rows_halt_closed():
    ready = build_risk_input(_audited_rows(complete=True), V21_M1_1_CUTOFF_UTC)
    assert ready["status"] == "VALID"
    assert ready["selected_count"] == 13
    assert ready["diagnostic_status"] == "INITIAL_STATE_DIAGNOSTIC_ONLY"
    assert ready["evidence_status"] == "NOT_PERFORMANCE_EVIDENCE"

    incomplete_rows = _audited_rows(complete=True)
    incomplete_rows[5]["weekly_return_complete"] = False
    halted = build_risk_input(incomplete_rows, V21_M1_1_CUTOFF_UTC)
    assert halted["status"] == V2_DATA_INTEGRITY_HALT
    assert halted["fail_closed_status"] == V2_DATA_INTEGRITY_HALT
    assert "reference_vol" not in halted
    assert "position_scale" not in halted


def test_warm_start_gates_require_all_components_and_fail_closed_risk():
    weekly = _audited_rows(complete=True)
    risk = build_risk_input(weekly, V21_M1_1_CUTOFF_UTC)
    gates = _warm_start_gates(
        frozen_identity_ok=True,
        extension_manifest=_minimal_extension_manifest(),
        weekly=weekly,
        risk=risk,
        legacy_unchanged=True,
    )
    assert all(value == "PASS" for value in gates.values())

    weekly[0]["weekly_return_complete"] = False
    halted = build_risk_input(weekly, V21_M1_1_CUTOFF_UTC)
    gates = _warm_start_gates(
        frozen_identity_ok=True,
        extension_manifest=_minimal_extension_manifest(),
        weekly=weekly,
        risk=halted,
        legacy_unchanged=True,
    )
    assert gates["W7_risk_input_fail_closed"] == "PASS"
    assert gates["W6_weekly_return_completeness"] == "FAIL"


def test_anchor_remains_frozen_and_m1_1_schema_has_no_formal_performance_fields():
    assert V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
    assert verify_v2_1_forward_anchor_hash() == (
        "a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b"
    )
    assert_no_formal_performance_fields(
        {
            "weekly_net_return": 0.01,
            "funding_component_complete": True,
            "forward_evidence_created": False,
        }
    )
    with pytest.raises(WarmStartAuditError, match="forbidden formal performance fields"):
        assert_no_formal_performance_fields({"diagnostics": {"Return": 0.01}})
