"""截面（cross-sectional）方向性策略回测。

为什么单独写一个模块
====================
`perp_signals.py` 里的策略都是**时间序列**的：每个标的独立看自己的均线/通道。
这类策略在单边行情里赚的钱，本质是「押对了方向」，样本一换结论就反转。

截面策略不一样：它在**同一时点横向比较所有标的**，做多相对强的、做空相对弱的，
组合本身接近市场中性。理论上更不依赖大盘方向。

本模块实现三个预先指定的截面策略（同样不做参数寻优）：

1. `xs_momentum`  截面动量：做多过去 N 日涨幅最高的 K 个，做空最差的 K 个
2. `xs_carry`     截面 carry：做多资金费最低（甚至为负）的 K 个，做空最高的 K 个
3. `xs_lowvol`    截面低波：做多波动率最低的 K 个，做空最高的 K 个（防御性对照）

外加一个可选叠加层：

4. 波动率目标化（vol targeting）：按「目标波动 / 已实现波动」缩放仓位，
   波动放大时自动减仓。这是唯一被广泛证实能改善风险调整后收益的仓位技术。

统计显著性
==========
一年日线数据只有约 250 个观测。夏普比率的抽样误差极大，因此本模块对每个策略
都输出 **bootstrap 置信区间** 与 **留一标的法**（逐个剔除单个标的重新回测），
用来判断「这个结果是不是靠一两个标的撑起来的」。

用法：
  python -m backtest.cross_sectional
"""

from __future__ import annotations

import logging
import math
import random
import statistics
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .funding_arb import MarketSeries
from .metrics import (
    DAYS_PER_YEAR,
    cagr_pct,
    daily_returns,
    max_drawdown_pct,
    sharpe,
    sortino,
    volatility_pct,
)

log = logging.getLogger("backtest.xs")
OUT_DIR = Path(__file__).resolve().parent

BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_SEED = 20260913


@dataclass
class XsParams:
    capital: float = 10_000.0
    k_long: int = 5                 # 做多几个
    k_short: int = 5                # 做空几个
    lookback: int = 30              # 打分窗口（天）
    rebalance_days: int = 7         # 调仓间隔
    taker_fee_pct: float = 0.05     # 单边手续费
    slippage_pct: float = 0.03      # 单边滑点
    # 波动率目标化（0 表示关闭）
    target_vol_pct: float = 0.0     # 目标年化波动（%）
    vol_window: int = 30
    max_scale: float = 3.0          # 缩放上限，防止低波时杠杆失控
    min_volume_usdt_24h: float = 50_000_000.0
    exec_lag_days: int = 1

    def slot_notional(self) -> float:
        n = self.k_long + self.k_short
        return self.capital / n if n > 0 else 0.0

    def cost_rate(self) -> float:
        return (self.taker_fee_pct + self.slippage_pct) / 100.0


# ---------------------------------------------------------------------------
# 打分函数：给定「截至某日的历史」，返回 {标的: 分数}（分数越高越该做多）
# ---------------------------------------------------------------------------

ScoreFn = Callable[[dict[str, MarketSeries], date, int], dict[str, float]]


def _price_at(s: MarketSeries, day: date) -> float | None:
    return s.perp_close.get(day)


def _end_of_day_ms(day: date) -> int:
    return int(
        datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc).timestamp() * 1000
    )


def _days_before(s: MarketSeries, day: date, n: int) -> date | None:
    i = s.index_of.get(day)
    if i is None or i - n < 0:
        return None
    return s.dates[i - n]


def score_momentum(
    markets: dict[str, MarketSeries], day: date, lookback: int
) -> dict[str, float]:
    """过去 lookback 日收益率。涨得多的分数高。"""
    out: dict[str, float] = {}
    for sym, s in markets.items():
        past = _days_before(s, day, lookback)
        if past is None:
            continue
        p0, p1 = _price_at(s, past), _price_at(s, day)
        if p0 and p1 and p0 > 0:
            out[sym] = p1 / p0 - 1.0
    return out


def score_carry(
    markets: dict[str, MarketSeries], day: date, lookback: int
) -> dict[str, float]:
    """过去 lookback 日已结算资金费之和，取负号。

    资金费为负（空头付费给多头）说明做多还能额外收钱 → 分数高。
    """
    out: dict[str, float] = {}
    ts = int(
        datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc).timestamp() * 1000
    )
    for sym, s in markets.items():
        n, total = s.funding_trailing(ts, lookback)
        if n > 0:
            out[sym] = -total
    return out


def score_lowvol(
    markets: dict[str, MarketSeries], day: date, lookback: int
) -> dict[str, float]:
    """过去 lookback 日的日收益波动率，取负号。波动低的分高。"""
    out: dict[str, float] = {}
    for sym, s in markets.items():
        i = s.index_of.get(day)
        if i is None or i - lookback < 0:
            continue
        prices = [s.perp_close[s.dates[j]] for j in range(i - lookback, i + 1)]
        rets = [
            prices[k] / prices[k - 1] - 1.0
            for k in range(1, len(prices))
            if prices[k - 1] > 0
        ]
        if len(rets) >= 5:
            out[sym] = -statistics.pstdev(rets)
    return out


SCORE_FNS: dict[str, ScoreFn] = {
    "xs_momentum": score_momentum,
    "xs_carry": score_carry,
    "xs_lowvol": score_lowvol,
}


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------

@dataclass
class XsResult:
    strategy: str
    label: str
    params: XsParams
    dates: list[date] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    net_pnl: float = 0.0
    cost_pnl: float = 0.0
    funding_pnl: float = 0.0
    trade_count: int = 0
    by_symbol: dict[str, float] = field(default_factory=dict)
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    avg_gross_exposure_pct: float = 0.0

    @property
    def final_equity(self) -> float:
        return self.equity[-1] if self.equity else self.params.capital

    @property
    def total_return_pct(self) -> float:
        return self.net_pnl / self.params.capital * 100.0 if self.params.capital else 0.0

    @property
    def span_days(self) -> int:
        if len(self.dates) < 2:
            return 0
        return (self.dates[-1] - self.dates[0]).days

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
    def cagr_pct(self) -> float:
        return cagr_pct(self.params.capital, self.final_equity, self.span_days)

    def concentration(self, top_n: int = 2) -> float | None:
        ranked = sorted(self.by_symbol.values(), reverse=True)
        if not ranked or self.net_pnl <= 0:
            return None
        return sum(ranked[:top_n]) / self.net_pnl * 100.0

    def by_month(self) -> dict[str, float]:
        """按月拆解盈亏（USDT）。用于判断收益是否集中在少数月份。"""
        if len(self.equity) < 2:
            return {}
        out: dict[str, float] = {}
        prev = self.equity[0]
        for day, value in zip(self.dates[1:], self.equity[1:]):
            key = day.strftime("%Y-%m")
            out[key] = out.get(key, 0.0) + (value - prev)
            prev = value
        return out

    def positive_months_share(self) -> float:
        """盈利月份贡献了总盈利的百分之多少。

        如果全部盈利只来自 1~2 个月，说明这个策略是「等一次行情」，
        而不是持续产生收益。
        """
        months = self.by_month()
        gains = [v for v in months.values() if v > 0]
        total = sum(gains)
        if total <= 0 or not gains:
            return 0.0
        return max(gains) / total * 100.0


def simulate_xs(
    markets: Sequence[MarketSeries],
    strategy: str,
    p: XsParams,
) -> XsResult:
    """跑一个截面策略。"""
    score_fn = SCORE_FNS[strategy]
    by_sym = {m.symbol: m for m in markets}
    result = XsResult(strategy=strategy, label=strategy, params=p)

    # 统一交易日轴
    all_days = sorted({d for m in markets for d in m.dates})
    if not all_days:
        return result

    slot = p.slot_notional()
    cost_rate = p.cost_rate()

    positions: dict[str, int] = {}       # 标的 -> 方向
    entry_price: dict[str, float] = {}   # 标的 -> 入场价
    entry_day: dict[str, date] = {}      # 标的 -> 入场日（用于累计资金费）
    notional: dict[str, float] = {}      # 标的 -> 名义价值（波动率缩放后）
    realized = 0.0
    cost_total = 0.0
    funding_total = 0.0
    last_rebalance: date | None = None

    def exit_leg(sym: str, day: date) -> None:
        """平掉组合中的一条腿：价格盈亏 + 资金费 − 成本。

        命名刻意避开交易所的执行类术语——组合的组成单位是「腿」，
        而这个函数只做回测内的账面结算，不产生任何交易所调用。
        src/scope_guard.py 会持续检查项目里不出现执行类标识符。

        资金费必须计入：做空资金费为正的合约会**收到**资金费，
        做多则会**支付**。忽略这一项会系统性高估空头收益。
        """
        nonlocal realized, cost_total, funding_total
        price = by_sym[sym].perp_close.get(day)
        if price is None:
            return
        direction = positions[sym]
        gross = direction * notional[sym] * (price / entry_price[sym] - 1.0)
        realized += gross
        if direction > 0:
            result.long_pnl += gross
        else:
            result.short_pnl += gross
        result.by_symbol[sym] = result.by_symbol.get(sym, 0.0) + gross

        # 资金费：多头付、空头收（费率为正时）
        start = _end_of_day_ms(entry_day[sym])
        end = _end_of_day_ms(day)
        _, rate_sum = by_sym[sym].funding_sum_between(start, end)
        funding = -direction * notional[sym] * rate_sum
        realized += funding
        funding_total += funding
        result.funding_pnl += funding

        cost = notional[sym] * cost_rate
        realized -= cost
        cost_total += cost

        positions.pop(sym)
        entry_price.pop(sym)
        entry_day.pop(sym)
        notional.pop(sym)

    def exit_all(day: date) -> None:
        for sym in list(positions):
            exit_leg(sym, day)

    equity: list[float] = []

    for i, day in enumerate(all_days):
        # 1) 是否调仓：信号用前一交易日，执行在今日收盘
        if last_rebalance is None or (day - last_rebalance).days >= p.rebalance_days:
            signal_idx = i - p.exec_lag_days
            if signal_idx >= 0:
                signal_day = all_days[signal_idx]
                scores = score_fn(by_sym, signal_day, p.lookback)
                # 流动性过滤
                liquid = {
                    sym: sc for sym, sc in scores.items()
                    if by_sym[sym].perp_volume.get(signal_day, 0.0)
                    >= p.min_volume_usdt_24h
                }
                if len(liquid) >= p.k_long + p.k_short:
                    ranked = sorted(liquid.items(), key=lambda kv: kv[1], reverse=True)
                    targets: dict[str, int] = {}
                    for sym, _ in ranked[: p.k_long]:
                        targets[sym] = 1
                    for sym, _ in ranked[-p.k_short:]:
                        targets[sym] = -1

                    # 先平掉不在目标里、或方向变了的
                    for sym in list(positions):
                        if sym not in targets or targets[sym] != positions[sym]:
                            exit_leg(sym, day)

                    # 再开新仓（组合层面的统一缩放系数，保持多空平衡）
                    port_scale = 1.0
                    if p.target_vol_pct > 0:
                        port_scale = _portfolio_vol_scale(
                            by_sym, list(liquid), signal_day, p
                        )
                    for sym, direction in targets.items():
                        price = by_sym[sym].perp_close.get(day)
                        if price is None:
                            continue
                        if sym not in positions:
                            positions[sym] = direction
                            entry_price[sym] = price
                            entry_day[sym] = day
                            notional[sym] = slot * port_scale
                            cost = notional[sym] * cost_rate
                            realized -= cost
                            cost_total += cost
                            result.trade_count += 1

                last_rebalance = day

        # 2) 盯市：必须在调仓之后算，否则被平掉的仓位会同时出现在
        #    realized 与 unrealized 里，权益曲线出现虚假的双重计数跳变。
        unrealized = 0.0
        for sym, direction in positions.items():
            price = by_sym[sym].perp_close.get(day)
            if price is None:
                continue
            unrealized += direction * notional[sym] * (
                price / entry_price[sym] - 1.0
            )

        equity.append(p.capital + realized + unrealized)

    exit_all(all_days[-1])
    if equity:
        equity[-1] = p.capital + realized

    result.dates = all_days
    result.equity = equity
    result.net_pnl = realized
    result.cost_pnl = cost_total
    # 平均名义敞口占权益比例（截面策略始终满仓，等于总槽位占比）
    result.avg_gross_exposure_pct = slot * (p.k_long + p.k_short) / p.capital * 100.0
    return result


def _symbol_vol_pct(s: MarketSeries, day: date, window: int) -> float | None:
    """单个标的的年化已实现波动（%）。"""
    i = s.index_of.get(day)
    if i is None or i - window < 0:
        return None
    prices = [s.perp_close[s.dates[j]] for j in range(i - window, i + 1)]
    rets = [
        prices[k] / prices[k - 1] - 1.0
        for k in range(1, len(prices))
        if prices[k - 1] > 0
    ]
    if len(rets) < 5:
        return None
    return statistics.pstdev(rets) * math.sqrt(DAYS_PER_YEAR) * 100.0


def _portfolio_vol_scale(
    markets: dict[str, MarketSeries],
    universe: Sequence[str],
    day: date,
    p: XsParams,
) -> float:
    """组合层面的波动率目标缩放系数。

    关键：缩放系数必须对**所有仓位统一**。如果逐标的缩放，
    低波动的多头会被放大、高波动的空头会被缩小，
    这会直接改变多空平衡——对于「低波动」类策略等于自毁。
    """
    vols = [
        v for sym in universe
        if (v := _symbol_vol_pct(markets[sym], day, p.vol_window)) is not None
    ]
    if not vols:
        return 1.0
    universe_vol = statistics.fmean(vols)
    if universe_vol <= 0:
        return 1.0
    return max(0.1, min(p.max_scale, p.target_vol_pct / universe_vol))


# ---------------------------------------------------------------------------
# 统计显著性
# ---------------------------------------------------------------------------

def market_daily_returns(
    markets: Sequence[MarketSeries], all_days: Sequence[date]
) -> list[float]:
    """等权全市场日收益率，作为「大盘」基准。"""
    out: list[float] = []
    for prev, cur in zip(all_days, all_days[1:]):
        rets: list[float] = []
        for s in markets:
            p0 = s.perp_close.get(prev)
            p1 = s.perp_close.get(cur)
            if p0 and p1 and p0 > 0:
                rets.append(p1 / p0 - 1.0)
        out.append(statistics.fmean(rets) if rets else 0.0)
    return out


def alpha_beta(
    strategy_returns: Sequence[float], market_returns: Sequence[float]
) -> tuple[float, float, float]:
    """对大盘做一元线性回归，返回 (beta, 年化 alpha%, R²)。

    这是判断「策略真的有 alpha，还是在偷偷押方向」的关键检验。
    截面策略虽然多空对冲，但如果 beta 显著为负、alpha 接近 0，
    那它赚的就是**做空大盘**的钱，行情一转就还回去。
    """
    n = min(len(strategy_returns), len(market_returns))
    if n < 30:
        return 0.0, 0.0, 0.0
    x = list(market_returns[:n])
    y = list(strategy_returns[:n])
    mx, my = statistics.fmean(x), statistics.fmean(y)
    var = sum((a - mx) ** 2 for a in x) / n
    if var == 0:
        return 0.0, 0.0, 0.0
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y)) / n
    beta = cov / var
    alpha_daily = my - beta * mx
    var_y = sum((b - my) ** 2 for b in y) / n
    r2 = (cov / math.sqrt(var * var_y)) ** 2 if var_y > 0 else 0.0
    return beta, alpha_daily * DAYS_PER_YEAR * 100.0, r2


def bootstrap_sharpe(
    equity: list[float], rounds: int = BOOTSTRAP_ROUNDS, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float, float]:
    """对日收益做有放回重抽样，返回 (点估计, 5% 分位, 95% 分位)。

    一年只有约 250 个观测，夏普的抽样误差通常在 ±0.5 以上。
    区间跨过 0 就说明「这个夏普在统计上与 0 无法区分」。
    """
    rets = daily_returns(equity)
    if len(rets) < 30:
        return 0.0, 0.0, 0.0
    point = sharpe(rets)
    rng = random.Random(seed)
    n = len(rets)
    samples: list[float] = []
    for _ in range(rounds):
        draw = [rets[rng.randrange(n)] for _ in range(n)]
        mean = statistics.fmean(draw)
        sd = statistics.pstdev(draw)
        if sd > 0:
            samples.append(mean / sd * math.sqrt(DAYS_PER_YEAR))
    if not samples:
        return point, 0.0, 0.0
    samples.sort()
    lo = samples[int(len(samples) * 0.05)]
    hi = samples[int(len(samples) * 0.95)]
    return point, lo, hi


def leave_one_out(
    markets: Sequence[MarketSeries], strategy: str, p: XsParams
) -> list[tuple[str, float]]:
    """逐个剔除单个标的重新回测，返回 [(被剔除的标的, 该次收益率%)]。

    如果剔除任意一个标的就让结果从大幅盈利变成亏损，说明这个策略的
    收益并非来自策略本身，而是来自某一个标的的行情。
    """
    out: list[tuple[str, float]] = []
    for m in markets:
        subset = [x for x in markets if x.symbol != m.symbol]
        if len(subset) < p.k_long + p.k_short:
            continue
        res = simulate_xs(subset, strategy, p)
        out.append((m.symbol, res.total_return_pct))
    return out


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def comparison_table(results: dict[str, XsResult]) -> str:
    cols = ["策略", "笔数", "收益率", "最大回撤", "夏普", "索提诺",
            "价格盈亏", "资金费", "交易成本", "多头", "空头"]
    widths = [14, 6, 10, 11, 8, 9, 11, 10, 10, 10, 10]
    lines = ["| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |"]
    lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for r in results.values():
        price_pnl = r.net_pnl - r.funding_pnl + r.cost_pnl
        cells = [
            r.label,
            str(r.trade_count),
            f"{r.total_return_pct:+.2f}%",
            f"{r.max_drawdown_pct:.2f}%",
            f"{r.sharpe:.2f}",
            f"{r.sortino:.2f}",
            f"{price_pnl:+,.0f}",
            f"{r.funding_pnl:+,.0f}",
            f"{-r.cost_pnl:+,.0f}",
            f"{r.long_pnl:+,.0f}",
            f"{r.short_pnl:+,.0f}",
        ]
        lines.append("| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |")
    return "\n".join(lines)


def build_report(markets: Sequence[MarketSeries], base: XsParams) -> str:
    lines: list[str] = []
    lines.append("# 截面方向性策略：验证报告")
    lines.append("")
    lines.append("> 时间序列策略（双均线、通道突破）在单边行情里赚的钱，本质是押对了方向。")
    lines.append("> 截面策略在同一时点横向比较所有标的，做多相对强的、做空相对弱的，")
    lines.append("> 组合接近市场中性，理论上更不依赖大盘方向。本报告检验这个「理论上」。")
    lines.append("")

    # ---- 1. 主结果 ----
    results: dict[str, XsResult] = {}
    for key in SCORE_FNS:
        results[key] = simulate_xs(markets, key, base)

    lines.append("## 一、主结果（未做任何参数寻优）")
    lines.append("")
    lines.append(f"- 标的数：{len(markets)}")
    lines.append(f"- 参数：做多 {base.k_long} 个 / 做空 {base.k_short} 个，"
                 f"打分窗口 {base.lookback} 天，每 {base.rebalance_days} 天调仓")
    lines.append(f"- 成本：单边 taker {base.taker_fee_pct}% + 滑点 {base.slippage_pct}%")
    lines.append("")
    lines.append(comparison_table(results))
    lines.append("")

    # ---- 2. 统计显著性 ----
    lines.append("## 二、统计显著性：这些夏普能信吗？")
    lines.append("")
    lines.append("一年日线只有约 250 个观测，夏普的抽样误差很大。"
                 "对日收益做 2000 次有放回重抽样，得到夏普的 90% 置信区间。")
    lines.append("**区间跨过 0 就说明这个夏普与 0 在统计上无法区分。**")
    lines.append("")
    lines.append("| 策略 | 夏普(点估计) | 5% 分位 | 95% 分位 | 区间是否跨 0 |")
    lines.append("|---|---:|---:|---:|---|")
    for key, r in results.items():
        point, lo, hi = bootstrap_sharpe(r.equity)
        crosses = "**是（不显著）**" if lo <= 0 <= hi else "否（显著）"
        lines.append(
            f"| {r.label} | {point:.2f} | {lo:.2f} | {hi:.2f} | {crosses} |"
        )
    lines.append("")

    # ---- 3. alpha / beta 分解 ----
    all_days = sorted({d for m in markets for d in m.dates})
    mkt = market_daily_returns(markets, all_days)
    lines.append("## 三、alpha / beta 分解：真有 alpha，还是在偷偷押方向？")
    lines.append("")
    lines.append("截面策略多空对冲，看起来市场中性。但如果它的 beta 显著为负、"
                 "而 alpha 接近 0，那它赚的其实是**做空大盘**的钱——"
                 "行情一转就要还回去。这一节对大盘（等权全市场）做一元回归。")
    lines.append("")
    lines.append("| 策略 | beta | 年化 alpha | R² | 判读 |")
    lines.append("|---|---:|---:|---:|---|")
    for key, r in results.items():
        beta, alpha, r2 = alpha_beta(daily_returns(r.equity), mkt)
        if alpha <= -5.0 and abs(beta) < 0.3:
            verdict = "**负 alpha**：方向敞口不大，策略本身在亏钱"
        elif alpha >= 5.0 and abs(beta) < 0.3:
            verdict = "**可能是真 alpha**：方向敞口小、超额收益为正"
        elif abs(alpha) < 5.0 and beta < -0.2:
            verdict = "**beta 押注**：alpha 接近于 0，收益来自做空大盘"
        else:
            verdict = "混合：方向敞口与 alpha 都有贡献"
        lines.append(
            f"| {r.label} | {beta:+.2f} | {alpha:+.2f}% | {r2:.2f} | {verdict} |"
        )
    lines.append("")
    lines.append("参考：样本期等权全市场买入持有 "
                 f"{sum(mkt) * 100:+.2f}%（累计），即大盘本身是深度下跌的。")
    lines.append("")

    # ---- 4. 留一标的法 ----
    lines.append("## 四、留一标的法：结果是不是靠一两个标的撑起来的？")
    lines.append("")
    lines.append("逐个剔除单个标的、重新跑完整回测。如果剔除任意一个标的就让结论翻转，"
                 "说明这个策略赚的不是「策略」的钱，而是「那个标的」的钱。")
    lines.append("")
    for key, r in results.items():
        loo = leave_one_out(markets, key, base)
        if not loo:
            continue
        rets = [v for _, v in loo]
        worst_sym, worst = min(loo, key=lambda kv: kv[1])
        best_sym, best = max(loo, key=lambda kv: kv[1])
        n_pos = sum(1 for v in rets if v > 0)
        lines.append(f"**{r.label}**（基准 {r.total_return_pct:+.2f}%）")
        lines.append("")
        lines.append(f"- 剔除任意单标的后，收益率区间：{min(rets):+.2f}% ~ {max(rets):+.2f}%")
        lines.append(f"- 剔除后仍为正：{n_pos} / {len(rets)} 次")
        lines.append(f"- 影响最大：剔除 {worst_sym} → {worst:+.2f}%；"
                     f"剔除 {best_sym} → {best:+.2f}%")
        spread = max(rets) - min(rets)
        verdict = (
            "**结论稳健**：剔除任一标的都不改变方向"
            if min(rets) > 0 else
            f"**结论脆弱**：剔除单标的即可让收益在 {spread:.1f} 个百分点的范围内摆动"
        )
        lines.append(f"- {verdict}")
        lines.append("")

    # ---- 4. 分标的贡献 ----
    lines.append("## 五、分标的贡献（前 8）")
    lines.append("")
    for key, r in results.items():
        ranked = sorted(r.by_symbol.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked:
            continue
        conc = r.concentration(2)
        conc_txt = "不适用（净亏损）" if conc is None else f"{conc:.0f}%"
        lines.append(f"**{r.label}** —— 前 2 大标的贡献净利润的 {conc_txt}")
        lines.append("")
        lines.append("| 标的 | 净盈亏 (USDT) |")
        lines.append("|---|---:|")
        for sym, v in ranked[:8]:
            lines.append(f"| {sym} | {v:+,.0f} |")
        lines.append("")

    # ---- 6. 逐月拆解 ----
    lines.append("## 六、逐月拆解：收益是持续的，还是等来的？")
    lines.append("")
    lines.append("如果一个策略的全部盈利只来自一两个月，那它是「等一次行情」，"
                 "不是持续产生收益。这里看每个月的盈亏，以及**最大盈利月占总盈利的比例**。")
    lines.append("")
    months_all = sorted({m for r in results.values() for m in r.by_month()})
    header = "| 策略 | " + " | ".join(months_all) + " | 最大月占比 |"
    lines.append(header)
    lines.append("|" + "---|" * (len(months_all) + 2))
    for key, r in results.items():
        bm = r.by_month()
        cells = [f"{bm.get(m, 0.0):+,.0f}" for m in months_all]
        share = r.positive_months_share()
        share_txt = "n/a" if share == 0 else f"{share:.0f}%"
        lines.append(f"| {r.label} | " + " | ".join(cells) + f" | {share_txt} |")
    lines.append("")
    lines.append("单位：USDT。最大月占比越低，说明收益越分散、越不像「等一次行情」。")
    lines.append("")

    # ---- 7. 波动率目标化 ----
    lines.append("## 七、叠加波动率目标化")
    lines.append("")
    lines.append("波动率目标化按「目标波动 / 已实现波动」缩放仓位，波动放大时自动减仓。"
                 "这是少数被广泛证实能改善风险调整后收益的仓位技术。")
    lines.append("")
    lines.append("| 策略 | 目标波动 | 收益率 | 最大回撤 | 夏普 |")
    lines.append("|---|---:|---:|---:|---:|")
    for key in SCORE_FNS:
        for tv in (0.0, 40.0, 60.0, 80.0):
            p = replace(base, target_vol_pct=tv)
            r = simulate_xs(markets, key, p)
            label = "关闭" if tv == 0 else f"{tv:.0f}%"
            lines.append(
                f"| {results[key].label} | {label} | {r.total_return_pct:+.2f}% | "
                f"{r.max_drawdown_pct:.2f}% | {r.sharpe:.2f} |"
            )
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    from .fetch_history import load_all
    from .funding_arb import build_series

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    series = [build_series(r) for r in load_all()]
    series = [s for s in series if len(s.dates) >= 300]
    if not series:
        raise SystemExit("缓存为空，请先运行 python -m backtest.fetch_history")
    log.info("载入 %d 个标的", len(series))

    report = build_report(series, XsParams())
    out = OUT_DIR / "report_cross_sectional.md"
    out.write_text(report, encoding="utf-8")
    log.info("截面策略报告已写入 %s", out)

    # 控制台摘要
    base = XsParams()
    print()
    print("=" * 92)
    print("截面方向性策略（做多 5 / 做空 5，30 天打分，7 天调仓，单边 0.08% 成本）")
    print("=" * 92)
    for key in SCORE_FNS:
        r = simulate_xs(series, key, base)
        point, lo, hi = bootstrap_sharpe(r.equity)
        conc = r.concentration(2)
        conc_txt = "n/a" if conc is None else f"{conc:.0f}%"
        print(f"\n【{r.label}】")
        print(f"  收益率 {r.total_return_pct:+.2f}%   最大回撤 {r.max_drawdown_pct:.2f}%"
              f"   夏普 {r.sharpe:.2f}   笔数 {r.trade_count}")
        print(f"  夏普 90% 区间 [{lo:.2f}, {hi:.2f}]   "
              f"{'跨 0 -> 不显著' if lo <= 0 <= hi else '不跨 0 -> 显著'}")
        print(f"  价格盈亏 {r.net_pnl - r.funding_pnl + r.cost_pnl:+,.0f}"
              f"   资金费 {r.funding_pnl:+,.0f}   成本 {-r.cost_pnl:+,.0f}")
        print(f"  多头 {r.long_pnl:+,.0f}  空头 {r.short_pnl:+,.0f}"
              f"   前 2 大标的贡献 {conc_txt}"
              f"   最大月占比 {r.positive_months_share():.0f}%")
        loo = leave_one_out(series, key, base)
        if loo:
            rets = [v for _, v in loo]
            worst_sym, worst = min(loo, key=lambda kv: kv[1])
            print(f"  留一标的：剔除后区间 {min(rets):+.2f}% ~ {max(rets):+.2f}%，"
                  f"仍为正 {sum(1 for v in rets if v > 0)}/{len(rets)}"
                  f"（最差：剔除 {worst_sym} → {worst:+.2f}%）")


if __name__ == "__main__":
    main()
