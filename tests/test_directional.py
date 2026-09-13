"""截面低波动引擎与方向性规则的测试。

全部用合成数据，期望值可手工推导。
"""

from __future__ import annotations

import math

import pytest

from src.config import Config
from src.directional_engine import (
    EVIDENCE,
    DirectionalSignal,
    SymbolVol,
    build_signal,
    build_symbol_vol,
    evidence_block,
    format_signal_table,
    parse_klines,
    position_scale,
    rank_lowvol,
    realized_vol_pct,
)
from src.models import Severity
from src.rules import evaluate_directional_rules


def make_vol(symbol: str, vol: float, ret_1d: float = 0.0) -> SymbolVol:
    return SymbolVol(
        symbol=symbol,
        realized_vol_pct=vol,
        last_price=100.0,
        return_1d_pct=ret_1d,
        return_window_pct=1.0,
        quote_volume_24h=8e7,
        days_used=30,
    )


# ---------------------------------------------------------------------------
# 波动率计算
# ---------------------------------------------------------------------------

def test_realized_vol_alternating_one_percent():
    """日收益交替 ±1% 时，总体标准差正好是 1%，年化 = 1% × √365。"""
    closes = [100.0]
    for i in range(4):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 0.99))
    expected = 0.01 * math.sqrt(365.0) * 100.0
    assert realized_vol_pct(closes) == pytest.approx(expected, rel=1e-6)


def test_realized_vol_flat_series_is_zero():
    assert realized_vol_pct([100.0] * 10) == 0.0


def test_realized_vol_too_few_points():
    assert realized_vol_pct([100.0, 101.0]) == 0.0


def test_realized_vol_scales_with_periods_per_year():
    closes = [100.0, 101.0, 100.0, 101.0, 100.0]
    daily = realized_vol_pct(closes, periods_per_year=1.0)
    yearly = realized_vol_pct(closes, periods_per_year=365.0)
    assert yearly == pytest.approx(daily * math.sqrt(365.0), rel=1e-9)


# ---------------------------------------------------------------------------
# K 线解析与快照
# ---------------------------------------------------------------------------

def test_parse_klines_extracts_close():
    raw = [
        [1, "10", "12", "9", "11", "100", 2, "1000"],
        [2, "11", "13", "10", "12", "200", 3, "2000"],
    ]
    assert parse_klines(raw) == [11.0, 12.0]


def test_parse_klines_skips_malformed_rows():
    raw = [[1, "10", "12", "9", "11"], ["bad"], [3, "1", "1", "1", "7"]]
    assert parse_klines(raw) == [11.0, 7.0]


def test_build_symbol_vol_returns_none_when_not_enough_data():
    closes = [100.0 + i for i in range(10)]
    assert build_symbol_vol("X", closes, 1e8, lookback=30) is None


def test_build_symbol_vol_returns_none_for_flat_prices():
    closes = [100.0] * 40
    assert build_symbol_vol("X", closes, 1e8, lookback=30) is None


def test_build_symbol_vol_computes_window_metrics():
    closes = [100.0]
    for i in range(35):
        closes.append(closes[-1] * (1.02 if i % 2 == 0 else 0.98))
    snap = build_symbol_vol("X", closes, 5e7, lookback=30)
    assert snap is not None
    assert snap.days_used == 30
    assert snap.realized_vol_pct > 0
    assert snap.quote_volume_24h == 5e7
    # 窗口首个收盘价到最后一个的整体涨跌
    window = closes[-31:]
    assert snap.return_window_pct == pytest.approx(
        (window[-1] / window[0] - 1.0) * 100.0, rel=1e-9
    )


# ---------------------------------------------------------------------------
# 排序
# ---------------------------------------------------------------------------

def test_rank_lowvol_splits_correctly():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(10)]
    longs, shorts = rank_lowvol(vols, k_long=3, k_short=3)
    assert [v.symbol for v in longs] == ["S0", "S1", "S2"]
    # 空头按波动率从高到低
    assert [v.symbol for v in shorts] == ["S9", "S8", "S7"]


def test_rank_lowvol_returns_empty_when_universe_too_small():
    vols = [make_vol(f"S{i}", 10.0) for i in range(5)]
    assert rank_lowvol(vols, k_long=3, k_short=3) == ([], [])


def test_rank_lowvol_does_not_overlap_legs():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(10)]
    longs, shorts = rank_lowvol(vols, k_long=5, k_short=5)
    assert not (set(v.symbol for v in longs) & set(v.symbol for v in shorts))


def test_build_signal_records_universe_size():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(12)]
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    assert sig.universe_size == 12
    assert len(sig.longs) == 3 and len(sig.shorts) == 3
    assert sig.as_of == "2026-09-13"


# ---------------------------------------------------------------------------
# 挤空风险
# ---------------------------------------------------------------------------

def test_squeeze_warnings_flags_hot_short_candidates():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(10)]
    # 波动率最高的那个单日暴涨
    vols[-1] = make_vol("S9", 100.0, ret_1d=25.0)
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    hot = sig.squeeze_warnings(15.0)
    assert [v.symbol for v in hot] == ["S9"]


def test_squeeze_warnings_ignores_long_leg():
    """做多腿的上涨不是风险，不该报挤空。"""
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(10)]
    vols[0] = make_vol("S0", 1.0, ret_1d=30.0)   # 波动率最低，做多腿
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    assert sig.squeeze_warnings(15.0) == []


def test_squeeze_warnings_empty_when_calm():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1), ret_1d=1.0) for i in range(10)]
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    assert sig.squeeze_warnings(15.0) == []


# ---------------------------------------------------------------------------
# 仓位缩放
# ---------------------------------------------------------------------------

def test_position_scale_targets_universe_vol():
    # 全市场平均波动 50%，目标 50% -> 缩放 1.0
    assert position_scale([40.0, 60.0], 50.0, 3.0) == pytest.approx(1.0)


def test_position_scale_is_capped():
    # 目标 200% / 实际 50% = 4.0，但上限 3.0
    assert position_scale([50.0], 200.0, 3.0) == 3.0


def test_position_scale_has_floor():
    # 目标 1% / 实际 50% = 0.02，但有 0.1 下限
    assert position_scale([50.0], 1.0, 3.0) == 0.1


def test_position_scale_disabled_returns_one():
    assert position_scale([50.0], 0.0, 3.0) == 1.0


def test_position_scale_no_data_returns_one():
    assert position_scale([], 80.0, 3.0) == 1.0


# ---------------------------------------------------------------------------
# 证据块
# ---------------------------------------------------------------------------

def test_evidence_block_reports_the_validated_numbers():
    text = evidence_block()
    assert f"{EVIDENCE['sharpe']:.2f}" in text
    assert "不跨 0" in text
    assert f"{EVIDENCE['loo_positive']}/{EVIDENCE['loo_total']}" in text
    # 必须带上失效条件，否则告警会诱导人过度自信
    assert "什么时候会失效" in text
    assert "挤空" in text


def test_evidence_matches_cross_sectional_backtest():
    """证据数字必须与回测报告一致，改了策略就得同步改这里。"""
    assert EVIDENCE["strategy"] == "xs_lowvol"
    assert EVIDENCE["sharpe"] == pytest.approx(2.36, abs=0.01)
    assert EVIDENCE["sharpe_ci90"][0] > 0, "置信区间必须不跨 0 才够格做自动化依据"
    assert EVIDENCE["loo_positive"] == EVIDENCE["loo_total"]


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------

def test_signal_rule_triggers_when_portfolio_ready():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(12)]
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    results = evaluate_directional_rules(Config(), sig)
    signal_rule = next(r for r in results if r.rule_id == "xs_lowvol_signal")
    assert signal_rule.triggered is True
    # WARN：出现即推送，不进每日摘要
    assert signal_rule.severity == Severity.WARN
    # 简洁版：给的是可执行的买入/做空清单，不是统计术语
    assert "S0" in signal_rule.body
    assert "买入这" in signal_rule.body
    assert "做空这" in signal_rule.body
    assert "不跨 0" not in signal_rule.body


def test_signal_rule_detailed_includes_evidence():
    """detail_level=detailed 时才附上统计依据。"""
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(12)]
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    cfg = Config(detail_level="detailed")
    results = evaluate_directional_rules(cfg, sig)
    signal_rule = next(r for r in results if r.rule_id == "xs_lowvol_signal")
    assert "不跨 0" in signal_rule.body
    assert "年化波动" in signal_rule.body


def test_signal_rule_simple_hides_volatility_numbers():
    """简洁版不应出现年化波动这类专业字段。"""
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(12)]
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    results = evaluate_directional_rules(Config(), sig)
    signal_rule = next(r for r in results if r.rule_id == "xs_lowvol_signal")
    assert "年化波动" not in signal_rule.body
    assert "夏普" not in signal_rule.body


def test_signal_rule_silent_when_universe_too_small():
    """标的不够时必须保持沉默，不能拿半个组合凑数。"""
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(4)]
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    results = evaluate_directional_rules(Config(), sig)
    signal_rule = next(r for r in results if r.rule_id == "xs_lowvol_signal")
    assert signal_rule.triggered is False


def test_no_signal_returns_no_results():
    assert evaluate_directional_rules(Config(), None) == []


def test_squeeze_rule_triggers_on_hot_short():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(10)]
    vols[-1] = make_vol("S9", 100.0, ret_1d=20.0)
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    results = evaluate_directional_rules(Config(), sig)
    squeeze = next(r for r in results if r.rule_id == "xs_lowvol_squeeze")
    assert squeeze.triggered is True
    assert squeeze.severity == Severity.WARN, "挤空是风险，必须走即时通道"
    assert "S9" in squeeze.body


def test_squeeze_rule_silent_when_no_hot_symbols():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1), ret_1d=0.5) for i in range(10)]
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    results = evaluate_directional_rules(Config(), sig)
    squeeze = next(r for r in results if r.rule_id == "xs_lowvol_squeeze")
    assert squeeze.triggered is False


def test_rules_use_distinct_dedup_keys():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(12)]
    sig = build_signal(vols, 3, 3, 30, "2026-09-13")
    results = evaluate_directional_rules(Config(), sig)
    keys = [r.dedup_key for r in results]
    assert len(set(keys)) == len(keys)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def test_format_signal_table_handles_empty_signal():
    sig = DirectionalSignal(as_of="2026-09-13", lookback=30)
    assert "无法构建" in format_signal_table(sig)


def test_format_signal_table_lists_both_legs():
    vols = [make_vol(f"S{i}", 10.0 * (i + 1)) for i in range(10)]
    sig = build_signal(vols, 2, 2, 30, "2026-09-13")
    text = format_signal_table(sig)
    assert "做多候选" in text
    assert "做空候选" in text
    assert "S0" in text and "S9" in text
