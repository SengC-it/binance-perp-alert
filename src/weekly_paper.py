"""与 XS-LOWVOL 回测同口径的周度 PAPER portfolio ledger。

这个模块只做账面计算：每日 NAV、真实 funding event、成本和 target-diff
换仓都持久化前先在内存中按同一状态机计算。相同方向的 target 会保留原
持仓和入场日，不会为了生成统计而制造一次完整换仓。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .directional_engine import DirectionalSignal, position_scale
from .xs_lowvol_spec import (
    CONTROL_RULES,
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
    SHADOW_RULES,
    SHADOW_SPEC_HASH,
    SHADOW_STRATEGY_ID,
)


class PaperDataError(ValueError):
    """PAPER 计算所需的行情不完整。"""


@dataclass(frozen=True)
class PaperNav:
    day: date
    nav: float
    daily_pnl: float
    long_pnl: float
    short_pnl: float
    funding_pnl: float
    cost_pnl: float
    rebalance: bool
    changed_symbols: tuple[str, ...]
    positions: dict[str, int]
    position_notionals: dict[str, float] = field(default_factory=dict)
    resized_symbols: tuple[str, ...] = ()
    turnover_notional: float = 0.0
    scale: float = 1.0
    targets: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WeeklyPaperResult:
    strategy_id: str
    spec_hash: str
    initial_capital: float
    nav: tuple[PaperNav, ...]
    final_nav: float
    long_pnl: float
    short_pnl: float
    funding_pnl: float
    cost_pnl: float
    trade_count: int
    turnover_events: int
    turnover_notional: float = 0.0
    resized_symbols: tuple[str, ...] = ()
    targets_by_day: dict[date, dict[str, int]] = field(default_factory=dict)
    scales_by_day: dict[date, float] = field(default_factory=dict)

    @property
    def net_pnl(self) -> float:
        return self.final_nav - self.initial_capital


@dataclass
class _Position:
    direction: int
    notional: float
    mark_price: float


def _as_day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).date()
    return date.fromisoformat(str(value)[:10])


def _event_parts(event: Any) -> tuple[date, float] | None:
    if isinstance(event, Mapping):
        stamp = event.get("day", event.get("date", event.get("fundingTime")))
        rate = event.get("rate", event.get("fundingRate"))
    elif isinstance(event, Sequence) and not isinstance(event, (str, bytes)):
        if len(event) < 2:
            return None
        stamp, rate = event[0], event[1]
    else:
        return None
    try:
        return _as_day(stamp), float(rate)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _funding_sum(
    events: Sequence[Any], start_day: date, end_day: date
) -> float:
    total = 0.0
    for event in events:
        parts = _event_parts(event)
        if parts is not None and start_day < parts[0] <= end_day:
            total += parts[1]
    return total


def execution_day(signal_day: date | str, lag_days: int = CONTROL_RULES.execution_lag_days) -> date:
    """返回 signal 后第一个允许使用的日线执行日。"""
    return _as_day(signal_day) + timedelta(days=int(lag_days))


def target_map(signal: DirectionalSignal) -> dict[str, int]:
    """把信号转成不带排序副作用的 target map。"""
    return {
        **{v.symbol: 1 for v in signal.longs},
        **{v.symbol: -1 for v in signal.shorts},
    }


def scale_for_strategy(strategy_id: str, universe_vols: Iterable[float]) -> float:
    """Control 永远为 1；VT80 才允许使用 80% / 3.0。"""
    if strategy_id != SHADOW_STRATEGY_ID:
        return 1.0
    return position_scale(universe_vols, SHADOW_RULES.target_vol_pct, SHADOW_RULES.max_scale)


class WeeklyPaperPortfolioLedger:
    """按日盯市、按周 target-diff 调整的 PAPER 账本。"""

    def __init__(
        self,
        capital: float = 10_000.0,
        *,
        strategy_id: str = CONTROL_STRATEGY_ID,
        spec_hash: str = CONTROL_SPEC_HASH,
        taker_fee_pct: float | None = None,
        slippage_pct: float | None = None,
    ) -> None:
        if capital <= 0:
            raise ValueError("capital 必须为正")
        if strategy_id == SHADOW_STRATEGY_ID:
            rules = SHADOW_RULES
            expected_hash = SHADOW_SPEC_HASH
        elif strategy_id == CONTROL_STRATEGY_ID:
            rules = CONTROL_RULES
            expected_hash = CONTROL_SPEC_HASH
        else:
            raise ValueError("未知 PAPER strategy_id")
        if spec_hash != expected_hash:
            raise ValueError("strategy_id 与 spec hash 不匹配")
        self.capital = float(capital)
        self.strategy_id = strategy_id
        self.spec_hash = spec_hash
        self._rules = rules
        self._slot_notional = self.capital / rules.total_slots
        self._cost_rate = (
            (rules.taker_fee_pct if taker_fee_pct is None else float(taker_fee_pct))
            + (rules.slippage_pct if slippage_pct is None else float(slippage_pct))
        ) / 100.0
        self._nav = self.capital
        self._last_day: date | None = None
        self._positions: dict[str, _Position] = {}
        self._rows: dict[date, PaperNav] = {}
        self._targets: dict[date, dict[str, int]] = {}
        self._scales: dict[date, float] = {}
        self._long_pnl = 0.0
        self._short_pnl = 0.0
        self._funding_pnl = 0.0
        self._cost_pnl = 0.0
        self._trade_count = 0
        self._turnover_events = 0
        self._turnover_notional = 0.0
        self._resized_symbols: set[str] = set()

    @property
    def positions(self) -> dict[str, int]:
        return {symbol: p.direction for symbol, p in self._positions.items()}

    @property
    def rows(self) -> list[PaperNav]:
        return [self._rows[day] for day in sorted(self._rows)]

    @property
    def position_notionals(self) -> dict[str, float]:
        return {symbol: position.notional for symbol, position in self._positions.items()}

    def _record(
        self,
        day: date,
        daily_pnl: float,
        long_pnl: float,
        short_pnl: float,
        funding_pnl: float,
        cost_pnl: float,
        *,
        rebalance: bool = False,
        changed_symbols: Iterable[str] = (),
        resized_symbols: Iterable[str] = (),
        turnover_notional: float = 0.0,
        scale: float = 1.0,
        targets: Mapping[str, int] | None = None,
    ) -> PaperNav:
        previous = self._rows.get(day)
        if previous is not None:
            daily_pnl += previous.daily_pnl
            long_pnl += previous.long_pnl
            short_pnl += previous.short_pnl
            funding_pnl += previous.funding_pnl
            cost_pnl += previous.cost_pnl
            rebalance = rebalance or previous.rebalance
            changed_symbols = tuple(sorted(set(previous.changed_symbols) | set(changed_symbols)))
            resized_symbols = tuple(sorted(set(previous.resized_symbols) | set(resized_symbols)))
            turnover_notional += previous.turnover_notional
            if not rebalance:
                scale = previous.scale
            targets = dict(previous.targets if targets is None else targets)
        else:
            changed_symbols = tuple(sorted(set(changed_symbols)))
            resized_symbols = tuple(sorted(set(resized_symbols)))
            targets = dict({} if targets is None else targets)
        row = PaperNav(
            day=day,
            nav=self._nav,
            daily_pnl=daily_pnl,
            long_pnl=long_pnl,
            short_pnl=short_pnl,
            funding_pnl=funding_pnl,
            cost_pnl=cost_pnl,
            rebalance=rebalance,
            changed_symbols=changed_symbols,
            positions=self.positions,
            position_notionals=self.position_notionals,
            resized_symbols=resized_symbols,
            turnover_notional=float(turnover_notional),
            scale=float(scale),
            targets=targets,
        )
        self._rows[day] = row
        return row

    def mark_day(
        self,
        day: date | str,
        prices: Mapping[str, float],
        funding_events: Mapping[str, Sequence[Any]] | None = None,
    ) -> PaperNav:
        """记录一个完成日线；重复调用同一天不重复计入 PnL/funding。"""
        day = _as_day(day)
        if self._last_day is not None and day <= self._last_day:
            row = self._rows.get(day)
            if row is not None:
                return row
            raise PaperDataError("日线必须按时间升序提供")
        price_pnl = 0.0
        long_pnl = 0.0
        short_pnl = 0.0
        funding_pnl = 0.0
        for symbol, position in self._positions.items():
            try:
                price = float(prices[symbol])
            except (KeyError, TypeError, ValueError):
                raise PaperDataError(f"缺少 {symbol} 的 {day} 完成收盘价") from None
            if price <= 0 or position.mark_price <= 0:
                raise PaperDataError(f"{symbol} 在 {day} 的价格非法")
            if funding_events is not None and symbol not in funding_events:
                raise PaperDataError(f"缺少 {symbol} 的已结算 funding events")
            move = position.direction * position.notional * (price / position.mark_price - 1.0)
            fund = -position.direction * position.notional * _funding_sum(
                funding_events.get(symbol, ()) if funding_events is not None else (),
                self._last_day or day,
                day,
            )
            price_pnl += move
            funding_pnl += fund
            leg = move + fund
            if position.direction > 0:
                long_pnl += leg
            else:
                short_pnl += leg
            position.mark_price = price
        self._nav += price_pnl + funding_pnl
        self._long_pnl += long_pnl
        self._short_pnl += short_pnl
        self._funding_pnl += funding_pnl
        self._last_day = day
        return self._record(
            day,
            price_pnl + funding_pnl,
            long_pnl,
            short_pnl,
            funding_pnl,
            0.0,
            rebalance=False,
        )

    def rebalance(
        self,
        day: date | str,
        targets: Mapping[str, int],
        prices: Mapping[str, float],
        funding_events: Mapping[str, Sequence[Any]] | None = None,
        *,
        scale: float = 1.0,
    ) -> PaperNav:
        """在 day 的已完成收盘价执行 target-diff 调整。"""
        day = _as_day(day)
        clean = {str(symbol): int(direction) for symbol, direction in targets.items()}
        if any(direction not in (-1, 1) for direction in clean.values()):
            raise PaperDataError("target direction 必须为 +1 或 -1")
        if day in self._targets:
            if self._targets[day] != clean:
                raise PaperDataError("同一 rebalance 日的 target 不一致")
            if not math.isclose(self._scales[day], scale, rel_tol=0.0, abs_tol=1e-12):
                raise PaperDataError("同一 rebalance 日的 scale 不一致")
            return self._rows[day]
        if not math.isfinite(scale) or scale <= 0:
            raise PaperDataError("仓位缩放必须为正数")
        if self.strategy_id == CONTROL_STRATEGY_ID and not math.isclose(
            scale, 1.0, rel_tol=0.0, abs_tol=1e-12
        ):
            raise PaperDataError("Control 的仓位缩放必须恒为 1")
        if self.strategy_id == SHADOW_STRATEGY_ID and scale > self._rules.max_scale:
            raise PaperDataError("Shadow 的仓位缩放超过冻结 max_scale")
        target_notional = self._slot_notional * float(scale)
        old_positions = {
            symbol: _Position(position.direction, position.notional, position.mark_price)
            for symbol, position in self._positions.items()
        }
        changed = {
            symbol
            for symbol, direction in self.positions.items()
            if clean.get(symbol) != direction
        } | {symbol for symbol in clean if symbol not in self._positions}
        resized = {
            symbol
            for symbol, direction in clean.items()
            if symbol in old_positions
            and old_positions[symbol].direction == direction
            and not math.isclose(
                old_positions[symbol].notional, target_notional, rel_tol=0.0, abs_tol=1e-12
            )
        }
        affected = changed | resized
        for symbol in affected:
            try:
                price = float(prices[symbol])
            except (KeyError, TypeError, ValueError):
                raise PaperDataError(f"缺少 {symbol} 的 {day} 调仓价格") from None
            if price <= 0:
                raise PaperDataError(f"{symbol} 在 {day} 的调仓价格非法")

        turnover_notional = 0.0
        for symbol, position in old_positions.items():
            if clean.get(symbol) == position.direction:
                turnover_notional += abs(target_notional - position.notional)
            else:
                turnover_notional += position.notional
        for symbol, direction in clean.items():
            if symbol not in old_positions or old_positions[symbol].direction != direction:
                turnover_notional += target_notional

        self.mark_day(day, prices, funding_events)
        daily_cost = 0.0
        cost_long = 0.0
        cost_short = 0.0
        for symbol in list(self._positions):
            if clean.get(symbol) == self._positions[symbol].direction:
                continue
            position = self._positions.pop(symbol)
            cost = position.notional * self._cost_rate
            daily_cost += cost
            if position.direction > 0:
                cost_long -= cost
            else:
                cost_short -= cost
        for symbol in resized:
            position = self._positions[symbol]
            cost = abs(target_notional - position.notional) * self._cost_rate
            daily_cost += cost
            if position.direction > 0:
                cost_long -= cost
            else:
                cost_short -= cost
            position.notional = target_notional
        for symbol, direction in clean.items():
            if symbol in self._positions:
                continue
            notional = target_notional
            cost = notional * self._cost_rate
            daily_cost += cost
            cost_long -= cost if direction > 0 else 0.0
            cost_short -= cost if direction < 0 else 0.0
            self._positions[symbol] = _Position(direction, notional, float(prices[symbol]))
            self._trade_count += 1
        self._nav -= daily_cost
        self._long_pnl += cost_long
        self._short_pnl += cost_short
        self._cost_pnl -= daily_cost
        self._turnover_events += int(turnover_notional > 1e-12)
        self._turnover_notional += turnover_notional
        self._resized_symbols.update(resized)
        self._targets[day] = clean
        self._scales[day] = float(scale)
        return self._record(
            day,
            -daily_cost,
            cost_long,
            cost_short,
            0.0,
            -daily_cost,
            rebalance=True,
            changed_symbols=changed,
            resized_symbols=resized,
            turnover_notional=turnover_notional,
            scale=float(scale),
            targets=clean,
        )

    def run(
        self,
        days: Iterable[date | str],
        targets_by_day: Mapping[date | str, Mapping[str, int]],
        prices_by_day: Mapping[date | str, Mapping[str, float]],
        funding_events: Mapping[str, Sequence[Any]] | None = None,
        scales_by_day: Mapping[date | str, float] | None = None,
    ) -> WeeklyPaperResult:
        """按完成日线连续运行，target key 必须是 execution/rebalance 日。"""
        target_map_by_day = {_as_day(day): targets for day, targets in targets_by_day.items()}
        price_map = {_as_day(day): prices for day, prices in prices_by_day.items()}
        scale_map = {_as_day(day): scale for day, scale in (scales_by_day or {}).items()}
        observed_days = sorted(
            {_as_day(day) for day in days} | set(price_map) | set(target_map_by_day)
        )
        all_days: list[date] = []
        if observed_days:
            cursor = observed_days[0]
            end = observed_days[-1]
            while cursor <= end:
                all_days.append(cursor)
                cursor += timedelta(days=1)
        for day in all_days:
            if day not in price_map:
                raise PaperDataError(f"缺少 {day} 的完成日线")
            if day in target_map_by_day:
                self.rebalance(
                    day,
                    target_map_by_day[day],
                    price_map[day],
                    funding_events,
                    scale=scale_map.get(day, 1.0),
                )
            else:
                self.mark_day(day, price_map[day], funding_events)
        return self.result()

    def result(self) -> WeeklyPaperResult:
        return WeeklyPaperResult(
            strategy_id=self.strategy_id,
            spec_hash=self.spec_hash,
            initial_capital=self.capital,
            nav=tuple(self.rows),
            final_nav=self._nav,
            long_pnl=self._long_pnl,
            short_pnl=self._short_pnl,
            funding_pnl=self._funding_pnl,
            cost_pnl=self._cost_pnl,
            trade_count=self._trade_count,
            turnover_events=self._turnover_events,
            turnover_notional=self._turnover_notional,
            resized_symbols=tuple(sorted(self._resized_symbols)),
            targets_by_day={
                day: dict(targets) for day, targets in sorted(self._targets.items())
            },
            scales_by_day=dict(sorted(self._scales.items())),
        )


def simulate_weekly_paper(
    prices_by_day: Mapping[date | str, Mapping[str, float]],
    targets_by_day: Mapping[date | str, Mapping[str, int]],
    funding_events: Mapping[str, Sequence[Any]] | None = None,
    *,
    capital: float = 10_000.0,
    strategy_id: str = CONTROL_STRATEGY_ID,
    spec_hash: str | None = None,
    scales_by_day: Mapping[date | str, float] | None = None,
) -> WeeklyPaperResult:
    """fixture 友好的周度 PAPER 入口。"""
    if spec_hash is None:
        spec_hash = SHADOW_SPEC_HASH if strategy_id == SHADOW_STRATEGY_ID else CONTROL_SPEC_HASH
    ledger = WeeklyPaperPortfolioLedger(
        capital,
        strategy_id=strategy_id,
        spec_hash=spec_hash,
    )
    return ledger.run(
        prices_by_day.keys(),
        targets_by_day,
        prices_by_day,
        funding_events,
        scales_by_day,
    )
