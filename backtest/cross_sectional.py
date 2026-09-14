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
import hashlib
import json
import math
import random
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
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
from src.xs_lowvol_spec import (
    CONTROL_RULES,
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
    SHADOW_RULES,
    SHADOW_SPEC_HASH,
    SHADOW_STRATEGY_ID,
    validate_frozen_spec,
)
from src.evidence import EVIDENCE_GENERATOR, EVIDENCE_SCHEMA_VERSION, stale_evidence, utc_now_iso

log = logging.getLogger("backtest.xs")
OUT_DIR = Path(__file__).resolve().parent

BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_SEED = 20260913


@dataclass
class XsParams:
    capital: float = 10_000.0
    k_long: int = CONTROL_RULES.k_long                 # 做多几个
    k_short: int = CONTROL_RULES.k_short                # 做空几个
    lookback: int = CONTROL_RULES.lookback_days         # 打分窗口（天）
    rebalance_days: int = CONTROL_RULES.rebalance_days # 调仓间隔
    taker_fee_pct: float = CONTROL_RULES.taker_fee_pct  # 单边手续费
    slippage_pct: float = CONTROL_RULES.slippage_pct    # 单边滑点
    # 波动率目标化（0 表示关闭）
    target_vol_pct: float = 0.0     # 目标年化波动（%）
    vol_window: int = 30
    max_scale: float = CONTROL_RULES.max_scale  # Control 不启用波动率缩放
    min_volume_usdt_24h: float = CONTROL_RULES.min_quote_volume_usdt
    exec_lag_days: int = CONTROL_RULES.execution_lag_days
    min_universe_size: int = CONTROL_RULES.min_symbols
    strategy_id: str = CONTROL_STRATEGY_ID
    spec_hash: str = CONTROL_SPEC_HASH

    @classmethod
    def for_variant(cls, variant: str = "control") -> "XsParams":
        if variant in ("shadow", "vt80-shadow"):
            return cls(
                target_vol_pct=SHADOW_RULES.target_vol_pct,
                max_scale=SHADOW_RULES.max_scale,
                strategy_id=SHADOW_STRATEGY_ID,
                spec_hash=SHADOW_SPEC_HASH,
            )
        return cls()

    def slot_notional(self) -> float:
        n = self.k_long + self.k_short
        return self.capital / n if n > 0 else 0.0

    def cost_rate(self) -> float:
        return (self.taker_fee_pct + self.slippage_pct) / 100.0


@dataclass(frozen=True)
class XsRebalanceAudit:
    """一次成功 target rebalance 的经济审计。"""

    signal_day: date
    execution_day: date
    targets: dict[str, int]
    scale: float
    changed_symbols: tuple[str, ...]
    resized_symbols: tuple[str, ...]
    turnover_notional: float


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
    window = s.dates[i - n:i + 1]
    if any((later - earlier).days != 1 for earlier, later in zip(window, window[1:])):
        return None
    return s.dates[i - n]


def score_momentum(
    markets: dict[str, MarketSeries], day: date, lookback: int
) -> dict[str, float]:
    """过去 lookback 日收益率。涨得多的分数高。"""
    out: dict[str, float] = {}
    for sym, s in markets.items():
        if not s.is_active_on(day):
            continue
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
        if not s.is_active_on(day):
            continue
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
        if not s.is_active_on(day):
            continue
        i = s.index_of.get(day)
        if i is None or i - lookback < 0:
            continue
        window_days = s.dates[i - lookback:i + 1]
        if any(
            (later - earlier).days != 1
            for earlier, later in zip(window_days, window_days[1:])
        ):
            continue
        try:
            prices = [s.perp_close[s.dates[j]] for j in range(i - lookback, i + 1)]
        except KeyError:
            continue
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
    strategy_id: str = CONTROL_STRATEGY_ID
    spec_hash: str = CONTROL_SPEC_HASH
    dates: list[date] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    long_equity: list[float] = field(default_factory=list)
    short_equity: list[float] = field(default_factory=list)
    net_pnl: float = 0.0
    cost_pnl: float = 0.0
    funding_pnl: float = 0.0
    trade_count: int = 0
    by_symbol: dict[str, float] = field(default_factory=dict)
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    avg_gross_exposure_pct: float = 0.0
    no_signal_reasons: list[str] = field(default_factory=list)
    complete: bool = True
    turnover_notional: float = 0.0
    resized_symbols: set[str] = field(default_factory=set)
    targets_by_signal_day: dict[date, dict[str, int]] = field(default_factory=dict)
    scales_by_signal_day: dict[date, float] = field(default_factory=dict)
    rebalance_audits: list[XsRebalanceAudit] = field(default_factory=list)

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
    def long_sharpe(self) -> float:
        return sharpe(daily_returns(self.long_equity))

    @property
    def short_sharpe(self) -> float:
        return sharpe(daily_returns(self.short_equity))

    @property
    def long_short_contribution_ratio(self) -> dict[str, float]:
        if self.net_pnl == 0:
            return {"long": 0.0, "short": 0.0}
        return {
            "long": self.long_pnl / self.net_pnl * 100.0,
            "short": self.short_pnl / self.net_pnl * 100.0,
        }

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
    """跑一个截面策略；xs_lowvol 的参数来源于冻结 spec。"""
    score_fn = SCORE_FNS[strategy]
    by_sym = {m.symbol: m for m in markets}
    result = XsResult(
        strategy=strategy,
        label=strategy,
        params=p,
        strategy_id=p.strategy_id,
        spec_hash=p.spec_hash,
    )

    # 统一交易日轴；eligible universe 在每个 signal day 重新判定。
    all_days = sorted({d for m in markets for d in m.dates})
    if not all_days:
        return result

    slot = p.slot_notional()
    cost_rate = p.cost_rate()
    positions: dict[str, int] = {}
    mark_price: dict[str, float] = {}
    mark_day: dict[str, date] = {}
    notional: dict[str, float] = {}
    realized = 0.0
    cost_total = 0.0
    last_rebalance: date | None = None
    long_realized = 0.0
    short_realized = 0.0
    exposure_sum = 0.0

    def mark_positions(day: date) -> bool:
        """按完成收盘逐日结算价格变动和已结算 funding。"""
        nonlocal realized, long_realized, short_realized
        ok = True
        for sym, direction in positions.items():
            price = by_sym[sym].perp_close.get(day)
            if price is None:
                result.complete = False
                result.no_signal_reasons.append(f"NO_SIGNAL: missing mark price {sym} {day}")
                ok = False
                continue
            previous_price = mark_price[sym]
            previous_day = mark_day[sym]
            if (day - previous_day).days != 1:
                result.complete = False
                result.no_signal_reasons.append(
                    f"NO_SIGNAL: non-consecutive mark data {sym} {previous_day} -> {day}"
                )
                ok = False
                continue
            move = direction * notional[sym] * (price / previous_price - 1.0)
            settlements, rate_sum = by_sym[sym].funding_sum_between(
                _end_of_day_ms(previous_day), _end_of_day_ms(day)
            )
            if settlements <= 0:
                result.complete = False
                result.no_signal_reasons.append(
                    f"NO_SIGNAL: missing funding coverage {sym} {previous_day} -> {day}"
                )
                ok = False
                continue
            funding = -direction * notional[sym] * rate_sum
            leg = move + funding
            realized += leg
            result.funding_pnl += funding
            result.by_symbol[sym] = result.by_symbol.get(sym, 0.0) + leg
            if direction > 0:
                result.long_pnl += leg
                long_realized += leg
            else:
                result.short_pnl += leg
                short_realized += leg
            mark_price[sym] = price
            mark_day[sym] = day
        return ok

    def exit_leg(sym: str, day: date) -> bool:
        """结算一条腿；缺少完成收盘价时不静默估算。"""
        nonlocal realized, cost_total, long_realized, short_realized
        if mark_day.get(sym) != day:
            result.complete = False
            result.no_signal_reasons.append(f"NO_SIGNAL: missing exit price {sym} {day}")
            return False
        direction = positions[sym]
        cost = notional[sym] * cost_rate
        realized -= cost
        result.by_symbol[sym] = result.by_symbol.get(sym, 0.0) - cost
        if direction > 0:
            result.long_pnl -= cost
            long_realized -= cost
        else:
            result.short_pnl -= cost
            short_realized -= cost
        cost_total += cost
        positions.pop(sym)
        mark_price.pop(sym)
        mark_day.pop(sym)
        notional.pop(sym)
        return True

    equity: list[float] = []
    long_equity: list[float] = []
    short_equity: list[float] = []

    for day in all_days:
        mark_ok = mark_positions(day)
        if last_rebalance is None or (day - last_rebalance).days >= p.rebalance_days:
            signal_day = day - timedelta(days=p.exec_lag_days)
            if signal_day in all_days:
                scores = score_fn(by_sym, signal_day, p.lookback)
                liquid = {
                    sym: sc for sym, sc in scores.items()
                    if by_sym[sym].perp_volume.get(signal_day, 0.0)
                    >= p.min_volume_usdt_24h
                }
                if strategy == "xs_lowvol":
                    eligible = {
                        sym for sym, market in by_sym.items()
                        if market.is_active_on(signal_day)
                        and market.perp_volume.get(signal_day, 0.0)
                        >= p.min_volume_usdt_24h
                    }
                    missing_history = sorted(eligible - set(liquid))
                else:
                    missing_history = []
                required = max(p.min_universe_size, p.k_long + p.k_short)
                if missing_history:
                    # 初始 warm-up 期本来就没有 lookback；只有在已有部分
                    # score、另一部分 eligible 数据缺失时才把回测标成不完整。
                    if liquid:
                        result.complete = False
                    result.no_signal_reasons.append(
                        "NO_SIGNAL: incomplete eligible history "
                        + ",".join(missing_history)
                    )
                elif len(liquid) < required:
                    result.no_signal_reasons.append(
                        f"NO_SIGNAL: insufficient universe {signal_day} "
                        f"({len(liquid)} < {required})"
                    )
                else:
                    if strategy == "xs_lowvol":
                        # 与 rank_lowvol 一致：波动率升序、symbol 升序平局，
                        # 空头腿取尾部后反转。
                        ranked = sorted(liquid.items(), key=lambda kv: (-kv[1], kv[0]))
                    else:
                        ranked = sorted(
                            liquid.items(), key=lambda kv: (kv[1], kv[0]), reverse=True
                        )
                    targets: dict[str, int] = {
                        sym: 1 for sym, _ in ranked[: p.k_long]
                    }
                    targets.update({sym: -1 for sym, _ in ranked[-p.k_short:]})
                    changed = {
                        sym for sym in positions
                        if sym not in targets or targets[sym] != positions[sym]
                    }
                    changed.update(sym for sym in targets if sym not in positions)
                    port_scale = 1.0
                    if p.strategy_id == SHADOW_STRATEGY_ID:
                        port_scale = _portfolio_vol_scale(
                            by_sym, list(liquid), signal_day, p
                        )
                    target_notional = slot * port_scale
                    resized = {
                        sym for sym, direction in targets.items()
                        if sym in positions
                        and positions[sym] == direction
                        and not math.isclose(
                            notional[sym], target_notional, rel_tol=0.0, abs_tol=1e-12
                        )
                    }
                    affected = changed | resized
                    missing_prices = [
                        sym for sym in affected
                        if by_sym[sym].perp_close.get(day) is None
                    ]
                    if missing_prices or not mark_ok:
                        result.complete = False
                        if missing_prices:
                            result.no_signal_reasons.append(
                                "NO_SIGNAL: missing execution prices "
                                + ",".join(sorted(missing_prices))
                            )
                    else:
                        turnover_notional = 0.0
                        for sym, direction in list(positions.items()):
                            if targets.get(sym) == direction:
                                turnover_notional += abs(
                                    target_notional - notional[sym]
                                )
                            else:
                                turnover_notional += notional[sym]
                        for sym, direction in targets.items():
                            if sym not in positions or positions[sym] != direction:
                                turnover_notional += target_notional

                        transition_ok = True
                        for sym in list(positions):
                            if sym in changed:
                                transition_ok = exit_leg(sym, day) and transition_ok
                        if transition_ok:
                            for sym in resized:
                                direction = positions[sym]
                                resize_cost = abs(target_notional - notional[sym]) * cost_rate
                                realized -= resize_cost
                                cost_total += resize_cost
                                result.by_symbol[sym] = (
                                    result.by_symbol.get(sym, 0.0) - resize_cost
                                )
                                if direction > 0:
                                    result.long_pnl -= resize_cost
                                    long_realized -= resize_cost
                                else:
                                    result.short_pnl -= resize_cost
                                    short_realized -= resize_cost
                                notional[sym] = target_notional
                            for sym, direction in targets.items():
                                if sym in positions:
                                    continue
                                price = by_sym[sym].perp_close[day]
                                positions[sym] = direction
                                mark_price[sym] = price
                                mark_day[sym] = day
                                notional[sym] = target_notional
                                cost = notional[sym] * cost_rate
                                realized -= cost
                                cost_total += cost
                                if direction > 0:
                                    result.long_pnl -= cost
                                    long_realized -= cost
                                else:
                                    result.short_pnl -= cost
                                    short_realized -= cost
                                result.by_symbol[sym] = result.by_symbol.get(sym, 0.0) - cost
                                result.trade_count += 1
                            result.turnover_notional += turnover_notional
                            result.resized_symbols.update(resized)
                            result.targets_by_signal_day[signal_day] = dict(targets)
                            result.scales_by_signal_day[signal_day] = port_scale
                            result.rebalance_audits.append(
                                XsRebalanceAudit(
                                    signal_day=signal_day,
                                    execution_day=day,
                                    targets=dict(targets),
                                    scale=port_scale,
                                    changed_symbols=tuple(sorted(changed)),
                                    resized_symbols=tuple(sorted(resized)),
                                    turnover_notional=turnover_notional,
                                )
                            )
                            last_rebalance = day

        exposure_sum += sum(notional.values())
        equity.append(p.capital + realized)
        long_equity.append(p.capital / 2.0 + long_realized)
        short_equity.append(p.capital / 2.0 + short_realized)

    result.dates = all_days
    result.equity = equity
    result.long_equity = long_equity
    result.short_equity = short_equity
    result.net_pnl = realized
    result.cost_pnl = cost_total
    result.avg_gross_exposure_pct = (
        exposure_sum / len(all_days) / p.capital * 100.0
        if all_days and p.capital
        else 0.0
    )
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
            if not s.is_active_on(cur):
                continue
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


def regime_attribution(
    strategy_returns: Sequence[float], market_returns: Sequence[float]
) -> dict[str, dict[str, float]]:
    """按大盘日收益正负拆分组合表现。"""
    out: dict[str, dict[str, float]] = {}
    for name, predicate in (("bull", lambda value: value > 0), ("bear", lambda value: value <= 0)):
        values = [
            float(strategy)
            for strategy, market in zip(strategy_returns, market_returns)
            if predicate(float(market))
        ]
        out[name] = {
            "days": float(len(values)),
            "mean_daily_return_pct": statistics.fmean(values) * 100.0 if values else 0.0,
            "cumulative_return_pct": (math.prod(1.0 + value for value in values) - 1.0) * 100.0
            if values else 0.0,
        }
    return out


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


def block_bootstrap(
    returns: Sequence[float],
    block_length: int = 4 * 7,
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """对日收益按连续 block 重抽样，准备 M1 所需的统计接口。

    ``mean_return_pct`` 是年化算术平均日收益，区间为 90%；
    ``probability_return_gt_zero`` 是同一观察长度下累计收益大于零的比例。
    M0 只用 fixture/已有 equity 调用这个接口，不据此选择参数。
    """
    if block_length <= 0:
        raise ValueError("block_length 必须为正")
    values = list(returns)
    if len(values) < block_length:
        return {
            "block_length": block_length,
            "rounds": 0,
            "mean_return_ci": (0.0, 0.0),
            "sharpe_ci": (0.0, 0.0),
            "probability_return_gt_zero": 0.0,
        }
    rng = random.Random(seed)
    n = len(values)
    mean_samples: list[float] = []
    sharpe_samples: list[float] = []
    positive = 0
    for _ in range(rounds):
        draw: list[float] = []
        while len(draw) < n:
            start = rng.randrange(0, n - block_length + 1)
            draw.extend(values[start:start + block_length])
        draw = draw[:n]
        mean_samples.append(statistics.fmean(draw) * DAYS_PER_YEAR * 100.0)
        sd = statistics.pstdev(draw)
        sharpe_samples.append(
            statistics.fmean(draw) / sd * math.sqrt(DAYS_PER_YEAR) if sd > 0 else 0.0
        )
        if math.prod(1.0 + value for value in draw) > 1.0:
            positive += 1

    def interval(samples: list[float]) -> tuple[float, float]:
        samples.sort()
        lo = samples[min(len(samples) - 1, int(len(samples) * 0.05))]
        hi = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
        return lo, hi

    return {
        "block_length": block_length,
        "rounds": rounds,
        "mean_return_ci": interval(mean_samples),
        "sharpe_ci": interval(sharpe_samples),
        "probability_return_gt_zero": positive / rounds if rounds else 0.0,
    }


def block_bootstrap_metrics(
    equity: Sequence[float],
    block_length: int = 4 * 7,
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """以 equity 曲线为输入的 block bootstrap 便捷入口。"""
    return block_bootstrap(daily_returns(equity), block_length, rounds, seed)


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


def dataset_fingerprint(markets: Sequence[MarketSeries]) -> str:
    """对回测实际使用的完整 MarketSeries 生成确定性 SHA-256。"""
    def series_map(values: dict[date, float]) -> list[list[Any]]:
        return [
            [day.isoformat(), float(value)]
            for day, value in sorted(values.items())
        ]

    payload = []
    for market in sorted(markets, key=lambda item: item.symbol):
        funding = [
            [
                int(timestamp),
                float(rate),
                float(market.funding_interval[index])
                if index < len(market.funding_interval)
                else 8.0,
            ]
            for index, (timestamp, rate) in enumerate(
                zip(market.funding_ts, market.funding_rate)
            )
        ]
        payload.append(
            {
                "symbol": market.symbol,
                "dates": [day.isoformat() for day in market.dates],
                "perp_close": series_map(market.perp_close),
                "spot_close": series_map(market.spot_close),
                "perp_volume": series_map(market.perp_volume),
                "funding": funding,
                "quote_asset": market.quote_asset,
                "contract_type": market.contract_type,
                "status": market.status,
                "listed_from": market.listed_from.isoformat()
                if market.listed_from
                else None,
                "delisted_at": market.delisted_at.isoformat()
                if market.delisted_at
                else None,
            }
        )
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _current_code_commit() -> str | None:
    """只有 clean Git worktree 才把 commit 作为 CURRENT provenance。"""
    repo_root = Path(__file__).resolve().parent.parent
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return rev if len(rev) == 40 and not dirty else None


def generate_evidence(
    markets: Sequence[MarketSeries],
    base: XsParams | None = None,
    *,
    generated_at: str | None = None,
    code_commit_sha: str | None = None,
) -> dict[str, Any]:
    """由当前回测代码自动生成 evidence；无法完整计算则生成 stale artifact。"""
    base = base or XsParams()
    if base.strategy_id != CONTROL_STRATEGY_ID or base.spec_hash != CONTROL_SPEC_HASH:
        raise ValueError("evidence generator 只能生成冻结 Control artifact")
    validate_frozen_spec()
    frozen_fields = (
        "lookback",
        "k_long",
        "k_short",
        "rebalance_days",
        "taker_fee_pct",
        "slippage_pct",
        "min_volume_usdt_24h",
        "exec_lag_days",
        "min_universe_size",
        "target_vol_pct",
        "max_scale",
    )
    expected_fields = {
        "lookback": CONTROL_RULES.lookback_days,
        "k_long": CONTROL_RULES.k_long,
        "k_short": CONTROL_RULES.k_short,
        "rebalance_days": CONTROL_RULES.rebalance_days,
        "taker_fee_pct": CONTROL_RULES.taker_fee_pct,
        "slippage_pct": CONTROL_RULES.slippage_pct,
        "min_volume_usdt_24h": CONTROL_RULES.min_quote_volume_usdt,
        "exec_lag_days": CONTROL_RULES.execution_lag_days,
        "min_universe_size": CONTROL_RULES.min_symbols,
        "target_vol_pct": 0.0,
        "max_scale": 1.0,
    }
    if any(getattr(base, field) != expected_fields[field] for field in frozen_fields):
        raise ValueError("evidence generator 只能使用冻结 Control 参数")
    generated_at = generated_at or utc_now_iso()
    dataset_sha256 = dataset_fingerprint(markets)
    dataset_id = f"market-series-{dataset_sha256[:16]}"
    code_commit_sha = code_commit_sha or _current_code_commit()
    if not markets:
        return stale_evidence(
            strategy_id=CONTROL_STRATEGY_ID,
            spec_hash=CONTROL_SPEC_HASH,
            reason="没有可用的本地 MarketSeries；未下载新的历史数据",
            generated_at=generated_at,
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
            code_commit_sha=code_commit_sha,
        )

    result = simulate_xs(markets, "xs_lowvol", base)
    if not result.complete:
        return stale_evidence(
            strategy_id=CONTROL_STRATEGY_ID,
            spec_hash=CONTROL_SPEC_HASH,
            reason="当前冻结规则回测数据不完整："
            + (result.no_signal_reasons[0] if result.no_signal_reasons else "unknown"),
            generated_at=generated_at,
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
            code_commit_sha=code_commit_sha,
        )
    if not result.targets_by_signal_day:
        return stale_evidence(
            strategy_id=CONTROL_STRATEGY_ID,
            spec_hash=CONTROL_SPEC_HASH,
            reason="当前数据未完成任何冻结规则 rebalance",
            generated_at=generated_at,
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
            code_commit_sha=code_commit_sha,
        )
    if code_commit_sha is None:
        return stale_evidence(
            strategy_id=CONTROL_STRATEGY_ID,
            spec_hash=CONTROL_SPEC_HASH,
            reason="无法取得 clean 当前代码 commit SHA",
            generated_at=generated_at,
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
            code_commit_sha=None,
        )

    point, lower, upper = bootstrap_sharpe(result.equity)
    loo = leave_one_out(markets, "xs_lowvol", base)
    months = result.by_month()
    market_returns = market_daily_returns(markets, result.dates)
    beta, alpha, r2 = alpha_beta(daily_returns(result.equity), market_returns)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "artifact_type": "xs_lowvol_evidence",
        "generator": EVIDENCE_GENERATOR,
        "strategy": "xs_lowvol",
        "strategy_id": CONTROL_STRATEGY_ID,
        "variant": "Control",
        "spec_hash": CONTROL_SPEC_HASH,
        "status": "CURRENT",
        "dataset_id": dataset_id,
        "dataset_sha256": dataset_sha256,
        "dataset_fingerprint": {
            "algorithm": "sha256",
            "sha256": dataset_sha256,
            "market_count": len(markets),
        },
        "code_commit_sha": code_commit_sha,
        "generated_at": generated_at,
        "provenance": {
            "dataset_id": dataset_id,
            "dataset_sha256": dataset_sha256,
            "code_commit_sha": code_commit_sha,
            "generated_at": generated_at,
            "generator": EVIDENCE_GENERATOR,
        },
        "date_range": {
            "start": min(result.dates).isoformat(),
            "end": max(result.dates).isoformat(),
        },
        "universe": {
            "eligible_symbols": len(markets),
            "selection": "current MarketSeries eligible under frozen liquidity rule",
            "quote_volume_threshold_usdt": CONTROL_RULES.min_quote_volume_usdt,
        },
        "total_return_pct": result.total_return_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe": point,
        "sharpe_ci90": [lower, upper],
        "bootstrap_interval": {
            "method": "iid",
            "confidence": 0.9,
            "metric": "sharpe",
            "lower": lower,
            "upper": upper,
        },
        "funding": {
            "pnl_usdt": result.funding_pnl,
            "source": "real_settled_funding_events",
        },
        "costs": {
            "pnl_usdt": -result.cost_pnl,
            "taker_pct_per_side": CONTROL_RULES.taker_fee_pct,
            "slippage_pct_per_side": CONTROL_RULES.slippage_pct,
        },
        "long_leg": {"pnl_usdt": result.long_pnl, "sharpe": result.long_sharpe},
        "short_leg": {"pnl_usdt": result.short_pnl, "sharpe": result.short_sharpe},
        "long_short_contribution_ratio": result.long_short_contribution_ratio,
        "bull_bear_regime_attribution": regime_attribution(
            daily_returns(result.equity), market_returns
        ),
        "months_positive": sum(1 for value in months.values() if value > 0),
        "months_total": len(months),
        "max_month_share_pct": result.positive_months_share(),
        "beta": beta,
        "annual_alpha_pct": alpha,
        "r_squared": r2,
        "loo_positive": sum(1 for _, value in loo if value > 0),
        "loo_total": len(loo),
        "top2_concentration_pct": result.concentration(2) or 0.0,
    }


def write_evidence_artifact(
    markets: Sequence[MarketSeries],
    path: str | Path = Path(__file__).resolve().parent.parent
    / "research"
    / "evidence"
    / "XS-LOWVOL-V1.json",
    base: XsParams | None = None,
) -> dict[str, Any]:
    """报告生成入口：计算并持久化 evidence artifact。"""
    artifact = generate_evidence(markets, base)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return artifact


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def comparison_table(results: dict[str, XsResult]) -> str:
    cols = ["策略", "笔数", "收益率", "最大回撤", "夏普", "索提诺",
            "价格盈亏", "资金费", "交易成本", "多头", "空头", "多头夏普", "空头夏普"]
    widths = [14, 6, 10, 11, 8, 9, 11, 10, 10, 10, 10, 10, 10]
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
            f"{r.long_sharpe:.2f}",
            f"{r.short_sharpe:.2f}",
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
    lines.append("## 规则身份")
    lines.append("")
    lines.append(
        f"- Control：`{CONTROL_STRATEGY_ID}`，spec SHA-256 `{CONTROL_SPEC_HASH}`；"
        "volatility targeting 关闭。"
    )
    lines.append(
        f"- Shadow：`{SHADOW_STRATEGY_ID}`，spec SHA-256 `{SHADOW_SPEC_HASH}`；"
        "仅增加 target_vol_pct=80、max_scale=3.0。"
    )
    lines.append("")

    # ---- 1. 主结果 ----
    results: dict[str, XsResult] = {}
    for key in SCORE_FNS:
        results[key] = simulate_xs(markets, key, base)

    lines.append("## 一、主结果（未做任何参数寻优）")
    lines.append("")
    lines.append(f"- 标的数：{len(markets)}")
    lines.append(f"- Control 参数：做多 {CONTROL_RULES.k_long} 个 / 做空 {CONTROL_RULES.k_short} 个，"
                 f"打分窗口 {CONTROL_RULES.lookback_days} 天，每 {CONTROL_RULES.rebalance_days} 天调仓")
    lines.append(f"- 成本：单边 taker {CONTROL_RULES.taker_fee_pct}% + 滑点 {CONTROL_RULES.slippage_pct}%")
    lines.append("")
    lines.append(comparison_table(results))
    lines.append("")

    lines.append("## 二、永久归因内容（Control）")
    lines.append("")
    lowvol = results["xs_lowvol"]
    ratio = lowvol.long_short_contribution_ratio
    lines.append(
        f"策略身份：`{lowvol.strategy_id}`，spec hash `{lowvol.spec_hash}`。"
    )
    lines.append(
        f"Long leg PnL：{lowvol.long_pnl:+,.2f} USDT；"
        f"Short leg PnL：{lowvol.short_pnl:+,.2f} USDT。"
    )
    lines.append(
        f"Long leg Sharpe：{lowvol.long_sharpe:.2f}；"
        f"Short leg Sharpe：{lowvol.short_sharpe:.2f}。"
    )
    lines.append(
        f"Long/Short contribution ratio：{ratio['long']:.1f}% / {ratio['short']:.1f}%。"
    )
    lines.append("")

    # ---- 3. 统计显著性 ----
    lines.append("## 三、统计显著性：这些夏普能信吗？")
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

    # ---- 4. alpha / beta 分解 ----
    all_days = sorted({d for m in markets for d in m.dates})
    mkt = market_daily_returns(markets, all_days)
    lines.append("## 四、alpha / beta 分解：真有 alpha，还是在偷偷押方向？")
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

    lines.append("## 五、Bull / Bear regime attribution（Control）")
    lines.append("")
    regime = regime_attribution(daily_returns(lowvol.equity), mkt)
    lines.append("| Regime | Days | Mean daily return | Cumulative return |")
    lines.append("|---|---:|---:|---:|")
    for name in ("bull", "bear"):
        item = regime[name]
        lines.append(
            f"| {name.title()} | {int(item['days'])} | "
            f"{item['mean_daily_return_pct']:+.4f}% | "
            f"{item['cumulative_return_pct']:+.2f}% |"
        )
    lines.append("")

    # ---- 6. 留一标的法 ----
    lines.append("## 六、留一标的法：结果是不是靠一两个标的撑起来的？")
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

    # ---- 7. 分标的贡献 ----
    lines.append("## 七、分标的贡献（前 8）")
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

    # ---- 8. 逐月拆解 ----
    lines.append("## 八、逐月拆解：收益是持续的，还是等来的？")
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

    # ---- 9. Block Bootstrap ----
    lines.append("## 九、Block Bootstrap（接口冻结，4 周 block）")
    lines.append("")
    lines.append("M0 只建立统计接口，不用新结果选择参数。")
    lines.append("")
    lines.append("| 策略 | Block | Mean return CI (annualized %) | Sharpe CI | P(return > 0) |")
    lines.append("|---|---:|---:|---:|---:|")
    for item in (lowvol,):
        stats = block_bootstrap_metrics(item.equity, block_length=4 * 7)
        mean_ci = stats["mean_return_ci"]
        sharpe_ci = stats["sharpe_ci"]
        lines.append(
            f"| {item.strategy_id} | {stats['block_length']}d | "
            f"[{mean_ci[0]:+.2f}, {mean_ci[1]:+.2f}] | "
            f"[{sharpe_ci[0]:+.2f}, {sharpe_ci[1]:+.2f}] | "
            f"{stats['probability_return_gt_zero']:.3f} |"
        )
    lines.append("")

    # ---- 10. Control / Shadow ----
    lines.append("## 十、Control 与 VT80 Shadow")
    lines.append("")
    lines.append("Shadow 完全复用 Control 信号，只增加 target_vol_pct=80 与 max_scale=3.0。")
    lines.append("")
    lines.append("| Strategy ID | Target vol | Spec hash | 收益率 | 最大回撤 | 夏普 |")
    lines.append("|---|---:|---|---:|---:|---:|")
    shadow = simulate_xs(markets, "xs_lowvol", XsParams.for_variant("shadow"))
    for item, label in ((lowvol, "0%"), (shadow, "80%")):
        lines.append(
            f"| {item.strategy_id} | {label} | `{item.spec_hash}` | "
            f"{item.total_return_pct:+.2f}% | {item.max_drawdown_pct:.2f}% | {item.sharpe:.2f} |"
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

    evidence = write_evidence_artifact(series, base=XsParams())
    log.info("XS-LOWVOL evidence 已由当前报告生成器写入（%s）", evidence["status"])
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
