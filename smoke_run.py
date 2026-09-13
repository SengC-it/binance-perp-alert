#!/usr/bin/env python3
"""端到端演练（不需要 API Key，不会发真实邮件）。

用假行情与假账户跑完整的「计算 → 规则 → 闸门 → 投递」链路，
在接真实账户之前先把四件事看清楚：

  1. 持续性确认是怎么挡住前几轮误报的
  2. 静默期下 WARN 进摘要、CRITICAL 仍然立即发
  3. 机会扫描如何把毛年化折算成净年化，以及流动性过滤挡掉了什么
  4. 邮件正文实际长什么样

用法：python smoke_run.py
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from src.config import Config
from src.engine import AlertService
from src.notifier import Notifier
from src.store import Store
from src.timeutil import resolve_tz

SGT = timezone(timedelta(hours=8))
# 14:00 新加坡时间（非静默期）
DAYTIME = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
# 04:00 新加坡时间（静默期内）
NIGHT = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)

# 假账户：BTC 多单已接近强平（强平距离 3.6%，计划止损 2% -> 1.8 倍，低于 3 倍安全线）
BTC_RAW = {
    "symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.1",
    "entryPrice": "60000", "markPrice": "61000", "unRealizedProfit": "-180",
    "liquidationPrice": "58805", "leverage": "20",
    "isolatedMargin": "250", "isolatedWallet": "250",
}
ETH_RAW = {
    "symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "-2",
    "entryPrice": "3000", "markPrice": "3050", "unRealizedProfit": "-100",
    "liquidationPrice": "3900", "leverage": "5",
    "isolatedMargin": "900", "isolatedWallet": "900",
}
V3 = [
    {"symbol": "BTCUSDT", "positionSide": "BOTH", "maintMargin": "200"},
    {"symbol": "ETHUSDT", "positionSide": "BOTH", "maintMargin": "45"},
]
PREMIUM = [
    {"symbol": "BTCUSDT", "markPrice": "61000", "lastFundingRate": "0.0003"},
    # ETH 资金费为负：此时空头付费、多头收费（熊市常见）
    {"symbol": "ETHUSDT", "markPrice": "3050", "lastFundingRate": "-0.00015"},
]


class FakeClient:
    """只实现本系统用到的那几个只读方法。"""

    def __init__(self, equity: float):
        self.equity = equity

    def account(self):
        return {
            "totalMarginBalance": f"{self.equity}",
            "totalWalletBalance": f"{self.equity}",
            "availableBalance": f"{self.equity * 0.6}",
        }

    def position_risk_v2(self):
        return [BTC_RAW, ETH_RAW]

    def position_risk_v3(self):
        return V3

    def premium_index(self):
        return PREMIUM


def build_cfg() -> Config:
    cfg = Config()
    cfg.dry_run = True
    cfg.symbols = {"BTCUSDT": 2.0, "ETHUSDT": 2.5}
    cfg.thresholds = {
        "liq_to_stop_min_ratio": 3.0,
        "margin_ratio_warn": 0.60,
        "margin_ratio_critical": 0.80,
        "funding_cost_r_warn": 0.30,
        "portfolio_heat_max_pct": 4.0,
        "daily_loss_warn_pct": 2.0,
        "daily_loss_critical_pct": 3.0,
        "drawdown_stop_pct": 20.0,
        "assumed_mmr": 0.005,
        # 机会类
        "assumed_hold_days": 30.0,
        "min_volume_usdt_24h": 50_000_000.0,
        "min_net_annual_pct": 10.0,
        "basis_warn_pct": 0.30,
        "max_alerts_per_scan": 10,
        "spot_taker_fee_pct": 0.10,
        "perp_taker_fee_pct": 0.05,
        "slippage_pct": 0.03,
    }
    return cfg


def build_service(cfg, tz, db_path: str):
    store = Store(db_path)
    client = FakeClient(equity=10000.0)
    notifier = Notifier(cfg, store, tz)
    service = AlertService(cfg, tz, store, client, notifier)

    # 模拟 ETH 空单已持有约 30 天，让资金费按负费率自然累积出成本
    # （资金费只有在持仓跨过结算周期后才有意义，这里通过首见时间回溯来体现）
    store.position_first_seen("ETHUSDT", "SHORT", DAYTIME - timedelta(days=30))
    return store, client, notifier, service


def record_sends(notifier: Notifier) -> list[tuple[str, str]]:
    """拦截 send，把真实会发出的邮件内容记录下来供打印。"""
    captured: list[tuple[str, str]] = []
    original = notifier.send

    def wrapper(subject: str, body: str) -> bool:
        captured.append((subject, body))
        return original(subject, body)

    notifier.send = wrapper  # type: ignore[method-assign]
    return captured


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def phase_daytime(cfg, tz) -> None:
    section("阶段一：非静默期（新加坡时间 14:00）—— 观察持续性确认")
    store, client, notifier, service = build_service(cfg, tz, "./data/smoke_day.db")
    sent = record_sends(notifier)

    for i in range(2):
        print(f"\n---- 第 {i + 1} 轮 ----")
        service.poll_positions(DAYTIME + timedelta(minutes=i))

    print("\n---- 第 3 轮（账户开始出现当日亏损）----")
    client.equity = 9800.0
    service.poll_positions(DAYTIME + timedelta(minutes=2))

    print("\n---- 第 4 轮 ----")
    service.poll_positions(DAYTIME + timedelta(minutes=3))

    print(f"\n本轮立即投递的邮件：{len(sent)} 封")
    for subject, _ in sent:
        print(f"  · {subject}")

    if sent:
        section("其中一封邮件的实际正文（强平距离告警）")
        subject, body = sent[0]
        print(f"主题：{subject}")
        print("-" * 70)
        print(body)

    store.close()


def phase_night(cfg, tz) -> None:
    section("阶段二：静默期（新加坡时间 04:00）—— WARN 进摘要，CRITICAL 仍立即发")
    store, client, notifier, service = build_service(cfg, tz, "./data/smoke_night.db")
    sent = record_sends(notifier)

    for i in range(3):
        print(f"\n---- 第 {i + 1} 轮 ----")
        service.poll_positions(NIGHT + timedelta(minutes=i))

    print(f"\n静默期内立即投递的邮件：{len(sent)} 封")
    for subject, _ in sent:
        print(f"  · {subject}")

    pending = notifier.pending()
    print(f"\n转入摘要队列：{len(pending)} 条")
    for item in pending:
        print(f"  [{item.severity.value:<8}] {item.title}")

    rendered = notifier.render_digest(NIGHT + timedelta(minutes=4))
    if rendered:
        subject, body, _ = rendered
        section("每日摘要邮件的实际正文")
        print(f"主题：{subject}")
        print("-" * 70)
        print(body)

    store.close()


def phase_scan(cfg) -> None:
    section("阶段三：机会扫描（资金费 / 基差）")
    from src.opportunity_engine import format_opportunity_table, scan_opportunities
    from src.rules import evaluate_opportunity_rules

    # 假行情：覆盖「有机会」「被流动性过滤」「负资金费」「基差偏离」四种情况
    fake_premium = [
        {"symbol": "BTCUSDT", "markPrice": "61000", "indexPrice": "60900",
         "lastFundingRate": "0.0001", "nextFundingTime": 1789000000000},
        {"symbol": "ETHUSDT", "markPrice": "3050", "indexPrice": "3060",
         "lastFundingRate": "-0.0002", "nextFundingTime": 1789000000000},
        {"symbol": "SOLUSDT", "markPrice": "148.5", "indexPrice": "148.1",
         "lastFundingRate": "0.0004", "nextFundingTime": 1789000000000},
        {"symbol": "HYPEUSDT", "markPrice": "45.2", "indexPrice": "44.8",
         "lastFundingRate": "0.0008", "nextFundingTime": 1789000000000},
        {"symbol": "DOGEUSDT", "markPrice": "0.21", "indexPrice": "0.21",
         "lastFundingRate": "0.0020", "nextFundingTime": 1789000000000},
    ]
    fake_tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "9000000000"},
        {"symbol": "ETHUSDT", "quoteVolume": "5000000000"},
        {"symbol": "SOLUSDT", "quoteVolume": "3000000000"},
        {"symbol": "HYPEUSDT", "quoteVolume": "800000000"},
        # DOGEUSDT 资金费很高但成交额极低，应被流动性过滤挡掉
        {"symbol": "DOGEUSDT", "quoteVolume": "1000000"},
    ]

    opportunities = scan_opportunities(fake_premium, fake_tickers, cfg)
    print()
    print(format_opportunity_table(opportunities))

    print()
    print("说明：DOGEUSDT 资金费 0.20%/8h（毛年化 219%）但 24h 成交额仅 100 万 USDT，")
    print("      已被流动性过滤挡掉 —— 这类合约的滑点会吃掉全部账面收益。")

    results = evaluate_opportunity_rules(cfg, opportunities)
    fired = [r for r in results if r.triggered]
    print()
    print(f"触发的机会类规则：{len(fired)} 条")
    for r in fired:
        print(f"  [{r.severity.value:<8}] {r.title}")

    target = [r for r in fired if r.rule_id == "funding_opportunity"]
    if target:
        section("资金费机会告警的实际正文")
        print(f"主题：[INFO] {target[0].title}")
        print("-" * 70)
        print(target[0].body)


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,  # 只保留告警级别日志，让输出聚焦在演练本身
        format="%(levelname)-7s | %(message)s",
        stream=sys.stdout,
    )
    cfg = build_cfg()
    tz = resolve_tz(cfg.timezone)

    print("=" * 70)
    print("Work Alert 端到端演练（dry-run，不会真的发邮件）")
    print("=" * 70)
    print("场景：权益 10000 USDT，两笔持仓")
    print("  · BTCUSDT 多单：强平距离 3.6%，计划止损 2% -> 倍数 1.8x（安全线 3.0x）")
    print("  · ETHUSDT 空单：持有约 30 天，资金费率为负 -> 空头持续付费（1R = 150 USDT）")

    phase_daytime(cfg, tz)
    phase_night(cfg, tz)
    phase_scan(cfg)

    section("演练结束")
    print("删除 data/smoke_day.db 与 data/smoke_night.db 可重置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
