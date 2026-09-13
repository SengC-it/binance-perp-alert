"""delta 中性资金费套利的历史回测引擎。

策略定义
========
这是**告警规则 `funding_opportunity` 所指向的那个策略**。告警本身不产生盈亏，
产生盈亏的是「做多现货 + 做空永续」的 delta 中性组合（cash-and-carry）。

信号（与线上扫描口径一致）
--------------------------
用**最近一次已结算**的资金费率 r（不含未来信息），并按该合约**实际的结算间隔**
annualize（币安对部分合约用 4 小时结算，硬编码 8 小时会低估一半）：

    毛年化 = r × (24 / 结算间隔小时数) × 365
    往返成本 = 2×(现货taker + 永续taker) + 2×滑点        默认 0.36%
    净年化 = 毛年化 − 往返成本 × 365 / 持有天数

净年化 ≥ min_net_annual_pct 且 24h 成交额 ≥ 阈值 → 开仓。

盈亏分解（单位：占单腿名义价值的百分比）
----------------------------------------
设入场基差 b0 = (永续−现货)/现货，出场基差 b1 同。

    资金费  = Σ 结算费率 × 100
              （费率为正时空头收取，为负时支付；符号由求和自然得出）
    基差    = b0 − b1
              推导：多头现货盈亏 (S1−S0)，空头永续盈亏 (F0−F1)，
                    合计 = (F0−S0) − (F1−S1) = b0 − b1
    手续费  = −往返成本
    --------------------------------------------------
    净盈亏  = 资金费 + 基差 − 往返成本

为什么必须把基差算进来
----------------------
如果只算资金费，会高估收益：入场时永续通常对现货有溢价（基差为正），
这个溢价在你建仓那一刻就已经"卖出"了；若出场时基差收敛，你确实赚到它，
但若基差走阔，它会吞掉资金费。忽略基差等于假装这笔风险不存在。

已知偏差（报告中必须披露）
--------------------------
1. **幸存者偏差**：标的池取自当前仍挂牌的合约，一年内已下架的没有纳入。
2. **无资金容量约束以外的冲击成本**：滑点用固定值，未按订单簿深度建模。
3. **无永续腿强平建模**：delta 中性下总权益不因价格波动受损，但永续腿
   单独在急涨行情中可能被强平（空头被挤）。本引擎不模拟这一情形。
4. **逐日收盘价成交**：真实成交价会有偏离，且信号到执行有 1 日延迟（已计入）。
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Sequence

HOURS_PER_DAY = 24.0
DAYS_PER_YEAR = 365.0
MS_PER_DAY = 86_400_000
DEFAULT_FUNDING_INTERVAL_HOURS = 8.0


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

@dataclass
class BacktestParams:
    """回测假设。默认值与 config.example.yaml 的 thresholds 段保持一致。"""

    capital: float = 10_000.0            # 初始权益（USDT）
    max_positions: int = 5               # 最大同时持仓数
    perp_margin_ratio: float = 0.20      # 永续腿保证金占名义价值比例（等价 5x）
    hold_days: int = 30                  # 计划持有天数
    min_net_annual_pct: float = 10.0     # 扣费后年化达到该值才开仓
    min_volume_usdt_24h: float = 50_000_000.0
    round_trip_cost_pct: float = 0.36    # 2*(0.10+0.05) + 2*0.03
    exec_lag_days: int = 1               # 信号次日执行
    max_pct_of_volume: float = 0.005     # 单笔名义价值上限 = 当日成交额 × 该比例
    min_hold_days: int = 1
    # 信号口径：
    #   "last"     —— 最近一次已结算费率，按该合约实际间隔年化（与线上扫描一致）
    #   "trailing" —— 最近 N 天实际结算费率之和年化（对结算间隔变化与单次尖峰更稳健）
    signal_mode: str = "last"
    signal_trailing_days: int = 3

    def slot_notional(self) -> float:
        """每个仓位的单腿名义价值：把一份资金均分到 max_positions 个槽位。"""
        if self.max_positions <= 0:
            return 0.0
        slot_capital = self.capital / self.max_positions
        return slot_capital / (1.0 + self.perp_margin_ratio)

    def amortized_cost_pct(self) -> float:
        if self.hold_days <= 0:
            return float("inf")
        return self.round_trip_cost_pct * DAYS_PER_YEAR / self.hold_days


# ---------------------------------------------------------------------------
# 行情序列
# ---------------------------------------------------------------------------

@dataclass
class MarketSeries:
    symbol: str
    dates: list[date]
    index_of: dict[date, int]
    perp_close: dict[date, float]
    spot_close: dict[date, float]
    perp_volume: dict[date, float]
    funding_ts: list[int]
    funding_rate: list[float]
    funding_interval: list[float] = field(default_factory=list)
    # 完整 OHLC，供方向性策略模块使用（资金费套利只用收盘价）
    perp_open: dict[date, float] = field(default_factory=dict)
    perp_high: dict[date, float] = field(default_factory=dict)
    perp_low: dict[date, float] = field(default_factory=dict)

    def basis_pct(self, day: date) -> float | None:
        perp = self.perp_close.get(day)
        spot = self.spot_close.get(day)
        if perp is None or spot is None or spot <= 0:
            return None
        return (perp - spot) / spot * 100.0

    def funding_asof(self, ts_ms: int) -> tuple[float, float] | None:
        """最近一次不晚于 ts_ms 的已结算 (费率, 结算间隔小时数)。"""
        i = bisect.bisect_right(self.funding_ts, ts_ms) - 1
        if i < 0:
            return None
        interval = self.funding_interval[i] if i < len(self.funding_interval) else 8.0
        return self.funding_rate[i], interval

    def funding_sum_between(self, start_ms: int, end_ms: int) -> tuple[int, float]:
        """返回 (结算次数, 费率之和)，窗口为 (start_ms, end_ms]。"""
        lo = bisect.bisect_right(self.funding_ts, start_ms)
        hi = bisect.bisect_right(self.funding_ts, end_ms)
        return hi - lo, sum(self.funding_rate[lo:hi])

    def funding_trailing(self, ts_ms: int, days: int = 3) -> tuple[int, float]:
        """最近 days 天内已结算的 (次数, 费率之和)。比单次费率更稳健。"""
        lo = bisect.bisect_left(self.funding_ts, ts_ms - days * MS_PER_DAY)
        hi = bisect.bisect_right(self.funding_ts, ts_ms)
        return hi - lo, sum(self.funding_rate[lo:hi])


def build_series(record: dict[str, Any]) -> MarketSeries:
    """把 fetch_history 产出的记录转成回测用的序列。

    兼容两种永续行格式：
      旧：[date, close, quote_volume]
      新：[date, open, high, low, close, volume, quote_volume]
    """
    dates: list[date] = []
    perp_close: dict[date, float] = {}
    perp_volume: dict[date, float] = {}
    perp_open: dict[date, float] = {}
    perp_high: dict[date, float] = {}
    perp_low: dict[date, float] = {}
    for row in record["perp"]:
        day = date.fromisoformat(row[0])
        if len(row) >= 7:
            perp_open[day] = float(row[1])
            perp_high[day] = float(row[2])
            perp_low[day] = float(row[3])
            perp_close[day] = float(row[4])
            perp_volume[day] = float(row[6])
        else:
            perp_close[day] = float(row[1])
            perp_volume[day] = float(row[2]) if len(row) > 2 else 0.0
        dates.append(day)

    spot_close: dict[date, float] = {}
    for row in record["spot"]:
        spot_close[date.fromisoformat(row[0])] = float(row[1])

    # 只保留两边都有数据的交易日，避免基差计算出现空洞
    dates = sorted(d for d in set(dates) & set(spot_close))
    index_of = {d: i for i, d in enumerate(dates)}

    funding_ts = [int(r[0]) for r in record["funding"]]
    funding_rate = [float(r[1]) for r in record["funding"]]
    funding_interval = [float(r[2]) if len(r) > 2 else 8.0 for r in record["funding"]]

    return MarketSeries(
        symbol=record["symbol"],
        dates=dates,
        index_of=index_of,
        perp_close=perp_close,
        spot_close=spot_close,
        perp_volume=perp_volume,
        funding_ts=funding_ts,
        funding_rate=funding_rate,
        funding_interval=funding_interval,
        perp_open=perp_open,
        perp_high=perp_high,
        perp_low=perp_low,
    )


# ---------------------------------------------------------------------------
# 信号
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    symbol: str
    signal_date: date
    exec_date: date
    exec_index: int
    rate: float
    gross_annual_pct: float
    net_annual_pct: float
    notional: float


def settlements_per_day(interval_hours: float) -> float:
    if interval_hours <= 0:
        return HOURS_PER_DAY / DEFAULT_FUNDING_INTERVAL_HOURS
    return HOURS_PER_DAY / interval_hours


def gross_annual_pct(
    rate: float, interval_hours: float = DEFAULT_FUNDING_INTERVAL_HOURS
) -> float:
    """把单次结算费率折算成年化。

    必须传入该合约**实际的结算间隔**：币安对部分合约使用 4 小时结算，
    若一律按 8 小时计算，这些合约的年化会被低估一半。
    """
    return rate * settlements_per_day(interval_hours) * DAYS_PER_YEAR * 100.0


def net_annual_pct(
    rate: float,
    params: BacktestParams,
    interval_hours: float = DEFAULT_FUNDING_INTERVAL_HOURS,
) -> float:
    return gross_annual_pct(rate, interval_hours) - params.amortized_cost_pct()


# ---------------------------------------------------------------------------
# 交易与结果
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    entry_date: date
    exit_date: date
    hold_days: int
    notional: float
    signal_net_annual_pct: float
    entry_rate: float
    settlements: int
    funding_pct: float
    basis_pct: float
    cost_pct: float
    net_pct: float
    net_pnl: float

    @property
    def annualized_pct(self) -> float:
        if self.hold_days <= 0:
            return 0.0
        return self.net_pct * DAYS_PER_YEAR / self.hold_days


@dataclass
class BacktestResult:
    params: BacktestParams
    trades: list[Trade]
    equity_dates: list[date]
    equity: list[float]
    open_counts: list[int]
    symbols_used: list[str]
    signals_generated: int = 0          # 通过全部过滤条件的开仓信号数
    signals_skipped_capacity: int = 0   # 因槽位已满或该标的已持仓而放弃的信号数

    # ---- 基础统计 ----

    @property
    def signal_fill_rate_pct(self) -> float:
        total = self.signals_generated + self.signals_skipped_capacity
        if total <= 0:
            return 0.0
        return self.signals_generated / total * 100.0

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def funding_pnl(self) -> float:
        return sum(t.notional * t.funding_pct / 100.0 for t in self.trades)

    @property
    def basis_pnl(self) -> float:
        return sum(t.notional * t.basis_pct / 100.0 for t in self.trades)

    @property
    def cost_pnl(self) -> float:
        return sum(t.notional * t.cost_pct / 100.0 for t in self.trades)

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
        span = self.span_days
        if span <= 0 or self.final_equity <= 0 or self.params.capital <= 0:
            return 0.0
        ratio = self.final_equity / self.params.capital
        return ((ratio ** (DAYS_PER_YEAR / span)) - 1.0) * 100.0

    # ---- 风险指标 ----

    @property
    def max_drawdown_pct(self) -> float:
        peak = -math.inf
        worst = 0.0
        for value in self.equity:
            peak = max(peak, value)
            if peak > 0:
                dd = (peak - value) / peak * 100.0
                worst = max(worst, dd)
        return worst

    @property
    def daily_returns(self) -> list[float]:
        out: list[float] = []
        for prev, cur in zip(self.equity, self.equity[1:]):
            if prev > 0:
                out.append(cur / prev - 1.0)
        return out

    @property
    def sharpe(self) -> float:
        rets = self.daily_returns
        if len(rets) < 2:
            return 0.0
        sd = statistics.pstdev(rets)
        if sd == 0:
            return 0.0
        return statistics.fmean(rets) / sd * math.sqrt(DAYS_PER_YEAR)

    @property
    def volatility_pct(self) -> float:
        rets = self.daily_returns
        if len(rets) < 2:
            return 0.0
        return statistics.pstdev(rets) * math.sqrt(DAYS_PER_YEAR) * 100.0

    @property
    def calmar(self) -> float:
        dd = self.max_drawdown_pct
        return self.cagr_pct / dd if dd > 0 else 0.0

    # ---- 逐笔统计 ----

    @property
    def win_rate_pct(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.net_pnl > 0)
        return wins / len(self.trades) * 100.0

    @property
    def avg_net_pct(self) -> float:
        return statistics.fmean([t.net_pct for t in self.trades]) if self.trades else 0.0

    @property
    def avg_hold_days(self) -> float:
        return statistics.fmean([t.hold_days for t in self.trades]) if self.trades else 0.0

    @property
    def best_trade(self) -> Trade | None:
        return max(self.trades, key=lambda t: t.net_pct) if self.trades else None

    @property
    def worst_trade(self) -> Trade | None:
        return min(self.trades, key=lambda t: t.net_pct) if self.trades else None

    @property
    def avg_open_positions(self) -> float:
        return statistics.fmean(self.open_counts) if self.open_counts else 0.0

    @property
    def utilization_pct(self) -> float:
        if not self.open_counts or self.params.max_positions <= 0:
            return 0.0
        return self.avg_open_positions / self.params.max_positions * 100.0

    @property
    def max_notional_deployed(self) -> float:
        if not self.trades:
            return 0.0
        return max(t.notional for t in self.trades) * self.params.max_positions

    # ---- 分组 ----

    def by_symbol(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for t in self.trades:
            row = out.setdefault(t.symbol, {"trades": 0, "net_pnl": 0.0, "net_pct_sum": 0.0})
            row["trades"] += 1
            row["net_pnl"] += t.net_pnl
            row["net_pct_sum"] += t.net_pct
        for row in out.values():
            row["avg_net_pct"] = row["net_pct_sum"] / row["trades"]
        return out

    def by_month(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for t in self.trades:
            key = f"{t.exit_date.year:04d}-{t.exit_date.month:02d}"
            out[key] = out.get(key, 0.0) + t.net_pnl
        return dict(sorted(out.items()))

    def to_json(self) -> dict[str, Any]:
        return {
            "params": asdict(self.params),
            "summary": {
                "trade_count": self.trade_count,
                "net_pnl": self.net_pnl,
                "funding_pnl": self.funding_pnl,
                "basis_pnl": self.basis_pnl,
                "cost_pnl": self.cost_pnl,
                "final_equity": self.final_equity,
                "total_return_pct": self.total_return_pct,
                "cagr_pct": self.cagr_pct,
                "max_drawdown_pct": self.max_drawdown_pct,
                "sharpe": self.sharpe,
                "volatility_pct": self.volatility_pct,
                "calmar": self.calmar,
                "win_rate_pct": self.win_rate_pct,
                "avg_net_pct": self.avg_net_pct,
                "avg_hold_days": self.avg_hold_days,
                "utilization_pct": self.utilization_pct,
                "span_days": self.span_days,
                "signals_generated": self.signals_generated,
                "signals_skipped_capacity": self.signals_skipped_capacity,
                "signal_fill_rate_pct": self.signal_fill_rate_pct,
            },
            "by_symbol": self.by_symbol(),
            "by_month": self.by_month(),
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
# 主模拟
# ---------------------------------------------------------------------------

def _ms_of(day: date) -> int:
    """当日 23:59:59 UTC 的毫秒时间戳，用作"当日收盘"的时点。"""
    return int(datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc).timestamp() * 1000)


def _unrealized(
    series: MarketSeries,
    entry_index: int,
    day: date,
    notional: float,
    entry_basis_pct: float,
    params: BacktestParams,
) -> float:
    """按 day 收盘盯市一个未平仓位的浮盈。"""
    entry_day = series.dates[entry_index]
    _, rate_sum = series.funding_sum_between(_ms_of(entry_day), _ms_of(day))
    funding = notional * rate_sum
    cur_basis = series.basis_pct(day)
    basis = notional * (entry_basis_pct - cur_basis) / 100.0 if cur_basis is not None else 0.0
    # 建仓成本在建仓时即计一半（另一半在平仓时计）
    entry_cost = notional * (params.round_trip_cost_pct / 2.0) / 100.0
    return funding + basis - entry_cost


def simulate(
    markets: Sequence[MarketSeries],
    params: BacktestParams,
) -> BacktestResult:
    """在给定行情上模拟 delta 中性资金费套利。"""
    by_symbol = {m.symbol: m for m in markets}
    timeline = sorted({d for m in markets for d in m.dates})
    if not timeline:
        return BacktestResult(params, [], [], [], [], [])

    slot = params.slot_notional()

    open_positions: list[dict[str, Any]] = []   # 当前持仓
    pending: dict[date, list[Signal]] = {}      # 待执行的开仓指令
    trades: list[Trade] = []
    equity_dates: list[date] = []
    equity: list[float] = []
    open_counts: list[int] = []
    realized = 0.0
    signals_generated = 0
    signals_skipped = 0

    for day in timeline:
        # ---- 1) 平仓 ----
        still_open: list[dict[str, Any]] = []
        for pos in open_positions:
            if pos["exit_date"] != day:
                still_open.append(pos)
                continue
            series: MarketSeries = pos["series"]
            entry_day = series.dates[pos["entry_index"]]
            settlements, rate_sum = series.funding_sum_between(_ms_of(entry_day), _ms_of(day))
            funding_pct = rate_sum * 100.0
            exit_basis = series.basis_pct(day)
            basis_pct = (pos["entry_basis_pct"] - exit_basis) if exit_basis is not None else 0.0
            cost_pct = params.round_trip_cost_pct
            net_pct = funding_pct + basis_pct - cost_pct
            notional = pos["notional"]
            net_pnl = notional * net_pct / 100.0
            realized += net_pnl
            trades.append(Trade(
                symbol=series.symbol,
                entry_date=entry_day,
                exit_date=day,
                hold_days=(day - entry_day).days,
                notional=notional,
                signal_net_annual_pct=pos["signal_net_annual_pct"],
                entry_rate=pos["entry_rate"],
                settlements=settlements,
                funding_pct=funding_pct,
                basis_pct=basis_pct,
                cost_pct=cost_pct,
                net_pct=net_pct,
                net_pnl=net_pnl,
            ))
        open_positions = still_open

        # ---- 2) 生成今日信号 ----
        # 注意顺序：信号必须先于开仓执行，否则 exec_lag_days=0 时
        # 当日产生的信号会被挂到"已经处理过"的 pending[day] 上而永远不执行。
        for series in markets:
            idx = series.index_of.get(day)
            if idx is None:
                continue
            exec_index = idx + params.exec_lag_days
            # 必须能持满 hold_days，否则不建仓（避免不完整交易污染统计）
            if exec_index + params.hold_days >= len(series.dates):
                continue
            if any(p["series"].symbol == series.symbol for p in open_positions):
                continue
            if any(s.symbol == series.symbol for lst in pending.values() for s in lst):
                continue

            rate_info = series.funding_asof(_ms_of(day))
            if rate_info is None:
                continue
            rate, interval = rate_info

            if params.signal_mode == "trailing":
                count, total = series.funding_trailing(_ms_of(day), params.signal_trailing_days)
                if count <= 0:
                    continue
                gross = total * DAYS_PER_YEAR / params.signal_trailing_days * 100.0
                net_annual = gross - params.amortized_cost_pct()
                rate = total / count          # 用于报告的平均单次费率
            else:
                gross = gross_annual_pct(rate, interval)
                net_annual = gross - params.amortized_cost_pct()

            if net_annual < params.min_net_annual_pct:
                continue

            volume = series.perp_volume.get(day, 0.0)
            if volume < params.min_volume_usdt_24h:
                continue

            # 容量约束：单笔不超过当日成交额的一定比例
            notional = min(slot, volume * params.max_pct_of_volume)
            if notional <= 0:
                continue

            exec_day = series.dates[exec_index]
            pending.setdefault(exec_day, []).append(Signal(
                symbol=series.symbol,
                signal_date=day,
                exec_date=exec_day,
                exec_index=exec_index,
                rate=rate,
                gross_annual_pct=gross,
                net_annual_pct=net_annual,
                notional=notional,
            ))
            signals_generated += 1

        # ---- 3) 开仓（执行到期指令）----
        todays_signals = pending.pop(day, [])
        for order, sig in enumerate(todays_signals):
            if len(open_positions) >= params.max_positions:
                signals_skipped += len(todays_signals) - order
                break
            if any(p["series"].symbol == sig.symbol for p in open_positions):
                signals_skipped += 1
                continue
            series = by_symbol[sig.symbol]
            entry_basis = series.basis_pct(sig.exec_date)
            if entry_basis is None:
                signals_skipped += 1
                continue
            open_positions.append({
                "series": series,
                "entry_index": sig.exec_index,
                "exit_date": series.dates[sig.exec_index + params.hold_days],
                "notional": sig.notional,
                "entry_basis_pct": entry_basis,
                "signal_net_annual_pct": sig.net_annual_pct,
                "entry_rate": sig.rate,
            })

        # ---- 4) 盯市 ----
        unrealized = 0.0
        for pos in open_positions:
            series = pos["series"]
            if series.index_of.get(day) is None:
                continue
            unrealized += _unrealized(
                series, pos["entry_index"], day, pos["notional"],
                pos["entry_basis_pct"], params,
            )
        equity_dates.append(day)
        equity.append(params.capital + realized + unrealized)
        open_counts.append(len(open_positions))

    return BacktestResult(
        params=params,
        trades=sorted(trades, key=lambda t: (t.entry_date, t.symbol)),
        equity_dates=equity_dates,
        equity=equity,
        open_counts=open_counts,
        symbols_used=sorted({t.symbol for t in trades}),
        signals_generated=signals_generated,
        signals_skipped_capacity=signals_skipped,
    )


# ---------------------------------------------------------------------------
# 文本报告
# ---------------------------------------------------------------------------

def format_summary(result: BacktestResult, title: str = "") -> str:
    p = result.params
    lines: list[str] = []
    if title:
        lines += [title, "=" * 78]
    lines += [
        f"初始权益        {p.capital:>12,.0f} USDT",
        f"最终权益        {result.final_equity:>12,.2f} USDT",
        f"净利润          {result.net_pnl:>12,.2f} USDT   ({result.total_return_pct:+.2f}%)",
        f"  其中 资金费   {result.funding_pnl:>12,.2f} USDT",
        f"  其中 基差     {result.basis_pnl:>12,.2f} USDT",
        f"  其中 手续费   {-result.cost_pnl:>12,.2f} USDT",
        "",
        f"交易笔数        {result.trade_count:>12d}",
        f"胜率            {result.win_rate_pct:>12.1f} %",
        f"平均单笔净收益  {result.avg_net_pct:>12.3f} %（占名义价值）",
        f"平均持有        {result.avg_hold_days:>12.1f} 天",
        f"年化收益(CAGR)  {result.cagr_pct:>12.2f} %",
        f"最大回撤        {result.max_drawdown_pct:>12.2f} %",
        f"年化波动        {result.volatility_pct:>12.2f} %",
        f"夏普            {result.sharpe:>12.2f}",
        f"Calmar          {result.calmar:>12.2f}",
        f"资金利用率      {result.utilization_pct:>12.1f} %（平均占用 "
        f"{result.avg_open_positions:.2f}/{p.max_positions} 个槽位）",
        f"信号/成交       {result.signals_generated:>12d} 个信号 → "
        f"{result.trade_count} 笔成交（{result.signal_fill_rate_pct:.0f}% 被采纳）",
        f"回测区间        {result.span_days:>12d} 天",
        f"覆盖标的        {len(result.symbols_used):>12d} 个",
    ]
    return "\n".join(lines)
