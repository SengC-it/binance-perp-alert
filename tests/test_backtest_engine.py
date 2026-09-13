"""回测引擎单元测试。

全部使用**合成数据**，期望值可由手工推导，避免"用实现验证实现"。
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

import pytest

from backtest.funding_arb import (
    BacktestParams,
    BacktestResult,
    build_series,
    gross_annual_pct,
    net_annual_pct,
    simulate,
)

START = date(2025, 1, 1)


def ms(day: date, hour: int = 0) -> int:
    return int(datetime.combine(day, time(hour, 0, 0), tzinfo=timezone.utc).timestamp() * 1000)


def make_record(
    symbol: str = "AAAUSDT",
    days: int = 100,
    perp_price: float = 100.0,
    spot_price: float = 99.8,
    rate: float = 0.0004,
    volume: float = 1e9,
    spot_schedule: dict[date, float] | None = None,
    perp_schedule: dict[date, float] | None = None,
    interval_hours: float = 8.0,
) -> dict:
    """构造一个标的的合成数据。

    默认：永续 100、现货 99.8（基差恒定 +0.2004%），每次结算费率 0.0004，
    结算间隔 8 小时（即每天 3 次）。
    """
    perp_rows, spot_rows, funding = [], [], []
    step = int(interval_hours)
    for i in range(days):
        d = START + timedelta(days=i)
        p = (perp_schedule or {}).get(d, perp_price)
        s = (spot_schedule or {}).get(d, spot_price)
        perp_rows.append([d.isoformat(), p, volume])
        spot_rows.append([d.isoformat(), s])
        for h in range(0, 24, step):
            funding.append([ms(d, h), rate, interval_hours])
    return {"symbol": symbol, "perp": perp_rows, "spot": spot_rows, "funding": funding}


def params(**kw) -> BacktestParams:
    base = dict(
        capital=10_000.0,
        max_positions=5,
        perp_margin_ratio=0.20,
        hold_days=30,
        min_net_annual_pct=10.0,
        min_volume_usdt_24h=50_000_000.0,
        round_trip_cost_pct=0.36,
        exec_lag_days=1,
        max_pct_of_volume=0.005,
    )
    base.update(kw)
    return BacktestParams(**base)


# ---------------------------------------------------------------------------
# 年化与成本摊销
# ---------------------------------------------------------------------------

def test_gross_annual_pct_matches_three_settlements_per_day():
    # 0.0004 × 3 × 365 = 0.438 → 43.8%
    assert gross_annual_pct(0.0004) == pytest.approx(43.8)


def test_gross_annual_pct_respects_four_hour_interval():
    # 币安对部分合约用 4 小时结算（每天 6 次），年化应是 8 小时口径的两倍
    assert gross_annual_pct(0.0004, 8.0) == pytest.approx(43.8)
    assert gross_annual_pct(0.0004, 4.0) == pytest.approx(87.6)


def test_four_hour_settlement_symbol_collects_twice_the_funding():
    series = build_series(make_record(days=45, rate=0.0004, interval_hours=4.0))
    result = simulate([series], params(max_positions=1, hold_days=30))
    trade = result.trades[0]
    # 30 天 × 6 次/天 = 180 次结算
    assert trade.settlements == 180
    assert trade.funding_pct == pytest.approx(180 * 0.0004 * 100)


def test_trailing_signal_mode_produces_trades():
    series = build_series(make_record(days=45, rate=0.0004))
    result = simulate([series], params(max_positions=1, hold_days=30,
                                       signal_mode="trailing"))
    assert result.trade_count == 1
    # 数据开头的窗口会被截断，故首个信号用的是不足 3 天的累计值
    assert result.trades[0].signal_net_annual_pct > 0


def test_cost_amortization_shrinks_with_longer_hold():
    p30 = params(hold_days=30)
    p90 = params(hold_days=90)
    assert p30.amortized_cost_pct() == pytest.approx(0.36 * 365 / 30)
    assert p90.amortized_cost_pct() == pytest.approx(0.36 * 365 / 90)
    # 持有越久，摊销成本越小，净年化越高
    assert net_annual_pct(0.0004, p90) > net_annual_pct(0.0004, p30)


def test_short_hold_can_turn_a_positive_gross_into_negative_net():
    # 毛年化 21.9% 看起来为正，但只持有 3 天时往返成本摊销高达 43.8%
    short = params(hold_days=3)
    assert gross_annual_pct(0.0002) > 0
    assert net_annual_pct(0.0002, short) < 0


def test_slot_notional_accounts_for_perp_margin():
    p = params(capital=10_000.0, max_positions=5, perp_margin_ratio=0.20)
    # 每槽 2000 USDT，其中 1/1.2 用于现货腿
    assert p.slot_notional() == pytest.approx(2000.0 / 1.2)


# ---------------------------------------------------------------------------
# 序列构造
# ---------------------------------------------------------------------------

def test_build_series_keeps_only_days_present_in_both_markets():
    rec = make_record(days=10)
    rec["spot"] = rec["spot"][:7]          # 现货少 3 天
    series = build_series(rec)
    assert len(series.dates) == 7
    assert series.dates[-1] == START + timedelta(days=6)


def test_basis_pct_sign_and_value():
    series = build_series(make_record(days=5, perp_price=100.0, spot_price=99.8))
    # (100 - 99.8) / 99.8 × 100
    assert series.basis_pct(START) == pytest.approx(0.2004008016, rel=1e-9)


def test_funding_sum_between_uses_half_open_window():
    series = build_series(make_record(days=5, rate=0.001))
    # 从第 0 日 23:59:59 到第 2 日 23:59:59：第 1、2 日各 3 次结算 = 6 次
    start = int(datetime.combine(START, time(23, 59, 59), tzinfo=timezone.utc).timestamp() * 1000)
    end = int(datetime.combine(START + timedelta(days=2), time(23, 59, 59),
                               tzinfo=timezone.utc).timestamp() * 1000)
    count, total = series.funding_sum_between(start, end)
    assert count == 6
    assert total == pytest.approx(0.006)


def test_funding_asof_returns_last_settled_rate_and_interval():
    series = build_series(make_record(days=5, rate=0.0007))
    # 第 3 日 12:00 时，最近一次结算是第 3 日 08:00
    assert series.funding_asof(ms(START + timedelta(days=3), 12)) == (0.0007, 8.0)


def test_funding_trailing_sums_realized_settlements():
    series = build_series(make_record(days=10, rate=0.0004))
    # 第 5 日 23:00 向前 3 天：第 3、4、5 日各 3 次 = 9 次结算，合计 0.0036
    count, total = series.funding_trailing(ms(START + timedelta(days=5), 23), 3)
    assert count == 9
    assert total == pytest.approx(0.0036)


# ---------------------------------------------------------------------------
# 完整模拟：手算校验
# ---------------------------------------------------------------------------

def test_single_trade_matches_hand_calculation():
    """1 个标的、恒定费率与基差，逐项手算核对。

    数据只给 45 天，使得 30 天持有期下只能容纳一笔完整交易。
    """
    series = build_series(make_record(days=45))
    p = params(max_positions=1, hold_days=30)
    result = simulate([series], p)

    assert result.trade_count == 1
    trade = result.trades[0]

    # 执行延迟 1 天：信号在第 0 日，入场在第 1 日，出场在第 31 日
    assert trade.entry_date == START + timedelta(days=1)
    assert trade.exit_date == START + timedelta(days=31)
    assert trade.hold_days == 30

    # 90 次结算 × 0.0004 × 100 = 3.6%
    assert trade.settlements == 90
    assert trade.funding_pct == pytest.approx(3.6)
    # 基差恒定，进出场相同 → 基差贡献为 0
    assert trade.basis_pct == pytest.approx(0.0, abs=1e-9)
    assert trade.cost_pct == pytest.approx(0.36)
    assert trade.net_pct == pytest.approx(3.6 - 0.36)

    # 名义价值 = 10000 / 1 / 1.2
    assert trade.notional == pytest.approx(10_000 / 1.2)
    assert trade.net_pnl == pytest.approx(10_000 / 1.2 * 3.24 / 100)


def test_signal_to_execution_lag_delays_entry():
    series = build_series(make_record(days=100))
    lag1 = simulate([series], params(max_positions=1, exec_lag_days=1))
    lag0 = simulate([series], params(max_positions=1, exec_lag_days=0))
    assert lag1.trades[0].entry_date == START + timedelta(days=1)
    assert lag0.trades[0].entry_date == START


def test_basis_convergence_adds_to_pnl():
    """入场时永续溢价 0.2004%，出场时溢价归零 → 基差贡献 +0.2004%。"""
    spot_schedule = {}
    for i in range(45):
        d = START + timedelta(days=i)
        # 第 31 日之后现货追上永续，基差归零
        spot_schedule[d] = 99.8 if i < 31 else 100.0
    series = build_series(make_record(days=45, spot_schedule=spot_schedule))
    result = simulate([series], params(max_positions=1, hold_days=30))
    trade = result.trades[0]
    assert trade.basis_pct == pytest.approx(0.2004008016, rel=1e-6)
    assert trade.net_pct == pytest.approx(3.6 + 0.2004008016 - 0.36, rel=1e-6)


def test_negative_funding_produces_negative_funding_leg():
    series = build_series(make_record(days=45, rate=-0.0004))
    result = simulate([series], params(max_positions=1, min_net_annual_pct=-100.0))
    assert result.trade_count == 1
    assert result.trades[0].funding_pct == pytest.approx(-3.6)
    assert result.trades[0].net_pnl < 0


def test_basis_widening_hurts():
    """入场基差 0，出场时永续相对现货走强 → 基差贡献为负。"""
    spot_schedule = {}
    for i in range(45):
        d = START + timedelta(days=i)
        spot_schedule[d] = 100.0 if i < 31 else 99.0
    series = build_series(make_record(days=45, perp_price=100.0,
                                      spot_schedule=spot_schedule))
    result = simulate([series], params(max_positions=1, hold_days=30))
    trade = result.trades[0]
    # b0 = 0，b1 = (100-99)/99×100 = +1.0101% → 基差贡献 = -1.0101%
    assert trade.basis_pct == pytest.approx(-1.01010101, rel=1e-6)


# ---------------------------------------------------------------------------
# 过滤与约束
# ---------------------------------------------------------------------------

def test_min_net_annual_threshold_blocks_entry():
    series = build_series(make_record(days=45, rate=0.0001))  # 毛年化 10.95%
    # 持有 30 天摊销 4.38% → 净年化 6.57%，低于 10% 门槛
    assert simulate([series], params(max_positions=1, min_net_annual_pct=10.0)).trade_count == 0
    assert simulate([series], params(max_positions=1, min_net_annual_pct=5.0)).trade_count == 1


def test_volume_filter_blocks_entry():
    thin = build_series(make_record(days=45, volume=1_000_000))
    assert simulate([thin], params(max_positions=1)).trade_count == 0
    liquid = build_series(make_record(days=45, volume=1e9))
    assert simulate([liquid], params(max_positions=1)).trade_count == 1


def test_max_positions_caps_concurrent_entries():
    markets = [build_series(make_record(symbol=f"S{i}USDT", days=100)) for i in range(4)]
    result = simulate(markets, params(max_positions=2))
    # 任何一天的在仓数都不能超过上限
    assert max(result.open_counts) == 2
    assert result.signals_skipped_capacity > 0


def test_more_slots_produce_more_trades():
    markets = [build_series(make_record(symbol=f"S{i}USDT", days=100)) for i in range(4)]
    few = simulate(markets, params(max_positions=1))
    many = simulate(markets, params(max_positions=4))
    assert many.trade_count > few.trade_count
    assert many.signals_skipped_capacity < few.signals_skipped_capacity


def test_capacity_constraint_scales_notional_by_volume():
    # 成交额 2 亿，单笔上限 0.5% = 100 万，远高于槽位 8333，故不生效
    wide = build_series(make_record(days=100, volume=2e8))
    assert simulate([wide], params(max_positions=1)).trades[0].notional == pytest.approx(10_000 / 1.2)
    # 成交额 100 万，单笔上限 5000，低于槽位 → 生效
    narrow = build_series(make_record(days=100, volume=1_000_000,
                                      perp_price=100.0))
    res = simulate([narrow], params(max_positions=1, min_volume_usdt_24h=0.0))
    assert res.trades[0].notional == pytest.approx(1_000_000 * 0.005)


def test_no_reentry_while_symbol_already_held():
    markets = [build_series(make_record(symbol="AAAUSDT", days=100))]
    result = simulate(markets, params(max_positions=5))
    # 同一标的不允许重叠持仓
    spans = [(t.entry_date, t.exit_date) for t in result.trades]
    for a, b in zip(spans, spans[1:]):
        assert a[1] <= b[0]


def test_incomplete_trades_are_skipped_near_data_end():
    """数据尾部不足以持满 hold_days 时不建仓。"""
    series = build_series(make_record(days=40))
    result = simulate([series], params(max_positions=1, hold_days=30))
    assert result.trade_count == 1
    for t in result.trades:
        assert t.exit_date <= series.dates[-1]


# ---------------------------------------------------------------------------
# 风险指标
# ---------------------------------------------------------------------------

def test_max_drawdown_computation():
    res = BacktestResult(
        params=params(), trades=[], equity_dates=[], equity=[100.0, 120.0, 90.0, 130.0],
        open_counts=[], symbols_used=[],
    )
    # 峰值 120 → 谷底 90，回撤 25%
    assert res.max_drawdown_pct == pytest.approx(25.0)


def test_max_drawdown_zero_when_monotonic():
    res = BacktestResult(
        params=params(), trades=[], equity_dates=[], equity=[100.0, 101.0, 105.0],
        open_counts=[], symbols_used=[],
    )
    assert res.max_drawdown_pct == pytest.approx(0.0)


def test_pnl_decomposition_sums_to_net():
    series = build_series(make_record(days=200))
    result = simulate([series], params(max_positions=2))
    total = result.funding_pnl + result.basis_pnl - result.cost_pnl
    assert total == pytest.approx(result.net_pnl, rel=1e-9)


def test_equity_curve_matches_capital_plus_cumulative_pnl():
    series = build_series(make_record(days=200))
    result = simulate([series], params(max_positions=1))
    assert result.equity[-1] == pytest.approx(result.params.capital + result.net_pnl, rel=1e-9)


def test_cagr_and_sharpe_are_finite():
    series = build_series(make_record(days=200))
    result = simulate([series], params(max_positions=1))
    assert math.isfinite(result.cagr_pct)
    assert math.isfinite(result.sharpe)
    assert result.span_days > 0
