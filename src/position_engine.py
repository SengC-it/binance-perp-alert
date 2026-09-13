"""持仓风险计算。

把币安的原始接口数据转成 PositionRisk / AccountSnapshot，
并负责资金费累计估算与权益快照（供日亏损、回撤规则使用）。
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any

from .config import Config, threshold
from .models import AccountSnapshot, PositionRisk
from .store import Store
from .timeutil import local_day_start

FUNDING_INTERVAL_HOURS = 8.0


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _key(raw: dict[str, Any]) -> tuple[str, str]:
    return str(raw.get("symbol", "")), str(raw.get("positionSide", "BOTH"))


def _normalize_side(position_amt: float, position_side: str) -> str:
    if position_side == "LONG":
        return "LONG"
    if position_side == "SHORT":
        return "SHORT"
    return "LONG" if position_amt > 0 else "SHORT"


def build_position_risk(
    raw: dict[str, Any],
    maint_margin_raw: float | None,
    funding_rate: float,
    mark_price_raw: float | None,
    cfg: Config,
    store: Store,
    now: datetime,
    elapsed_hours: float,
) -> PositionRisk | None:
    """由单条 positionRisk 记录构造 PositionRisk。仓位为 0 时返回 None。"""
    position_amt = _f(raw.get("positionAmt"))
    if position_amt == 0:
        return None

    symbol = str(raw.get("symbol", "")).upper()
    entry_price = _f(raw.get("entryPrice"))
    mark_price = mark_price_raw or _f(raw.get("markPrice"))
    if mark_price <= 0:
        return None

    side = _normalize_side(position_amt, str(raw.get("positionSide", "BOTH")))
    notional = abs(position_amt) * mark_price
    unrealized = _f(raw.get("unRealizedProfit"))
    leverage = _f(raw.get("leverage"), 1.0)
    isolated_margin = _f(raw.get("isolatedMargin")) or _f(raw.get("isolatedWallet"))
    liq_price = _f(raw.get("liquidationPrice"))

    # 维持保证金：优先用 v3 返回的真实值，缺失时按假设的维持保证金率估算
    if maint_margin_raw is not None and maint_margin_raw >= 0:
        maint_margin = maint_margin_raw
        estimated = False
    else:
        maint_margin = notional * threshold(cfg, "assumed_mmr", 0.005)
        estimated = True

    margin_ratio = (maint_margin / isolated_margin) if isolated_margin > 0 else 0.0
    liq_distance_pct = (
        abs(liq_price - mark_price) / mark_price if liq_price > 0 else 0.0
    )

    stop_pct = cfg.planned_stop_pct(symbol)
    r_value = abs(position_amt) * entry_price * stop_pct if stop_pct else 0.0

    # 资金费按当前费率线性外推：正费率时多头付费、空头收费
    if funding_rate and notional > 0:
        intervals = elapsed_hours / FUNDING_INTERVAL_HOURS
        signed = notional * funding_rate * intervals
        funding_cost = signed if side == "LONG" else -signed
    else:
        funding_cost = 0.0

    first_seen = store.position_first_seen(symbol, side, now)
    cumulative_funding = store.add_funding_cost(symbol, side, funding_cost)

    return PositionRisk(
        symbol=symbol,
        side=side,
        position_amt=position_amt,
        entry_price=entry_price,
        mark_price=mark_price,
        notional=notional,
        unrealized_pnl=unrealized,
        leverage=leverage,
        isolated_margin=isolated_margin,
        maint_margin=maint_margin,
        margin_ratio=margin_ratio,
        liq_price=liq_price,
        liq_distance_pct=liq_distance_pct,
        margin_ratio_estimated=estimated,
        planned_stop_pct=stop_pct,
        r_value_usdt=r_value,
        funding_cost_usdt=cumulative_funding,
        funding_rate=funding_rate,
    )


def build_snapshot(
    account_raw: dict[str, Any],
    positions_v2: list[dict[str, Any]],
    positions_v3: list[dict[str, Any]] | None,
    premium_map: dict[str, dict[str, Any]],
    cfg: Config,
    store: Store,
    now: datetime,
    tz: tzinfo,
) -> AccountSnapshot:
    """组装账户全景快照，并写入权益快照。"""
    equity = _f(account_raw.get("totalMarginBalance")) or _f(
        account_raw.get("totalWalletBalance")
    )
    available = _f(account_raw.get("availableBalance"))

    maint_map: dict[tuple[str, str], float] = {}
    for raw in positions_v3 or []:
        maint_map[_key(raw)] = _f(raw.get("maintMargin"))

    positions: list[PositionRisk] = []
    for raw in positions_v2:
        symbol = str(raw.get("symbol", "")).upper()
        premium = premium_map.get(symbol, {})
        mark_price = _f(premium.get("markPrice")) or None
        funding_rate = _f(premium.get("lastFundingRate"))
        position_amt = _f(raw.get("positionAmt"))
        if position_amt == 0:
            continue

        side = _normalize_side(position_amt, str(raw.get("positionSide", "BOTH")))
        first_seen = store.position_first_seen(symbol, side, now)
        elapsed_hours = max(0.0, (now - first_seen).total_seconds() / 3600.0)

        risk = build_position_risk(
            raw=raw,
            maint_margin_raw=maint_map.get(_key(raw)),
            funding_rate=funding_rate,
            mark_price_raw=mark_price,
            cfg=cfg,
            store=store,
            now=now,
            elapsed_hours=elapsed_hours,
        )
        if risk is not None:
            positions.append(risk)

    # 清理已平掉的持仓累计状态
    open_keys = {(p.symbol, p.side) for p in positions}
    for symbol, side in store.seen_positions() - open_keys:
        store.forget_position(symbol, side)

    # 权益快照与日亏损 / 回撤
    store.record_equity(now, equity)
    day_start = local_day_start(now, tz)
    start_equity = store.first_equity_since(day_start)
    daily_loss_pct = 0.0
    if start_equity and start_equity > 0 and equity < start_equity:
        daily_loss_pct = (start_equity - equity) / start_equity * 100.0

    peak = store.peak_equity()
    drawdown_pct = 0.0
    if peak and peak > 0 and equity < peak:
        drawdown_pct = (peak - equity) / peak * 100.0

    return AccountSnapshot(
        equity=equity,
        available_balance=available,
        positions=positions,
        daily_loss_pct=daily_loss_pct,
        drawdown_pct=drawdown_pct,
        fetched_at=now,
    )
