"""权益曲线与收益序列的通用风险指标。

被 backtest.funding_arb 与 backtest.perp_signals 共用，避免两处各写一套口径。
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence

DAYS_PER_YEAR = 365.0


def daily_returns(equity: Sequence[float]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(equity, equity[1:]):
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def max_drawdown_pct(equity: Sequence[float]) -> float:
    """最大回撤（正数百分比）。"""
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100.0)
    return worst


def cagr_pct(initial: float, final: float, span_days: int) -> float:
    if span_days <= 0 or initial <= 0 or final <= 0:
        return 0.0
    return ((final / initial) ** (DAYS_PER_YEAR / span_days) - 1.0) * 100.0


def volatility_pct(returns: Sequence[float], periods_per_year: float = DAYS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    return statistics.pstdev(returns) * math.sqrt(periods_per_year) * 100.0


def sharpe(returns: Sequence[float], periods_per_year: float = DAYS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    sd = statistics.pstdev(returns)
    if sd == 0:
        return 0.0
    return statistics.fmean(returns) / sd * math.sqrt(periods_per_year)


def calmar(cagr: float, max_dd: float) -> float:
    return cagr / max_dd if max_dd > 0 else 0.0


def sortino(returns: Sequence[float], periods_per_year: float = DAYS_PER_YEAR) -> float:
    """只惩罚下行波动。"""
    if len(returns) < 2:
        return 0.0
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    dd = statistics.pstdev(downside)
    if dd == 0:
        return 0.0
    return statistics.fmean(returns) / dd * math.sqrt(periods_per_year)
