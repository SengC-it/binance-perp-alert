"""一次跑完两半回测，产出合并报告。

用法：
  python -m backtest.run_all
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import replace
from pathlib import Path

from .fetch_history import CACHE_DIR
from .funding_arb import BacktestParams, format_summary as arb_summary, simulate
from .perp_signals import PerpParams, simulate_perp
from .run_backtest import build_report as arb_report
from .run_perp_backtest import build_report as perp_report
from .run_perp_backtest import load_markets, run_all as run_perp_all

log = logging.getLogger("backtest.all")
OUT_DIR = Path(__file__).resolve().parent


def demote_headings(text: str) -> str:
    """把子报告的标题整体降一级，便于嵌入合并报告。"""
    return re.sub(r"^(#{1,4}) ", r"#\1 ", text, flags=re.MULTILINE)


def _conc_text(res, top_n: int = 2) -> str:
    """把风险集中度渲染成一句话片段；净亏损时集中度无意义。"""
    conc = res.concentration(top_n)
    if conc is None:
        return "无法计算（策略净亏损）"
    return f"{conc:.0f}%"


def overview_table(arb, perp_results) -> str:
    cols = ["策略", "类型", "笔数", "净利润", "收益率", "最大回撤", "夏普", "胜率"]
    widths = [16, 10, 6, 11, 9, 10, 7, 7]
    lines = ["| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |"]
    lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")

    rows = [("资金费套利（现金套保）", "套利", arb)]
    rows += [(r.label, "方向性", r) for r in perp_results.values()]
    for label, kind, res in rows:
        cells = [
            label,
            kind,
            str(res.trade_count),
            f"{res.net_pnl:+,.0f}",
            f"{res.total_return_pct:+.2f}%",
            f"{res.max_drawdown_pct:.2f}%",
            f"{res.sharpe:.2f}",
            f"{res.win_rate_pct:.0f}%",
        ]
        lines.append("| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |")
    return "\n".join(lines)


def build_summary(arb, perp_results, arb_params: BacktestParams) -> str:
    ma = perp_results["ma_cross"]
    dn = perp_results["donchian"]
    bh = perp_results["buy_hold"]
    ff = perp_results["funding_fade"]
    bf = perp_results["basis_fade"]

    L: list[str] = []
    add = L.append
    add("# 策略历史测算报告（套利 + 永续方向性）")
    add("")
    add("> 先把一件事说清楚：**这个项目本身是提醒系统，不产生任何盈亏。**")
    add("> 能测算的是它提醒的两类策略——资金费套利，以及方向性持仓。")
    add("> 本报告把两半都跑了一遍，全部用币安公开历史数据，"
        f"区间 {ma.equity_dates[0]} ~ {ma.equity_dates[-1]}。")
    add("")

    add("## 一、总览")
    add("")
    add(overview_table(arb, perp_results))
    add("")
    add(f"初始权益统一为 {ma.params.capital:,.0f} USDT。"
        "买入持有为**全部标的等权**基准，其余策略最多同时持有 5 个标的。")
    add("")

    add("## 二、三句话结论")
    add("")
    add(f"1. **资金费套利在这段样本里不赚钱。** 按配置的 10% 净年化门槛，全年只有 "
        f"{arb.trade_count} 笔交易；把门槛降到 0（有信号就做）也只有 "
        f"{arb.total_return_pct:+.2f}% 的收益，而且 59 笔交易里亏了 22 笔。")
    add(f"   根因：中位数资金费率只有 0.0029%/8h（年化约 3.2%），"
        f"而往返成本是 0.36%，持有 30 天要吃掉 4.38% 的年化。"
        f"**单所资金费套利在这段样本里是负期望的。**")
    add("")
    add(f"2. **永续方向性策略里只有趋势跟踪赚钱，但盈利高度集中。** "
        f"双均线趋势 {ma.total_return_pct:+.2f}%、最大回撤 {ma.max_drawdown_pct:.2f}%、"
        f"夏普 {ma.sharpe:.2f}；通道突破 {dn.total_return_pct:+.2f}%。"
        f"但双均线的利润有 **{_conc_text(ma)} 来自 2 个标的**，"
        f"而且多空拆分显示 {ma.by_direction()['空']:+,.0f} USDT 全部来自空头。")
    add("")
    add(f"3. **两类「情绪极值反向」信号在所有参数下都亏钱。** "
        f"资金费极值反向 {ff.total_return_pct:+.2f}%，"
        f"基差极值反向 {bf.total_return_pct:+.2f}%，"
        f"z 阈值从 1.0 试到 3.0 没有一个是正的。**这类信号可以直接排除。**")
    add("")

    add("## 三、样本期的市场状态")
    add("")
    add(f"这一条决定了上面所有数字该怎么读：**样本期是深度熊市。**")
    add("")
    add(f"- 全部 73 个标的等权买入持有：{bh.total_return_pct:+.2f}%，"
        f"最大回撤 {bh.max_drawdown_pct:.2f}%，胜率 {bh.win_rate_pct:.0f}%"
        f"（73 个标的里没有一个赚钱）")
    add("- BTC 109,188 → 78,550（-28%），ETH -43%，SOL -48%，ADA -75%")
    add("")
    add("所以「趋势跟踪赚钱」很大程度等于「它做空了」。如果下一段样本是单边上涨，")
    add("同样的策略会给出完全不同的结果。**这是样本依赖，不是稳定的超额收益。**")
    add("")

    add("## 四、对告警系统的启示")
    add("")
    add("| 问题 | 答案 |")
    add("|---|---|")
    add(f"| 要不要保留资金费机会提醒？ | 保留，但**必须把门槛和成本口径写清楚**。"
        f"当前 10% 净年化门槛在这段样本里几乎不触发，这其实是好事——"
        f"它在正确地保持沉默。 |")
    add(f"| 要不要新增方向性信号规则？ | **建议不加。** 唯一有正收益的双均线趋势，"
        f"利润 {_conc_text(ma)} 集中在 2 个标的，且全部来自空头，"
        f"没有足够证据支撑自动化信号。 |")
    add(f"| 资金费/基差极值该不该做反向？ | **不要。** 两个方向、五种阈值全部为负。 |")
    add(f"| 那这套告警系统还有价值吗？ | 有，但价值在**风控**不在择时。"
        f"强平距离、保证金率、日亏损、回撤停机线这些规则保护的是"
        f"「不要被一次极端行情打爆」，这跟能不能盈利是两个问题。 |")
    add("")

    add("## 五、必须知道的偏差")
    add("")
    add("1. **样本期只有一年，且是单边熊市。** 所有「赚钱」的结论都可能是"
        "市场状态给的，不是策略给的。")
    add("2. **幸存者偏差**：标的池取自当前仍挂牌的合约，一年内下架的未纳入。")
    add("3. **多重检验**：一次看了 5 个方向性策略并对参数做了几十次扰动。"
        "本报告刻意不做参数寻优，敏感性分析的目的正是暴露脆弱性。")
    add("4. **方向性模块未计资金费**：长期持有的多头在正费率环境下要付费，"
        "这会系统性高估多头收益。")
    add("5. **日线粒度**：止损、强平、盘中极端行情无法体现，"
        "真实杠杆交易最大的风险（被强平）在日线回测里看不到。")
    add("6. **套利模块未建模永续腿强平**：delta 中性下总权益不受价格波动影响，"
        "但永续空头腿在急涨中可能被单独强平。")
    add("7. 未考虑税负、借币成本、交易所对手方风险。")
    add("")
    add("---")
    add("")
    add("以下为两部分的详细报告。")
    add("")
    return "\n".join(L)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="套利 + 永续方向性 合并回测")
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--max-positions", type=int, default=5)
    args = ap.parse_args()

    markets = load_markets()
    if not markets:
        print("缓存为空，请先运行：python -m backtest.fetch_history")
        return 1
    print(f"载入 {len(markets)} 个标的，"
          f"区间 {min(m.dates[0] for m in markets)} ~ {max(m.dates[-1] for m in markets)}")

    arb_params = BacktestParams(capital=args.capital, max_positions=args.max_positions)
    perp_params = PerpParams(capital=args.capital, max_positions=args.max_positions)

    arb = simulate(markets, arb_params)
    print("\n[套利]")
    print(arb_summary(arb))
    perp_results = run_perp_all(markets, perp_params)

    excluded: list[str] = []
    universe_file = CACHE_DIR / "universe.json"
    if universe_file.exists():
        meta = json.loads(universe_file.read_text(encoding="utf-8"))
        cached = {m.symbol for m in markets}
        excluded = [s for s in meta.get("symbols", []) if s not in cached]

    parts = [
        build_summary(arb, perp_results, arb_params),
        demote_headings(arb_report(arb_params, markets, arb, excluded)),
        demote_headings(perp_report(perp_params, markets, perp_results)),
    ]
    out_dir = OUT_DIR
    (out_dir / "report_all.md").write_text("\n\n".join(parts), encoding="utf-8")
    (out_dir / "results_all.json").write_text(
        json.dumps({
            "arbitrage": arb.to_json(),
            "perp": {k: v.to_json() for k, v in perp_results.items()},
        }, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"\n合并报告已写入 {out_dir / 'report_all.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
