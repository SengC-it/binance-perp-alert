"""编排层：把数据、计算、规则、投递串成一个守护循环。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, tzinfo

from .binance_client import BinanceError, BinanceFuturesClient
from .config import Config
from .cross_exchange import (
    CrossExchangeOpportunity,
    ExchangeError,
    PublicExchangeClient,
    binance_quotes,
    build_cross_opportunities,
    format_cross_table,
)
from .config import Config, threshold
from .directional_engine import (
    DirectionalSignal,
    SymbolVol,
    build_signal,
    build_symbol_vol,
    format_signal_table,
    parse_klines,
)
from .forward_check import record_signal, render_reconciliation, verify_pending
from .models import AccountSnapshot, FundingOpportunity
from .notifier import Notifier
from .opportunity_engine import format_opportunity_table, scan_opportunities
from .position_engine import build_snapshot
from .rules import (
    RuleEngine,
    evaluate_api_error_rule,
    evaluate_cross_exchange_rules,
    evaluate_directional_rules,
    evaluate_heartbeat_rule,
    evaluate_opportunity_rules,
    evaluate_position_rules,
)
from .store import Store
from .timeutil import fmt_local

log = logging.getLogger(__name__)

POSITION_COMPONENT = "positions"
OPPORTUNITY_COMPONENT = "opportunity"
CROSS_COMPONENT = "cross_exchange"
DIRECTIONAL_COMPONENT = "directional"
TICK_SECONDS = 5


class AlertService:
    def __init__(
        self,
        cfg: Config,
        tz: tzinfo,
        store: Store,
        client: BinanceFuturesClient,
        notifier: Notifier,
        cross_client: PublicExchangeClient | None = None,
    ):
        self.cfg = cfg
        self.tz = tz
        self.store = store
        self.client = client
        self.notifier = notifier
        self.cross_client = cross_client
        self.engine = RuleEngine(cfg, store, tz)
        # 每条链路各自计数，避免一条链路持续失败被另一条的成功掩盖
        self.failures: dict[str, int] = {
            POSITION_COMPONENT: 0,
            OPPORTUNITY_COMPONENT: 0,
            CROSS_COMPONENT: 0,
            DIRECTIONAL_COMPONENT: 0,
        }
        self.last_errors: dict[str, str] = {
            POSITION_COMPONENT: "",
            OPPORTUNITY_COMPONENT: "",
            CROSS_COMPONENT: "",
            DIRECTIONAL_COMPONENT: "",
        }
        self.last_snapshot: AccountSnapshot | None = None
        self.last_opportunities: list[FundingOpportunity] = []
        self.last_cross: list[CrossExchangeOpportunity] = []
        self.last_directional: DirectionalSignal | None = None
        # 启动时写入基线心跳，避免"进程刚起来"就被判定为心跳丢失而误报
        self.store.heartbeat_ok(
            POSITION_COMPONENT, datetime.now(timezone.utc), "startup baseline"
        )

    # ---------- 持仓链路 ----------

    def poll_positions(self, now: datetime) -> AccountSnapshot | None:
        try:
            account = self.client.account()
            positions_v2 = self.client.position_risk_v2()
            try:
                positions_v3 = self.client.position_risk_v3()
            except BinanceError as exc:
                log.warning("v3 持仓接口不可用，维持保证金将使用估算值：%s", exc)
                positions_v3 = None
            premium_raw = self.client.premium_index()
            premium_map = {
                str(item.get("symbol", "")).upper(): item
                for item in (premium_raw if isinstance(premium_raw, list) else [premium_raw])
            }
        except BinanceError as exc:
            self.failures[POSITION_COMPONENT] += 1
            self.last_errors[POSITION_COMPONENT] = str(exc)
            log.error(
                "持仓轮询失败（连续 %d 次）：%s", self.failures[POSITION_COMPONENT], exc
            )
            return None

        self.failures[POSITION_COMPONENT] = 0
        self.last_errors[POSITION_COMPONENT] = ""

        snapshot = build_snapshot(
            account_raw=account,
            positions_v2=positions_v2,
            positions_v3=positions_v3,
            premium_map=premium_map,
            cfg=self.cfg,
            store=self.store,
            now=now,
            tz=self.tz,
        )
        self.last_snapshot = snapshot
        self.store.heartbeat_ok(
            POSITION_COMPONENT,
            now,
            f"equity={snapshot.equity:.2f} positions={len(snapshot.positions)}",
        )

        results = evaluate_position_rules(self.cfg, snapshot)
        for decision in self.engine.process(results, now):
            self.notifier.dispatch(decision.alert, decision.immediate, now)

        log.info(
            "持仓轮询 | 权益 %.2f | 持仓 %d | 组合风险 %.2f%% | 日亏损 %.2f%% | 回撤 %.2f%%",
            snapshot.equity,
            len(snapshot.positions),
            snapshot.portfolio_heat_pct,
            snapshot.daily_loss_pct,
            snapshot.drawdown_pct,
        )
        return snapshot

    # ---------- 机会链路 ----------

    def scan_opportunities(self, now: datetime) -> list[FundingOpportunity]:
        try:
            premium_raw = self.client.premium_index()
            ticker_raw = self.client.ticker_24hr()
        except BinanceError as exc:
            self.failures[OPPORTUNITY_COMPONENT] += 1
            self.last_errors[OPPORTUNITY_COMPONENT] = str(exc)
            log.error(
                "机会扫描失败（连续 %d 次）：%s", self.failures[OPPORTUNITY_COMPONENT], exc
            )
            return []

        self.failures[OPPORTUNITY_COMPONENT] = 0
        self.last_errors[OPPORTUNITY_COMPONENT] = ""

        opportunities = scan_opportunities(premium_raw, ticker_raw, self.cfg)
        self.last_opportunities = opportunities

        results = evaluate_opportunity_rules(self.cfg, opportunities)
        for decision in self.engine.process(results, now):
            self.notifier.dispatch(decision.alert, decision.immediate, now)

        if opportunities:
            best = opportunities[0]
            log.info(
                "机会扫描 | 通过流动性过滤 %d 个合约 | 净年化最高 %s %.2f%%",
                len(opportunities),
                best.symbol,
                best.net_annual_pct,
            )
        else:
            log.info("机会扫描 | 没有通过流动性过滤的合约")
        return opportunities

    # ---------- 方向性链路 ----------

    def scan_directional(self, now: datetime) -> DirectionalSignal | None:
        """截面低波动扫描。

        需要拉取每个标的的日线，因此先用成交额筛出流动性最好的前 N 个，
        把请求数控制住。任何一个标的拉取失败都跳过，不影响整体。
        """
        try:
            ticker_raw = self.client.ticker_24hr()
        except BinanceError as exc:
            self.failures[DIRECTIONAL_COMPONENT] += 1
            self.last_errors[DIRECTIONAL_COMPONENT] = str(exc)
            log.error("方向性扫描失败（连续 %d 次）：%s",
                      self.failures[DIRECTIONAL_COMPONENT], exc)
            return None

        min_volume = threshold(self.cfg, "min_volume_usdt_24h", 50_000_000.0)
        top_n = int(threshold(self.cfg, "xs_scan_top_n", 60))
        lookback = int(threshold(self.cfg, "xs_lookback_days", 30))

        candidates: list[tuple[str, float]] = []
        for item in ticker_raw:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol.endswith("USDT"):
                continue
            try:
                volume = float(item.get("quoteVolume") or 0.0)
            except (TypeError, ValueError):
                continue
            if volume >= min_volume:
                candidates.append((symbol, volume))
        candidates.sort(key=lambda kv: kv[1], reverse=True)
        candidates = candidates[:top_n]

        vols: list[SymbolVol] = []
        failures = 0
        for symbol, volume in candidates:
            try:
                raw = self.client.klines(symbol, "1d", lookback + 2)
            except BinanceError as exc:
                failures += 1
                log.debug("K 线拉取失败 %s：%s", symbol, exc)
                continue
            closes = parse_klines(raw)
            snap = build_symbol_vol(symbol, closes, volume, lookback)
            if snap is not None:
                vols.append(snap)

        if failures:
            log.warning("方向性扫描：%d 个标的的 K 线拉取失败", failures)

        self.failures[DIRECTIONAL_COMPONENT] = 0
        self.last_errors[DIRECTIONAL_COMPONENT] = ""

        min_symbols = int(threshold(self.cfg, "xs_min_symbols", 20))
        k_long = int(threshold(self.cfg, "xs_k_long", 5))
        k_short = int(threshold(self.cfg, "xs_k_short", 5))

        if len(vols) < max(min_symbols, k_long + k_short):
            # 标的不足时保持沉默，而不是拿半个组合凑数
            log.info("方向性扫描 | 仅 %d 个标的可用，低于下限 %d，保持沉默",
                     len(vols), max(min_symbols, k_long + k_short))
            self.last_directional = None
            return None

        signal = build_signal(
            vols, k_long, k_short, lookback, now.astimezone(self.tz).strftime("%Y-%m-%d")
        )
        self.last_directional = signal

        # 前向验证：每条信号都登记下来，到期后用真实行情回填。
        # 一年回测在统计上不足以证明正期望，只有持续对账才能。
        try:
            record_signal(self.store, signal, self.cfg)
        except Exception as exc:
            log.warning("前向验证登记失败（不影响告警）：%s", exc)

        results = evaluate_directional_rules(self.cfg, signal)
        for decision in self.engine.process(results, now):
            self.notifier.dispatch(decision.alert, decision.immediate, now)

        log.info("方向性扫描 | %d 个标的 | 做多 %s | 做空 %s",
                 len(vols),
                 ",".join(v.symbol for v in signal.longs),
                 ",".join(v.symbol for v in signal.shorts))
        return signal

    # ---------- 跨所链路 ----------

    def scan_cross_exchange(self, now: datetime) -> list[CrossExchangeOpportunity]:
        if not self.cfg.cross_enabled or self.cross_client is None:
            return []

        quotes = []
        try:
            # 币安侧复用公开接口，不需要私有 Key
            premium_raw = self.client.premium_index()
            ticker_raw = self.client.ticker_24hr()
            quotes.extend(binance_quotes(premium_raw, ticker_raw))

            if "bybit" in self.cfg.cross_exchanges:
                quotes.extend(self.cross_client.bybit_quotes(self.cfg.bybit_base_url))
            if "okx" in self.cfg.cross_exchanges:
                quotes.extend(
                    self.cross_client.okx_quotes(
                        self.cfg.okx_base_url, self.cfg.okx_max_symbols
                    )
                )
        except (ExchangeError, BinanceError) as exc:
            self.failures[CROSS_COMPONENT] += 1
            self.last_errors[CROSS_COMPONENT] = str(exc)
            log.error(
                "跨所扫描失败（连续 %d 次）：%s", self.failures[CROSS_COMPONENT], exc
            )
            return []

        self.failures[CROSS_COMPONENT] = 0
        self.last_errors[CROSS_COMPONENT] = ""

        opportunities = build_cross_opportunities(quotes, self.cfg)
        self.last_cross = opportunities

        results = evaluate_cross_exchange_rules(self.cfg, opportunities)
        for decision in self.engine.process(results, now):
            self.notifier.dispatch(decision.alert, decision.immediate, now)

        if opportunities:
            best = opportunities[0]
            log.info(
                "跨所扫描 | 报价 %d 条 | 价差标的 %d 个 | 净年化最高 %s %.2f%%（%s 空 / %s 多）",
                len(quotes),
                len(opportunities),
                best.base,
                best.net_annual_pct,
                best.high.exchange,
                best.low.exchange,
            )
        else:
            log.info("跨所扫描 | 报价 %d 条 | 没有满足流动性要求的跨所价差", len(quotes))
        return opportunities

    # ---------- 系统规则 ----------

    def check_system_rules(self, now: datetime) -> None:
        results = [
            evaluate_heartbeat_rule(self.cfg, self.store, now, self.tz, POSITION_COMPONENT),
        ]
        for component in (POSITION_COMPONENT, OPPORTUNITY_COMPONENT, CROSS_COMPONENT):
            results.append(
                evaluate_api_error_rule(
                    self.cfg,
                    component,
                    self.failures[component],
                    self.last_errors[component],
                    now,
                    self.tz,
                )
            )
        for decision in self.engine.process(results, now):
            self.notifier.dispatch(decision.alert, decision.immediate, now)

    def maybe_send_digest(self, now: datetime) -> None:
        if self.notifier.should_send_digest(now):
            if self.notifier.send_digest(now):
                self.notifier.mark_digest_sent(now)

    # ---------- 守护循环 ----------

    def run_forever(self) -> None:
        log.info(
            "启动守护循环 | 持仓 %ds | 机会 %ds | 跨所 %ds | 方向性 %ds | 心跳 %ds | 摘要 %s | 时区 %s",
            self.cfg.positions_seconds,
            self.cfg.opportunity_seconds,
            self.cfg.cross_seconds,
            self.cfg.directional_seconds,
            self.cfg.heartbeat_seconds,
            self.cfg.digest_at.strftime("%H:%M"),
            self.cfg.timezone,
        )
        last_positions = 0.0
        last_opportunity = 0.0
        last_cross = 0.0
        last_directional = 0.0
        last_heartbeat = 0.0

        while True:
            now = datetime.now(timezone.utc)
            monotonic = time.monotonic()
            try:
                if monotonic - last_positions >= self.cfg.positions_seconds:
                    self.poll_positions(now)
                    last_positions = monotonic
                if monotonic - last_opportunity >= self.cfg.opportunity_seconds:
                    self.scan_opportunities(now)
                    last_opportunity = monotonic
                if monotonic - last_cross >= self.cfg.cross_seconds:
                    self.scan_cross_exchange(now)
                    last_cross = monotonic
                if monotonic - last_directional >= self.cfg.directional_seconds:
                    self.scan_directional(now)
                    last_directional = monotonic
                if monotonic - last_heartbeat >= self.cfg.heartbeat_seconds:
                    self.check_system_rules(now)
                    last_heartbeat = monotonic
                self.maybe_send_digest(now)
            except Exception:  # 任何未预期异常都不应让守护进程退出
                log.exception("主循环出现未预期异常，本轮跳过")
            time.sleep(TICK_SECONDS)


def format_cross_scan(opportunities: list[CrossExchangeOpportunity], cfg: Config) -> str:
    """渲染跨所机会榜，供 cross 命令使用。"""
    from .cross_exchange import cross_leg_cost_pct

    hold_days = float(cfg.thresholds.get("assumed_hold_days", 30.0))
    lines = [
        "=" * 100,
        "跨交易所资金费价差扫描",
        "=" * 100,
        f"  参与交易所        : {', '.join(cfg.cross_exchanges)}（+ 币安作为基准）",
        f"  假设持有天数      : {hold_days:.0f} 天",
        f"  两条腿往返成本    : {cross_leg_cost_pct(cfg):.3f}% 名义价值（4 笔成交）",
        f"  最低 24h 成交额   : "
        f"{cfg.thresholds.get('min_volume_usdt_24h', 5e7) / 1e8:.2f} 亿 USDT",
        f"  满足条件的标的    : {len(opportunities)} 个",
        "-" * 100,
        format_cross_table(opportunities, top_n=20),
        "=" * 100,
        "  方向：在费率高的所做空，在费率低的所做多，赚取费率差。",
        "  说明：净年化 = 费率差年化 - 往返成本 x 365 / 持有天数。",
        "        跨所需要两所各留保证金，资金效率低于单所内套利；",
        "        且两个永续之间的价差本身会波动，建仓时间差会带来单边暴露。",
    ]
    return "\n".join(lines)


def format_snapshot(snapshot: AccountSnapshot, tz: tzinfo) -> str:
    """把快照渲染成人类可读文本，供 --once 模式查看。"""
    lines = [
        "=" * 62,
        f"账户快照  {fmt_local(snapshot.fetched_at, tz)}",
        "=" * 62,
        f"  权益            : {snapshot.equity:,.2f} USDT",
        f"  可用余额        : {snapshot.available_balance:,.2f} USDT",
        f"  组合风险敞口    : {snapshot.portfolio_heat_pct:.2f}% 权益",
        f"  当日亏损        : {snapshot.daily_loss_pct:.2f}%",
        f"  自峰值回撤      : {snapshot.drawdown_pct:.2f}%",
        f"  持仓数量        : {len(snapshot.positions)}",
    ]
    for p in snapshot.positions:
        ratio = p.liq_to_stop_ratio
        lines += [
            "-" * 62,
            f"  {p.symbol}  {p.side}  {p.leverage:.0f}x",
            f"    名义价值        : {p.notional:,.2f} USDT",
            f"    入场 / 标记价   : {p.entry_price:,.4f} / {p.mark_price:,.4f}",
            f"    未实现盈亏      : {p.unrealized_pnl:,.2f} USDT",
            f"    保证金率        : {p.margin_ratio * 100:.2f}%"
            + ("（估算）" if p.margin_ratio_estimated else ""),
            f"    强平价 / 距离   : {p.liq_price:,.4f} / {p.liq_distance_pct * 100:.2f}%",
            f"    强平/止损倍数   : {'未配置止损' if ratio is None else f'{ratio:.2f}x'}",
            f"    1R 金额         : {p.r_value_usdt:,.2f} USDT",
            f"    累计资金费      : {p.funding_cost_usdt:,.2f} USDT",
        ]
    lines.append("=" * 62)
    return "\n".join(lines)


def format_scan(opportunities: list[FundingOpportunity], cfg: Config) -> str:
    """渲染机会榜，供 scan 命令使用。"""
    from .opportunity_engine import round_trip_cost_pct

    hold_days = float(cfg.thresholds.get("assumed_hold_days", 30.0))
    lines = [
        "=" * 84,
        "资金费 / 基差机会扫描",
        "=" * 84,
        f"  假设持有天数      : {hold_days:.0f} 天",
        f"  往返成本（VIP0）  : {round_trip_cost_pct(cfg):.3f}% 名义价值",
        f"  最低 24h 成交额   : "
        f"{cfg.thresholds.get('min_volume_usdt_24h', 5e7) / 1e8:.2f} 亿 USDT",
        f"  通过过滤的合约    : {len(opportunities)} 个",
        "-" * 84,
        format_opportunity_table(opportunities, top_n=20),
        "=" * 84,
        "  说明：净年化 = 毛年化 - 往返成本 x 365 / 持有天数。",
        "        资金费率会变，此处为按当前费率线性外推的估算值，不是可锁定收益。",
    ]
    return "\n".join(lines)
