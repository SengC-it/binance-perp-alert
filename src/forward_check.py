"""前向验证（paper trading）与滚动对账。

为什么必须有这一层
==================
一年日线的样本量有限。即使回测 bootstrap 区间不跨 0，
那也只是「在特定样本期里没有找到反驳它的证据」。
**统计上不足以证明正期望。**

所以系统不能停在「回测说行」这一步。做法是：

1. 每条方向性信号登记 signal、execution lag 和 target
2. 每日记录 NAV、真实 funding 和成本；每 7 天只对变化的 target 产生换仓成本
3. 输出连续滚动对账，让系统自己持续证明或证伪

对账结果与回测出现系统性偏离时，应当停用策略，而不是解释它。
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence

from .config import Config, threshold
from .directional_engine import (
    DirectionalSignal,
    load_evidence,
    parse_completed_klines,
)
from .store import Store
from .weekly_paper import (
    PaperDataError,
    WeeklyPaperPortfolioLedger,
    WeeklyPaperResult,
    execution_day,
    target_map,
)
from .xs_lowvol_spec import (
    CONTROL_RULES,
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
    SHADOW_RULES,
    SHADOW_SPEC_HASH,
    SHADOW_STRATEGY_ID,
)

log = logging.getLogger(__name__)

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
    is_shadow = signal.strategy_id == SHADOW_STRATEGY_ID
    rules = SHADOW_RULES if is_shadow else CONTROL_RULES
    strategy_id = SHADOW_STRATEGY_ID if is_shadow else CONTROL_STRATEGY_ID
    spec_hash = SHADOW_SPEC_HASH if is_shadow else CONTROL_SPEC_HASH
    try:
        signal_day = date.fromisoformat(signal.as_of)
    except ValueError:
        return None
    previous = store.paper_rebalances(strategy_id)
    if previous:
        try:
            last_signal_day = date.fromisoformat(
                str(previous[-1]["signal_date"])[:10]
            )
        except (KeyError, TypeError, ValueError):
            last_signal_day = None
        if (
            last_signal_day is not None
            and (signal_day - last_signal_day).days < rules.rebalance_days
        ):
            return None
    execution_date = signal.execution_date or execution_day(
        signal.as_of, rules.execution_lag_days
    ).isoformat()
    trade_id = store.insert_paper_trade(
        strategy=strategy_id,
        signal_date=signal.as_of,
        horizon_days=rules.rebalance_days,
        lookback_days=signal.lookback,
        k_long=len(signal.longs),
        k_short=len(signal.shorts),
        longs=[v.symbol for v in signal.longs],
        shorts=[v.symbol for v in signal.shorts],
        entry_prices=_entry_prices(signal),
        strategy_id=strategy_id,
        spec_hash=spec_hash,
        variant=rules.variant,
        signal_timestamp=signal.signal_timestamp,
        execution_date=execution_date,
        execution_lag_days=rules.execution_lag_days,
    )
    try:
        store.record_paper_rebalance(
            strategy_id=strategy_id,
            spec_hash=spec_hash,
            rebalance_date=execution_date,
            signal_date=signal.as_of,
            execution_date=execution_date,
            targets=target_map(signal),
            changed_symbols=[],
        )
    except Exception as exc:
        log.warning("周度 PAPER 调仓事件登记失败：%s", exc)
    if trade_id is not None:
        log.info("已登记周度 PAPER 记录 #%d（%s signal，%s execution）",
                 trade_id, signal.as_of, execution_date)
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
    symbols = list(longs) + list(shorts)
    if any(symbol not in price_lookup for symbol in symbols):
        return None

    long_rets, short_rets, long_fund, short_fund = [], [], [], []
    for symbol in longs:
        entry = entry_prices.get(symbol, price_lookup[symbol][0])
        _, exit_price = price_lookup[symbol]
        long_rets.append(leg_price_return_pct(entry, exit_price, 1))
        long_fund.append(leg_funding_pct(funding_lookup.get(symbol, 0.0), 1))
    for symbol in shorts:
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


def _day_start_ms(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp() * 1000)


def _completed_observation_ms(today: date) -> int:
    """只把观察时刻前已经收盘的日线交给 PAPER。"""
    now = datetime.now(timezone.utc)
    if today < now.date():
        return int(
            datetime.combine(today, time.max, tzinfo=timezone.utc).timestamp() * 1000
        )
    return int(now.timestamp() * 1000)


def _weekly_inputs_from_client(
    store: Store,
    client: Any,
    today: date,
) -> tuple[
    dict[date, dict[str, float]],
    dict[date, dict[str, int]],
    dict[str, Sequence[Any]],
] | None:
    """读取 Control 的 signal schedule 与完成日线，失败时保持 fail-closed。

    ``None`` 表示客户端没有可识别的 completed-candle 形状，供旧的纯
    fixture 兼容路径使用；一旦部分标的有数据、部分标的没有，则抛出
    ``PaperDataError``，避免偷偷缩小 universe。
    """
    rows = store.paper_rebalances(CONTROL_STRATEGY_ID)
    usable_rows: list[dict[str, Any]] = []
    for row in rows:
        if row["spec_hash"] != CONTROL_SPEC_HASH:
            raise PaperDataError("PAPER rebalance 的 spec hash 已过期")
        try:
            rebalance_day = date.fromisoformat(str(row["rebalance_date"])[:10])
        except ValueError as exc:
            raise PaperDataError("PAPER rebalance 日期非法") from exc
        if rebalance_day <= today:
            usable_rows.append(row)
    if not usable_rows:
        return None

    targets_by_signal_day: dict[date, dict[str, int]] = {}
    symbols: set[str] = set()
    for row in usable_rows:
        try:
            signal_day = date.fromisoformat(str(row["signal_date"])[:10])
            execution = date.fromisoformat(str(row["execution_date"])[:10])
            targets = json.loads(row["targets_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PaperDataError("PAPER target schedule 损坏") from exc
        if execution > today or not isinstance(targets, dict):
            continue
        clean_targets = {str(symbol): int(direction) for symbol, direction in targets.items()}
        targets_by_signal_day[signal_day] = clean_targets
        symbols.update(clean_targets)
    if not symbols:
        return None

    observation_ms = _completed_observation_ms(today)
    candles_by_symbol: dict[str, list[Any]] = {}
    strict_client = callable(getattr(client, "exchange_info", None))
    for symbol in sorted(symbols):
        try:
            raw = client.klines(symbol, "1d", 1000)
        except Exception as exc:
            # 旧的 fixture 客户端没有 Binance completed-candle 字段；交给
            # verify_pending 的兼容路径处理，真实客户端则由下层失败记录。
            if strict_client:
                raise PaperDataError(f"无法读取 {symbol} 的 completed candles") from exc
            return None
        candles_by_symbol[symbol] = parse_completed_klines(raw, observation_ms)

    if not any(candles_by_symbol.values()):
        if strict_client:
            raise PaperDataError("客户端没有返回可识别的 completed candles")
        return None
    missing_candles = [symbol for symbol, candles in candles_by_symbol.items() if not candles]
    if missing_candles:
        raise PaperDataError(
            "PAPER 缺少 eligible symbol 的 completed candles: "
            + ",".join(sorted(missing_candles))
        )

    prices_by_day: dict[date, dict[str, float]] = {}
    for symbol, candles in candles_by_symbol.items():
        for candle in candles:
            prices_by_day.setdefault(candle.day, {})[symbol] = candle.close

    first_signal = min(targets_by_signal_day)
    start_ms = _day_start_ms(first_signal - timedelta(days=1))
    funding_events: dict[str, Sequence[Any]] = {}
    for symbol in sorted(symbols):
        try:
            history = client.funding_history(symbol, start_ms)
        except Exception as exc:
            raise PaperDataError(f"无法读取 {symbol} 的已结算 funding events") from exc
        if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
            raise PaperDataError(f"{symbol} 的 funding events 响应格式非法")
        funding_events[symbol] = list(history)

    return prices_by_day, targets_by_signal_day, funding_events


def _verify_weekly_pending(
    store: Store,
    cfg: Config,
    client: Any,
    today: date,
    max_trades: int,
) -> tuple[dict[str, int], set[int]] | None:
    """用连续 ledger 核对 Control，并返回已接管的 paper id。"""
    inputs = _weekly_inputs_from_client(store, client, today)
    if inputs is None:
        return None
    prices_by_day, targets_by_signal_day, funding_events = inputs
    result = run_weekly_paper(
        store,
        cfg,
        prices_by_day,
        targets_by_signal_day,
        funding_events,
    )
    rows = list(result.nav)
    by_day = {row.day: row for row in rows}
    execution_days = sorted(
        execution_day(signal_day) for signal_day in targets_by_signal_day
    )
    pending = [
        trade
        for trade in store.pending_paper_trades()
        if trade.get("strategy_id") == CONTROL_STRATEGY_ID
    ]
    verified_ids: set[int] = set()
    stats = {"verified": 0, "failed": 0, "skipped": 0}
    processed = 0
    for trade in pending:
        try:
            entry_day = date.fromisoformat(str(trade["execution_date"])[:10])
        except (TypeError, ValueError):
            continue
        if entry_day not in by_day:
            continue
        next_days = [day for day in execution_days if day > entry_day and day <= today]
        if not next_days:
            continue
        end_day = next_days[0]
        start_index = next(
            (index for index, row in enumerate(rows) if row.day == entry_day), None
        )
        end_index = next(
            (index for index, row in enumerate(rows) if row.day == end_day), None
        )
        if start_index is None or end_index is None or end_index <= start_index:
            continue
        if processed >= max_trades:
            stats["skipped"] += 1
            continue
        period = rows[start_index:end_index + 1]
        capital = result.initial_capital
        net_pct = sum(row.daily_pnl for row in period) / capital * 100.0
        long_pct = sum(row.long_pnl for row in period) / capital * 100.0
        short_pct = sum(row.short_pnl for row in period) / capital * 100.0
        store.mark_paper_verified(
            int(trade["id"]), datetime.now(timezone.utc), net_pct, long_pct, short_pct
        )
        verified_ids.add(int(trade["id"]))
        processed += 1
        stats["verified"] += 1
    return stats, verified_ids


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
    weekly_mode = False
    weekly_control_ids: set[int] = set()
    try:
        weekly_result = _verify_weekly_pending(
            store, cfg, client, today, max_trades
        )
    except PaperDataError as exc:
        log.error("周度 PAPER 数据不完整，保持 fail-closed：%s", exc)
        weekly_result = ({"verified": 0, "failed": 0, "skipped": 0}, set())
        for trade in store.pending_paper_trades():
            if (
                trade.get("strategy_id") == CONTROL_STRATEGY_ID
                and is_due(trade, today)
            ):
                store.mark_paper_failed(int(trade["id"]), str(exc))
                stats["failed"] += 1
        weekly_mode = True
    if weekly_result is not None:
        weekly_mode = True
        weekly_stats, verified_ids = weekly_result
        stats["verified"] += weekly_stats["verified"]
        stats["failed"] += weekly_stats["failed"]
        stats["skipped"] += weekly_stats["skipped"]
        weekly_control_ids = {
            int(trade["id"])
            for trade in store.pending_paper_trades()
            if trade.get("strategy_id") == CONTROL_STRATEGY_ID
        } | verified_ids

    pending = [
        trade
        for trade in store.pending_paper_trades()
        if is_due(trade, today)
        and (not weekly_mode or int(trade["id"]) not in weekly_control_ids)
    ]

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

    stats["skipped"] += len(pending) - min(len(pending), max_trades)
    return stats


def run_weekly_paper(
    store: Store,
    cfg: Config,
    prices_by_day: dict[date | str, dict[str, float]],
    targets_by_signal_day: dict[date | str, dict[str, int]],
    funding_events: dict[str, Sequence[Any]] | None = None,
    *,
    capital: float = 10_000.0,
    strategy_id: str = CONTROL_STRATEGY_ID,
    spec_hash: str | None = None,
    scales_by_signal_day: Mapping[date | str, float] | None = None,
) -> WeeklyPaperResult:
    """用已完成日线运行并持久化连续周度 PAPER NAV。

    ``targets_by_signal_day`` 的 key 是 signal T；函数先施加一个完整的
    execution lag，再在每个 execution day 只处理 target 发生变化的标的。
    价格或 funding 数据不足时抛出 ``PaperDataError``，不会写出半个组合。
    """
    rules = SHADOW_RULES if strategy_id == SHADOW_STRATEGY_ID else CONTROL_RULES
    resolved_hash = spec_hash or (
        SHADOW_SPEC_HASH if strategy_id == SHADOW_STRATEGY_ID else CONTROL_SPEC_HASH
    )
    signal_days = sorted(date.fromisoformat(str(day)[:10]) for day in targets_by_signal_day)
    if any(
        (later - earlier).days < rules.rebalance_days
        for earlier, later in zip(signal_days, signal_days[1:])
    ):
        raise PaperDataError("PAPER target schedule 不能在 7 天内重复调仓")
    scheduled: dict[date, dict[str, int]] = {
        execution_day(signal_day, rules.execution_lag_days): targets
        for signal_day, targets in targets_by_signal_day.items()
    }
    scheduled_scales = {
        execution_day(signal_day, rules.execution_lag_days): float(scale)
        for signal_day, scale in (scales_by_signal_day or {}).items()
    }
    ledger = WeeklyPaperPortfolioLedger(
        capital,
        strategy_id=strategy_id,
        spec_hash=resolved_hash,
    )
    result = ledger.run(
        prices_by_day.keys(),
        scheduled,
        prices_by_day,
        funding_events,
        scales_by_day=scheduled_scales,
    )

    signal_dates = {
        execution_day(signal_day, rules.execution_lag_days): str(signal_day)[:10]
        for signal_day in targets_by_signal_day
    }
    for row in result.nav:
        store.record_paper_nav(
            strategy_id=result.strategy_id,
            spec_hash=result.spec_hash,
            day=row.day.isoformat(),
            nav=row.nav,
            daily_pnl=row.daily_pnl,
            long_pnl=row.long_pnl,
            short_pnl=row.short_pnl,
            funding_pnl=row.funding_pnl,
            cost_pnl=row.cost_pnl,
            rebalance=row.rebalance,
            changed_symbols=list(row.changed_symbols),
            positions=row.positions,
        )
    for rebalance_date, targets in scheduled.items():
        row = next((item for item in result.nav if item.day == rebalance_date), None)
        store.record_paper_rebalance(
            strategy_id=result.strategy_id,
            spec_hash=result.spec_hash,
            rebalance_date=rebalance_date.isoformat(),
            signal_date=signal_dates[rebalance_date],
            execution_date=rebalance_date.isoformat(),
            targets=targets,
            changed_symbols=list(row.changed_symbols) if row else [],
        )
        if rebalance_date in prices_by_day:
            execution_prices = {
                symbol: float(prices_by_day[rebalance_date][symbol])
                for symbol in targets
                if symbol in prices_by_day[rebalance_date]
            }
            if execution_prices:
                store.update_paper_entry_prices(
                    result.strategy_id,
                    rebalance_date.isoformat(),
                    execution_prices,
                )
    last = result.nav[-1] if result.nav else None
    store.save_paper_portfolio(
        strategy_id=result.strategy_id,
        spec_hash=result.spec_hash,
        last_day=last.day.isoformat() if last else None,
        state={"nav": result.final_nav, "positions": last.positions if last else {}},
        updated_at=datetime.now(timezone.utc),
    )
    return result


# 更明确的别名，供 PAPER 任务入口调用。
update_weekly_paper = run_weekly_paper


# ---------------------------------------------------------------------------
# 对账报告
# ---------------------------------------------------------------------------

def render_reconciliation(store: Store) -> str:
    """滚动对账：前向验证结果 vs 回测预期。

    这是整个系统里最重要的一份输出——它决定了策略该继续用还是该停。
    """
    all_trades = store.paper_trades()
    current_control = [
        trade
        for trade in all_trades
        if trade.get("strategy_id") == CONTROL_STRATEGY_ID
        and trade.get("spec_hash") == CONTROL_SPEC_HASH
    ]
    # 旧数据库中的未版本化记录只在没有任何新 Control 记录时用于兼容展示；
    # 一旦有当前版本，就严格按 strategy_id + spec_hash 隔离统计。
    stats = (
        store.paper_stats(CONTROL_STRATEGY_ID, CONTROL_SPEC_HASH)
        if current_control
        else store.paper_stats()
    )
    legacy_count = sum(
        1 for trade in all_trades
        if not trade.get("strategy_id") or not trade.get("spec_hash")
    )
    evidence = load_evidence()
    ledger_rows = store.paper_nav(CONTROL_STRATEGY_ID)
    shadow_rows = store.paper_nav(SHADOW_STRATEGY_ID)
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("前向验证滚动对账")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"记录总数    : {stats['total']}")
    lines.append(f"已核对      : {stats['verified']}")
    lines.append(f"待核对      : {stats['pending']}")
    lines.append(f"核对失败    : {stats['failed']}")
    lines.append(f"PAPER Control: {CONTROL_STRATEGY_ID} / {CONTROL_SPEC_HASH}")
    lines.append(f"PAPER Shadow : {SHADOW_STRATEGY_ID} / {SHADOW_SPEC_HASH}")
    if legacy_count:
        lines.append(f"旧未版本化记录: {legacy_count} 条（不作为当前 V1 证据）")
    if ledger_rows:
        latest = ledger_rows[-1]
        lines.append(
            f"连续 NAV     : {latest['nav']:+,.2f}（{len(ledger_rows)} 个完成日，"
            f"最新 {latest['day']}）"
        )
        lines.append(
            f"多头/空头累计: {sum(float(r['long_pnl']) for r in ledger_rows):+,.2f} / "
            f"{sum(float(r['short_pnl']) for r in ledger_rows):+,.2f} USDT"
        )
    else:
        lines.append("连续 NAV     : 尚无周度 PAPER ledger")
    if shadow_rows:
        latest_shadow = shadow_rows[-1]
        lines.append(
            f"Shadow NAV   : {latest_shadow['nav']:+,.2f}（{len(shadow_rows)} 个完成日，"
            f"最新 {latest_shadow['day']}）"
        )
    else:
        lines.append("Shadow NAV   : 尚无周度 PAPER ledger")
    lines.append("")

    if stats["verified"] == 0:
        lines.append("还没有可核对的记录。PAPER ledger 需要至少一个 execution lag 后的完成日线。")
        lines.append("")
        lines.append("在此之前，回测结论只能当作「未被反驳的假设」，不能当作已验证的结论。")
        return "\n".join(lines)

    lines.append(f"平均净收益  : {stats['mean_return_pct']:+.2f}%")
    lines.append(f"胜率        : {stats['win_rate_pct']:.1f}%")
    lines.append(f"最好 / 最差 : {stats['best_pct']:+.2f}% / {stats['worst_pct']:+.2f}%")
    lines.append("")
    lines.append("── 与回测预期的对照 ──")
    if evidence.get("status") == "CURRENT":
        lines.append(
            f"回测证据（{evidence['strategy_id']} / {evidence['spec_hash']}）："
            f"收益率 {evidence['total_return_pct']:+.2f}%，"
            f"最大回撤 {evidence['max_drawdown_pct']:.2f}%，"
            f"夏普 {evidence['sharpe']:.2f}"
        )
    else:
        lines.append("EVIDENCE_STALE：当前 spec 没有匹配的回测统计")
    lines.append("")

    n = stats["verified"]
    if n < 10:
        verdict = (
            f"样本量只有 {n} 条，**还不足以下任何结论**。"
            "重叠的调仓记录不等于独立事件；前向验证同样要等到独立事件足够多才有意义。"
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
