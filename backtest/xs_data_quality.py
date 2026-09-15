"""Fail-closed data integrity checks for the M1 Point-in-Time dataset."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from .xs_history import SymbolHistory


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


def _day_start(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp() * 1000)


def _day_end(day: date) -> int:
    return _day_start(day + timedelta(days=1)) - 1


def _append(issues: list[QualityIssue], code: str, symbol: str, detail: str, day: date | None = None) -> None:
    issues.append(QualityIssue(code=code, symbol=symbol, detail=detail, day=day))


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

    seen_funding: set[int] = set()
    previous_funding: int | None = None
    ordered_funding = list(history.funding_events)
    for event in ordered_funding:
        timestamp = event.funding_time_ms
        if timestamp in seen_funding:
            _append(issues, "DUPLICATE_FUNDING_EVENT", symbol, "duplicate funding timestamp")
        seen_funding.add(timestamp)
        if previous_funding is not None and timestamp <= previous_funding:
            _append(issues, "NON_MONOTONIC_TIMESTAMP", symbol, "funding_time is not increasing")
        previous_funding = timestamp
        if timestamp < _day_start(window_start) or timestamp > _day_end(window_end):
            _append(issues, "OUT_OF_WINDOW", symbol, "funding event outside data window")
        if observation_ms is not None and timestamp > observation_ms:
            _append(issues, "FUTURE_TIMESTAMP_LEAKAGE", symbol, "funding timestamp is after observation")
        if not math.isfinite(event.funding_rate):
            _append(issues, "INVALID_FUNDING_RATE", symbol, "funding rate is not finite")
        if not math.isfinite(event.funding_interval_hours) or event.funding_interval_hours <= 0:
            _append(issues, "INVALID_FUNDING_INTERVAL", symbol, "funding interval must be positive")

    ordered_for_gap = sorted({event.funding_time_ms: event for event in ordered_funding}.values(), key=lambda event: event.funding_time_ms)
    for previous, current in zip(ordered_for_gap, ordered_for_gap[1:]):
        delta_hours = (current.funding_time_ms - previous.funding_time_ms) / 3_600_000.0
        allowed = {previous.funding_interval_hours, current.funding_interval_hours}
        if not any(math.isclose(delta_hours, value, rel_tol=0.0, abs_tol=1e-9) for value in allowed):
            _append(issues, "FUNDING_COVERAGE_GAP", symbol, f"funding spacing {delta_hours:g}h")

    # A held symbol needs at least one real settlement in every consecutive
    # active daily interval.  Missing events are never silently interpreted as 0.
    if seen_days:
        sorted_days = sorted(day for day in seen_days if window_start <= day <= window_end)
        funding_times = sorted(seen_funding)
        for previous_day, current_day in zip(sorted_days, sorted_days[1:]):
            if (current_day - previous_day).days != 1:
                continue
            lower = _day_end(previous_day)
            upper = _day_end(current_day)
            count = sum(lower < timestamp <= upper for timestamp in funding_times)
            if count == 0 and history.active_on(current_day):
                _append(issues, "FUNDING_COVERAGE_GAP", symbol, "no settled funding event in held daily interval", current_day)
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
