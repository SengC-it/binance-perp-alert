"""跑永续合约方向性策略回测并产出报告。

用法：
  python -m backtest.run_perp_backtest
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Sequence

from .fetch_history import load_all
from .funding_arb import MarketSeries, build_series
from .perp_signals import (
    STRATEGIES,
    PerpParams,
    PerpResult,
    format_summary,
    simulate_perp,
)

log = logging.getLogger("backtest.perp")
OUT_DIR = Path(__file__).resolve().parent
SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float], width: int = 72) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    step = max(1, len(values) // width)
    return "".join(
        SPARK[min(len(SPARK) - 1, int((v - lo) / span * (len(SPARK) - 1)))]
        for v in values[::step]
    )


def load_markets(min_days: int = 300) -> list[MarketSeries]:
    markets = [build_series(r) for r in load_all()]
    return [m for m in markets if len(m.dates) >= min_days]


def run_all(markets: Sequence[MarketSeries], params: PerpParams) -> dict[str, PerpResult]:
    """跑全部策略。买入持有作为基准使用全部标的等权，不受 max_positions 限制。"""
    out: dict[str, PerpResult] = {}
    for key in STRATEGIES:
        p = replace(params, max_positions=len(markets)) if key == "buy_hold" else params
        out[key] = simulate_perp(markets, key, p)
    return out


def comparison_table(results: dict[str, PerpResult]) -> str:
    cols = ["策略", "笔数", "净利润", "收益率", "最大回撤", "夏普", "胜率", "盈亏比", "在场"]
    widths = [14, 6, 11, 9, 10, 7, 7, 8, 7]
    lines = ["| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |"]
    lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for key, res in results.items():
        cells = [
            res.label,
            str(res.trade_count),
            f"{res.net_pnl:+,.0f}",
            f"{res.total_return_pct:+.2f}%",
            f"{res.max_drawdown_pct:.2f}%",
            f"{res.sharpe:.2f}",
            f"{res.win_rate_pct:.0f}%",
            f"{res.profit_factor:.2f}",
            f"{res.exposure_pct:.0f}%",
        ]
        lines.append("| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |")
    return "\n".join(lines)


def sweep(
    markets: Sequence[MarketSeries],
    strategy: str,
    base: PerpParams,
    field: str,
    values: Sequence[Any],
    label: Callable[[Any], str] = str,
) -> list[tuple[str, PerpResult]]:
    out: list[tuple[str, PerpResult]] = []
    for value in values:
        res = simulate_perp(markets, strategy, replace(base, **{field: value}))
        out.append((label(value), res))
    return out


def sweep_table(rows: list[tuple[str, PerpResult]]) -> str:
    cols = ["情形", "笔数", "净利润", "收益率", "最大回撤", "夏普", "胜率"]
    widths = [14, 6, 11, 9, 10, 7, 7]
    lines = ["| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |"]
    lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for name, res in rows:
        cells = [
            name,
            str(res.trade_count),
            f"{res.net_pnl:+,.0f}",
            f"{res.total_return_pct:+.2f}%",
            f"{res.max_drawdown_pct:.2f}%",
            f"{res.sharpe:.2f}",
            f"{res.win_rate_pct:.0f}%",
        ]
        lines.append("| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |")
    return "\n".join(lines)


def build_report(
    params: PerpParams,
    markets: Sequence[MarketSeries],
    results: dict[str, PerpResult],
) -> str:
    lines: list[str] = []
    add = lines.append

    add("# 永续合约方向性策略测算报告")
    add("")
    add(f"标的池 {len(markets)} 个 USDT 本位永续，"
        f"区间 {min(m.dates[0] for m in markets)} ~ {max(m.dates[-1] for m in markets)}。")
    add("")

    add("## 一、策略对比")
    add("")
    add(comparison_table(results))
    add("")
    add("> 「在场」指有持仓的交易日占比。买入持有用**全部标的等权**，不受并发上限约束，")
    add("> 其余策略最多同时持有 "
        f"{params.max_positions} 个标的（每笔名义价值 {params.slot_notional():,.0f} USDT）。")
    add("")

    add("## 二、各策略权益曲线")
    add("")
    add("```")
    for key, res in results.items():
        add(f"{res.label:<12s} {sparkline(res.equity)}")
    add("```")
    add("")

    add("## 三、逐个策略明细")
    add("")
    for key, res in results.items():
        add(f"### {res.label}（`{key}`）")
        add("")
        add("```")
        add(format_summary(res))
        add("```")
        add("")
        by_symbol = res.by_symbol()
        ranked = sorted(by_symbol.items(), key=lambda kv: kv[1], reverse=True)
        if ranked:
            add("贡献最大的 5 个标的：" +
                "、".join(f"{s}（{v:+,.0f}）" for s, v in ranked[:5]))
            if len(ranked) > 5:
                add("")
                add("拖累最大的 5 个标的：" +
                    "、".join(f"{s}（{v:+,.0f}）" for s, v in ranked[-5:][::-1]))
            add("")
            conc = res.concentration(2)
            if conc is None:
                # 净亏损时「占净利润比例」会给出误导性的正数，改看毛盈利集中度。
                gross_profit = sum(v for _, v in ranked if v > 0)
                if gross_profit > 0:
                    top2 = sum(v for _, v in ranked[:2] if v > 0)
                    add(f"**风险集中度**：策略净亏损，占净利润比例不适用；"
                        f"全部毛盈利中前 2 大标的占 {top2 / gross_profit * 100:.0f}%。")
                else:
                    add(f"**风险集中度**：共 {len(ranked)} 个标的参与，"
                        f"无标的取得正收益。")
            else:
                add(f"**风险集中度**：共 {len(ranked)} 个标的参与，"
                    f"前 2 大标的贡献了净利润的 {conc:.0f}%。")
            add("")
        direction = res.by_direction()
        add(f"**多空拆分**：多头 {direction['多']:+,.0f} USDT，"
            f"空头 {direction['空']:+,.0f} USDT。")
        add("")

    # ---- 敏感性 ----
    add("## 四、敏感性分析")
    add("")
    add("只挑两个最有代表性的策略做扰动：趋势类的「双均线」与均值回归类的「资金费极值反向」。")
    add("")

    add("### 4.1 交易成本（单边手续费 + 滑点）")
    add("")
    add("成本是方向性策略最容易致命的假设——换手越频繁越致命。")
    add("")
    for key in ("ma_cross", "donchian", "funding_fade"):
        add(f"**{results[key].label}**")
        add("")
        add(sweep_table(sweep(
            markets, key, params, "taker_fee_pct",
            [0.02, 0.05, 0.10, 0.20],
            lambda v: f"taker {v:.2f}%",
        )))
        add("")

    add("### 4.2 并发持仓上限")
    add("")
    for key in ("ma_cross", "donchian"):
        add(f"**{results[key].label}**")
        add("")
        add(sweep_table(sweep(
            markets, key, params, "max_positions",
            [1, 3, 5, 10, 20],
            lambda v: f"{v} 个",
        )))
        add("")

    add("### 4.3 双均线周期")
    add("")
    add("这一项**不是用来挑最优参数的**，而是用来看结论对参数有多敏感。")
    add("敏感度高 = 这条策略的历史表现大概率是拟合出来的。")
    add("")
    add(sweep_table(sweep(
        markets, "ma_cross", params, "ma_slow",
        [30, 45, 60, 90, 120],
        lambda v: f"快 {params.ma_fast} / 慢 {v}",
    )))
    add("")

    add("### 4.4 通道突破周期")
    add("")
    add(sweep_table(sweep(
        markets, "donchian", params, "donchian_entry",
        [10, 15, 20, 30, 55],
        lambda v: f"入场 {v} / 离场 {params.donchian_exit}",
    )))
    add("")

    add("### 4.5 资金费反向阈值")
    add("")
    add(sweep_table(sweep(
        markets, "funding_fade", params, "zscore_entry",
        [1.0, 1.5, 2.0, 2.5, 3.0],
        lambda v: f"z ≥ {v}",
    )))
    add("")

    # ---- 结论 ----
    add("## 五、必须知道的偏差")
    add("")
    add("1. **多重检验**：这里一次看了 5 个策略、又对参数做了几十次扰动。")
    add("   只要样本够多，总能挑出一个历史表现好的组合——但那不代表未来有效。")
    add("   本报告**刻意不做参数寻优**，敏感性分析的目的恰恰是暴露脆弱性。")
    add("2. **幸存者偏差**：标的池取自当前仍挂牌的合约，一年内下架的未纳入。")
    add("3. **日线粒度**：止损、强平、盘中极端行情都无法体现。真实杠杆交易的")
    add("   最大风险（被强平）在日线回测里完全看不到。")
    add("4. **无资金费成本**：方向性持仓同样要付/收资金费。本模块未计入，")
    add("   这对长期持有的多头在正费率环境下是**系统性高估**。")
    add("5. **无滑点深度模型**：固定滑点，未按订单簿建模。")
    add("6. **样本期仅一年**：2025-09 ~ 2026-08 只覆盖一种市场状态。")
    add("7. **未考虑爆仓与追加保证金**：实盘做空/做多的杠杆约束未建模。")
    add("")
    add("---")
    add("")
    add(f"参数：初始权益 {params.capital:,.0f} USDT，最大并发 {params.max_positions}，"
        f"单边成本 {params.taker_fee_pct + params.slippage_pct:.2f}%"
        f"（taker {params.taker_fee_pct:.2f}% + 滑点 {params.slippage_pct:.2f}%）。")
    add("")
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="永续合约方向性策略回测")
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--taker-fee", type=float, default=0.05)
    ap.add_argument("--slippage", type=float, default=0.03)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    markets = load_markets()
    if not markets:
        print("缓存为空，请先运行：python -m backtest.fetch_history")
        return 1
    print(f"载入 {len(markets)} 个标的，"
          f"区间 {min(m.dates[0] for m in markets)} ~ {max(m.dates[-1] for m in markets)}")

    params = PerpParams(
        capital=args.capital,
        max_positions=args.max_positions,
        taker_fee_pct=args.taker_fee,
        slippage_pct=args.slippage,
    )
    results = run_all(markets, params)

    print()
    print(comparison_table(results))
    print()

    report = build_report(params, markets, results)
    out_dir = Path(args.out) if args.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report_perp.md").write_text(report, encoding="utf-8")
    (out_dir / "results_perp.json").write_text(
        json.dumps({k: v.to_json() for k, v in results.items()},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"报告已写入 {out_dir / 'report_perp.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
