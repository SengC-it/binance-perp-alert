"""截面低波动信号扫描 —— 唯一通过回测验证的方向性策略。

为什么是这一个
==============
`backtest/cross_sectional.py` 测了三个预先指定的截面策略（不做参数寻优）：

    策略          收益率      最大回撤    夏普    bootstrap 90% 区间   留一标的
    xs_momentum   -38.07%    43.31%    -1.06   [-2.71, 0.62]       0/73 为正
    xs_carry      +27.02%    13.45%    +1.29   [-0.40, 2.85]       73/73 为正
    xs_lowvol     +63.79%    11.11%    +2.36   [0.76, 4.05]        73/73 为正

只有 `xs_lowvol` 同时满足四项检验：

1. 夏普 90% 置信区间不跨 0（统计显著）
2. 逐个剔除任一标的，73/73 仍为正（不依赖单个标的）
3. 前 2 大标的只贡献 24% 的利润（不集中）
4. 12 个月里 11 个月为正，最大月只占 19%（不是等一次行情）
5. beta 仅 -0.17、年化 alpha +38.68%（不是伪装的方向押注）
6. 已计入资金费（净拖累 -1,018 USDT）与交易成本

`xs_carry` 虽然为正，但置信区间跨 0，属于「有迹象、未验证」，不构成自动化依据。
`xs_momentum` 直接为负，排除。

策略逻辑
========
在每个调仓时点，按「过去 N 日已实现波动率」给全部标的排序：

    做多波动率最低的 K 个  /  做空波动率最高的 K 个

组合接近市场中性。经济上的解释是低波动异象（低风险资产的风险调整后收益被系统性低估），
该异象在传统资产有大量文献支持（如 Frazzini & Pedersen 的 betting-against-beta），
但**加密市场的证据仍然稀薄**。

必须知道的风险
==============
- 样本只有一年，且是深度熊市。低波动异象在急涨行情里会跑输。
- **做空高波动山寨币有挤空风险**：单日暴涨可能让空头腿巨亏。日线回测看不到盘中挤空。
- 未建模：借币成本、规模扩大后的滑点恶化、交易所对手方风险。
- 因此本模块只产生**提醒**，不产生交易指令。
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

DAYS_PER_YEAR = 365.0

# ---------------------------------------------------------------------------
# 回测证据：写死在代码里，随告警一起发出。
# 这些数字来自 backtest/report_cross_sectional.md，样本期 2025-09 ~ 2026-08，73 个标的。
# 改策略就必须同步改这里，否则告警里的统计会与实际策略不符。
# ---------------------------------------------------------------------------

EVIDENCE: dict[str, Any] = {
    "strategy": "xs_lowvol",
    "sample": "2025-09 ~ 2026-08，73 个标的，364 天",
    "total_return_pct": 63.79,
    "max_drawdown_pct": 11.11,
    "sharpe": 2.36,
    "sharpe_ci90": (0.76, 4.05),
    "months_positive": 11,
    "months_total": 12,
    "max_month_share_pct": 19,
    "beta": -0.17,
    "annual_alpha_pct": 38.68,
    "loo_positive": 73,
    "loo_total": 73,
    "top2_concentration_pct": 24,
}


def evidence_block() -> str:
    """把回测证据渲染成一段文字，附在每条方向性告警后面。

    告警必须自带「这个结论是怎么来的、有多可信、什么时候会失效」，
    否则收到提醒的人只能凭感觉决定要不要动手。
    """
    ci = EVIDENCE["sharpe_ci90"]
    return (
        "── 这个信号的可信度 ──\n"
        f"策略：截面低波动（做多最低波动 K 个 / 做空最高波动 K 个，每周调仓）\n"
        f"样本：{EVIDENCE['sample']}\n"
        f"回测：收益率 {EVIDENCE['total_return_pct']:+.2f}%，"
        f"最大回撤 {EVIDENCE['max_drawdown_pct']:.2f}%，夏普 {EVIDENCE['sharpe']:.2f}\n"
        f"夏普 90% 置信区间 [{ci[0]:.2f}, {ci[1]:.2f}]（不跨 0，统计显著）\n"
        f"逐月：{EVIDENCE['months_positive']}/{EVIDENCE['months_total']} 个月为正，"
        f"最大月占比 {EVIDENCE['max_month_share_pct']}%\n"
        f"留一检验：剔除任一标的仍有 {EVIDENCE['loo_positive']}/"
        f"{EVIDENCE['loo_total']} 次为正；前 2 大标的仅贡献 "
        f"{EVIDENCE['top2_concentration_pct']}% 利润\n"
        f"已扣除：交易成本与资金费\n\n"
        "── 什么时候会失效 ──\n"
        "1. 样本只有一年且是深度熊市，急涨行情里该策略会跑输。\n"
        "2. 做空高波动山寨币有挤空风险，日线回测看不到盘中挤空。\n"
        "3. 低波动异象在传统资产有文献支持，但加密市场证据稀薄。\n"
        "4. 这是提醒，不是交易指令。先按交易计划书的测试协议验证再动手。"
    )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SymbolVol:
    """单个标的的波动率与流动性快照。"""

    symbol: str
    realized_vol_pct: float          # 年化已实现波动（%）
    last_price: float
    return_1d_pct: float             # 最近一日涨跌幅
    return_window_pct: float         # 打分窗口累计涨跌幅
    quote_volume_24h: float
    days_used: int


@dataclass
class DirectionalSignal:
    """一次截面排序的结果。"""

    as_of: str
    lookback: int
    longs: list[SymbolVol] = field(default_factory=list)   # 波动率最低的
    shorts: list[SymbolVol] = field(default_factory=list)  # 波动率最高的
    universe_size: int = 0

    def squeeze_warnings(self, threshold_pct: float) -> list[SymbolVol]:
        """空头腿里出现单日急涨的标的 —— 挤空风险。

        做空高波动标的最大的尾部风险就是被逼空。任何单日涨幅超过阈值的
        空头候选都必须单独标出来，而不是混在列表里。
        """
        return [s for s in self.shorts if s.return_1d_pct >= threshold_pct]


# ---------------------------------------------------------------------------
# 计算（纯函数，便于单测）
# ---------------------------------------------------------------------------

def realized_vol_pct(closes: Sequence[float], periods_per_year: float = DAYS_PER_YEAR) -> float:
    """由收盘价序列计算年化已实现波动（%）。样本不足返回 0。"""
    if len(closes) < 3:
        return 0.0
    rets = [
        closes[i] / closes[i - 1] - 1.0
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    if len(rets) < 2:
        return 0.0
    return statistics.pstdev(rets) * math.sqrt(periods_per_year) * 100.0


def parse_klines(raw: Iterable[Sequence[Any]]) -> list[float]:
    """从币安 K 线里取收盘价。"""
    out: list[float] = []
    for row in raw:
        try:
            out.append(float(row[4]))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def build_symbol_vol(
    symbol: str, closes: Sequence[float], quote_volume_24h: float, lookback: int
) -> SymbolVol | None:
    """由收盘价序列构造波动率快照。数据不足返回 None。"""
    if len(closes) < lookback + 1:
        return None
    window = list(closes[-(lookback + 1):])
    vol = realized_vol_pct(window)
    if vol <= 0:
        return None
    last = window[-1]
    prev = window[-2]
    return SymbolVol(
        symbol=symbol,
        realized_vol_pct=vol,
        last_price=last,
        return_1d_pct=(last / prev - 1.0) * 100.0 if prev > 0 else 0.0,
        return_window_pct=(
            (last / window[0] - 1.0) * 100.0 if window[0] > 0 else 0.0
        ),
        quote_volume_24h=quote_volume_24h,
        days_used=len(window) - 1,
    )


def rank_lowvol(
    vols: Iterable[SymbolVol], k_long: int, k_short: int
) -> tuple[list[SymbolVol], list[SymbolVol]]:
    """按已实现波动率排序，返回 (波动率最低的 k_long 个, 最高的 k_short 个)。

    标的不够时返回空列表——宁可不出信号，也不要出半个组合。
    """
    ranked = sorted(vols, key=lambda v: v.realized_vol_pct)
    if len(ranked) < k_long + k_short:
        return [], []
    return ranked[:k_long], ranked[-k_short:][::-1]


def build_signal(
    vols: Iterable[SymbolVol],
    k_long: int,
    k_short: int,
    lookback: int,
    as_of: str,
) -> DirectionalSignal:
    longs, shorts = rank_lowvol(vols, k_long, k_short)
    universe = list(vols)
    return DirectionalSignal(
        as_of=as_of,
        lookback=lookback,
        longs=longs,
        shorts=shorts,
        universe_size=len(universe),
    )


def position_scale(universe_vols: Iterable[float], target_vol_pct: float, max_scale: float) -> float:
    """组合层面的波动率目标缩放系数。

    回测显示：目标波动设在 80% 时，收益率从 +63.79% 略升到 +64.49%，
    而最大回撤从 11.11% 压到 7.66%。设在 40% 则把回撤压到 4.40%，
    夏普基本不变（2.34 vs 2.36）——即可以在不损失风险调整收益的前提下降低风险。
    """
    vols = [v for v in universe_vols if v > 0]
    if not vols or target_vol_pct <= 0:
        return 1.0
    universe_vol = statistics.fmean(vols)
    if universe_vol <= 0:
        return 1.0
    return max(0.1, min(max_scale, target_vol_pct / universe_vol))


def format_signal_table(signal: DirectionalSignal) -> str:
    """渲染候选表，供 scan 命令与邮件正文使用。"""
    if not signal.longs and not signal.shorts:
        return "（标的数不足，无法构建截面组合）"

    lines = [f"截面低波动信号 ｜ 截至 {signal.as_of} ｜ 窗口 {signal.lookback} 天 "
             f"｜ 全市场 {signal.universe_size} 个标的", ""]

    def block(title: str, items: list[SymbolVol]) -> None:
        lines.append(title)
        lines.append(f"  {'标的':<14}{'年化波动':>10}{'近1日':>10}{'窗口涨跌':>10}"
                     f"{'24h成交额(亿)':>16}")
        for v in items:
            lines.append(
                f"  {v.symbol:<14}{v.realized_vol_pct:>9.1f}%{v.return_1d_pct:>9.2f}%"
                f"{v.return_window_pct:>9.2f}%{v.quote_volume_24h / 1e8:>15.2f}"
            )
        lines.append("")

    block(f"做多候选（波动率最低 {len(signal.longs)} 个）", signal.longs)
    block(f"做空候选（波动率最高 {len(signal.shorts)} 个）", signal.shorts)
    return "\n".join(lines)
