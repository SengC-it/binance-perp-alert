"""跑资金费套利回测并产出测算报告。

用法：
  python -m backtest.run_backtest
  python -m backtest.run_backtest --capital 10000 --max-positions 5 --hold-days 30
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Sequence

from .fetch_history import CACHE_DIR, load_all
from .funding_arb import (
    BacktestParams,
    BacktestResult,
    MarketSeries,
    build_series,
    format_summary,
    simulate,
)

log = logging.getLogger("backtest.run")
OUT_DIR = Path(__file__).resolve().parent

SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float], width: int = 72) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    step = max(1, len(values) // width)
    sampled = values[::step]
    return "".join(SPARK[min(len(SPARK) - 1, int((v - lo) / span * (len(SPARK) - 1)))]
                   for v in sampled)


def pct_table(rows: list[tuple[str, BacktestResult]], header: str) -> str:
    """渲染一张敏感性对比表。"""
    cols = ["情形", "笔数", "净利润", "收益率", "最大回撤", "夏普", "胜率", "利用率"]
    widths = [16, 6, 11, 9, 10, 8, 8, 9]
    lines = ["| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |"]
    lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for label, res in rows:
        cells = [
            label,
            str(res.trade_count),
            f"{res.net_pnl:+,.0f}",
            f"{res.total_return_pct:+.2f}%",
            f"{res.max_drawdown_pct:.2f}%",
            f"{res.sharpe:.2f}",
            f"{res.win_rate_pct:.0f}%",
            f"{res.utilization_pct:.0f}%",
        ]
        lines.append("| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |")
    return "\n".join(lines)


def sweep(
    base: BacktestParams,
    markets: Sequence[MarketSeries],
    field: str,
    values: Sequence[Any],
    label: Callable[[Any], str] = str,
) -> list[tuple[str, BacktestResult]]:
    out: list[tuple[str, BacktestResult]] = []
    for value in values:
        params = replace(base, **{field: value})
        out.append((label(value), simulate(markets, params)))
    return out


def build_report(
    base: BacktestParams,
    markets: Sequence[MarketSeries],
    baseline: BacktestResult,
    excluded: list[str],
) -> str:
    lines: list[str] = []
    add = lines.append

    add("# 资金费套利策略历史测算报告")
    add("")
    add("> 本报告测算的对象是**告警系统会提醒的那个策略**（delta 中性资金费套利 / cash-and-carry），")
    add("> 而不是告警系统本身。告警系统只产生信息，不产生盈亏。")
    add("")

    # ---- 1 结论摘要 ----
    add("## 一、结论摘要")
    add("")
    add("```")
    add(format_summary(baseline))
    add("```")
    add("")
    add(f"- 权益曲线（{baseline.span_days} 天，{SPARK} 从低到高）：")
    add("")
    add("```")
    add(sparkline(baseline.equity))
    add("```")
    add("")

    # ---- 2 盈亏分解 ----
    add("## 二、盈亏分解")
    add("")
    add("单笔盈亏 = 资金费收入 + 基差变动 − 往返手续费。三项拆开看：")
    add("")
    add("| 项目 | 金额 (USDT) | 占净利润 |")
    add("|---|---:|---:|")
    net = baseline.net_pnl or 1.0
    add(f"| 资金费收入 | {baseline.funding_pnl:+,.2f} | {baseline.funding_pnl / net * 100:+.1f}% |")
    add(f"| 基差变动 | {baseline.basis_pnl:+,.2f} | {baseline.basis_pnl / net * 100:+.1f}% |")
    add(f"| 手续费与滑点 | {-baseline.cost_pnl:+,.2f} | {-baseline.cost_pnl / net * 100:+.1f}% |")
    add(f"| **合计** | **{baseline.net_pnl:+,.2f}** | **100.0%** |")
    add("")
    add(f"平均单笔：资金费 {baseline.funding_pnl / max(1, baseline.trade_count):+,.2f} USDT，"
        f"基差 {baseline.basis_pnl / max(1, baseline.trade_count):+,.2f} USDT，"
        f"成本 {-baseline.cost_pnl / max(1, baseline.trade_count):+,.2f} USDT。")
    add("")

    # ---- 3 逐笔分布 ----
    add("## 三、逐笔分布")
    add("")
    add("| 指标 | 数值 |")
    add("|---|---:|")
    add(f"| 交易笔数 | {baseline.trade_count} |")
    add(f"| 胜率 | {baseline.win_rate_pct:.1f}% |")
    add(f"| 平均单笔净收益（占名义价值） | {baseline.avg_net_pct:+.3f}% |")
    add(f"| 平均持有天数 | {baseline.avg_hold_days:.1f} |")
    best, worst = baseline.best_trade, baseline.worst_trade
    if best:
        add(f"| 最好一笔 | {best.symbol} {best.entry_date} → {best.exit_date}，"
            f"{best.net_pct:+.3f}%（{best.net_pnl:+,.2f} USDT） |")
    if worst:
        add(f"| 最差一笔 | {worst.symbol} {worst.entry_date} → {worst.exit_date}，"
            f"{worst.net_pct:+.3f}%（{worst.net_pnl:+,.2f} USDT） |")
    add(f"| 信号数 / 成交数 | {baseline.signals_generated} / {baseline.trade_count} |")
    add(f"| 因槽位满或已持仓而放弃 | {baseline.signals_skipped_capacity} 个 |")
    add("")

    # ---- 4 分标的 ----
    add("## 四、分标的贡献（前 15）")
    add("")
    by_symbol = baseline.by_symbol()
    ranked = sorted(by_symbol.items(), key=lambda kv: kv[1]["net_pnl"], reverse=True)
    add("| 标的 | 笔数 | 净盈亏 (USDT) | 平均单笔 |")
    add("|---|---:|---:|---:|")
    for symbol, row in ranked[:15]:
        add(f"| {symbol} | {int(row['trades'])} | {row['net_pnl']:+,.2f} | "
            f"{row['avg_net_pct']:+.3f}% |")
    if len(ranked) > 15:
        rest = sum(r["net_pnl"] for _, r in ranked[15:])
        add(f"| 其余 {len(ranked) - 15} 个标的 | "
            f"{int(sum(r['trades'] for _, r in ranked[15:]))} | {rest:+,.2f} | - |")
    add("")

    losers = [kv for kv in ranked if kv[1]["net_pnl"] < 0]
    if losers:
        add(f"**亏损标的 {len(losers)} 个**：" +
            "、".join(f"{s}（{r['net_pnl']:+,.0f}）" for s, r in losers[:10]) +
            ("…" if len(losers) > 10 else ""))
        add("")

    # ---- 5 分月 ----
    add("## 五、分月盈亏（按平仓月）")
    add("")
    add("| 月份 | 净盈亏 (USDT) | 累计 (USDT) |")
    add("|---|---:|---:|")
    cumulative = 0.0
    for month, pnl in baseline.by_month().items():
        cumulative += pnl
        add(f"| {month} | {pnl:+,.2f} | {cumulative:+,.2f} |")
    add("")
    positive = sum(1 for v in baseline.by_month().values() if v > 0)
    add(f"盈利月份 {positive}/{len(baseline.by_month())}。")
    add("")

    # ---- 6 敏感性分析 ----
    add("## 六、敏感性分析")
    add("")
    add("回测结论是否可信，取决于它对假设有多敏感。下面逐项扰动。")
    add("")

    add("### 6.1 往返成本（手续费与滑点）")
    add("")
    add("这是最关键的一项。VIP 等级、是否用 maker 单、标的市场深度都体现在这里。")
    add("")
    add(pct_table(sweep(base, markets, "round_trip_cost_pct",
                       [0.16, 0.26, 0.36, 0.50, 0.70, 1.00],
                       lambda v: f"{v:.2f}%"), "cost"))
    add("")

    add("### 6.2 持有天数")
    add("")
    add("往返成本固定，持有越短，成本对年化的侵蚀越重。")
    add("")
    add(pct_table(sweep(base, markets, "hold_days", [7, 14, 30, 60, 90, 180],
                       lambda v: f"{v} 天"), "hold"))
    add("")

    add("### 6.3 开仓门槛（扣费后年化）")
    add("")
    add(pct_table(sweep(base, markets, "min_net_annual_pct", [0, 5, 10, 20, 30, 50],
                       lambda v: f"≥{v}%"), "threshold"))
    add("")

    add("### 6.4 并发持仓上限")
    add("")
    add("槽位越多，能抓到的机会越多，但单笔名义价值被摊薄。")
    add("")
    add(pct_table(sweep(base, markets, "max_positions", [1, 2, 3, 5, 8, 12],
                       lambda v: f"{v} 个"), "slots"))
    add("")

    add("### 6.5 资金规模与容量约束")
    add("")
    add(f"单笔名义价值被限制在当日成交额的 {base.max_pct_of_volume * 100:.2f}% 以内。"
        f"资金越大，越容易撞上这条上限，收益率随之下降。")
    add("")
    add(pct_table(sweep(base, markets, "capital",
                       [10_000, 50_000, 100_000, 500_000, 1_000_000],
                       lambda v: f"{v / 1000:.0f}k"), "capital"))
    add("")

    add("### 6.6 永续腿保证金比例")
    add("")
    add("保证金比例越低（杠杆越高），资金效率越高，但被强平的风险越大。")
    add("")
    add(pct_table(sweep(base, markets, "perp_margin_ratio", [0.10, 0.20, 0.33, 0.50],
                       lambda v: f"{v * 100:.0f}%（{1 / v:.1f}x）"), "margin"))
    add("")

    # ---- 7 局限 ----
    add("## 七、已知偏差与局限")
    add("")
    add("1. **幸存者偏差**：标的池取自当前仍挂牌的合约，一年内已下架或退市的未纳入，")
    add("   这会系统性高估收益。已剔除的候选标的：" + ("、".join(excluded) if excluded else "无"))
    add("2. **未建模永续腿强平**：delta 中性下总权益不因价格波动受损，但永续空头腿")
    add("   在急涨行情中可能被单独强平。本引擎不模拟这一情形，实际风险高于回测。")
    add("3. **滑点用固定值**：真实滑点随订单簿深度变化，小市值合约在波动期会显著恶化。")
    add("4. **逐日收盘价成交**：真实成交价会偏离，且信号到执行有 1 日延迟（已计入）。")
    add("5. **资金费按已结算值累加**：这是真实结算值，不是估算值，此项无偏差。")
    add("6. **基差用收盘价计算**：日内极值未纳入，可能低估基差波动的影响。")
    add("7. **未考虑资金费税负、借币成本、交易所对手方风险**。")
    add("8. **样本期仅一年**：资金费环境高度依赖市场情绪，单年样本不足以代表长期水平。")
    add("")
    add("---")
    add("")
    add(f"参数：初始权益 {base.capital:,.0f} USDT，最大并发 {base.max_positions}，"
        f"持有 {base.hold_days} 天，往返成本 {base.round_trip_cost_pct:.2f}%，"
        f"开仓门槛净年化 ≥{base.min_net_annual_pct}%，"
        f"成交额门槛 {base.min_volume_usdt_24h:,.0f} USDT，"
        f"执行延迟 {base.exec_lag_days} 天。")
    add("")
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="资金费套利历史回测")
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--hold-days", type=int, default=30)
    ap.add_argument("--cost-pct", type=float, default=0.36)
    ap.add_argument("--min-net-annual", type=float, default=10.0)
    ap.add_argument("--min-volume", type=float, default=50_000_000.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    records = load_all()
    if not records:
        print("缓存为空，请先运行：python -m backtest.fetch_history")
        return 1

    markets = [build_series(r) for r in records]
    markets = [m for m in markets if len(m.dates) >= 300]
    print(f"载入 {len(markets)} 个标的，"
          f"区间 {min(m.dates[0] for m in markets)} ~ {max(m.dates[-1] for m in markets)}")

    universe_file = CACHE_DIR / "universe.json"
    excluded: list[str] = []
    if universe_file.exists():
        meta = json.loads(universe_file.read_text(encoding="utf-8"))
        cached = {m.symbol for m in markets}
        excluded = [s for s in meta.get("symbols", []) if s not in cached]

    base = BacktestParams(
        capital=args.capital,
        max_positions=args.max_positions,
        hold_days=args.hold_days,
        round_trip_cost_pct=args.cost_pct,
        min_net_annual_pct=args.min_net_annual,
        min_volume_usdt_24h=args.min_volume,
    )

    baseline = simulate(markets, base)
    print()
    print(format_summary(baseline))
    print()

    report = build_report(base, markets, baseline, excluded)
    out_dir = Path(args.out) if args.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "results.json").write_text(
        json.dumps(baseline.to_json(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"报告已写入 {out_dir / 'report.md'}")
    print(f"明细已写入 {out_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
