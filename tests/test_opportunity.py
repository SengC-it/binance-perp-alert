"""机会扫描引擎与机会类规则测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Config, RuleConfig
from src.models import Severity
from src.opportunity_engine import (
    build_opportunity,
    format_opportunity_table,
    round_trip_cost_pct,
    scan_opportunities,
)
from src.rules import RuleEngine, evaluate_opportunity_rules
from src.store import Store

SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)  # 新加坡时间 14:00


def make_cfg(**thresholds) -> Config:
    cfg = Config()
    cfg.thresholds = {
        "assumed_hold_days": 30.0,
        "min_volume_usdt_24h": 50_000_000.0,
        "min_net_annual_pct": 10.0,
        "basis_warn_pct": 0.30,
        "max_alerts_per_scan": 10,
        "spot_taker_fee_pct": 0.10,
        "perp_taker_fee_pct": 0.05,
        "slippage_pct": 0.03,
    }
    cfg.thresholds.update(thresholds)
    return cfg


def premium(symbol="BTCUSDT", funding="0.0001", mark="61000", index="60900"):
    return {
        "symbol": symbol,
        "markPrice": mark,
        "indexPrice": index,
        "lastFundingRate": funding,
        "nextFundingTime": 1789000000000,
    }


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "opp.db")
    yield s
    s.close()


# ---------- 成本与收益折算 ----------


def test_round_trip_cost_defaults_to_vip0():
    # 2 x (0.10 现货 + 0.05 永续) + 2 x 0.03 滑点 = 0.36
    assert round_trip_cost_pct(make_cfg()) == pytest.approx(0.36)


def test_net_annual_deducts_amortized_cost():
    cfg = make_cfg()
    opp = build_opportunity(premium(), 1e9, cfg, hold_days=30.0)
    assert opp is not None
    assert opp.gross_annual_pct == pytest.approx(0.0001 * 3 * 365 * 100)  # 10.95%
    expected_net = opp.gross_annual_pct - 0.36 * 365 / 30.0               # 10.95 - 4.38
    assert opp.net_annual_pct == pytest.approx(expected_net)
    assert opp.net_annual_pct == pytest.approx(6.57, abs=0.01)


def test_shorter_hold_period_lowers_net_yield():
    cfg = make_cfg()
    long_hold = build_opportunity(premium(), 1e9, cfg, hold_days=30.0)
    short_hold = build_opportunity(premium(), 1e9, cfg, hold_days=3.0)
    assert long_hold is not None and short_hold is not None
    assert short_hold.net_annual_pct < long_hold.net_annual_pct
    # 持有 3 天时成本摊销 43.8%，毛收益 10.95% 被吃成负数
    assert short_hold.net_annual_pct < 0


def test_basis_and_direction():
    cfg = make_cfg()
    opp = build_opportunity(premium(mark="61000", index="60900"), 1e9, cfg, 30.0)
    assert opp is not None
    assert opp.basis_pct == pytest.approx((61000 - 60900) / 60900 * 100)
    assert opp.is_short_perp_direction is True

    neg = build_opportunity(premium(funding="-0.0002"), 1e9, cfg, 30.0)
    assert neg is not None
    assert neg.is_short_perp_direction is False


def test_invalid_prices_return_none():
    cfg = make_cfg()
    assert build_opportunity(premium(mark="0"), 1e9, cfg, 30.0) is None
    assert build_opportunity({"symbol": ""}, 1e9, cfg, 30.0) is None


# ---------- 扫描与过滤 ----------


def test_scan_filters_by_volume_and_quote_asset():
    cfg = make_cfg()
    premiums = [
        premium("BTCUSDT"),
        premium("DOGEUSDT"),                      # 成交额不足，应被过滤
        premium("BTCUSD_PERP", mark="61000"),     # 非 USDT 本位，应被过滤
    ]
    tickers = [
        {"symbol": "BTCUSDT", "quoteVolume": "900000000"},
        {"symbol": "DOGEUSDT", "quoteVolume": "1000"},
        {"symbol": "BTCUSD_PERP", "quoteVolume": "900000000"},
    ]
    out = scan_opportunities(premiums, tickers, cfg)
    assert [o.symbol for o in out] == ["BTCUSDT"]


def test_scan_sorts_by_net_annual_desc():
    cfg = make_cfg()
    premiums = [
        premium("AAAUSDT", funding="0.0001"),
        premium("BBBUSDT", funding="0.0005"),
        premium("CCCUSDT", funding="0.0003"),
    ]
    tickers = [
        {"symbol": "AAAUSDT", "quoteVolume": "1e9"},
        {"symbol": "BBBUSDT", "quoteVolume": "1e9"},
        {"symbol": "CCCUSDT", "quoteVolume": "1e9"},
    ]
    out = scan_opportunities(premiums, tickers, cfg)
    assert [o.symbol for o in out] == ["BBBUSDT", "CCCUSDT", "AAAUSDT"]


def test_format_table_handles_empty_and_nonempty():
    assert "没有通过" in format_opportunity_table([])
    cfg = make_cfg()
    out = scan_opportunities([premium()], [{"symbol": "BTCUSDT", "quoteVolume": "1e9"}], cfg)
    table = format_opportunity_table(out)
    assert "BTCUSDT" in table and "净年化" in table


# ---------- 机会类规则 ----------


def test_funding_opportunity_triggers_above_threshold():
    cfg = make_cfg()
    opp = build_opportunity(premium(funding="0.001"), 1e9, cfg, 30.0)
    assert opp is not None
    results = evaluate_opportunity_rules(cfg, [opp])
    funding = [r for r in results if r.rule_id == "funding_opportunity"][0]
    assert funding.triggered
    assert funding.dedup_key == "BTCUSDT"
    assert funding.severity is Severity.INFO


def test_funding_opportunity_not_triggered_below_threshold():
    cfg = make_cfg()
    # 资金费 0.0001 -> 净年化 6.57%，低于阈值 10%
    opp = build_opportunity(premium(), 1e9, cfg, 30.0)
    assert opp is not None
    results = evaluate_opportunity_rules(cfg, [opp])
    funding = [r for r in results if r.rule_id == "funding_opportunity"][0]
    assert not funding.triggered


def test_info_severity_always_goes_to_digest(store):
    """机会类 INFO 不应占用即时通道，即使是非静默期的白天。"""
    cfg = make_cfg()
    cfg.rules = {"funding_opportunity": RuleConfig("funding_opportunity", confirmations=1)}
    engine = RuleEngine(cfg, store, SGT)
    opp = build_opportunity(premium(funding="0.001"), 1e9, cfg, 30.0)
    assert opp is not None

    decisions = engine.process(evaluate_opportunity_rules(cfg, [opp]), NOW)
    funding = [d for d in decisions if d.alert.rule_id == "funding_opportunity"]
    assert funding and funding[0].immediate is False


def test_basis_wide_triggers_on_deviation():
    cfg = make_cfg()
    opp = build_opportunity(premium(mark="61000", index="60500"), 1e9, cfg, 30.0)
    assert opp is not None
    results = evaluate_opportunity_rules(cfg, [opp])
    basis = [r for r in results if r.rule_id == "basis_wide"][0]
    assert basis.triggered
    assert basis.severity is Severity.WARN


def test_max_alerts_per_scan_caps_evaluation():
    cfg = make_cfg(max_alerts_per_scan=3)
    opportunities = []
    for i in range(20):
        opp = build_opportunity(premium(f"SYM{i}USDT"), 1e9, cfg, 30.0)
        assert opp is not None
        opportunities.append(opp)
    results = evaluate_opportunity_rules(cfg, opportunities)
    # 3 个资金费 + 3 个基差
    assert len(results) == 6


def test_opportunity_rules_respect_disable_switch(store):
    cfg = make_cfg()
    cfg.rules = {
        "funding_opportunity": RuleConfig("funding_opportunity", enabled=False, confirmations=1),
        "basis_wide": RuleConfig("basis_wide", enabled=False, confirmations=1),
    }
    engine = RuleEngine(cfg, store, SGT)
    opp = build_opportunity(premium(funding="0.001", mark="61000", index="60500"), 1e9, cfg, 30.0)
    assert opp is not None
    assert engine.process(evaluate_opportunity_rules(cfg, [opp]), NOW) == []
