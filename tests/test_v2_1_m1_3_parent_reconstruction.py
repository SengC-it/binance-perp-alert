from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backtest.m1_b import build_stateful_schedule
from backtest.v2_1_m1_1_warm_start_audit import load_frozen_dataset
from backtest.v2_1_m1_3_parent_reconstruction import (
    DEFAULT_OVERLAY,
    ParentReconstructionError,
    assert_state_only_schema,
    snapshot_legacy_artifacts,
)
from backtest.xs_data_quality import HoldingInterval
from backtest.xs_history import DailyBar, FundingEvent, LifecycleRecord, SymbolHistory
from src.xs_lowvol_v2_1_anchor import V21_FORWARD_ANCHOR_STATUS
from src.xs_lowvol_v2_1_lifecycle import (
    LifecycleOverride,
    LifecycleOverlay,
    LifecycleOverlayError,
    apply_lifecycle_overlay,
    funding_exposure_end_ms,
    terminal_daily_close,
    validate_economic_funding_coverage,
)


UTC = timezone.utc


def _timestamp(day: date, hour: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp() * 1000)


def _bar(symbol: str, day: date, close: float, volume: float = 10.0) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        day=day,
        open=close,
        high=close,
        low=close,
        close=close,
        quote_volume=volume,
        open_time_ms=_timestamp(day),
        close_time_ms=_timestamp(day + timedelta(days=1)) - 1,
    )


def _history(
    symbol: str = "TESTUSDT",
    *,
    days: tuple[date, ...] = (
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 5),
    ),
    funding_events: tuple[FundingEvent, ...] = (),
) -> SymbolHistory:
    bars = tuple(_bar(symbol, day, float(index + 1)) for index, day in enumerate(days))
    lifecycle = LifecycleRecord(
        symbol=symbol,
        first_available_day=days[0],
        last_available_day=days[-1],
        listed_from=days[0],
        delisted_at=None,
        currently_active=True,
        lifecycle_source="test_fixture",
        lifecycle_confidence="test",
    )
    return SymbolHistory(symbol, bars, funding_events, lifecycle)


def _record(
    symbol: str = "TESTUSDT",
    *,
    published: str = "2026-01-01T00:00:00Z",
    terminated: str = "2026-01-03T09:00:00Z",
    new_symbol: str | None = None,
) -> LifecycleOverride:
    last = date.fromisoformat(terminated[:10])
    return LifecycleOverride(
        symbol=symbol,
        contract_type="USD_M_PERPETUAL",
        official_source="https://www.binance.com/en/support/announcement/detail/test",
        announcement_publish_time_utc=published,
        economic_termination_timestamp_utc=terminated,
        last_valid_trading_day=last,
        delisted_at=last + timedelta(days=1),
        evidence_status="CONFIRMED_OFFICIAL_BINANCE",
        classification="LIFECYCLE_TERMINATION_CONFIRMED",
        official_evidence="test official termination",
        new_symbol=new_symbol,
    )


def _overlay(record: LifecycleOverride) -> LifecycleOverlay:
    return LifecycleOverlay.from_records((record,))


def test_official_termination_overrides_flat_archive_bars_and_sets_plus_one_day():
    history = _history()
    record = _record()
    corrected = apply_lifecycle_overlay((history,), _overlay(record))[0]

    assert tuple(bar.day for bar in corrected.daily_bars) == (
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
    )
    assert corrected.lifecycle.delisted_at == date(2026, 1, 4)
    assert corrected.lifecycle.currently_active is False
    assert corrected.active_on(date(2026, 1, 3)) is True
    assert corrected.active_on(date(2026, 1, 4)) is False


def test_zero_volume_archival_bar_is_not_activity_and_does_not_replace_daily_close():
    history = _history()
    history = SymbolHistory(
        history.symbol,
        history.daily_bars[:-1] + (_bar(history.symbol, date(2026, 1, 5), 99.0, 0.0),),
        history.funding_events,
        history.lifecycle,
    )
    record = _record()
    corrected = apply_lifecycle_overlay((history,), _overlay(record))[0]
    assert corrected.daily_bars[-1].day == date(2026, 1, 3)
    assert terminal_daily_close(history, record) == pytest.approx(3.0)


def test_funding_exposure_uses_economic_termination_not_daily_accounting_exit():
    record = _record()
    overlay = _overlay(record)
    holding = HoldingInterval("TESTUSDT", _timestamp(date(2026, 1, 3), 1), _timestamp(date(2026, 1, 5)))
    assert funding_exposure_end_ms(holding, overlay) == _timestamp(date(2026, 1, 3), 9)


def test_funding_after_term_is_not_required():
    symbol = "TESTUSDT"
    base = _timestamp(date(2026, 1, 1))
    events = tuple(
        FundingEvent(symbol, base + hours * 3_600_000, 0.0, 4.0)
        for hours in (0, 4, 8)
    )
    history = _history(symbol, funding_events=events)
    record = _record(symbol, terminated="2026-01-01T09:00:00Z")
    holding = HoldingInterval(symbol, base + 3_600_000, base + 24 * 3_600_000)
    result = validate_economic_funding_coverage((history,), (holding,), _overlay(record))
    assert result.passed
    assert result.exposure_records[0].economic_exposure_end_timestamp_ms == base + 9 * 3_600_000


def test_funding_missing_before_term_fails_closed():
    symbol = "TESTUSDT"
    base = _timestamp(date(2026, 1, 1))
    events = tuple(
        FundingEvent(symbol, base + hours * 3_600_000, 0.0, 4.0)
        for hours in (0, 8)
    )
    history = _history(symbol, funding_events=events)
    record = _record(symbol, terminated="2026-01-02T00:00:00Z")
    holding = HoldingInterval(symbol, base + 3_600_000, base + 13 * 3_600_000)
    result = validate_economic_funding_coverage((history,), (holding,), _overlay(record))
    assert not result.passed
    assert result.issues[0].code == "MISSING_REQUIRED_SETTLEMENT"


def test_unknown_lifecycle_classification_is_rejected_fail_closed():
    with pytest.raises(LifecycleOverlayError, match="unresolved classification"):
        LifecycleOverride(
            symbol="TESTUSDT",
            contract_type="USD_M_PERPETUAL",
            official_source="https://www.binance.com/en/support/announcement/detail/test",
            announcement_publish_time_utc="2026-01-01T00:00:00Z",
            economic_termination_timestamp_utc="2026-01-03T09:00:00Z",
            last_valid_trading_day=date(2026, 1, 3),
            delisted_at=date(2026, 1, 4),
            evidence_status="UNRESOLVED",
            classification="UNRESOLVED",
            official_evidence="",
        )


def test_future_pit_boundary_does_not_remove_symbol_before_official_record():
    record = _record(published="2026-01-02T12:00:00Z")
    history = _history()
    corrected = apply_lifecycle_overlay((history,), _overlay(record))[0]
    assert record.officially_known_on(date(2026, 1, 1)) is False
    assert record.officially_known_on(date(2026, 1, 2)) is True
    assert corrected.active_on(date(2026, 1, 2)) is True
    assert corrected.active_on(date(2026, 1, 4)) is False


def test_symbol_rename_is_provenance_only_and_does_not_merge_histories():
    record = _record(symbol="KEEPUSDT", new_symbol="TUSDT")
    history = _history("KEEPUSDT")
    corrected = apply_lifecycle_overlay((history,), _overlay(record))[0]
    assert record.new_symbol == "TUSDT"
    assert corrected.symbol == "KEEPUSDT"


def test_state_only_schema_rejects_historical_result_keys():
    with pytest.raises(ParentReconstructionError, match="forbidden key"):
        assert_state_only_schema({"return": 1.0})
    assert_state_only_schema({"combined_component": 1.0, "complete": True})


def test_frozen_overlay_removes_all_known_parent_mark_failures_without_new_data():
    dataset = load_frozen_dataset()
    histories = tuple(dataset["usable_histories"])
    overlay = DEFAULT_OVERLAY.with_terminal_closes(histories)
    corrected = apply_lifecycle_overlay(histories, overlay)
    schedule = build_stateful_schedule(
        corrected, data_start=date(2020, 1, 1), data_end=date(2026, 8, 31)
    )
    assert not [attempt for attempt in schedule.attempts if attempt.status == "MARK_FAILURE"]


def test_legacy_snapshot_is_available_and_anchor_remains_unstarted():
    snapshot = snapshot_legacy_artifacts()
    assert any(key.endswith("research\\evidence\\XS-LOWVOL-V1.json") for key in snapshot)
    assert V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
