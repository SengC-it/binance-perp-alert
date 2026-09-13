"""前向验证（paper trading）与滚动对账。

为什么必须有这一层
==================
一年日线只有约 250 个观测。即使回测夏普 2.36、bootstrap 区间不跨 0，
那也只是「在 2025-09 ~ 2026-08 这段特定行情里，我们没有找到反驳它的证据」。
**统计上不足以证明正期望。**

所以系统不能停在「回测说行」这一步。做法是：

1. 每条方向性信号在发出的同时，落一条 paper trade 记录（含入场价）
2. 到期（默认 30 天）后用真实行情回填实际结果：价格变动 + 资金费 − 交易成本
3. 输出滚动对账，让系统自己持续证明或证伪

对账结果与回测出现系统性偏离时，应当停用策略，而不是解释它。
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from .config import Config, threshold
from .directional_engine import DirectionalSignal, EVIDENCE
from .store import Store

log = logging.getLogger(__name__)

MS_PER_DAY = 86_400_000
STRATEGY = "xs_lowvol"


@dataclass
class LegOutcome:
    symbol: str
    entry_price: float
    exit_price: float
    direction: int              # +1 做多, -1 做空
    price_return_pct: float     # 已按方向调整
    funding_pct: float          # 已按方向调整（正数表示收到）


def _entry_prices(signal: DirectionalSignal) -> dict[str, float]:
    out: dict[str, float] = {}
    for v in signal.longs + signal.shorts:
        out[v.symbol] = v.last_price
    return out


def record_signal(
    store: Store,
    signal: DirectionalSignal,
    cfg: Config,
) -> int | None:
    """把一次信号登记为待验证的 paper trade。重复登记返回 None。"""
    if not signal.longs or not signal.shorts:
        return None
    horizon = int(threshold(cfg, "paper_horizon_days", 30))
    trade_id = store.insert_paper_trade(
        strategy=STRATEGY,
        signal_date=signal.as_of,
        horizon_days=horizon,
        lookback_days=signal.lookback,
        k_long=len(signal.longs),
        k_short=len(signal.shorts),
        longs=[v.symbol for v in signal.longs],
        shorts=[v.symbol for v in signal.shorts],
        entry_prices=_entry_prices(signal),
    )
    if trade_id is not None:
        log.info("已登记前向验证记录 #%d（%s，%d 天后核对）",
                 trade_id, signal.as_of, horizon)
    return trade_id


# ---------------------------------------------------------------------------
# 验证（网络部分与计算部分分离，计算可单测）
# ---------------------------------------------------------------------------

def leg_price_return_pct(entry: float, exit_price: float, direction: int) -> float:
    """按方向计算价格收益（%）。做空时方向取 -1。"""
    if entry <= 0:
        return 0.0
    return direction * (exit_price / entry - 1.0) * 100.0


def leg_funding_pct(rate_sum: float, direction: int) -> float:
    """按方向计算资金费收益（%）。

    多头在费率为正时付出，空头收到，因此方向取 -1 时符号翻转。
    """
    return -direction * rate_sum * 100.0


def portfolio_return_pct(
    long_rets: Sequence[float],
    short_rets: Sequence[float],
    long_funding: Sequence[float],
    short_funding: Sequence[float],
    cost_pct: float,
) -> tuple[float, float, float]:
    """两条腿等权，返回 (组合净收益%, 多头腿净%, 空头腿净%)。

    每条腿各占一半资金，各自承担一次往返成本。
    """
    if not long_rets or not short_rets:
        return 0.0, 0.0, 0.0
    long_gross = statistics.fmean(long_rets) + statistics.fmean(long_funding)
    short_gross = statistics.fmean(short_rets) + statistics.fmean(short_funding)
    long_net = long_gross - cost_pct
    short_net = short_gross - cost_pct
    return 0.5 * (long_net + short_net), long_net, short_net


def verify_one(
    trade: dict[str, Any],
    price_lookup: dict[str, tuple[float, float]],
    funding_lookup: dict[str, float],
    cost_pct: float,
) -> tuple[float, float, float] | None:
    """用已取到的行情验证一条记录。

    price_lookup: {symbol: (入场价, 出场价)}
    funding_lookup: {symbol: 持有期内资金费费率之和}
    """
    longs = json.loads(trade["longs_json"])
    shorts = json.loads(trade["shorts_json"])
    entry_prices = json.loads(trade["entry_prices"])

    long_rets, short_rets, long_fund, short_fund = [], [], [], []
    for symbol in longs:
        if symbol not in price_lookup:
            continue
        entry = entry_prices.get(symbol, price_lookup[symbol][0])
        _, exit_price = price_lookup[symbol]
        long_rets.append(leg_price_return_pct(entry, exit_price, 1))
        long_fund.append(leg_funding_pct(funding_lookup.get(symbol, 0.0), 1))
    for symbol in shorts:
        if symbol not in price_lookup:
            continue
        entry = entry_prices.get(symbol, price_lookup[symbol][0])
        _, exit_price = price_lookup[symbol]
        short_rets.append(leg_price_return_pct(entry, exit_price, -1))
        short_fund.append(leg_funding_pct(funding_lookup.get(symbol, 0.0), -1))

    if not long_rets or not short_rets:
        return None
    return portfolio_return_pct(long_rets, short_rets, long_fund, short_fund, cost_pct)


def is_due(trade: dict[str, Any], today: date) -> bool:
    """信号日 + 持有期是否已到期。"""
    try:
        signal_date = date.fromisoformat(str(trade["signal_date"]))
    except ValueError:
        return False
    return (today - signal_date).days >= int(trade["horizon_days"])


def verify_pending(
    store: Store,
    cfg: Config,
    client: Any,
    today: date | None = None,
    max_trades: int = 20,
) -> dict[str, int]:
    """回填所有已到期的 paper trade。

    返回 {"verified": n, "failed": n, "skipped": n}。
    任何一条失败都不影响其他条——前向验证最怕的就是静默丢样本。
    """
    today = today or datetime.now(timezone.utc).date()
    taker = threshold(cfg, "perp_taker_fee_pct", 0.05)
    slip = threshold(cfg, "slippage_pct", 0.03)
    cost_pct = 2 * (taker + slip)   # 单条腿的往返成本

    stats = {"verified": 0, "failed": 0, "skipped": 0}
    pending = [t for t in store.pending_paper_trades() if is_due(t, today)]

    for trade in pending[:max_trades]:
        longs = json.loads(trade["longs_json"])
        shorts = json.loads(trade["shorts_json"])
        symbols = longs + shorts
        entry_prices = json.loads(trade["entry_prices"])

        price_lookup: dict[str, tuple[float, float]] = {}
        funding_lookup: dict[str, float] = {}
        try:
            for symbol in symbols:
                raw = client.klines(symbol, "1d", int(trade["horizon_days"]) + 3)
                closes = [float(r[4]) for r in raw if len(r) > 4]
                if len(closes) < 2:
                    continue
                # 出场价取最后一个收盘价，入场价优先用登记时的价格
                price_lookup[symbol] = (
                    entry_prices.get(symbol, closes[0]),
                    closes[-1],
                )
                try:
                    start_ms = int(
                        datetime.fromisoformat(str(trade["signal_date"]))
                        .replace(tzinfo=timezone.utc)
                        .timestamp()
                        * 1000
                    )
                    history = client.funding_history(symbol, start_ms)
                    funding_lookup[symbol] = sum(
                        float(h.get("fundingRate") or 0.0) for h in history
                    )
                except Exception as exc:  # 资金费取不到时不阻塞，但要留痕
                    log.warning("资金费回填失败 %s：%s", symbol, exc)
                    funding_lookup[symbol] = 0.0
        except Exception as exc:
            store.mark_paper_failed(int(trade["id"]), str(exc))
            stats["failed"] += 1
            continue

        outcome = verify_one(trade, price_lookup, funding_lookup, cost_pct)
        if outcome is None:
            store.mark_paper_failed(int(trade["id"]), "行情不完整，无法计算")
            stats["failed"] += 1
            continue

        net, long_net, short_net = outcome
        store.mark_paper_verified(
            int(trade["id"]),
            datetime.now(timezone.utc),
            net,
            long_net,
            short_net,
        )
        stats["verified"] += 1

    stats["skipped"] = len(pending) - min(len(pending), max_trades)
    return stats


# ---------------------------------------------------------------------------
# 对账报告
# ---------------------------------------------------------------------------

def render_reconciliation(store: Store) -> str:
    """滚动对账：前向验证结果 vs 回测预期。

    这是整个系统里最重要的一份输出——它决定了策略该继续用还是该停。
    """
    stats = store.paper_stats()
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("前向验证滚动对账")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"记录总数    : {stats['total']}")
    lines.append(f"已核对      : {stats['verified']}")
    lines.append(f"待核对      : {stats['pending']}")
    lines.append(f"核对失败    : {stats['failed']}")
    lines.append("")

    if stats["verified"] == 0:
        lines.append("还没有可核对的记录。前向验证需要等至少一个持有期（默认 30 天）。")
        lines.append("")
        lines.append("在此之前，回测结论只能当作「未被反驳的假设」，不能当作已验证的结论。")
        return "\n".join(lines)

    lines.append(f"平均净收益  : {stats['mean_return_pct']:+.2f}%")
    lines.append(f"胜率        : {stats['win_rate_pct']:.1f}%")
    lines.append(f"最好 / 最差 : {stats['best_pct']:+.2f}% / {stats['worst_pct']:+.2f}%")
    lines.append("")
    lines.append("── 与回测预期的对照 ──")
    lines.append(f"回测（{EVIDENCE['sample']}）："
                 f"收益率 {EVIDENCE['total_return_pct']:+.2f}%，"
                 f"最大回撤 {EVIDENCE['max_drawdown_pct']:.2f}%，"
                 f"夏普 {EVIDENCE['sharpe']:.2f}")
    lines.append("")

    n = stats["verified"]
    if n < 10:
        verdict = (
            f"样本量只有 {n} 条，**还不足以下任何结论**。"
            "回测里「26 笔」去重后只有 3 个独立事件——"
            "前向验证同样要等到独立事件足够多才有意义。"
        )
    elif stats["mean_return_pct"] <= 0:
        verdict = (
            "**前向验证为负**。这与回测结论冲突，"
            "应按交易计划书的停机条件暂停该策略，而不是给它找解释。"
        )
    elif stats["mean_return_pct"] < 1.0:
        verdict = (
            "**收益远低于回测水平**。可能原因：行情状态不同、执行成本高于假设、"
            "或回测结论本身就是样本内拟合。继续观察，不要放大仓位。"
        )
    else:
        verdict = "前向验证与回测方向一致。继续保持记录，样本越多越可信。"

    lines.append(f"判读：{verdict}")
    lines.append("")
    lines.append("提示：单条记录的名义笔数会高估独立样本量。"
                 "同一标的高度重叠的入场日，实际只是一个事件。")
    return "\n".join(lines)
