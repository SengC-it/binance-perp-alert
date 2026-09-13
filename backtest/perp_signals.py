"""永续合约方向性策略回测。

与资金费套利（`funding_arb.py`）不同，这里做的是**有方向敞口**的交易：
看涨做多、看跌做空，盈亏来自价格本身。之所以放进这个项目，是因为它回答
一个具体问题：

    「告警系统要不要再加一条方向性信号规则？加了之后历史上赚不赚钱？」

策略清单（全部预先指定，不做参数寻优）
======================================
1. `buy_hold`      买入持有，作为基准
2. `ma_cross`      双均线趋势：快线在慢线上方做多，下方做空（始终在场）
3. `donchian`      N 日通道突破：创 N 日新高做多、新低做空，反向通道离场
4. `funding_fade`  资金费极值反向：费率 z-score 过高说明多头拥挤 → 做空
5. `basis_fade`    基差极值反向：永续相对现货溢价 z-score 过高 → 做空

成本
====
每次换仓按「永续 taker 0.05% + 滑点 0.03%」单边计，开平各一次。
反转方向时算两次。

执行时序（防未来函数）
======================
信号用**昨日收盘**计算，在**今日收盘**执行，赚取**今日到明日**的收益。
即：信号 → 执行 → 收益 各差一天，没有任何一环用到未来数据。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Sequence

from .funding_arb import MarketSeries
from .metrics import (
    DAYS_PER_YEAR,
    cagr_pct,
    calmar,
    daily_returns,
    max_drawdown_pct,
    sharpe,
    sortino,
    volatility_pct,
)


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

@dataclass
class PerpParams:
    capital: float = 10_000.0
    max_positions: int = 5               # 最多同时持有的标的数
    taker_fee_pct: float = 0.05          # 永续 taker 手续费（单边）
    slippage_pct: float = 0.03           # 滑点（单边）
    ma_fast: int = 20
    ma_slow: int = 60
    donchian_entry: int = 20
    donchian_exit: int = 10
    zscore_window: int = 30
    zscore_entry: float = 2.0

    def slot_notional(self) -> float:
        return self.capital / self.max_positions if self.max_positions > 0 else 0.0

    def cost_rate(self) -> float:
        """单边成本率（小数）。"""
        return (self.taker_fee_pct + self.slippage_pct) / 100.0


# ---------------------------------------------------------------------------
# 信号函数：返回 {日期: 目标仓位}，仓位取值 -1 / 0 / +1
# ---------------------------------------------------------------------------

SignalFn = Callable[[MarketSeries, PerpParams], dict[date, int]]


def _closes(series: MarketSeries) -> list[float]:
    return [series.perp_close[d] for d in series.dates]


def _highs(series: MarketSeries) -> list[float]:
    if series.perp_high:
        return [series.perp_high.get(d, series.perp_close[d]) for d in series.dates]
    return _closes(series)


def _lows(series: MarketSeries) -> list[float]:
    if series.perp_low:
        return [series.perp_low.get(d, series.perp_close[d]) for d in series.dates]
    return _closes(series)


def buy_hold_signals(series: MarketSeries, p: PerpParams) -> dict[date, int]:
    return {d: 1 for d in series.dates}


def ma_cross_signals(series: MarketSeries, p: PerpParams) -> dict[date, int]:
    """双均线：快线在上做多，在下做空。始终持仓。"""
    closes = _closes(series)
    out: dict[date, int] = {}
    for i, d in enumerate(series.dates):
        if i + 1 < p.ma_slow:
            out[d] = 0
            continue
        fast = statistics.fmean(closes[i + 1 - p.ma_fast:i + 1])
        slow = statistics.fmean(closes[i + 1 - p.ma_slow:i + 1])
        out[d] = 1 if fast > slow else -1
    return out


def donchian_signals(series: MarketSeries, p: PerpParams) -> dict[date, int]:
    """通道突破：跌破短通道离场；若同时跌破长通道则直接反手。

    上下轨都**不含当日**，避免用当日最高价去判断当日突破。
    """
    closes, highs, lows = _closes(series), _highs(series), _lows(series)
    out: dict[date, int] = {}
    pos = 0
    for i, d in enumerate(series.dates):
        # 1) 离场：跌破反向短通道
        if pos > 0 and i >= p.donchian_exit:
            if closes[i] < min(lows[i - p.donchian_exit:i]):
                pos = 0
        elif pos < 0 and i >= p.donchian_exit:
            if closes[i] > max(highs[i - p.donchian_exit:i]):
                pos = 0
        # 2) 进场：突破长通道（若上一步刚离场，这里可能立刻反手）
        if pos == 0 and i >= p.donchian_entry:
            if closes[i] > max(highs[i - p.donchian_entry:i]):
                pos = 1
            elif closes[i] < min(lows[i - p.donchian_entry:i]):
                pos = -1
        out[d] = pos
    return out


def _zscore_signals(series: MarketSeries, p: PerpParams, values: list[float]) -> dict[date, int]:
    """通用 z-score 反向信号：z 过高做空、过低做多。"""
    out: dict[date, int] = {}
    for i, d in enumerate(series.dates):
        if i < p.zscore_window:
            out[d] = 0
            continue
        window = values[i - p.zscore_window:i]
        sd = statistics.pstdev(window)
        if sd <= 0:
            out[d] = 0
            continue
        z = (values[i] - statistics.fmean(window)) / sd
        if z >= p.zscore_entry:
            out[d] = -1
        elif z <= -p.zscore_entry:
            out[d] = 1
        else:
            out[d] = 0
    return out


def _daily_funding_rates(series: MarketSeries) -> list[float]:
    """每个交易日取当日 16:00 那次已结算费率。"""
    out: list[float] = []
    for d in series.dates:
        ts = int(datetime.combine(d, time(23, 59, 59), tzinfo=timezone.utc).timestamp() * 1000)
        info = series.funding_asof(ts)
        out.append(info[0] if info else 0.0)
    return out


def funding_fade_signals(series: MarketSeries, p: PerpParams) -> dict[date, int]:
    return _zscore_signals(series, p, _daily_funding_rates(series))


def basis_fade_signals(series: MarketSeries, p: PerpParams) -> dict[date, int]:
    values = [series.basis_pct(d) or 0.0 for d in series.dates]
    return _zscore_signals(series, p, values)


STRATEGIES: dict[str, tuple[str, SignalFn]] = {
    "buy_hold": ("买入持有（基准）", buy_hold_signals),
    "ma_cross": ("双均线趋势", ma_cross_signals),
    "donchian": ("通道突破", donchian_signals),
    "funding_fade": ("资金费极值反向", funding_fade_signals),
    "basis_fade": ("基差极值反向", basis_fade_signals),
}


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------

@dataclass
class PerpTrade:
    symbol: str
    direction: int          # +1 多，-1 空
    entry_date: date
    exit_date: date
    days: int
    entry_price: float
    exit_price: float
    gross_pnl: float
    cost_pnl: float
    net_pnl: float
    notional: float

    @property
    def gross_pct(self) -> float:
        return self.gross_pnl / self.notional * 100.0 if self.notional else 0.0

    @property
    def net_pct(self) -> float:
        return self.net_pnl / self.notional * 100.0 if self.notional else 0.0

    @property
    def direction_label(self) -> str:
        return "多" if self.direction > 0 else "空"


@dataclass
class PerpResult:
    strategy: str
    label: str
    params: PerpParams
    trades: list[PerpTrade]
    equity_dates: list[date]
    equity: list[float]
    exposure_days: int
    open_at_end: int

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def cost_pnl(self) -> float:
        return sum(t.cost_pnl for t in self.trades)

    @property
    def final_equity(self) -> float:
        return self.equity[-1] if self.equity else self.params.capital

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.params.capital - 1.0) * 100.0

    @property
    def span_days(self) -> int:
        if len(self.equity_dates) < 2:
            return 0
        return (self.equity_dates[-1] - self.equity_dates[0]).days

    @property
    def cagr_pct(self) -> float:
        return cagr_pct(self.params.capital, self.final_equity, self.span_days)

    @property
    def max_drawdown_pct(self) -> float:
        return max_drawdown_pct(self.equity)

    @property
    def volatility_pct(self) -> float:
        return volatility_pct(daily_returns(self.equity))

    @property
    def sharpe(self) -> float:
        return sharpe(daily_returns(self.equity))

    @property
    def sortino(self) -> float:
        return sortino(daily_returns(self.equity))

    @property
    def calmar(self) -> float:
        return calmar(self.cagr_pct, self.max_drawdown_pct)

    @property
    def win_rate_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.net_pnl > 0) / len(self.trades) * 100.0

    @property
    def avg_days(self) -> float:
        return statistics.fmean([t.days for t in self.trades]) if self.trades else 0.0

    @property
    def avg_net_pct(self) -> float:
        return statistics.fmean([t.net_pct for t in self.trades]) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.net_pnl for t in self.trades if t.net_pnl > 0)
        losses = -sum(t.net_pnl for t in self.trades if t.net_pnl < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def exposure_pct(self) -> float:
        if not self.equity_dates:
            return 0.0
        return self.exposure_days / len(self.equity_dates) * 100.0

    @property
    def long_share_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.direction > 0) / len(self.trades) * 100.0

    def by_symbol(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for t in self.trades:
            out[t.symbol] = out.get(t.symbol, 0.0) + t.net_pnl
        return out

    def by_direction(self) -> dict[str, float]:
        """多头与空头各自的净盈亏。熊市里趋势策略的利润通常几乎全来自空头。"""
        out = {"多": 0.0, "空": 0.0}
        for t in self.trades:
            out["多" if t.direction > 0 else "空"] += t.net_pnl
        return out

    def concentration(self, top_n: int = 2) -> float | None:
        """前 top_n 个标的贡献了净利润的百分之多少（衡量风险集中度）。

        仅在策略净盈利时有意义。净亏损时返回 None——此时「前 N 名贡献占比」
        会因为分子分母同为负数而给出误导性的正数，报告层应显式说明不适用。
        """
        ranked = sorted(self.by_symbol().values(), reverse=True)
        if not ranked or self.net_pnl <= 0:
            return None
        return sum(ranked[:top_n]) / self.net_pnl * 100.0

    def to_json(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "label": self.label,
            "params": asdict(self.params),
            "summary": {
                "trade_count": self.trade_count,
                "net_pnl": self.net_pnl,
                "gross_pnl": self.gross_pnl,
                "cost_pnl": self.cost_pnl,
                "final_equity": self.final_equity,
                "total_return_pct": self.total_return_pct,
                "cagr_pct": self.cagr_pct,
                "max_drawdown_pct": self.max_drawdown_pct,
                "sharpe": self.sharpe,
                "sortino": self.sortino,
                "calmar": self.calmar,
                "volatility_pct": self.volatility_pct,
                "win_rate_pct": self.win_rate_pct,
                "avg_days": self.avg_days,
                "avg_net_pct": self.avg_net_pct,
                "profit_factor": self.profit_factor,
                "exposure_pct": self.exposure_pct,
                "long_share_pct": self.long_share_pct,
                "span_days": self.span_days,
                "concentration_top2_pct": self.concentration(2),
            },
            "by_symbol": self.by_symbol(),
            "by_direction": self.by_direction(),
            "trades": [
                {**asdict(t), "entry_date": t.entry_date.isoformat(),
                 "exit_date": t.exit_date.isoformat()}
                for t in self.trades
            ],
            "equity_curve": [
                [d.isoformat(), round(v, 4)] for d, v in zip(self.equity_dates, self.equity)
            ],
        }


# ---------------------------------------------------------------------------
# 组合模拟
# ---------------------------------------------------------------------------

def simulate_perp(
    markets: Sequence[MarketSeries],
    strategy: str,
    params: PerpParams,
) -> PerpResult:
    """在给定行情上模拟一个方向性策略。

    时序：signal_day 收盘算出信号 → today 收盘调整仓位 → 赚 today→tomorrow 的收益。
    """
    if strategy not in STRATEGIES:
        raise KeyError(f"未知策略：{strategy}")
    label, fn = STRATEGIES[strategy]

    signals = {m.symbol: fn(m, params) for m in markets}
    by_symbol = {m.symbol: m for m in markets}
    timeline = sorted({d for m in markets for d in m.dates})
    if len(timeline) < 3:
        return PerpResult(strategy, label, params, [], [], [], 0, 0)

    slot = params.slot_notional()
    cost_rate = params.cost_rate()

    pos: dict[str, int] = {}
    open_trade: dict[str, dict[str, Any]] = {}
    trades: list[PerpTrade] = []
    equity_dates: list[date] = []
    equity: list[float] = []
    realized = 0.0
    exposure_days = 0

    def close_trade(symbol: str, day: date, price: float, side_cost: float) -> None:
        """结算一笔持仓。

        盈亏口径 = 方向 × 入场名义价值 × (出场价/入场价 − 1)。
        这是标准合约盈亏：多头最多亏光名义价值，不会亏成负数。
        """
        nonlocal realized
        info = open_trade.pop(symbol, None)
        if info is None:
            return
        gross = info["direction"] * slot * (price / info["entry_price"] - 1.0)
        total_cost = info["cost_pnl"] + side_cost
        realized += gross - total_cost
        trades.append(PerpTrade(
            symbol=symbol,
            direction=info["direction"],
            entry_date=info["entry_date"],
            exit_date=day,
            days=(day - info["entry_date"]).days,
            entry_price=info["entry_price"],
            exit_price=price,
            gross_pnl=gross,
            cost_pnl=total_cost,
            net_pnl=gross - total_cost,
            notional=slot,
        ))

    for i in range(1, len(timeline)):
        signal_day = timeline[i - 1]
        today = timeline[i]

        # ---- 1) 按昨日信号调整仓位（今日收盘执行）----
        targets: dict[str, int] = {}
        for m in markets:
            if today in m.index_of:
                targets[m.symbol] = signals[m.symbol].get(signal_day, 0)

        # 先平掉需要退出的（每平一个仓位付一次单边成本）
        for symbol in sorted(targets):
            target = targets[symbol]
            cur = pos.get(symbol, 0)
            if cur == 0 or target == cur:
                continue
            price = by_symbol[symbol].perp_close[today]
            close_trade(symbol, today, price, cost_rate * slot)
            pos[symbol] = 0

        # 再开新仓，受并发上限约束
        active = sum(1 for v in pos.values() if v != 0)
        for symbol in sorted(targets):
            target = targets[symbol]
            if target == 0 or pos.get(symbol, 0) == target:
                continue
            if active >= params.max_positions:
                continue
            price = by_symbol[symbol].perp_close[today]
            open_trade[symbol] = {
                "direction": target,
                "entry_date": today,
                "entry_price": price,
                "cost_pnl": cost_rate * slot,
            }
            pos[symbol] = target
            active += 1

        # ---- 2) 盯市 ----
        # 未平仓位的盈亏从入场价算起，不逐日累加，避免大额累计行情下
        # 把多头亏损放大到超过名义价值（那是非物理的）。
        if any(v != 0 for v in pos.values()):
            exposure_days += 1
        unrealized = 0.0
        open_costs = 0.0
        for symbol, info in open_trade.items():
            price = by_symbol[symbol].perp_close.get(today)
            if price is None:
                continue
            unrealized += info["direction"] * slot * (price / info["entry_price"] - 1.0)
            open_costs += info["cost_pnl"]

        equity_dates.append(today)
        equity.append(params.capital + realized + unrealized - open_costs)

    # 收尾：按最后一个交易日收盘强制平掉未平仓位，保证每笔交易都有完整记录
    last_day = timeline[-1]
    for symbol in sorted(list(pos)):
        if pos[symbol] == 0:
            continue
        price = by_symbol[symbol].perp_close.get(last_day)
        if price is None:
            continue
        close_trade(symbol, last_day, price, cost_rate * slot)
        pos[symbol] = 0

    # 平仓后已无未实现盈亏，权益应等于 初始权益 + 已实现盈亏
    if equity:
        equity[-1] = params.capital + realized

    return PerpResult(
        strategy=strategy,
        label=label,
        params=params,
        trades=sorted(trades, key=lambda t: (t.entry_date, t.symbol)),
        equity_dates=equity_dates,
        equity=equity,
        exposure_days=exposure_days,
        open_at_end=0,
    )


def format_summary(result: PerpResult) -> str:
    lines = [
        f"策略            {result.label}（{result.strategy}）",
        f"初始权益        {result.params.capital:>12,.0f} USDT",
        f"最终权益        {result.final_equity:>12,.2f} USDT",
        f"净利润          {result.net_pnl:>12,.2f} USDT   ({result.total_return_pct:+.2f}%)",
        f"  其中 毛盈亏   {result.gross_pnl:>12,.2f} USDT",
        f"  其中 成本     {-result.cost_pnl:>12,.2f} USDT",
        "",
        f"交易笔数        {result.trade_count:>12d}",
        f"胜率            {result.win_rate_pct:>12.1f} %",
        f"盈亏比          {result.profit_factor:>12.2f}",
        f"平均单笔净收益  {result.avg_net_pct:>12.3f} %（占名义价值）",
        f"平均持有        {result.avg_days:>12.1f} 天",
        f"多头占比        {result.long_share_pct:>12.1f} %",
        f"在场时间占比    {result.exposure_pct:>12.1f} %",
        "",
        f"年化收益(CAGR)  {result.cagr_pct:>12.2f} %",
        f"最大回撤        {result.max_drawdown_pct:>12.2f} %",
        f"年化波动        {result.volatility_pct:>12.2f} %",
        f"夏普            {result.sharpe:>12.2f}",
        f"索提诺          {result.sortino:>12.2f}",
        f"Calmar          {result.calmar:>12.2f}",
    ]
    return "\n".join(lines)
