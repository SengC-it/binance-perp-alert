"""Fail-closed data integrity checks for the M1 Point-in-Time dataset."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from .xs_history import FundingEvent, SymbolHistory


# Binance's official funding exports retain millisecond-level settlement-time
# jitter. Treat that representation noise as the same frozen interval while
# still rejecting a real missing/extra settlement (hours apart).
_FUNDING_TIMESTAMP_TOLERANCE_HOURS = 1e-3


@dataclass(frozen=True)
class QualityIssue:
    code: str
    symbol: str
    detail: str
    day: date | None = None


@dataclass(frozen=True)
class DataQualityReport:
    window_start: date
    window_end: date
    issues: tuple[QualityIssue, ...] = ()
    checked_symbols: int = 0
    duplicate_symbols: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.issues and not self.duplicate_symbols

    @property
    def issue_codes(self) -> tuple[str, ...]:
        return tuple(sorted({issue.code for issue in self.issues}))


@dataclass(frozen=True)
class HoldingInterval:
    """One actual held interval, represented as (entry, exit]."""

    symbol: str
    entry_timestamp_ms: int
    exit_timestamp_ms: int


@dataclass(frozen=True)
class FundingCoverageReport:
    """Separate structural funding validity from coverage of actual holds."""

    structural_issues: tuple[QualityIssue, ...] = ()
    hold_issues: tuple[QualityIssue, ...] = ()

    @property
    def structural_passed(self) -> bool:
        return not self.structural_issues

    @property
    def holds_passed(self) -> bool:
        return not self.hold_issues

    @property
    def passed(self) -> bool:
        return self.structural_passed and self.holds_passed

    @property
    def issues(self) -> tuple[QualityIssue, ...]:
        return self.structural_issues + self.hold_issues


def _day_start(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp() * 1000)


def _day_end(day: date) -> int:
    return _day_start(day + timedelta(days=1)) - 1


def _append(issues: list[QualityIssue], code: str, symbol: str, detail: str, day: date | None = None) -> None:
    issues.append(QualityIssue(code=code, symbol=symbol, detail=detail, day=day))


def _funding_input(
    events_or_history: SymbolHistory | FundingEvent | Iterable[FundingEvent],
) -> tuple[str, tuple[FundingEvent, ...]]:
    if isinstance(events_or_history, SymbolHistory):
        return events_or_history.symbol, tuple(events_or_history.funding_events)
    if isinstance(events_or_history, FundingEvent):
        return events_or_history.symbol, (events_or_history,)
    events = tuple(events_or_history)
    return (events[0].symbol if events else "UNKNOWN"), events


def _same_funding_event(left: FundingEvent, right: FundingEvent) -> bool:
    return (
        left.symbol == right.symbol
        and left.funding_time_ms == right.funding_time_ms
        and left.funding_rate == right.funding_rate
        and left.funding_interval_hours == right.funding_interval_hours
    )


def _funding_transition_is_valid(previous: FundingEvent, current: FundingEvent) -> bool:
    """Validate one actual settlement transition using both event intervals."""
    delta_hours = (current.funding_time_ms - previous.funding_time_ms) / 3_600_000.0
    return any(
        math.isfinite(interval_hours)
        and interval_hours > 0
        and math.isclose(
            delta_hours,
            interval_hours,
            rel_tol=0.0,
            abs_tol=_FUNDING_TIMESTAMP_TOLERANCE_HOURS,
        )
        for interval_hours in (
            previous.funding_interval_hours,
            current.funding_interval_hours,
        )
    )


def _expected_settlement_is_held(
    origin: FundingEvent,
    interval_hours: Iterable[float],
    holding: HoldingInterval,
) -> bool:
    """Return whether a candidate next settlement falls inside one hold."""
    for interval in interval_hours:
        if not math.isfinite(interval) or interval <= 0:
            continue
        candidate_timestamp = origin.funding_time_ms + interval * 3_600_000.0
        if holding.entry_timestamp_ms < candidate_timestamp <= holding.exit_timestamp_ms:
            return True
    return False


def validate_funding_structure(
    events_or_history: SymbolHistory | FundingEvent | Iterable[FundingEvent],
) -> tuple[QualityIssue, ...]:
    """Validate funding chronology and spacing without assuming a position was held.

    Identical duplicate settlements are safely deduplicable.  Conflicting
    duplicates remain a data error, and no missing settlement is converted to
    a zero funding PnL here.
    """
    symbol, events = _funding_input(events_or_history)
    issues: list[QualityIssue] = []
    by_timestamp: dict[int, FundingEvent] = {}
    previous_timestamp: int | None = None
    for event in events:
        timestamp = event.funding_time_ms
        if not math.isfinite(event.funding_rate):
            _append(issues, "INVALID_FUNDING_RATE", symbol, "funding rate is not finite")
        if not math.isfinite(event.funding_interval_hours) or event.funding_interval_hours <= 0:
            _append(issues, "INVALID_FUNDING_INTERVAL", symbol, "funding interval must be positive")
        previous = by_timestamp.get(timestamp)
        if previous is not None:
            if not _same_funding_event(previous, event):
                _append(
                    issues,
                    "DUPLICATE_FUNDING_EVENT",
                    symbol,
                    "conflicting duplicate funding timestamp",
                )
            # Identical duplicates are deliberately deduped for structural
            # spacing checks and do not fail the dataset.
            continue
        by_timestamp[timestamp] = event
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            _append(issues, "NON_MONOTONIC_TIMESTAMP", symbol, "funding_time is not increasing")
        previous_timestamp = timestamp

    ordered = sorted(by_timestamp.values(), key=lambda event: event.funding_time_ms)
    for previous, current in zip(ordered, ordered[1:]):
        delta_hours = (current.funding_time_ms - previous.funding_time_ms) / 3_600_000.0
        if not _funding_transition_is_valid(previous, current):
            _append(issues, "FUNDING_COVERAGE_GAP", symbol, f"funding spacing {delta_hours:g}h")
    return tuple(issues)


def _unique_funding_events(history: SymbolHistory) -> tuple[FundingEvent, ...]:
    unique: dict[int, FundingEvent] = {}
    for event in history.funding_events:
        unique.setdefault(event.funding_time_ms, event)
    return tuple(sorted(unique.values(), key=lambda event: event.funding_time_ms))


def validate_funding_coverage_for_holds(
    histories: Iterable[SymbolHistory],
    holding_intervals: Iterable[HoldingInterval],
    *,
    expected_interval_hours: float | None = None,
) -> FundingCoverageReport:
    """Require real settled events only for symbols and intervals actually held.

    Funding intervals are event metadata, not a global clock.  Coverage is
    therefore checked against the local real-event sequence surrounding each
    hold; an unrelated gap elsewhere in a symbol's history remains a
    structural issue only.
    """
    history_by_symbol = {history.symbol.upper(): history for history in histories}
    histories_tuple = tuple(history_by_symbol.values())
    structural: list[QualityIssue] = []
    for history in histories_tuple:
        structural.extend(validate_funding_structure(history))

    hold_issues: list[QualityIssue] = []
    if expected_interval_hours is not None and (
        not math.isfinite(expected_interval_hours) or expected_interval_hours <= 0
    ):
        raise ValueError("expected_interval_hours 必须为正数")
    for holding in holding_intervals:
        symbol = str(holding.symbol).upper()
        if holding.exit_timestamp_ms <= holding.entry_timestamp_ms:
            _append(hold_issues, "FUNDING_COVERAGE_GAP", symbol, "holding interval is not positive")
            continue
        history = history_by_symbol.get(symbol)
        if history is None:
            _append(hold_issues, "FUNDING_COVERAGE_GAP", symbol, "held symbol has no normalized history")
            continue
        events = _unique_funding_events(history)
        invalid_held_events = tuple(
            event
            for event in events
            if holding.entry_timestamp_ms < event.funding_time_ms <= holding.exit_timestamp_ms
            and (
                not math.isfinite(event.funding_rate)
                or not math.isfinite(event.funding_interval_hours)
                or event.funding_interval_hours <= 0
            )
        )
        if invalid_held_events:
            _append(
                hold_issues,
                "FUNDING_COVERAGE_GAP",
                symbol,
                "held interval contains invalid funding event metadata",
            )
        valid_events = tuple(
            event
            for event in events
            if math.isfinite(event.funding_rate)
            and math.isfinite(event.funding_interval_hours)
            and event.funding_interval_hours > 0
        )
        if not valid_events:
            _append(hold_issues, "FUNDING_COVERAGE_GAP", symbol, "held interval has no settled funding events")
            continue
        held_events = tuple(
            event
            for event in valid_events
            if holding.entry_timestamp_ms < event.funding_time_ms <= holding.exit_timestamp_ms
        )
        previous = next(
            (
                event
                for event in reversed(valid_events)
                if event.funding_time_ms <= holding.entry_timestamp_ms
            ),
            None,
        )
        following = next(
            (
                event
                for event in valid_events
                if event.funding_time_ms > holding.exit_timestamp_ms
            ),
            None,
        )
        context = tuple(
            event
            for event in (previous, *held_events, following)
            if event is not None
        )
        local_gap = any(
            not _funding_transition_is_valid(left, right)
            for left, right in zip(held_events, held_events[1:])
        )
        if previous is not None and held_events:
            first_held = held_events[0]
            if not _funding_transition_is_valid(previous, first_held):
                local_gap = local_gap or _expected_settlement_is_held(
                    previous,
                    (previous.funding_interval_hours, first_held.funding_interval_hours),
                    holding,
                )
        if following is not None and held_events:
            last_held = held_events[-1]
            if not _funding_transition_is_valid(last_held, following):
                local_gap = local_gap or _expected_settlement_is_held(
                    last_held,
                    (last_held.funding_interval_hours, following.funding_interval_hours),
                    holding,
                )
        if not held_events:
            if not context:
                local_gap = True
            elif previous is not None and following is None:
                exit_gap_hours = (
                    holding.exit_timestamp_ms - previous.funding_time_ms
                ) / 3_600_000.0
                local_gap = _expected_settlement_is_held(
                    previous,
                    (previous.funding_interval_hours,),
                    holding,
                ) or exit_gap_hours < 0
            elif previous is None and following is not None:
                entry_gap_hours = (
                    following.funding_time_ms - holding.entry_timestamp_ms
                ) / 3_600_000.0
                local_gap = entry_gap_hours > following.funding_interval_hours
            elif previous is not None and following is not None:
                if not _funding_transition_is_valid(previous, following):
                    local_gap = _expected_settlement_is_held(
                        previous,
                        (previous.funding_interval_hours, following.funding_interval_hours),
                        holding,
                    )
        else:
            first_held = held_events[0]
            last_held = held_events[-1]
            if previous is None:
                entry_gap_hours = (
                    first_held.funding_time_ms - holding.entry_timestamp_ms
                ) / 3_600_000.0
                if entry_gap_hours < 0 or entry_gap_hours > first_held.funding_interval_hours:
                    local_gap = True
            if following is None:
                if _expected_settlement_is_held(
                    last_held,
                    (last_held.funding_interval_hours,),
                    holding,
                ):
                    local_gap = True
        if local_gap:
            _append(
                hold_issues,
                "FUNDING_COVERAGE_GAP",
                symbol,
                "held interval has an unverified funding transition or boundary",
            )

    return FundingCoverageReport(tuple(structural), tuple(hold_issues))


def funding_pnl_for_hold(
    notional: float,
    direction: int,
    events: Iterable[FundingEvent],
) -> float:
    """Calculate signed funding only from supplied real settlements."""
    if direction not in (-1, 1):
        raise ValueError("direction 必须为 +1 或 -1")
    if not math.isfinite(notional) or notional < 0:
        raise ValueError("notional 必须为有限非负数")
    events = tuple(events)
    if not events:
        raise ValueError("没有 settled funding event，不能伪造零 funding PnL")
    if any(not math.isfinite(event.funding_rate) for event in events):
        raise ValueError("funding event rate 非法")
    return sum(-float(direction) * float(notional) * event.funding_rate for event in events)


def signed_funding_pnl(notional: float, direction: int, events: Iterable[FundingEvent]) -> float:
    """Alias exposing the frozen long-pays/short-receives sign convention."""
    return funding_pnl_for_hold(notional, direction, events)


def validate_symbol_history(
    history: SymbolHistory,
    window_start: date,
    window_end: date,
    *,
    observation_ms: int | None = None,
) -> tuple[QualityIssue, ...]:
    """Validate one symbol without sorting, filling, or deleting bad rows."""
    issues: list[QualityIssue] = []
    symbol = history.symbol
    if history.quote_asset.upper() != "USDT":
        _append(issues, "WRONG_QUOTE_ASSET", symbol, history.quote_asset)
    if history.contract_type.upper() != "PERPETUAL":
        _append(issues, "WRONG_CONTRACT_TYPE", symbol, history.contract_type)

    seen_days: set[date] = set()
    previous_open: int | None = None
    for bar in history.daily_bars:
        if not window_start <= bar.day <= window_end:
            _append(issues, "OUT_OF_WINDOW", symbol, "daily bar outside data window", bar.day)
        if bar.day in seen_days:
            _append(issues, "DUPLICATE_DAILY_BAR", symbol, "duplicate daily day", bar.day)
        seen_days.add(bar.day)
        if previous_open is not None and bar.open_time_ms <= previous_open:
            _append(issues, "NON_MONOTONIC_TIMESTAMP", symbol, "daily open_time is not increasing", bar.day)
        previous_open = bar.open_time_ms
        if observation_ms is not None and max(bar.open_time_ms, bar.close_time_ms) > observation_ms:
            _append(issues, "FUTURE_TIMESTAMP_LEAKAGE", symbol, "daily timestamp is after observation", bar.day)
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            _append(issues, "INVALID_PRICE", symbol, "OHLC must be finite and positive", bar.day)
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
            _append(issues, "INVALID_OHLC", symbol, "OHLC ordering is impossible", bar.day)
        if not math.isfinite(bar.quote_volume) or bar.quote_volume < 0:
            _append(issues, "INVALID_QUOTE_VOLUME", symbol, "quote volume must be finite and non-negative", bar.day)

    if seen_days:
        first = min(seen_days)
        last = max(seen_days)
        active_start = max(first, history.lifecycle.listed_from, window_start)
        active_end = min(
            window_end,
            history.lifecycle.delisted_at - timedelta(days=1)
            if history.lifecycle.delisted_at is not None
            else window_end,
        )
        if active_start <= active_end:
            expected = {
                active_start + timedelta(days=index)
                for index in range((active_end - active_start).days + 1)
            }
            missing = sorted(expected - seen_days)
            for day in missing:
                _append(issues, "MISSING_INTERNAL_DAILY_BAR", symbol, "unexplained active-period daily gap", day)
        if history.lifecycle.listed_from > first:
            _append(issues, "LIFECYCLE_INCONSISTENCY", symbol, "listed_from is after first bar", first)
        if history.lifecycle.delisted_at is not None:
            if history.lifecycle.delisted_at <= history.lifecycle.listed_from:
                _append(issues, "LIFECYCLE_INCONSISTENCY", symbol, "delisted_at is not after listed_from")
            if any(day >= history.lifecycle.delisted_at for day in seen_days):
                _append(issues, "LIFECYCLE_INCONSISTENCY", symbol, "bars exist after delisted_at")
        if history.lifecycle.currently_active and history.lifecycle.delisted_at is not None:
            _append(issues, "LIFECYCLE_INCONSISTENCY", symbol, "active symbol has delisted_at")
        if history.lifecycle.status == "DATA_AMBIGUOUS":
            _append(issues, "UNEXPLAINED_SYMBOL_GAP", symbol, "lifecycle marked DATA_AMBIGUOUS")

    issues.extend(validate_funding_structure(history))
    for event in history.funding_events:
        timestamp = event.funding_time_ms
        if timestamp < _day_start(window_start) or timestamp > _day_end(window_end):
            _append(issues, "OUT_OF_WINDOW", symbol, "funding event outside data window")
        if observation_ms is not None and timestamp > observation_ms:
            _append(issues, "FUTURE_TIMESTAMP_LEAKAGE", symbol, "funding timestamp is after observation")
    return tuple(issues)


def validate_dataset(
    histories: Iterable[SymbolHistory],
    window_start: date,
    window_end: date,
    *,
    observation_ms: int | None = None,
) -> DataQualityReport:
    """Validate the full normalized dataset and return a machine-readable report."""
    histories = tuple(histories)
    seen_symbols: set[str] = set()
    duplicate_symbols: list[str] = []
    issues: list[QualityIssue] = []
    for history in histories:
        symbol = history.symbol.upper()
        if symbol in seen_symbols:
            duplicate_symbols.append(symbol)
        seen_symbols.add(symbol)
        issues.extend(
            validate_symbol_history(
                history,
                window_start,
                window_end,
                observation_ms=observation_ms,
            )
        )
    return DataQualityReport(
        window_start=window_start,
        window_end=window_end,
        issues=tuple(issues),
        checked_symbols=len(histories),
        duplicate_symbols=tuple(sorted(set(duplicate_symbols))),
    )
