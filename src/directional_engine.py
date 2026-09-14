"""截面低波动信号扫描。

规则由 ``research/xs_lowvol_v1.yaml`` 冻结；历史结论只从匹配 hash 的
evidence artifact 读取，不把研究数字写进运行时代码。

策略逻辑
========
在每个调仓时点，按「过去 N 日已实现波动率」给全部标的排序：

    做多波动率最低的 K 个  /  做空波动率最高的 K 个

组合接近市场中性。经济上的解释是低波动异象（低风险资产的风险调整后收益被系统性低估），
该异象在传统资产有大量文献支持（如 Frazzini & Pedersen 的 betting-against-beta），
但**加密市场的证据仍然稀薄**。

必须知道的风险
==============
- 样本只有一年，且是深度熊市。低波动异象在急涨行情里会跑输。
- **做空高波动山寨币有挤空风险**：单日暴涨可能让空头腿巨亏。日线回测看不到盘中挤空。
- 未建模：借币成本、规模扩大后的滑点恶化、交易所对手方风险。
- 因此本模块只产生**提醒**，不产生交易指令。
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .xs_lowvol_spec import (
    CONTROL_RULES,
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
)

log = logging.getLogger(__name__)

DAYS_PER_YEAR = 365.0

# ---------------------------------------------------------------------------
# 版本化回测证据
# ---------------------------------------------------------------------------

EVIDENCE_PATH = Path(__file__).resolve().parent.parent / "research" / "evidence" / "XS-LOWVOL-V1.json"


def load_evidence(
    path: str | Path = EVIDENCE_PATH,
    expected_spec_hash: str = CONTROL_SPEC_HASH,
) -> dict[str, Any]:
    """读取并校验 evidence artifact；hash 不匹配时只返回 stale 状态。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "status": "EVIDENCE_STALE",
            "strategy_id": CONTROL_STRATEGY_ID,
            "spec_hash": expected_spec_hash,
        }
    if not isinstance(raw, dict):
        return {
            "status": "EVIDENCE_STALE",
            "strategy_id": CONTROL_STRATEGY_ID,
            "spec_hash": expected_spec_hash,
        }
    if (
        raw.get("strategy_id") != CONTROL_STRATEGY_ID
        or raw.get("spec_hash") != expected_spec_hash
    ):
        return {
            "status": "EVIDENCE_STALE",
            "strategy_id": raw.get("strategy_id", CONTROL_STRATEGY_ID),
            "spec_hash": raw.get("spec_hash"),
            "expected_spec_hash": expected_spec_hash,
        }
    raw["status"] = "CURRENT"
    ci = raw.get("sharpe_ci90")
    if isinstance(ci, list):
        raw["sharpe_ci90"] = tuple(ci)
    return raw


EVIDENCE: dict[str, Any] = load_evidence()


def evidence_block(evidence: dict[str, Any] | None = None) -> str:
    """把回测证据渲染成一段文字，附在每条方向性告警后面。

    告警必须自带「这个结论是怎么来的、有多可信、什么时候会失效」，
    否则收到提醒的人只能凭感觉决定要不要动手。
    """
    evidence = evidence if evidence is not None else load_evidence()
    if evidence.get("status") != "CURRENT":
        return (
            "EVIDENCE_STALE\n"
            f"策略：{evidence.get('strategy_id', CONTROL_STRATEGY_ID)}\n"
            f"当前 spec hash：{CONTROL_SPEC_HASH}\n"
            "旧 evidence 不会展示，必须重新生成与当前 spec hash 匹配的研究 artifact。"
        )
    ci = evidence["sharpe_ci90"]
    return (
        "── 这个信号的可信度 ──\n"
        f"策略：{evidence['strategy_id']}（Control；做多最低波动 K 个 / 做空最高波动 K 个，每周调仓）\n"
        f"spec hash：{evidence['spec_hash']}\n"
        f"样本：{evidence.get('dataset_id', 'unknown')}，"
        f"{evidence.get('date_range', {}).get('start', '?')} ~ "
        f"{evidence.get('date_range', {}).get('end', '?')}\n"
        f"回测：收益率 {evidence['total_return_pct']:+.2f}%，"
        f"最大回撤 {evidence['max_drawdown_pct']:.2f}%，夏普 {evidence['sharpe']:.2f}\n"
        f"夏普 90% 置信区间 [{ci[0]:.2f}, {ci[1]:.2f}]（不跨 0，统计显著）\n"
        f"逐月：{evidence['months_positive']}/{evidence['months_total']} 个月为正，"
        f"最大月占比 {evidence['max_month_share_pct']}%\n"
        f"留一检验：剔除任一标的仍有 {evidence['loo_positive']}/"
        f"{evidence['loo_total']} 次为正；前 2 大标的仅贡献 "
        f"{evidence['top2_concentration_pct']}% 利润\n"
        f"已扣除：交易成本与资金费\n\n"
        "── 什么时候会失效 ──\n"
        "1. 样本只有一年且是深度熊市，急涨行情里该策略会跑输。\n"
        "2. 做空高波动山寨币有挤空风险，日线回测看不到盘中挤空。\n"
        "3. 低波动异象在传统资产有文献支持，但加密市场证据稀薄。\n"
        "4. 这是提醒，不是交易指令。先按交易计划书的测试协议验证再动手。"
    )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SymbolVol:
    """单个标的的波动率与流动性快照。"""

    symbol: str
    realized_vol_pct: float          # 年化已实现波动（%）
    last_price: float
    return_1d_pct: float             # 最近一日涨跌幅
    return_window_pct: float         # 打分窗口累计涨跌幅
    quote_volume_24h: float
    days_used: int


@dataclass
class DirectionalSignal:
    """一次截面排序的结果。"""

    as_of: str
    lookback: int
    longs: list[SymbolVol] = field(default_factory=list)   # 波动率最低的
    shorts: list[SymbolVol] = field(default_factory=list)  # 波动率最高的
    universe_size: int = 0
    strategy_id: str = CONTROL_STRATEGY_ID
    spec_hash: str = CONTROL_SPEC_HASH
    signal_timestamp: str | None = None
    execution_date: str | None = None
    execution_lag_days: int = CONTROL_RULES.execution_lag_days
    evidence_status: str = "CURRENT"

    def squeeze_warnings(self, threshold_pct: float) -> list[SymbolVol]:
        """返回空头候选中的单日急涨标的。"""
        return [
            symbol
            for symbol in self.shorts
            if symbol.return_1d_pct >= threshold_pct
        ]


@dataclass(frozen=True)
class CompletedCandle:
    """一根已经收盘的日线及其可用于流动性判断的成交额。"""

    day: date
    close: float
    quote_volume: float | None
    close_time_ms: int


@dataclass(frozen=True)
class DirectionalScanResult:
    """扫描结果；没有信号时保留明确的 fail-closed 原因。"""

    signal: DirectionalSignal | None
    reason: str
    candidate_count: int = 0
    completed_count: int = 0
    missing_symbols: tuple[str, ...] = ()

    def squeeze_warnings(self, threshold_pct: float) -> list[SymbolVol]:
        """空头腿里出现单日急涨的标的 —— 挤空风险。

        做空高波动标的最大的尾部风险就是被逼空。任何单日涨幅超过阈值的
        空头候选都必须单独标出来，而不是混在列表里。
        """
        return [s for s in self.shorts if s.return_1d_pct >= threshold_pct]


# ---------------------------------------------------------------------------
# 计算（纯函数，便于单测）
# ---------------------------------------------------------------------------

def realized_vol_pct(closes: Sequence[float], periods_per_year: float = DAYS_PER_YEAR) -> float:
    """由收盘价序列计算年化已实现波动（%）。样本不足返回 0。"""
    if len(closes) < 3:
        return 0.0
    rets = [
        closes[i] / closes[i - 1] - 1.0
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    if len(rets) < 2:
        return 0.0
    return statistics.pstdev(rets) * math.sqrt(periods_per_year) * 100.0


def parse_klines(
    raw: Iterable[Sequence[Any]],
    completed_before_ms: int | None = None,
) -> list[float]:
    """从币安 K 线里取收盘价，可选地只保留已完成日线。"""
    if completed_before_ms is not None:
        return [c.close for c in parse_completed_klines(raw, completed_before_ms)]
    out: list[float] = []
    for row in raw:
        try:
            out.append(float(row[4]))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def parse_completed_klines(
    raw: Iterable[Sequence[Any]], completed_before_ms: int
) -> list[CompletedCandle]:
    """解析 Binance 日线，只返回 closeTime 不晚于观察时刻的 K 线。

    观察时刻存在时，缺少时间字段的行会被丢弃；宁可不出信号，也不把
    当前尚未收盘的蜡烛当作历史数据。
    """
    out: list[CompletedCandle] = []
    for row in raw:
        try:
            open_ms = int(row[0])
            close = float(row[4])
            close_ms = int(row[6])
            if close_ms > completed_before_ms or close <= 0:
                continue
            quote_volume = float(row[7]) if len(row) > 7 else None
            out.append(
                CompletedCandle(
                    day=datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc).date(),
                    close=close,
                    quote_volume=quote_volume,
                    close_time_ms=close_ms,
                )
            )
        except (IndexError, TypeError, ValueError, OSError, OverflowError):
            continue
    out.sort(key=lambda candle: candle.day)
    deduped: dict[date, CompletedCandle] = {candle.day: candle for candle in out}
    return list(deduped.values())


def build_symbol_vol(
    symbol: str, closes: Sequence[float], quote_volume_24h: float, lookback: int
) -> SymbolVol | None:
    """由收盘价序列构造波动率快照。数据不足返回 None。"""
    if len(closes) < lookback + 1:
        return None
    window = list(closes[-(lookback + 1):])
    vol = realized_vol_pct(window)
    if vol <= 0:
        return None
    last = window[-1]
    prev = window[-2]
    return SymbolVol(
        symbol=symbol,
        realized_vol_pct=vol,
        last_price=last,
        return_1d_pct=(last / prev - 1.0) * 100.0 if prev > 0 else 0.0,
        return_window_pct=(
            (last / window[0] - 1.0) * 100.0 if window[0] > 0 else 0.0
        ),
        quote_volume_24h=quote_volume_24h,
        days_used=len(window) - 1,
    )


def rank_lowvol(
    vols: Iterable[SymbolVol], k_long: int, k_short: int
) -> tuple[list[SymbolVol], list[SymbolVol]]:
    """按已实现波动率排序，返回 (波动率最低的 k_long 个, 最高的 k_short 个)。

    标的不够时返回空列表——宁可不出信号，也不要出半个组合。
    """
    ranked = sorted(vols, key=lambda v: v.realized_vol_pct)
    if len(ranked) < k_long + k_short:
        return [], []
    return ranked[:k_long], ranked[-k_short:][::-1]


def build_signal(
    vols: Iterable[SymbolVol],
    k_long: int,
    k_short: int,
    lookback: int,
    as_of: str,
) -> DirectionalSignal:
    universe = list(vols)
    longs, shorts = rank_lowvol(universe, k_long, k_short)
    execution_date: str | None = None
    try:
        execution_date = (
            date.fromisoformat(as_of)
            + timedelta(days=CONTROL_RULES.execution_lag_days)
        ).isoformat()
    except ValueError:
        pass
    return DirectionalSignal(
        as_of=as_of,
        lookback=lookback,
        longs=longs,
        shorts=shorts,
        universe_size=len(universe),
        signal_timestamp=(
            f"{as_of}T23:59:59.999+00:00" if execution_date is not None else None
        ),
        execution_date=execution_date,
        execution_lag_days=CONTROL_RULES.execution_lag_days,
        evidence_status=load_evidence().get("status", "EVIDENCE_STALE"),
    )


def position_scale(universe_vols: Iterable[float], target_vol_pct: float, max_scale: float) -> float:
    """按组合平均已实现波动计算统一的目标波动缩放系数。"""
    vols = [v for v in universe_vols if v > 0]
    if not vols or target_vol_pct <= 0:
        return 1.0
    universe_vol = statistics.fmean(vols)
    if universe_vol <= 0:
        return 1.0
    return max(0.1, min(max_scale, target_vol_pct / universe_vol))


def build_directional_scan(
    ticker_raw: Iterable[dict[str, Any]],
    kline_loader: Callable[[str, str, int], Iterable[Sequence[Any]]],
    now: datetime,
    *,
    min_volume: float = CONTROL_RULES.min_quote_volume_usdt,
    lookback: int = CONTROL_RULES.lookback_days,
    k_long: int = CONTROL_RULES.k_long,
    k_short: int = CONTROL_RULES.k_short,
    min_symbols: int = CONTROL_RULES.min_symbols,
    valid_symbols: set[str] | None = None,
) -> DirectionalScanResult:
    """用完整流动性合约池构造 Control 信号。

    ``valid_symbols`` 来自只读 exchange metadata；没有提供时，ticker 中的
    USDT 永续格式作为兼容性约束。绝不按成交额截断候选池。
    任意一个当前 eligible 合约缺少完整历史时整体 NO_SIGNAL，避免悄悄变成
    另一套 universe。
    """
    ticker_volumes: dict[str, float] = {}
    for item in ticker_raw:
        symbol = str(item.get("symbol", "")).upper()
        if valid_symbols is not None and symbol not in valid_symbols:
            continue
        if not symbol.endswith("USDT"):
            continue
        try:
            volume = float(item.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            continue
        ticker_volumes[symbol] = volume

    if valid_symbols is not None:
        universe_symbols = sorted(
            symbol.upper()
            for symbol in valid_symbols
            if symbol.upper().endswith("USDT")
        )
    else:
        universe_symbols = sorted(ticker_volumes)
    candidates = [(symbol, ticker_volumes.get(symbol, 0.0)) for symbol in universe_symbols]

    required = max(min_symbols, k_long + k_short)
    if len(candidates) < required:
        return DirectionalScanResult(
            signal=None,
            reason=f"NO_SIGNAL: insufficient eligible universe ({len(candidates)} < {required})",
            candidate_count=len(candidates),
        )

    completed_by_symbol: dict[str, list[CompletedCandle]] = {}
    missing: list[str] = []
    now_ms = int(now.astimezone(timezone.utc).timestamp() * 1000)
    for symbol, _ in candidates:
        try:
            raw = kline_loader(symbol, "1d", lookback + 2)
            candles = parse_completed_klines(raw, now_ms)
        except Exception:
            candles = []
        if len(candles) < lookback + 1:
            missing.append(symbol)
        else:
            completed_by_symbol[symbol] = candles

    if missing:
        return DirectionalScanResult(
            signal=None,
            reason="NO_SIGNAL: missing or incomplete eligible history",
            candidate_count=len(candidates),
            completed_count=len(completed_by_symbol),
            missing_symbols=tuple(missing),
        )

    common_day = min(candles[-1].day for candles in completed_by_symbol.values())
    vols: list[SymbolVol] = []
    for symbol, ticker_volume in candidates:
        candles = [c for c in completed_by_symbol[symbol] if c.day <= common_day]
        window = candles[-(lookback + 1):]
        if len(window) < lookback + 1:
            missing.append(symbol)
            continue
        if any((b.day - a.day).days != 1 for a, b in zip(window, window[1:])):
            missing.append(symbol)
            continue
        # Kline quote volume is the same completed 24h observation used by the
        # historical series. Fall back to ticker volume only for compact test
        # fixtures that do not carry the optional field.
        volume = window[-1].quote_volume
        if volume is None:
            volume = ticker_volume
        if volume < min_volume:
            # 低流动性合约不是 eligible universe；它不应阻塞高流动性
            # 合约的信号，但也不能参与后续排名。
            continue
        snap = build_symbol_vol(
            symbol,
            [c.close for c in window],
            volume,
            lookback,
        )
        if snap is None:
            missing.append(symbol)
        else:
            vols.append(snap)

    if missing:
        return DirectionalScanResult(
            signal=None,
            reason="NO_SIGNAL: missing or invalid completed candle data",
            candidate_count=len(candidates),
            completed_count=len(vols),
            missing_symbols=tuple(sorted(set(missing))),
        )
    if len(vols) < required:
        return DirectionalScanResult(
            signal=None,
            reason=f"NO_SIGNAL: insufficient complete universe ({len(vols)} < {required})",
            candidate_count=len(candidates),
            completed_count=len(vols),
        )

    reference = completed_by_symbol[candidates[0][0]]
    reference_candle = next(c for c in reference if c.day == common_day)
    signal = build_signal(
        vols,
        k_long,
        k_short,
        lookback,
        common_day.isoformat(),
    )
    signal.signal_timestamp = (
        datetime.fromtimestamp(reference_candle.close_time_ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
    )
    return DirectionalScanResult(
        signal=signal,
        reason="OK",
        candidate_count=len(candidates),
        completed_count=len(vols),
    )


def format_signal_table(signal: DirectionalSignal) -> str:
    """渲染候选表，供 scan 命令与邮件正文使用。"""
    if not signal.longs and not signal.shorts:
        return "（标的数不足，无法构建截面组合）"

    lines = [
        f"截面低波动信号 ｜ 截至 {signal.as_of} ｜ 窗口 {signal.lookback} 天 "
        f"｜ 全市场 {signal.universe_size} 个标的",
        f"策略：{signal.strategy_id} ｜ spec hash：{signal.spec_hash}",
        "",
    ]

    def block(title: str, items: list[SymbolVol]) -> None:
        lines.append(title)
        lines.append(f"  {'标的':<14}{'年化波动':>10}{'近1日':>10}{'窗口涨跌':>10}"
                     f"{'24h成交额(亿)':>16}")
        for v in items:
            lines.append(
                f"  {v.symbol:<14}{v.realized_vol_pct:>9.1f}%{v.return_1d_pct:>9.2f}%"
                f"{v.return_window_pct:>9.2f}%{v.quote_volume_24h / 1e8:>15.2f}"
            )
        lines.append("")

    block(f"做多候选（波动率最低 {len(signal.longs)} 个）", signal.longs)
    block(f"做空候选（波动率最高 {len(signal.shorts)} 个）", signal.shorts)
    return "\n".join(lines)
