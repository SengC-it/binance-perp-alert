"""Deterministic M1-B.1A parity and data-integrity regression tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from backtest.cross_sectional import XsParams, simulate_xs
from backtest.funding_arb import build_series
from backtest.m1_b import build_signal_cache, simulate_frozen_portfolio
from backtest.xs_data_quality import HoldingInterval, validate_funding_coverage_for_holds, validate_funding_structure
from backtest.xs_history import DailyBar, FundingEvent, SymbolHistory, make_symbol_history


UTC = timezone.utc
START = date(2025, 1, 1)
SYMBOLS = tuple(f"SYN{index:02d}USDT" for index in range(11))


def _ms(day: date, hour: int = 0, offset_ms: int = 0) -> int:
    return int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp() * 1000) + hour * 3_600_000 + offset_ms


def _history_from_bars(
    symbol: str,
    bars: list[DailyBar],
    funding: list[FundingEvent],
    *,
    currently_active: bool = True,
    delisted_at: date | None = None,
) -> SymbolHistory:
    return make_symbol_history(
        symbol,
        bars,
        funding,
        currently_active=currently_active,
        listed_from=START,
        delisted_at=delisted_at,
    )


def _fixture(days: int) -> tuple[tuple[SymbolHistory, ...], list[object]]:
    histories: list[SymbolHistory] = []
    markets: list[object] = []
    for index, symbol in enumerate(SYMBOLS):
        bars: list[DailyBar] = []
        funding: list[FundingEvent] = []
        close = 100.0 + index
        for offset in range(days):
            day = START + timedelta(days=offset)
            if offset < days // 2:
                base = 0.0035
                volatility_rank = index + 1
            else:
                base = 0.0055
                volatility_rank = len(SYMBOLS) - index
            move = base * volatility_rank * (1 if (offset + index) % 2 else -1)
            close *= 1.0 + move
            bars.append(
                DailyBar(
                    symbol=symbol,
                    day=day,
                    open=close,
                    high=close * 1.001,
                    low=close * 0.999,
                    close=close,
                    quote_volume=60_000_000.0,
                    open_time_ms=_ms(day),
                    close_time_ms=_ms(day, 23) + 3_599_999,
                )
            )
            funding.append(
                FundingEvent(
                    symbol=symbol,
                    funding_time_ms=_ms(day, 12),
                    funding_rate=0.0001,
                    funding_interval_hours=24.0,
                )
            )
        histories.append(_history_from_bars(symbol, bars, funding))
        markets.append(
            build_series(
                {
                    "symbol": symbol,
                    "perp": [[bar.day.isoformat(), bar.close, bar.quote_volume] for bar in bars],
                    "spot": [[bar.day.isoformat(), bar.close] for bar in bars],
                    "funding": [[event.funding_time_ms, event.funding_rate, event.funding_interval_hours] for event in funding],
                }
            )
        )
    return tuple(histories), markets


def _replace_day_volume(
    histories: tuple[SymbolHistory, ...],
    markets: list[object],
    symbol: str,
    day: date,
    volume: float,
) -> tuple[tuple[SymbolHistory, ...], list[object]]:
    history = next(item for item in histories if item.symbol == symbol)
    bars = [replace(bar, quote_volume=volume) if bar.day == day else bar for bar in history.daily_bars]
    updated_history = replace(history, daily_bars=tuple(bars))
    updated_markets = list(markets)
    market = next(item for item in updated_markets if item.symbol == symbol)
    market.perp_volume[day] = volume
    return tuple(updated_history if item.symbol == symbol else item for item in histories), updated_markets


def _remove_day(
    histories: tuple[SymbolHistory, ...],
    markets: list[object],
    symbol: str,
    day: date,
) -> tuple[tuple[SymbolHistory, ...], list[object]]:
    history = next(item for item in histories if item.symbol == symbol)
    updated_history = replace(
        history,
        daily_bars=tuple(bar for bar in history.daily_bars if bar.day != day),
    )
    updated_markets = list(markets)
    market = next(item for item in updated_markets if item.symbol == symbol)
    market.perp_close.pop(day, None)
    market.perp_volume.pop(day, None)
    return tuple(updated_history if item.symbol == symbol else item for item in histories), updated_markets


def _assert_m0_m1_parity(
    histories: tuple[SymbolHistory, ...],
    markets: list[object],
    data_end: date,
) -> tuple[object, object]:
    cache = build_signal_cache(histories, data_start=START, data_end=data_end)
    successful = [attempt for attempt in cache.attempts if attempt.status == "SUCCESS"]
    for variant in ("control", "shadow"):
        m0 = simulate_xs(markets, "xs_lowvol", XsParams.for_variant(variant))
        m1 = simulate_frozen_portfolio(
            histories,
            cache.signals,
            variant=variant,
            scenario="COST_1X",
            capital=10_000.0,
            data_start=START,
            data_end=data_end,
        )
        m0_audits = [(audit.signal_day, audit.execution_day) for audit in m0.rebalance_audits]
        m1_audits = [(attempt.signal_day, attempt.execution_day) for attempt in successful]
        assert m1_audits == m0_audits
        assert m1.targets_by_signal_day == {
            day.isoformat(): targets for day, targets in m0.targets_by_signal_day.items()
        }
        assert m1.scales_by_signal_day == pytest.approx(
            {day.isoformat(): scale for day, scale in m0.scales_by_signal_day.items()}
        )
        assert m1.turnover_notional == pytest.approx(m0.turnover_notional, rel=1e-12, abs=1e-8)
        assert m1.funding_pnl == pytest.approx(m0.funding_pnl, rel=1e-12, abs=1e-8)
        assert m1.cost_pnl == pytest.approx(-m0.cost_pnl, rel=1e-12, abs=1e-8)
        assert m1.equity == pytest.approx(m0.equity, rel=1e-12, abs=1e-8)
        assert m1.equity[-1] == pytest.approx(m0.final_equity, rel=1e-12, abs=1e-8)
    return cache, simulate_xs(markets, "xs_lowvol", XsParams.for_variant("shadow"))


def test_stateful_schedule_parity_covers_resize_flip_scale_and_nav():
    histories, markets = _fixture(100)
    cache, shadow_m0 = _assert_m0_m1_parity(histories, markets, START + timedelta(days=99))
    successes = [attempt for attempt in cache.attempts if attempt.status == "SUCCESS"]
    assert len(successes) >= 6
    target_maps = [cache.signals[attempt.signal_day].targets for attempt in successes]
    assert any(
        left.get(symbol) is not None
        and right.get(symbol) is not None
        and left[symbol] != right[symbol]
        for left, right in zip(target_maps, target_maps[1:])
        for symbol in set(left) & set(right)
    )
    assert len({round(scale, 8) for scale in shadow_m0.scales_by_signal_day.values()}) > 1
    assert shadow_m0.resized_symbols


def test_no_signal_does_not_consume_clock_and_recovers_next_calendar_day():
    histories, markets = _fixture(50)
    signal_day = START + timedelta(days=30)
    histories, markets = _replace_day_volume(histories, markets, SYMBOLS[0], signal_day, 0.0)
    histories, markets = _replace_day_volume(histories, markets, SYMBOLS[1], signal_day, 0.0)
    cache, _ = _assert_m0_m1_parity(histories, markets, START + timedelta(days=49))
    first_two = cache.attempts[:2]
    assert first_two[0].signal_day == signal_day
    assert first_two[0].execution_day == signal_day + timedelta(days=1)
    assert first_two[0].status == "NO_SIGNAL"
    assert first_two[1].execution_day == signal_day + timedelta(days=2)
    assert first_two[1].status == "SUCCESS"


def test_eligible_history_gap_does_not_consume_clock_and_recovers_after_lookback():
    data_end = START + timedelta(days=71)
    histories, markets = _fixture(72)
    baseline = build_signal_cache(histories, data_start=START, data_end=data_end)
    first_success = next(attempt for attempt in baseline.attempts if attempt.status == "SUCCESS")
    first_targets = baseline.signals[first_success.signal_day].targets
    gap_symbol = next(symbol for symbol in SYMBOLS if symbol not in first_targets)
    gap_day = first_success.signal_day + timedelta(days=2)
    histories, markets = _remove_day(histories, markets, gap_symbol, gap_day)
    cache, _ = _assert_m0_m1_parity(histories, markets, data_end)
    gap_attempts = [
        attempt
        for attempt in cache.attempts
        if first_success.execution_day < attempt.execution_day < gap_day + timedelta(days=32)
    ]
    assert gap_attempts
    assert any(attempt.status == "NO_SIGNAL" for attempt in gap_attempts)
    recovered = next(
        attempt for attempt in cache.attempts if attempt.execution_day >= gap_day + timedelta(days=32)
    )
    assert recovered.status == "SUCCESS"
    assert recovered.signal_day == gap_day + timedelta(days=31)


def test_missing_t1_target_price_retries_without_advancing_clock():
    data_end = START + timedelta(days=38)
    histories, markets = _fixture(39)
    baseline = build_signal_cache(histories, data_start=START, data_end=data_end)
    first_success = next(attempt for attempt in baseline.attempts if attempt.status == "SUCCESS")
    target_symbol = next(iter(baseline.signals[first_success.signal_day].targets))
    histories, markets = _remove_day(
        histories,
        markets,
        target_symbol,
        first_success.execution_day,
    )
    cache, _ = _assert_m0_m1_parity(histories, markets, data_end)
    failed = next(attempt for attempt in cache.attempts if attempt.status == "MISSING_EXECUTION_PRICE")
    assert failed.execution_day == first_success.execution_day
    recovered = next(
        attempt for attempt in cache.attempts if attempt.execution_day > failed.execution_day
    )
    assert recovered.status == "SUCCESS"
    assert recovered.execution_day == failed.execution_day + timedelta(days=1)


def test_confirmed_delisting_forces_terminal_exit_and_preserves_prior_parity():
    data_end = START + timedelta(days=54)
    histories, markets = _fixture(55)
    baseline = build_signal_cache(histories, data_start=START, data_end=data_end)
    first_success = next(attempt for attempt in baseline.attempts if attempt.status == "SUCCESS")
    target_symbol = next(iter(baseline.signals[first_success.signal_day].targets))
    delisted_at = first_success.execution_day + timedelta(days=10)
    updated: list[SymbolHistory] = []
    for history in histories:
        if history.symbol != target_symbol:
            updated.append(history)
            continue
        bars = [bar for bar in history.daily_bars if bar.day < delisted_at]
        updated.append(
            _history_from_bars(
                history.symbol,
                bars,
                list(history.funding_events),
                currently_active=False,
                delisted_at=delisted_at,
            )
        )
    cache = build_signal_cache(tuple(updated), data_start=START, data_end=data_end)
    result = simulate_frozen_portfolio(
        tuple(updated),
        cache.signals,
        variant="control",
        scenario="COST_1X",
        capital=10_000.0,
        data_start=START,
        data_end=data_end,
    )
    assert result.complete
    terminal_bar = next(bar for bar in updated[SYMBOLS.index(target_symbol)].daily_bars if bar.day == delisted_at - timedelta(days=1))
    forced_interval = next(
        interval
        for interval in result.holding_intervals
        if interval.symbol == target_symbol and interval.exit_timestamp_ms == terminal_bar.close_time_ms
    )
    assert forced_interval.exit_timestamp_ms == terminal_bar.close_time_ms
    assert result.turnover_notional > 0
    assert result.cost_pnl < 0

    m0 = simulate_xs(markets, "xs_lowvol", XsParams.for_variant("control"))
    prior_m0 = [(audit.signal_day, audit.execution_day) for audit in m0.rebalance_audits if audit.execution_day < delisted_at]
    prior_m1 = [
        (attempt.signal_day, attempt.execution_day)
        for attempt in cache.attempts
        if attempt.status == "SUCCESS" and attempt.execution_day < delisted_at
    ]
    assert prior_m1 == prior_m0


def test_official_millisecond_funding_jitter_is_not_a_false_gap():
    first = FundingEvent("SYNUSDT", _ms(START, 0), 0.0001, 8.0)
    second = FundingEvent("SYNUSDT", _ms(START, 8, 7), 0.0001, 8.0)
    assert validate_funding_structure((first, second)) == ()
    holding = HoldingInterval("SYNUSDT", first.funding_time_ms - 1_000, second.funding_time_ms + 1_000)
    report = validate_funding_coverage_for_holds(
        (
            make_symbol_history(
                "SYNUSDT",
                [
                    DailyBar(
                        symbol="SYNUSDT",
                        day=START,
                        open=1.0,
                        high=1.0,
                        low=1.0,
                        close=1.0,
                        quote_volume=60_000_000.0,
                        open_time_ms=_ms(START),
                        close_time_ms=_ms(START, 23) + 3_599_999,
                    )
                ],
                (first, second),
            ),
        ),
        (holding,),
    )
    assert report.hold_issues == ()
