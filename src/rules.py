"""规则引擎 —— 本系统真正的核心缺口。

职责：
  1. 把指标转成布尔判断（阈值）
  2. 持续性确认：连续 N 次为真才告警，避免单次插针误报
  3. 去重 + 冷却：同一 (规则, 标的, 方向) 在冷却期内不重复发
  4. 分级路由：CRITICAL 立即发；其余在静默期或超限流时进每日摘要
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

from .config import Config, threshold
from .cross_exchange import CrossExchangeOpportunity
from .directional_engine import DirectionalSignal, evidence_block, position_scale
from .models import AccountSnapshot, Alert, FundingOpportunity, PositionRisk, Severity
from .store import Store
from .timeutil import fmt_local, in_quiet_hours

# 规则默认值。此处只有 confirmations 会被引擎消费；
# severity 记录的是规则的"基础分级"，仅作文档用途——
# 实际分级由规则函数按阈值动态计算（如保证金率跨过危险线时从 WARN 升级为 CRITICAL），
# 再由 config.yaml 的 rules 段显式覆盖。切勿用此处的 severity 覆盖计算结果。
DEFAULTS: dict[str, dict] = {
    "liq_distance_insufficient": {"severity": Severity.CRITICAL, "confirmations": 2},
    "margin_ratio_low": {"severity": Severity.WARN, "confirmations": 3},
    "funding_cost_high": {"severity": Severity.WARN, "confirmations": 3},
    "portfolio_heat_exceeded": {"severity": Severity.WARN, "confirmations": 3},
    "daily_loss_limit": {"severity": Severity.WARN, "confirmations": 2},
    "drawdown_stop": {"severity": Severity.CRITICAL, "confirmations": 1},
    "heartbeat_stale": {"severity": Severity.CRITICAL, "confirmations": 1},
    "api_error": {"severity": Severity.WARN, "confirmations": 1},
    # 机会类：默认走摘要，不占用即时通道
    # 注意：funding_opportunity 在 2025-09 ~ 2026-08 的回测中为负期望，
    # config.yaml 里默认已关闭。保留代码是为了可复现与将来重验。
    "funding_opportunity": {"severity": Severity.INFO, "confirmations": 2},
    "basis_wide": {"severity": Severity.WARN, "confirmations": 3},
    "cross_exchange_funding": {"severity": Severity.INFO, "confirmations": 2},
    # 方向性：唯一通过全部统计检验的截面策略（见 src/directional_engine.py）
    "xs_lowvol_signal": {"severity": Severity.INFO, "confirmations": 1},
    "xs_lowvol_squeeze": {"severity": Severity.WARN, "confirmations": 1},
}

ACCOUNT_KEY = "ACCOUNT"


@dataclass
class RuleResult:
    """一次规则评估的原始结果（尚未经过持续性确认与冷却）。"""

    rule_id: str
    dedup_key: str
    symbol: str
    triggered: bool
    severity: Severity
    title: str
    body: str


@dataclass
class Decision:
    """通过全部闸门、可以投递的告警。"""

    alert: Alert
    immediate: bool  # True = 立刻发；False = 进每日摘要


def _side_cn(side: str) -> str:
    return "多" if side == "LONG" else "空"


def _fmt_pct(value: float, digits: int = 2) -> str:
    return f"{value * 100:.{digits}f}%"


# --------------------------------------------------------------------------
# 持仓类规则
# --------------------------------------------------------------------------


def _rule_liq_distance(cfg: Config, p: PositionRisk) -> RuleResult:
    limit = threshold(cfg, "liq_to_stop_min_ratio", 3.0)
    ratio = p.liq_to_stop_ratio
    triggered = ratio is not None and ratio < limit
    if ratio is None:
        body = (
            f"标的 {p.symbol} 未配置 planned_stop_pct，无法计算强平距离倍数。\n"
            f"强平距离：{_fmt_pct(p.liq_distance_pct)}  强平价：{p.liq_price:,.4f}"
        )
    else:
        body = (
            f"标的：{p.symbol}（{_side_cn(p.side)}）\n"
            f"强平距离 / 止损距离 = {ratio:.2f}x，低于安全下限 {limit:.2f}x\n\n"
            f"标记价：{p.mark_price:,.4f}\n"
            f"强平价：{p.liq_price:,.4f}\n"
            f"强平距离：{_fmt_pct(p.liq_distance_pct)}\n"
            f"计划止损距离：{_fmt_pct(p.planned_stop_pct or 0)}\n"
            f"当前杠杆：{p.leverage:.0f}x  名义价值：{p.notional:,.2f} USDT\n\n"
            f"动作建议：降低名义价值或调低杠杆设置，使强平距离恢复到止损距离的 3 倍以上。"
        )
    return RuleResult(
        rule_id="liq_distance_insufficient",
        dedup_key=f"{p.symbol}:{p.side}",
        symbol=p.symbol,
        triggered=triggered,
        severity=Severity.CRITICAL,
        title=f"{p.symbol} 强平距离不足",
        body=body,
    )


def _rule_margin_ratio(cfg: Config, p: PositionRisk) -> RuleResult:
    warn = threshold(cfg, "margin_ratio_warn", 0.60)
    crit = threshold(cfg, "margin_ratio_critical", 0.80)
    triggered = p.margin_ratio >= warn
    severity = Severity.CRITICAL if p.margin_ratio >= crit else Severity.WARN
    body = (
        f"标的：{p.symbol}（{_side_cn(p.side)}）\n"
        f"保证金率 = {_fmt_pct(p.margin_ratio)}（100% 即触发强平）\n"
        f"预警线 {_fmt_pct(warn)} / 危险线 {_fmt_pct(crit)}\n\n"
        f"维持保证金：{p.maint_margin:,.2f} USDT\n"
        f"逐仓保证金：{p.isolated_margin:,.2f} USDT\n"
        f"标记价：{p.mark_price:,.4f}  强平价：{p.liq_price:,.4f}\n"
        f"未实现盈亏：{p.unrealized_pnl:,.2f} USDT\n\n"
        f"动作建议：追加保证金或主动减仓，不要等待系统强平。"
    )
    return RuleResult(
        rule_id="margin_ratio_low",
        dedup_key=f"{p.symbol}:{p.side}",
        symbol=p.symbol,
        triggered=triggered,
        severity=severity,
        title=f"{p.symbol} 保证金率偏高（{_fmt_pct(p.margin_ratio)}）",
        body=body,
    )


def _rule_funding_cost(cfg: Config, p: PositionRisk) -> RuleResult:
    limit = threshold(cfg, "funding_cost_r_warn", 0.30)
    cost_r = p.funding_cost_r
    triggered = cost_r is not None and cost_r >= limit
    body = (
        f"标的：{p.symbol}（{_side_cn(p.side)}）\n"
        f"累计资金费成本 = {cost_r if cost_r is None else round(cost_r, 3)} R，"
        f"超过阈值 {limit:.2f} R\n\n"
        f"累计资金费：{p.funding_cost_usdt:,.2f} USDT\n"
        f"1R 金额：{p.r_value_usdt:,.2f} USDT\n"
        f"当前资金费率：{_fmt_pct(p.funding_rate, 4)} / 8h\n\n"
        f"说明：资金费按当前费率线性外推估算，仅用于提示成本方向。\n"
        f"动作建议：若持有逻辑已不成立，考虑提前平仓以停止资金费损耗。"
    )
    return RuleResult(
        rule_id="funding_cost_high",
        dedup_key=f"{p.symbol}:{p.side}",
        symbol=p.symbol,
        triggered=triggered,
        severity=Severity.WARN,
        title=f"{p.symbol} 资金费成本累积偏高",
        body=body,
    )


def _rule_portfolio_heat(cfg: Config, snap: AccountSnapshot) -> RuleResult:
    limit = threshold(cfg, "portfolio_heat_max_pct", 4.0)
    heat = snap.portfolio_heat_pct
    triggered = heat > limit
    lines = "\n".join(
        f"  {p.symbol}（{_side_cn(p.side)}）1R = {p.r_value_usdt:,.2f} USDT"
        for p in snap.positions
        if p.r_value_usdt > 0
    ) or "  （无已配置止损距离的持仓）"
    body = (
        f"全部持仓止损同时触发时的风险敞口 = {heat:.2f}% 权益，"
        f"超过上限 {limit:.2f}%\n\n"
        f"账户权益：{snap.equity:,.2f} USDT\n"
        f"风险明细：\n{lines}\n\n"
        f"动作建议：减少同时在场的持仓数量或降低单笔风险。"
    )
    return RuleResult(
        rule_id="portfolio_heat_exceeded",
        dedup_key=ACCOUNT_KEY,
        symbol="-",
        triggered=triggered,
        severity=Severity.WARN,
        title=f"组合风险敞口超限（{heat:.2f}%）",
        body=body,
    )


def _rule_daily_loss(cfg: Config, snap: AccountSnapshot) -> RuleResult:
    warn = threshold(cfg, "daily_loss_warn_pct", 2.0)
    crit = threshold(cfg, "daily_loss_critical_pct", 3.0)
    loss = snap.daily_loss_pct
    triggered = loss >= warn
    severity = Severity.CRITICAL if loss >= crit else Severity.WARN
    body = (
        f"当日亏损 = {loss:.2f}% 权益\n"
        f"预警线 {warn:.2f}% / 危险线 {crit:.2f}%\n\n"
        f"当前权益：{snap.equity:,.2f} USDT\n\n"
        f"动作建议：按交易计划书的纪律，当日停止开新仓并关闭终端。"
    )
    return RuleResult(
        rule_id="daily_loss_limit",
        dedup_key=ACCOUNT_KEY,
        symbol="-",
        triggered=triggered,
        severity=severity,
        title=f"当日亏损达 {loss:.2f}%",
        body=body,
    )


def _rule_drawdown(cfg: Config, snap: AccountSnapshot) -> RuleResult:
    limit = threshold(cfg, "drawdown_stop_pct", 20.0)
    dd = snap.drawdown_pct
    triggered = dd >= limit
    body = (
        f"自峰值回撤 = {dd:.2f}%，已达到停机线 {limit:.2f}%\n\n"
        f"当前权益：{snap.equity:,.2f} USDT\n\n"
        f"动作建议：按交易计划书执行全面停机，回到回测阶段重审优势假设，"
        f"在复盘完成前不要恢复实盘。"
    )
    return RuleResult(
        rule_id="drawdown_stop",
        dedup_key=ACCOUNT_KEY,
        symbol="-",
        triggered=triggered,
        severity=Severity.CRITICAL,
        title=f"回撤达到停机线（{dd:.2f}%）",
        body=body,
    )


def evaluate_position_rules(cfg: Config, snap: AccountSnapshot) -> list[RuleResult]:
    """持仓类 + 账户类规则。每轮全部返回，由引擎决定是否告警。"""
    results: list[RuleResult] = []
    for p in snap.positions:
        results.append(_rule_liq_distance(cfg, p))
        results.append(_rule_margin_ratio(cfg, p))
        results.append(_rule_funding_cost(cfg, p))
    results.append(_rule_portfolio_heat(cfg, snap))
    results.append(_rule_daily_loss(cfg, snap))
    results.append(_rule_drawdown(cfg, snap))
    return results


# --------------------------------------------------------------------------
# 机会类规则（资金费 / 基差）
# --------------------------------------------------------------------------


def _rule_funding_opportunity(cfg: Config, opp: FundingOpportunity) -> RuleResult:
    limit = threshold(cfg, "min_net_annual_pct", 10.0)
    triggered = opp.net_annual_pct >= limit
    direction = "做空永续 + 做多现货" if opp.is_short_perp_direction else "做多永续 + 做空现货"
    next_ts = (
        opp.next_funding_time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if opp.next_funding_time
        else "-"
    )
    body = (
        f"标的：{opp.symbol}\n"
        f"净年化收益 = {opp.net_annual_pct:.2f}%（阈值 {limit:.2f}%）\n\n"
        f"资金费率：{opp.funding_rate * 100:.4f}% / 8h\n"
        f"毛年化：{opp.gross_annual_pct:.2f}%\n"
        f"往返成本摊销（按持有 {opp.hold_days:.0f} 天）："
        f"{opp.gross_annual_pct - opp.net_annual_pct:.2f}%\n\n"
        f"标记价：{opp.mark_price:,.4f}   指数价：{opp.index_price:,.4f}\n"
        f"基差：{opp.basis_pct:+.3f}%\n"
        f"24h 成交额：{opp.quote_volume_24h / 1e8:.2f} 亿 USDT\n"
        f"下次结算：{next_ts}\n\n"
        f"方向：{direction}\n"
        f"动作建议：先核对现货与永续的实际盘口深度能否承接目标名义价值，"
        f"再按交易计划书的测试协议验证，不要直接按年化数字下单。\n"
        f"注意：资金费率会变，年化是按当前费率线性外推的估算值。\n\n"
        f"── 重要：这个信号在回测中是负期望的 ──\n"
        f"2025-09 ~ 2026-08 的样本里，25 个（信号窗口 × 持有期）组合 × 4 个成本水平\n"
        f"共 100 个配置，**没有任何一个**在笔数 ≥ 30 的前提下取得正的平均净收益。\n"
        f"根因有两条：\n"
        f"  1. 高资金费不可持续。入场时费率年化 8~12% 的那一桶，"
        f"持有期内实际收到的资金费是 **-0.337%**（即转为付出）。\n"
        f"  2. 基差贡献虽略为正，但幅度（约 0.03% / 30 天）远小于往返成本（0.36%）。\n"
        f"本规则默认已在 config.yaml 中关闭。若你仍要开启，请把它当作\n"
        f"「观察资金费环境」的信息，而**不要**当作可交易的收益来源。"
    )
    return RuleResult(
        rule_id="funding_opportunity",
        dedup_key=opp.symbol,
        symbol=opp.symbol,
        triggered=triggered,
        severity=Severity.INFO,
        title=f"{opp.symbol} 资金费机会 净年化 {opp.net_annual_pct:.1f}%",
        body=body,
    )


def _rule_basis_wide(cfg: Config, opp: FundingOpportunity) -> RuleResult:
    limit = threshold(cfg, "basis_warn_pct", 0.30)
    triggered = abs(opp.basis_pct) >= limit
    body = (
        f"标的：{opp.symbol}\n"
        f"基差 = {opp.basis_pct:+.3f}%，超过阈值 ±{limit:.3f}%\n\n"
        f"标记价：{opp.mark_price:,.4f}   指数价：{opp.index_price:,.4f}\n"
        f"资金费率：{opp.funding_rate * 100:.4f}% / 8h\n"
        f"净年化（持有 {opp.hold_days:.0f} 天）：{opp.net_annual_pct:.2f}%\n"
        f"24h 成交额：{opp.quote_volume_24h / 1e8:.2f} 亿 USDT\n\n"
        f"含义：永续相对指数{'溢价' if opp.basis_pct > 0 else '折价'}，"
        f"通常伴随资金费同向偏移。\n"
        f"动作建议：确认是趋势性溢价还是短时错价；若是后者，收敛时点难以预测，"
        f"不要把基差本身当成可交易的收益来源。"
    )
    return RuleResult(
        rule_id="basis_wide",
        dedup_key=opp.symbol,
        symbol=opp.symbol,
        triggered=triggered,
        severity=Severity.WARN,
        title=f"{opp.symbol} 基差偏离 {opp.basis_pct:+.3f}%",
        body=body,
    )


def _rule_xs_lowvol_signal(cfg: Config, signal: DirectionalSignal) -> RuleResult:
    """截面低波动调仓提醒。

    只在「标的数足够、能构建完整组合」时触发。标的不够时保持沉默——
    半个组合不是这个策略，硬凑出来的是另一个策略。
    """
    ready = bool(signal.longs) and bool(signal.shorts)
    k = len(signal.longs) + len(signal.shorts)
    scale = position_scale(
        [v.realized_vol_pct for v in signal.longs + signal.shorts],
        threshold(cfg, "xs_target_vol_pct", 80.0),
        threshold(cfg, "xs_max_scale", 3.0),
    )

    long_lines = "\n".join(
        f"  做多  {v.symbol:<12} 年化波动 {v.realized_vol_pct:>6.1f}%"
        f"  窗口涨跌 {v.return_window_pct:+7.2f}%"
        for v in signal.longs
    )
    short_lines = "\n".join(
        f"  做空  {v.symbol:<12} 年化波动 {v.realized_vol_pct:>6.1f}%"
        f"  窗口涨跌 {v.return_window_pct:+7.2f}%"
        for v in signal.shorts
    )

    body = (
        f"调仓时点：{signal.as_of}\n"
        f"打分窗口：{signal.lookback} 天已实现波动率 ｜ 全市场 {signal.universe_size} 个标的\n"
        f"组合：做多波动率最低的 {len(signal.longs)} 个，"
        f"做空波动率最高的 {len(signal.shorts)} 个\n\n"
        f"【做多腿】\n{long_lines or '  （无）'}\n\n"
        f"【做空腿】\n{short_lines or '  （无）'}\n\n"
        f"建议仓位缩放：{scale:.2f}x（按组合层面波动率目标 "
        f"{threshold(cfg, 'xs_target_vol_pct', 80.0):.0f}% 计算，已封顶）\n"
        f"组合名义敞口：约 {k} 个槽位等权\n\n"
        f"【执行前请自行确认】\n"
        f"  1. 成本：往返约 0.18%~0.36%（视 maker/taker 与 BNB 抵扣），"
        f"先确认预期收益能否覆盖\n"
        f"  2. 保证金：做空高波动山寨币是尾部风险来源，"
        f"逐腿确认保证金率与强平距离\n"
        f"  3. 仓位缩放 {scale:.2f}x 是按波动率目标的参考值，"
        f"请按你自己的单笔风险预算调整\n\n"
        f"【本信号何时失效】\n"
        f"  · 市场进入普涨且高波动标的领涨时，做空腿会反复被打穿——"
        f"此时低波动因子的超额收益反转\n"
        f"  · 回测样本期是单边熊市（BTC −28%、ETH −43%），"
        f"策略 11/12 个月为正受益于该环境；若市场切换为单边牛市，需重新验证\n"
        f"  · 若连续 3 次调仓的组合收益为负，停止使用并重新跑一遍去伪检验\n\n"
        f"为什么是这两个方向：低波动异象——低风险资产的风险调整后收益被系统性低估。\n\n"
        f"{evidence_block()}"
    )
    return RuleResult(
        rule_id="xs_lowvol_signal",
        dedup_key=f"XSLOWVOL:{signal.as_of}",
        symbol="PORTFOLIO",
        triggered=ready,
        severity=Severity.INFO,
        title=f"截面低波动调仓信号（{signal.as_of}）",
        body=body,
    )


def _rule_xs_lowvol_squeeze(cfg: Config, signal: DirectionalSignal) -> RuleResult:
    """空头腿挤空风险。

    做空高波动山寨币最大的尾部风险就是被逼空。任何单日急涨的空头候选
    都必须单独标出来——这类风险不会体现在日线的回撤数字里。
    """
    limit = threshold(cfg, "xs_squeeze_1d_pct", 15.0)
    hot = signal.squeeze_warnings(limit)

    lines = "\n".join(
        f"  {v.symbol:<12} 近 1 日 {v.return_1d_pct:+7.2f}%"
        f"  年化波动 {v.realized_vol_pct:>6.1f}%"
        f"  24h 成交额 {v.quote_volume_24h / 1e8:.2f} 亿"
        for v in hot
    )
    body = (
        f"截至 {signal.as_of}，空头候选中有 {len(hot)} 个标的单日涨幅超过 {limit:.0f}%：\n\n"
        f"{lines or '  （无）'}\n\n"
        f"含义：做空腿正在被逼空。高波动标的的上涨往往又快又猛，\n"
        f"而日线回测**完全看不到盘中挤空**——回撤数字会低估这条腿的真实风险。\n\n"
        f"动作建议：\n"
        f"  1. 核对空头腿的实际保证金率与强平距离\n"
        f"  2. 考虑按交易计划书的止损规则主动减掉被逼空的腿，而不是等强平\n"
        f"  3. 不要因为「策略回测夏普 2.36」就放大仓位扛过去"
    )
    return RuleResult(
        rule_id="xs_lowvol_squeeze",
        dedup_key=f"XSSQUEEZE:{signal.as_of}",
        symbol="PORTFOLIO",
        triggered=bool(hot),
        severity=Severity.WARN,
        title=f"截面低波动空头腿挤空风险（{len(hot)} 个标的急涨）",
        body=body,
    )


def evaluate_directional_rules(
    cfg: Config, signal: DirectionalSignal | None
) -> list[RuleResult]:
    """方向性规则。signal 为 None（数据不足）时返回空列表，即保持沉默。"""
    if signal is None:
        return []
    return [
        _rule_xs_lowvol_signal(cfg, signal),
        _rule_xs_lowvol_squeeze(cfg, signal),
    ]


def evaluate_opportunity_rules(
    cfg: Config, opportunities: list[FundingOpportunity]
) -> list[RuleResult]:
    """机会类规则。

    只对最靠前的 N 个标的评估，避免市场整体资金费飙升时一次产生几百条告警。
    这个上限由 thresholds.max_alerts_per_scan 控制。
    """
    cap = int(threshold(cfg, "max_alerts_per_scan", 10))
    results: list[RuleResult] = []

    by_net = sorted(opportunities, key=lambda o: o.net_annual_pct, reverse=True)[:cap]
    for opp in by_net:
        results.append(_rule_funding_opportunity(cfg, opp))

    by_basis = sorted(opportunities, key=lambda o: abs(o.basis_pct), reverse=True)[:cap]
    for opp in by_basis:
        results.append(_rule_basis_wide(cfg, opp))

    return results


def _rule_cross_exchange_funding(
    cfg: Config, opp: CrossExchangeOpportunity
) -> RuleResult:
    limit = threshold(cfg, "cross_min_net_annual_pct", 15.0)
    triggered = opp.net_annual_pct >= limit
    body = (
        f"标的：{opp.base}\n"
        f"跨所净年化 = {opp.net_annual_pct:.2f}%（阈值 {limit:.2f}%）\n\n"
        f"做空（高费率）：{opp.high.exchange} {opp.high.symbol}"
        f"  费率 {opp.high.funding_rate * 100:.4f}% / 8h"
        f"  24h 成交额 {opp.high.quote_volume_24h / 1e8:.2f} 亿\n"
        f"做多（低费率）：{opp.low.exchange} {opp.low.symbol}"
        f"  费率 {opp.low.funding_rate * 100:.4f}% / 8h"
        f"  24h 成交额 {opp.low.quote_volume_24h / 1e8:.2f} 亿\n\n"
        f"费率差：{opp.spread_rate * 100:.4f}% / 8h\n"
        f"毛年化：{opp.gross_annual_pct:.2f}%\n"
        f"两条腿往返成本摊销（按持有 {opp.hold_days:.0f} 天）："
        f"{opp.gross_annual_pct - opp.net_annual_pct:.2f}%\n\n"
        f"动作建议：先确认两边盘口能同时成交（跨所建仓有时间差，单边暴露是真实风险），"
        f"并确认两所都留有足够保证金。\n"
        f"注意：跨所套利资金效率低于单所内套利，且两个永续之间的价差本身会波动。"
    )
    return RuleResult(
        rule_id="cross_exchange_funding",
        dedup_key=f"{opp.high.exchange}:{opp.low.exchange}:{opp.base}",
        symbol=opp.base,
        triggered=triggered,
        severity=Severity.INFO,
        title=f"{opp.base} 跨所资金费差 净年化 {opp.net_annual_pct:.1f}%",
        body=body,
    )


def evaluate_cross_exchange_rules(
    cfg: Config, opportunities: list[CrossExchangeOpportunity]
) -> list[RuleResult]:
    cap = int(threshold(cfg, "max_alerts_per_scan", 10))
    ordered = sorted(opportunities, key=lambda o: o.net_annual_pct, reverse=True)[:cap]
    return [_rule_cross_exchange_funding(cfg, opp) for opp in ordered]


# --------------------------------------------------------------------------
# 系统类规则
# --------------------------------------------------------------------------


def evaluate_heartbeat_rule(
    cfg: Config, store: Store, now: datetime, tz: tzinfo, component: str = "positions"
) -> RuleResult:
    last_ok = store.heartbeat_last_ok(component)
    limit = cfg.heartbeat_stale_seconds
    stale = last_ok is None or (now - last_ok).total_seconds() > limit
    body = (
        f"组件 {component} 已超过 {limit} 秒未成功轮询。\n\n"
        f"上次成功：{fmt_local(last_ok, tz)}\n"
        f"当前时间：{fmt_local(now, tz)}\n\n"
        f"可能原因：网络中断、API 限频、进程异常退出。\n"
        f"动作建议：检查进程与网络；注意此时持仓风险处于盲区。"
    )
    return RuleResult(
        rule_id="heartbeat_stale",
        dedup_key=f"HEARTBEAT:{component}",
        symbol="-",
        triggered=stale,
        severity=Severity.CRITICAL,
        title=f"心跳丢失：{component}",
        body=body,
    )


def evaluate_api_error_rule(
    cfg: Config,
    component: str,
    consecutive_failures: int,
    last_error: str,
    now: datetime,
    tz: tzinfo,
) -> RuleResult:
    triggered = consecutive_failures >= cfg.api_error_threshold
    body = (
        f"组件 {component} 连续 {consecutive_failures} 次 API 请求失败"
        f"（阈值 {cfg.api_error_threshold}）。\n\n"
        f"最近错误：{last_error or '-'}\n"
        f"时间：{fmt_local(now, tz)}\n\n"
        f"动作建议：检查 IP 白名单、API Key 权限与限频状态。"
    )
    return RuleResult(
        rule_id="api_error",
        dedup_key=f"API:{component}",
        symbol="-",
        triggered=triggered,
        severity=Severity.WARN,
        title=f"{component} API 连续失败 {consecutive_failures} 次",
        body=body,
    )


# --------------------------------------------------------------------------
# 引擎：闸门与路由
# --------------------------------------------------------------------------


class RuleEngine:
    """把 RuleResult 转成 Decision，中间穿过四道闸门。"""

    def __init__(self, cfg: Config, store: Store, tz: tzinfo):
        self.cfg = cfg
        self.store = store
        self.tz = tz

    def _severity(self, rule_id: str, computed: Severity) -> Severity:
        """分级优先级：config.yaml 显式覆盖 > 规则函数按阈值动态计算的分级。

        注意：不能用 DEFAULTS 里的 severity 覆盖计算结果，否则
        「保证金率 80% 应升级为 CRITICAL」这类动态升级会被静态默认值抹掉。
        """
        rc = self.cfg.rule(rule_id)
        if rc.severity:
            return Severity(rc.severity)
        return computed

    def _confirmations(self, rule_id: str) -> int:
        rc = self.cfg.rule(rule_id)
        if rc.confirmations is not None:
            return rc.confirmations
        return int(DEFAULTS.get(rule_id, {}).get("confirmations", 1))

    def _cooldown(self, rule_id: str, severity: Severity) -> int:
        rc = self.cfg.rule(rule_id)
        if rc.cooldown_seconds is not None:
            return rc.cooldown_seconds
        if severity is Severity.CRITICAL:
            return self.cfg.cooldown_critical
        return self.cfg.cooldown_default

    def _rate_limited(self, now: datetime) -> bool:
        return self.store.sent_count_since(now - timedelta(hours=1)) >= self.cfg.max_per_hour

    def process(self, results: list[RuleResult], now: datetime) -> list[Decision]:
        decisions: list[Decision] = []
        quiet = in_quiet_hours(now.astimezone(self.tz), self.cfg.quiet_start, self.cfg.quiet_end)

        for res in results:
            rc = self.cfg.rule(res.rule_id)
            if not rc.enabled:
                continue

            severity = self._severity(res.rule_id, res.severity)

            # 闸门 1：持续性确认
            streak = self.store.bump_streak(
                res.rule_id, res.dedup_key, res.triggered, now
            )
            if not res.triggered or streak < self._confirmations(res.rule_id):
                continue

            # 闸门 2：冷却去重
            last = self.store.last_fired_at(res.rule_id, res.dedup_key)
            cooldown = self._cooldown(res.rule_id, severity)
            if last is not None and (now - last).total_seconds() < cooldown:
                continue

            # 闸门 3：静默期与限流（CRITICAL 不受静默期限制）
            # INFO 级一律走摘要：机会类信息不该占用即时通道，即时通道留给风险
            immediate = True
            if severity is Severity.INFO:
                immediate = False
            elif severity is not Severity.CRITICAL:
                if quiet or self._rate_limited(now):
                    immediate = False

            self.store.mark_fired(res.rule_id, res.dedup_key, now)
            decisions.append(
                Decision(
                    alert=Alert(
                        rule_id=res.rule_id,
                        dedup_key=res.dedup_key,
                        symbol=res.symbol,
                        severity=severity,
                        title=res.title,
                        body=res.body,
                        created_at=now,
                    ),
                    immediate=immediate,
                )
            )
        return decisions
