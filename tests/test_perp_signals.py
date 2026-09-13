"""永续方向性策略回测的单元测试。

全部用合成价格序列，期望值可手工推导。
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

import pytest

from backtest.funding_arb import MarketSeries, build_series
from backtest.perp_signals import (
    PerpParams,
    basis_fade_signals,
    buy_hold_signals,
    donchian_signals,
    funding_fade_signals,
    ma_cross_signals,
    simulate_perp,
)

START = date(2025, 1, 1)


def ms(day: date, hour: int = 0) -> int:
    return int(datetime.combine(day, time(hour, 0, 0), tzinfo=timezone.utc).timestamp() * 1000)


def make_series(
    prices: list[float],
    symbol: str = "AAAUSDT",
    rates: list[float] | None = None,
    spot_prices: list[float] | None = None,
    volume: float = 1e9,
) -> MarketSeries:
    """用显式价格序列构造 MarketSeries。默认现货与永续同价（基差 0）。"""
    perp, spot, funding = [], [], []
    for i, price in enumerate(prices):
        d = START + timedelta(days=i)
        perp.append([d.isoformat(), price, volume])
        s = spot_prices[i] if spot_prices else price
        spot.append([d.isoformat(), s])
        r = rates[i] if rates else 0.0
        funding.append([ms(d, 0), r, 8.0])
    return build_series({"symbol": symbol, "perp": perp, "spot": spot, "funding": funding})


def params(**kw) -> PerpParams:
    base = dict(capital=10_000.0, max_positions=5, taker_fee_pct=0.05, slippage_pct=0.03,
                ma_fast=20, ma_slow=60, donchian_entry=20, donchian_exit=10,
                zscore_window=30, zscore_entry=2.0)
    base.update(kw)
    return PerpParams(**base)


# ---------------------------------------------------------------------------
# 信号函数
# ---------------------------------------------------------------------------

def test_buy_hold_always_long():
    series = make_series([100.0] * 30)
    sig = buy_hold_signals(series, params())
    assert len(sig) == len(series.dates)
    assert set(sig.values()) == {1}


def test_ma_cross_long_in_uptrend():
    series = make_series([100.0 * (1.01 ** i) for i in range(80)])
    sig = ma_cross_signals(series, params(ma_fast=5, ma_slow=20))
    assert sig[series.dates[-1]] == 1


def test_ma_cross_short_in_downtrend():
    series = make_series([100.0 * (0.99 ** i) for i in range(80)])
    sig = ma_cross_signals(series, params(ma_fast=5, ma_slow=20))
    assert sig[series.dates[-1]] == -1


def test_ma_cross_flat_before_enough_history():
    series = make_series([100.0] * 30)
    sig = ma_cross_signals(series, params(ma_fast=5, ma_slow=20))
    # 前 19 天数据不足，应为空仓
    assert sig[series.dates[0]] == 0
    assert sig[series.dates[18]] == 0
    assert sig[series.dates[19]] != 0


def test_donchian_enters_on_breakout():
    prices = [100.0] * 25 + [120.0] * 5
    series = make_series(prices)
    sig = donchian_signals(series, params(donchian_entry=20, donchian_exit=10))
    assert sig[series.dates[24]] == 0        # 仍在区间内
    assert sig[series.dates[25]] == 1        # 突破 20 日高点
    assert sig[series.dates[29]] == 1        # 维持


def test_donchian_exits_long_on_shallow_pullback():
    """跌破短通道但未跌破长通道 → 回到空仓，不反手。"""
    prices = [100.0] * 20 + [130.0] * 15 + [125.0] * 5
    series = make_series(prices)
    sig = donchian_signals(series, params(donchian_entry=20, donchian_exit=10))
    assert sig[series.dates[34]] == 1
    assert sig[series.dates[35]] == 0


def test_donchian_flips_short_when_breakdown_is_deep():
    """同时跌破长短两个通道 → 直接反手做空（经典反向系统行为）。"""
    prices = [100.0] * 25 + [120.0] * 5 + [90.0] * 10
    series = make_series(prices)
    sig = donchian_signals(series, params(donchian_entry=20, donchian_exit=10))
    assert sig[series.dates[29]] == 1
    assert sig[series.dates[30]] == -1


def test_donchian_shorts_on_breakdown():
    prices = [100.0] * 25 + [80.0] * 5
    series = make_series(prices)
    sig = donchian_signals(series, params(donchian_entry=20, donchian_exit=10))
    assert sig[series.dates[25]] == -1


def test_funding_fade_shorts_when_funding_spikes():
    rates = [0.0001] * 40 + [0.005] * 5
    series = make_series([100.0] * 45, rates=rates)
    sig = funding_fade_signals(series, params(zscore_window=30, zscore_entry=2.0))
    assert sig[series.dates[44]] == -1


def test_funding_fade_longs_when_funding_goes_negative():
    rates = [0.0001] * 40 + [-0.005] * 5
    series = make_series([100.0] * 45, rates=rates)
    sig = funding_fade_signals(series, params(zscore_window=30, zscore_entry=2.0))
    assert sig[series.dates[44]] == 1


def test_basis_fade_shorts_when_perp_premium_spikes():
    prices = [100.0] * 45
    spot = [100.0] * 40 + [95.0] * 5      # 永续相对现货溢价扩大
    series = make_series(prices, spot_prices=spot)
    sig = basis_fade_signals(series, params(zscore_window=30, zscore_entry=2.0))
    assert sig[series.dates[44]] == -1


def test_signals_do_not_look_ahead():
    """把未来数据截掉，历史信号必须完全不变。"""
    prices = [100.0 * (1 + 0.01 * math.sin(i / 3.0)) for i in range(120)]
    full = make_series(prices)
    truncated = make_series(prices[:80])
    p = params(ma_fast=5, ma_slow=20)

    for fn in (ma_cross_signals, donchian_signals, buy_hold_signals):
        a = fn(full, p)
        b = fn(truncated, p)
        for d in truncated.dates:
            assert a[d] == b[d], f"{fn.__name__} 在 {d} 用了未来数据"


# ---------------------------------------------------------------------------
# 组合模拟
# ---------------------------------------------------------------------------

def test_position_is_established_one_day_after_signal():
    """执行延迟：第 0 日信号 → 第 1 日建仓。"""
    series = make_series([100.0] * 30)
    result = simulate_perp([series], "buy_hold", params(max_positions=1))
    assert result.trades[0].entry_date == series.dates[1]


def test_round_trip_cost_is_two_sides():
    series = make_series([100.0] * 30)
    p = params(capital=1_000.0, max_positions=1, taker_fee_pct=0.05, slippage_pct=0.03)
    result = simulate_perp([series], "buy_hold", p)
    # 单边 0.08%，往返 0.16%，名义价值 1000 → 1.6 USDT
    assert result.cost_pnl == pytest.approx(1.6)
    assert result.trades[0].cost_pnl == pytest.approx(1.6)


def test_gross_pnl_matches_price_change():
    """盈亏 = 入场名义价值 × (出场价/入场价 − 1)，标准合约口径。"""
    prices = [100.0] + [100.0 + i for i in range(1, 30)]
    series = make_series(prices)
    p = params(capital=1_000.0, max_positions=1)
    result = simulate_perp([series], "buy_hold", p)
    slot = p.slot_notional()
    # 第 1 日建仓，持有到最后一日的收盘
    expected = slot * (prices[-1] / prices[1] - 1.0)
    assert result.gross_pnl == pytest.approx(expected)


def test_long_loss_is_bounded_by_notional():
    """多头最多亏光名义价值，不可能亏更多（固定名义价值逐日累加会算错这一点）。"""
    prices = [100.0, 50.0, 10.0, 1.0] + [1.0] * 20
    series = make_series(prices)
    p = params(capital=1_000.0, max_positions=1)
    result = simulate_perp([series], "buy_hold", p)
    trade = result.trades[0]
    # 第 1 日（价格 50）建仓，持有到价格 1.0
    assert trade.gross_pnl == pytest.approx(p.slot_notional() * (1.0 / 50.0 - 1.0))
    assert trade.gross_pnl >= -trade.notional


def test_flat_prices_produce_only_cost_loss():
    series = make_series([100.0] * 30)
    result = simulate_perp([series], "buy_hold", params(max_positions=1))
    assert result.gross_pnl == pytest.approx(0.0)
    assert result.net_pnl < 0
    assert result.net_pnl == pytest.approx(-result.cost_pnl)


def test_short_position_profits_when_price_falls():
    series = make_series([100.0 * (0.99 ** i) for i in range(60)])
    p = params(ma_fast=5, ma_slow=20, max_positions=1)
    result = simulate_perp([series], "ma_cross", p)
    assert result.trades[0].direction == -1
    assert result.gross_pnl > 0


def test_max_positions_caps_concurrent_holdings():
    markets = [make_series([100.0 + i for i in range(40)], symbol=f"S{i}USDT")
               for i in range(6)]
    result = simulate_perp(markets, "buy_hold", params(max_positions=3))
    # 每个标的只交易一次，且并发不超过 3
    assert result.trade_count == 3
    assert {t.symbol for t in result.trades} == {"S0USDT", "S1USDT", "S2USDT"}


def test_equity_equals_capital_plus_net_pnl():
    series = make_series([100.0 + (i % 7) for i in range(80)])
    result = simulate_perp([series], "ma_cross", params(ma_fast=5, ma_slow=20, max_positions=1))
    assert result.equity[-1] == pytest.approx(result.params.capital + result.net_pnl, rel=1e-9)


def test_trade_gross_minus_cost_equals_net():
    series = make_series([100.0 * (1 + 0.02 * math.sin(i / 4.0)) for i in range(120)])
    result = simulate_perp([series], "donchian", params(max_positions=1))
    for t in result.trades:
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.cost_pnl)


def test_direction_flip_charges_two_sides():
    """从空翻多应各付一次单边成本。"""
    prices = [100.0 * (0.98 ** i) for i in range(40)] + [100.0 * (1.03 ** i) for i in range(40)]
    series = make_series(prices)
    p = params(capital=1_000.0, max_positions=1, ma_fast=3, ma_slow=8)
    result = simulate_perp([series], "ma_cross", p)
    assert result.cost_pnl > 0
    # 每个方向的仓位至少付了开平两次成本
    assert result.cost_pnl >= 1.6


def test_exposure_and_win_rate_are_bounded():
    series = make_series([100.0 + (i % 11) for i in range(150)])
    result = simulate_perp([series], "donchian", params(max_positions=1))
    assert 0 <= result.exposure_pct <= 100
    assert 0 <= result.win_rate_pct <= 100


def test_unknown_strategy_raises():
    series = make_series([100.0] * 10)
    with pytest.raises(KeyError):
        simulate_perp([series], "does_not_exist", params())


def test_empty_markets_is_safe():
    result = simulate_perp([], "buy_hold", params())
    assert result.trade_count == 0
    assert result.net_pnl == 0.0


def test_metrics_are_finite():
    series = make_series([100.0 + (i % 13) for i in range(200)])
    result = simulate_perp([series], "donchian", params(max_positions=1))
    assert math.isfinite(result.cagr_pct)
    assert math.isfinite(result.sharpe)
    assert math.isfinite(result.sortino)
    assert result.max_drawdown_pct >= 0


def test_concentration_is_none_when_strategy_loses():
    """净亏损时集中度无意义：分子分母同为负会给出误导性的正数，必须返回 None。"""
    prices = [100.0 * (0.99 ** i) for i in range(120)]
    series = make_series(prices)
    result = simulate_perp([series], "buy_hold", params(capital=1_000.0, max_positions=1))
    assert result.net_pnl < 0
    assert result.concentration(2) is None


def test_concentration_is_bounded_when_profitable():
    """净盈利时前 2 大标的贡献占比应为 (0, 100] 区间内的有限值。"""
    prices = [100.0 * (1.01 ** i) for i in range(120)]
    series = make_series(prices)
    result = simulate_perp([series], "buy_hold", params(capital=1_000.0, max_positions=1))
    assert result.net_pnl > 0
    conc = result.concentration(2)
    assert conc is not None
    assert 0 < conc <= 100.0
