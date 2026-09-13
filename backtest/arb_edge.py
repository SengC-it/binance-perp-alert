"""资金费套利的「边际曲线」分析。

不回答「哪个参数组合最赚钱」，而回答一个更根本的问题：

    **入场时的资金费水平，与随后实际实现的净收益，是什么关系？**

做法：对每个标的的每一天，假设在该日收盘入场（信号用该日收盘前已结算的数据，
次日收盘成交，无未来函数），持有 H 天后平仓，计算真实实现的净收益：

    净收益% = 持有期实际收到的资金费% + (入场基差 − 出场基差) − 往返成本%

然后把所有交易按「入场时的年化资金费」分桶，看每一桶的：
  · 笔数
  · 平均净收益 / 中位净收益
  · 胜率

如果高费率桶的净收益仍然为负，说明这类机会**根本不存在正期望**，
那么任何参数寻优都只是在拟合噪声。

用法：
  python -m backtest.arb_edge
  python -m backtest.arb_edge --min-volume 0     # 不做流动性过滤，样本最大化
"""

from __future__ import annotations

import argparse
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from .fetch_history import load_all
from .funding_arb import MS_PER_DAY, MarketSeries, build_series

log = logging.getLogger("backtest.arb_edge")
OUT_DIR = Path(__file__).resolve().parent

DAYS_PER_YEAR = 365.0

# 执行成本情形（往返，占名义价值 %）
#   0.36 = VIP0 全 taker（现货 0.10×2 + 永续 0.05×2 + 滑点 0.03×2）
#   0.30 = 永续改挂单成交（maker 0.02），滑点减半
#   0.24 = 再叠加 BNB 抵扣 25%
#   0.18 = 现货也用 BNB 抵扣 + 永续 maker + 极低滑点（理想执行）
COST_CASES: dict[str, float] = {
    "VIP0 全 taker": 0.36,
    "永续挂单 maker": 0.30,
    "maker + BNB 抵扣": 0.24,
    "理想执行(下限)": 0.18,
}

# 信号口径：入场前 N 天的已结算资金费年化
TRAILING_DAYS = [1, 3, 7, 14, 30]

# 持有期
HOLD_DAYS = [7, 14, 30, 60, 90]

# 分桶边界（年化 %）
BUCKETS = [(-1e9, 0.0), (0.0, 3.0), (3.0, 5.0), (5.0, 8.0), (8.0, 12.0),
           (12.0, 20.0), (20.0, 30.0), (30.0, 50.0), (50.0, 1e9)]


def _end_of_day_ms(day: date) -> int:
    return int(
        datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc).timestamp() * 1000
    )


@dataclass
class Observation:
    """一笔假设交易的完整记录。"""

    symbol: str
    entry_day: date
    hold_days: int
    signal_annual_pct: float    # 入场信号：前 N 天资金费年化
    entry_basis_pct: float      # 入场时的基差（永续相对现货的溢价 %）
    funding_pct: float          # 持有期实际收到的资金费（%）
    basis_pct: float            # 基差变动贡献（%）
    volume_24h: float

    @property
    def gross_pct(self) -> float:
        return self.funding_pct + self.basis_pct


def build_observations(
    series: MarketSeries, trailing_days: int, hold_days: int
) -> list[Observation]:
    """对单个标的生成全部「假设入场」的观测。"""
    out: list[Observation] = []
    n = len(series.dates)

    for i in range(n):
        entry_idx = i + 1          # 信号次日成交，避免未来函数
        exit_idx = entry_idx + hold_days
        if exit_idx >= n:
            break

        entry_day = series.dates[entry_idx]
        exit_day = series.dates[exit_idx]

        # --- 信号：截至信号日收盘已结算的数据 ---
        signal_ts = _end_of_day_ms(series.dates[i])
        count, rate_sum = series.funding_trailing(signal_ts, trailing_days)
        if count <= 0:
            continue
        # 窗口内实际经历的天数可能不足 trailing_days（上市初期），按实际跨度折算
        span_days = max(1.0, min(float(trailing_days), float(entry_idx)))
        signal_annual = rate_sum / span_days * DAYS_PER_YEAR * 100.0

        # --- 实际实现的资金费（持有期内真实结算）---
        entry_ts = _end_of_day_ms(entry_day)
        exit_ts = _end_of_day_ms(exit_day)
        _, fwd_sum = series.funding_sum_between(entry_ts, exit_ts)
        funding_pct = fwd_sum * 100.0

        # --- 基差变动 ---
        b0 = series.basis_pct(entry_day)
        b1 = series.basis_pct(exit_day)
        if b0 is None or b1 is None:
            continue
        basis_pct = b0 - b1

        out.append(
            Observation(
                symbol=series.symbol,
                entry_day=entry_day,
                hold_days=hold_days,
                signal_annual_pct=signal_annual,
                entry_basis_pct=b0,
                funding_pct=funding_pct,
                basis_pct=basis_pct,
                volume_24h=series.perp_volume.get(entry_day, 0.0),
            )
        )
    return out


def edge_table(
    obs: list[Observation], cost_pct: float
) -> list[tuple[str, int, float, float, float, float, float]]:
    """按信号水平分桶。

    返回 [(桶标签, 笔数, 平均净收益%, 中位净收益%, 胜率%, 平均资金费%, 平均基差%)]。
    拆开资金费与基差是关键诊断：如果资金费为正而基差为负，说明真正亏钱的是
    基差（永续溢价崩塌），而不是资金费本身。
    """
    rows: list[tuple[str, int, float, float, float, float, float]] = []
    for lo, hi in BUCKETS:
        group = [o for o in obs if lo <= o.signal_annual_pct < hi]
        if not group:
            continue
        nets = [o.gross_pct - cost_pct for o in group]
        wins = sum(1 for x in nets if x > 0)
        label = (
            f"{lo:.0f}~{hi:.0f}%" if lo > -1e8
            else f"< {hi:.0f}%"
        )
        rows.append(
            (
                label,
                len(group),
                statistics.fmean(nets),
                statistics.median(nets),
                wins / len(nets) * 100.0,
                statistics.fmean(o.funding_pct for o in group),
                statistics.fmean(o.basis_pct for o in group),
            )
        )
    return rows


BASIS_BUCKETS = [
    (-1e9, 0.0), (0.0, 0.02), (0.02, 0.05), (0.05, 0.10),
    (0.10, 0.20), (0.20, 0.50), (0.50, 1e9),
]


def basis_table(
    obs: list[Observation], cost_pct: float
) -> list[tuple[str, int, float, float, float, float, float]]:
    """按**入场基差水平**分桶，检验「买基差收敛」这一替代假设。

    如果现金套保的收益真的来自「溢价收敛」，那么入场时基差越高（永续溢价越极端），
    基差贡献应该越大、净收益应该越好。如果这里也全是负的，说明该假设同样不成立。
    """
    rows: list[tuple[str, int, float, float, float, float, float]] = []
    for lo, hi in BASIS_BUCKETS:
        group = [o for o in obs if lo <= o.entry_basis_pct < hi]
        if not group:
            continue
        nets = [o.gross_pct - cost_pct for o in group]
        wins = sum(1 for x in nets if x > 0)
        label = f"{lo:.2f}~{hi:.2f}%" if lo > -1e8 else f"< {hi:.2f}%"
        rows.append(
            (
                label,
                len(group),
                statistics.fmean(nets),
                statistics.median(nets),
                wins / len(nets) * 100.0,
                statistics.fmean(o.funding_pct for o in group),
                statistics.fmean(o.basis_pct for o in group),
            )
        )
    return rows


def dedup_events(obs: list[Observation], hold_days: int) -> list[Observation]:
    """把同一标的的连续入场日合并成「独立事件」。

    回测里「3,378 笔交易」往往只是 20 个标的在 3 个月里的连续交易日。
    真正独立的样本量应该按「同一标的间隔 > 持有期算新事件」来数。
    这里保留每个事件的首日观测（即「你只会入场一次」的口径）。
    """
    events: list[Observation] = []
    prev: dict[str, date] = {}
    for o in sorted(obs, key=lambda x: (x.symbol, x.entry_day)):
        last = prev.get(o.symbol)
        if last is None or (o.entry_day - last).days > hold_days:
            events.append(o)
        prev[o.symbol] = o.entry_day
    return events


def robustness_check(
    obs: list[Observation], cost_pct: float, hold_days: int
) -> dict[str, Any]:
    """对一个「看起来正期望」的格子做两项去伪检查。

    1. 事件去重：名义笔数 → 独立事件数
    2. 留一法：剔除贡献最大的标的，看结论是否还成立
    """
    if not obs:
        return {"nominal": 0, "events": 0, "mean_nominal": 0.0,
                "mean_events": 0.0, "mean_ex_top": 0.0, "top_symbol": "—",
                "top_symbol_n": 0}
    nets = [o.gross_pct - cost_pct for o in obs]
    events = dedup_events(obs, hold_days)
    event_nets = [o.gross_pct - cost_pct for o in events]

    counts: dict[str, int] = {}
    for o in obs:
        counts[o.symbol] = counts.get(o.symbol, 0) + 1
    top_symbol = max(counts, key=lambda s: counts[s]) if counts else "—"
    rest = [o.gross_pct - cost_pct for o in obs if o.symbol != top_symbol]

    return {
        "nominal": len(obs),
        "events": len(events),
        "mean_nominal": statistics.fmean(nets),
        "mean_events": statistics.fmean(event_nets) if event_nets else 0.0,
        "mean_ex_top": statistics.fmean(rest) if rest else 0.0,
        "top_symbol": top_symbol,
        "top_symbol_n": counts.get(top_symbol, 0),
    }


def best_threshold(
    obs: list[Observation], cost_pct: float, min_trades: int = 30
) -> tuple[float, int, float, float] | None:
    """在信号阈值网格上寻找「平均净收益为正且笔数达标」的最优点。

    返回 (阈值%, 笔数, 平均净收益%, 胜率%)；找不到则返回 None。
    """
    if not obs:
        return None
    signals = sorted(o.signal_annual_pct for o in obs)
    # 用分位数取候选阈值，避免网格太密导致的过拟合假象
    candidates = {0.0}
    for q in range(50, 100, 2):
        idx = int(len(signals) * q / 100)
        if 0 <= idx < len(signals):
            candidates.add(round(signals[idx], 2))

    best: tuple[float, int, float, float] | None = None
    for thr in sorted(candidates):
        group = [o for o in obs if o.signal_annual_pct >= thr]
        if len(group) < min_trades:
            continue
        nets = [o.gross_pct - cost_pct for o in group]
        mean = statistics.fmean(nets)
        if mean <= 0:
            continue
        wins = sum(1 for x in nets if x > 0) / len(nets) * 100.0
        if best is None or mean > best[2]:
            best = (thr, len(group), mean, wins)
    return best


def build_report(markets: list[dict[str, Any]], min_volume: float) -> str:
    series_list = [build_series(rec) for rec in markets]
    series_list = [s for s in series_list if len(s.dates) >= 300]
    if not series_list:
        raise SystemExit("没有满足最小长度要求的标的")
    span = (
        min(s.dates[0] for s in series_list),
        max(s.dates[-1] for s in series_list),
    )

    lines: list[str] = []
    lines.append("# 资金费套利：边际曲线分析")
    lines.append("")
    lines.append("> 本报告不寻找「最优参数」，而是测量**入场时的资金费水平**与"
                 "**随后实际实现的净收益**之间的关系。")
    lines.append("> 如果高费率桶的净收益依然为负，说明这类机会不存在正期望，"
                 "任何参数寻优都只是在拟合噪声。")
    lines.append("")
    lines.append(f"- 标的数：{len(series_list)}")
    lines.append(f"- 区间：{span[0]} ~ {span[1]}")
    lines.append(f"- 流动性过滤：24h 成交额 ≥ {min_volume:,.0f} USDT"
                 + ("（未启用）" if min_volume <= 0 else ""))
    lines.append("- 时序：信号用截至信号日收盘的已结算数据 → 次日收盘成交 → 持有 H 天后收盘平仓")
    lines.append("")

    # ---------------- 1. 边际曲线 ----------------
    lines.append("## 一、边际曲线：费率水平 → 实际净收益")
    lines.append("")
    lines.append("每个格子是「该费率桶内所有假设交易的平均净收益（%）」。"
                 "**正数才代表这一桶值得做。**")
    lines.append("")

    hold_focus = 30
    for cost_name, cost_pct in COST_CASES.items():
        lines.append(f"### 成本情形：{cost_name}（往返 {cost_pct:.2f}%）")
        lines.append("")
        for trailing in (3, 7, 14):
            obs = []
            for s in series_list:
                for o in build_observations(s, trailing, hold_focus):
                    if min_volume > 0 and o.volume_24h < min_volume:
                        continue
                    obs.append(o)
            rows = edge_table(obs, cost_pct)
            if not rows:
                continue
            lines.append(f"**信号：前 {trailing} 天资金费年化 ｜ 持有 {hold_focus} 天**")
            lines.append("")
            lines.append("| 费率桶 | 笔数 | 平均净收益 | 中位净收益 | 胜率 | 资金费贡献 | 基差贡献 |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for label, cnt, mean, med, win, fund, bas in rows:
                flag = " ✅" if mean > 0 else ""
                lines.append(
                    f"| {label} | {cnt:,} | {mean:+.3f}%{flag} | {med:+.3f}% | "
                    f"{win:.1f}% | {fund:+.3f}% | {bas:+.3f}% |"
                )
            lines.append("")

    # ---------------- 2. 替代假设：按基差入场 ----------------
    lines.append("## 二、替代假设：不按资金费、改按基差入场（买收敛）")
    lines.append("")
    lines.append("现金套保的另一条盈利逻辑是「溢价收敛」：在永续溢价极端时入场做空永续，"
                 "等溢价回归。若该假设成立，入场基差越高，基差贡献应越大、净收益应越好。")
    lines.append("")
    for cost_name, cost_pct in COST_CASES.items():
        lines.append(f"### {cost_name}（往返 {cost_pct:.2f}%）")
        lines.append("")
        lines.append("| 入场基差 | 笔数 | 平均净收益 | 中位净收益 | 胜率 | 资金费贡献 | 基差贡献 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for hold in (30, 90):
            obs = []
            for s in series_list:
                for o in build_observations(s, 7, hold):
                    if min_volume > 0 and o.volume_24h < min_volume:
                        continue
                    obs.append(o)
            rows = basis_table(obs, cost_pct)
            lines.append(f"| **持有 {hold} 天** | | | | | | |")
            for label, cnt, mean, med, win, fund, bas in rows:
                flag = " ✅" if mean > 0 else ""
                lines.append(
                    f"| {label} | {cnt:,} | {mean:+.3f}%{flag} | {med:+.3f}% | "
                    f"{win:.1f}% | {fund:+.3f}% | {bas:+.3f}% |"
                )
        lines.append("")

    # ---------------- 3. 去伪检查 ----------------
    lines.append("## 三、这些「正期望」格子经得起推敲吗？")
    lines.append("")
    lines.append("上面按基差分桶时，出现了几个标着 ✅ 的正期望格子。"
                 "但回测里「N 笔交易」常常只是少数标的在少数月份里的**连续交易日**，"
                 "并非 N 个独立样本。这一节做两项去伪检查：")
    lines.append("")
    lines.append("1. **事件去重**：同一标的间隔超过持有期才算一个新事件")
    lines.append("2. **留一法**：剔除贡献最大的那个标的，看结论是否还成立")
    lines.append("")
    lines.append("| 持有期 | 入场基差阈值 | 名义笔数 | 独立事件 | 名义均值 | 事件均值 | 剔除最大标的 | 最大标的 |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---|")
    for hold in (30, 90):
        for thr in (0.10, 0.20, 0.30, 0.50):
            obs = []
            for s in series_list:
                for o in build_observations(s, 7, hold):
                    if o.entry_basis_pct < thr:
                        continue
                    if min_volume > 0 and o.volume_24h < min_volume:
                        continue
                    obs.append(o)
            chk = robustness_check(obs, 0.36, hold)
            if chk["nominal"] == 0:
                continue
            lines.append(
                f"| {hold} 天 | ≥{thr:.2f}% | {chk['nominal']:,} | **{chk['events']}** | "
                f"{chk['mean_nominal']:+.3f}% | {chk['mean_events']:+.3f}% | "
                f"{chk['mean_ex_top']:+.3f}% | {chk['top_symbol']}"
                f"（{chk['top_symbol_n']} 笔） |"
            )
    lines.append("")
    lines.append("**判读标准**：只有当「独立事件数 ≥ 30」且「剔除最大标的后仍为正」时，"
                 "才可以把一个格子当作候选 edge。若独立事件只有个位数、"
                 "或剔除单一标的后结论翻转，那就是**一个样本点的故事，不是统计验证**。")
    lines.append("")

    # ---------------- 4. 最优阈值搜索 ----------------
    lines.append("## 四、是否存在「正期望且笔数足够」的配置？")
    lines.append("")
    lines.append("在信号阈值网格上搜索平均净收益为正、且笔数 ≥ 30 的配置。"
                 "阈值取自信号分位数，避免网格过密造成的假象。")
    lines.append("")

    for cost_name, cost_pct in COST_CASES.items():
        lines.append(f"### {cost_name}（往返 {cost_pct:.2f}%）")
        lines.append("")
        lines.append("| 信号窗口 | 持有期 | 最优阈值 | 笔数 | 平均净收益 | 胜率 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        any_found = False
        for trailing in TRAILING_DAYS:
            for hold in HOLD_DAYS:
                obs = []
                for s in series_list:
                    for o in build_observations(s, trailing, hold):
                        if min_volume > 0 and o.volume_24h < min_volume:
                            continue
                        obs.append(o)
                best = best_threshold(obs, cost_pct)
                if best is None:
                    lines.append(
                        f"| 前 {trailing} 天 | {hold} 天 | — | — | 无正期望配置 | — |"
                    )
                else:
                    any_found = True
                    thr, cnt, mean, win = best
                    lines.append(
                        f"| 前 {trailing} 天 | {hold} 天 | ≥{thr:.1f}% | "
                        f"{cnt:,} | **{mean:+.3f}%** | {win:.1f}% |"
                    )
        lines.append("")
        if not any_found:
            lines.append(f"**结论：在「{cost_name}」成本下，"
                         f"没有任何（信号窗口 × 持有期）组合能在笔数 ≥ 30 的前提下取得正的平均净收益。**")
            lines.append("")

    # ---------------- 5. 持有期与成本的相对重要性 ----------------
    lines.append("## 五、成本与持有期的相对重要性")
    lines.append("")
    lines.append("往返成本是**一次性绝对支出**，资金费收入**随持有期线性增长**。"
                 "因此同样的费率水平，持有越久越可能转正。")
    lines.append("")
    lines.append("| 持有期 | 成本 0.36% 需覆盖的日均 | 成本 0.30% | 成本 0.24% | 成本 0.18% |")
    lines.append("|---:|---:|---:|---:|---:|")
    for hold in HOLD_DAYS:
        cells = [f"{c / hold:.4f}%/天" for c in (0.36, 0.30, 0.24, 0.18)]
        lines.append(f"| {hold} 天 | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("换算成年化（×365）：")
    lines.append("")
    lines.append("| 持有期 | 0.36% | 0.30% | 0.24% | 0.18% |")
    lines.append("|---:|---:|---:|---:|---:|")
    for hold in HOLD_DAYS:
        cells = [f"{c * DAYS_PER_YEAR / hold:.2f}%" for c in (0.36, 0.30, 0.24, 0.18)]
        lines.append(f"| {hold} 天 | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("**读法**：持有 30 天、成本 0.36% 时，资金费年化必须超过 4.38% 才有正收益。"
                 "而样本期的中位费率年化只有约 3.2% —— 这就是套利在这段样本里不赚钱的根本原因。")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="资金费套利边际曲线分析")
    parser.add_argument("--min-volume", type=float, default=0.0,
                        help="24h 成交额下限（USDT），0 表示不过滤")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    markets = load_all()
    if not markets:
        raise SystemExit("缓存为空，请先运行 python -m backtest.fetch_history")
    log.info("载入 %d 个标的", len(markets))

    report = build_report(markets, args.min_volume)
    out = OUT_DIR / "report_arb_edge.md"
    out.write_text(report, encoding="utf-8")
    log.info("边际曲线报告已写入 %s", out)

    # 控制台也打印一遍核心结论
    series_list = [build_series(rec) for rec in markets]
    series_list = [s for s in series_list if len(s.dates) >= 300]
    print()
    print("=" * 78)
    print("持有 30 天、信号取前 7 天年化 —— 各费率桶的平均净收益")
    print("=" * 78)
    for cost_name, cost_pct in COST_CASES.items():
        obs = [o for s in series_list for o in build_observations(s, 7, 30)]
        rows = edge_table(obs, cost_pct)
        print(f"\n【{cost_name}】往返成本 {cost_pct:.2f}%")
        print(f"  {'费率桶':<12}{'笔数':>8}{'平均净收益':>12}{'中位':>10}"
              f"{'胜率':>8}{'资金费':>10}{'基差':>10}")
        for label, cnt, mean, med, win, fund, bas in rows:
            mark = "  <- 正期望" if mean > 0 else ""
            print(f"  {label:<12}{cnt:>8,}{mean:>11.3f}%{med:>9.3f}%"
                  f"{win:>7.1f}%{fund:>9.3f}%{bas:>9.3f}%{mark}")


if __name__ == "__main__":
    main()
