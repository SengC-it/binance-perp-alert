"""跨交易所资金费扫描测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Config, RuleConfig
from src.cross_exchange import (
    FundingQuote,
    binance_quotes,
    build_cross_opportunities,
    cross_leg_cost_pct,
    format_cross_table,
    normalize_base,
    parse_bybit_tickers,
    parse_okx_funding,
    parse_okx_tickers,
)
from src.models import Severity
from src.rules import RuleEngine, evaluate_cross_exchange_rules
from src.store import Store

SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)


def make_cfg(**thresholds) -> Config:
    cfg = Config()
    cfg.thresholds = {
        "assumed_hold_days": 30.0,
        "min_volume_usdt_24h": 50_000_000.0,
        "perp_taker_fee_pct": 0.05,
        "slippage_pct": 0.03,
        "max_alerts_per_scan": 10,
        "cross_min_net_annual_pct": 15.0,
    }
    cfg.thresholds.update(thresholds)
    return cfg


def quote(exchange, symbol, base, rate, volume=1e9) -> FundingQuote:
    return FundingQuote(exchange, symbol, base, rate, volume)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "cross.db")
    yield s
    s.close()


# ---------- 符号归一化 ----------


def test_normalize_base_per_exchange():
    assert normalize_base("BTCUSDT", "binance") == "BTC"
    assert normalize_base("BTCUSDT", "bybit") == "BTC"
    assert normalize_base("BTC-USDT-SWAP", "okx") == "BTC"
    assert normalize_base("1000PEPEUSDT", "binance") == "1000PEPE"


def test_normalize_base_rejects_non_usdt_and_non_swap():
    assert normalize_base("BTCUSDC", "binance") is None
    assert normalize_base("BTCUSD_PERP", "binance") is None
    assert normalize_base("BTC-USDT", "okx") is None
    assert normalize_base("BTC-USD-SWAP", "okx") is None
    assert normalize_base("", "binance") is None


# ---------- 解析 ----------


def test_parse_bybit_tickers_filters_and_maps():
    payload = {
        "retCode": 0,
        "result": {
            "list": [
                {"symbol": "BTCUSDT", "fundingRate": "0.0001", "turnover24h": "900000000"},
                {"symbol": "BTCUSDC", "fundingRate": "0.0002", "turnover24h": "1000"},
            ]
        },
    }
    quotes = parse_bybit_tickers(payload)
    assert len(quotes) == 1
    assert quotes[0].exchange == "bybit"
    assert quotes[0].base == "BTC"
    assert quotes[0].funding_rate == pytest.approx(0.0001)


def test_parse_bybit_tickers_raises_on_error_code():
    from src.cross_exchange import ExchangeError

    with pytest.raises(ExchangeError):
        parse_bybit_tickers({"retCode": 10001, "retMsg": "bad"})


def test_parse_okx_tickers_takes_top_by_notional():
    payload = {
        "data": [
            {"instId": "BTC-USDT-SWAP", "last": "61000", "volCcy24h": "1000"},
            {"instId": "ETH-USDT-SWAP", "last": "3050", "volCcy24h": "5000"},
            # 非 USDT 本位，应被排除，哪怕成交额最大
            {"instId": "BTC-USD-SWAP", "last": "61000", "volCcy24h": "999999"},
        ]
    }
    # BTC 1000 x 61000 = 6100 万；ETH 5000 x 3050 = 1525 万
    rows = parse_okx_tickers(payload, max_symbols=2)
    assert rows == [
        ("BTC-USDT-SWAP", 1000 * 61000.0),
        ("ETH-USDT-SWAP", 5000 * 3050.0),
    ]
    assert len(parse_okx_tickers(payload, max_symbols=1)) == 1
    assert parse_okx_tickers(payload, max_symbols=1)[0][0] == "BTC-USDT-SWAP"


def test_parse_okx_funding():
    payload = {"data": [{"instId": "SOL-USDT-SWAP", "fundingRate": "-0.0003"}]}
    q = parse_okx_funding(payload, 12345.0)
    assert q is not None
    assert q.exchange == "okx"
    assert q.base == "SOL"
    assert q.funding_rate == pytest.approx(-0.0003)
    assert q.quote_volume_24h == 12345.0
    assert parse_okx_funding({"data": []}, 0.0) is None


def test_binance_quotes_merges_volume():
    premium = [
        {"symbol": "BTCUSDT", "lastFundingRate": "0.0001"},
        {"symbol": "BTCUSD_PERP", "lastFundingRate": "0.0009"},
    ]
    tickers = [{"symbol": "BTCUSDT", "quoteVolume": "900000000"}]
    quotes = binance_quotes(premium, tickers)
    assert len(quotes) == 1
    assert quotes[0].exchange == "binance"
    assert quotes[0].quote_volume_24h == pytest.approx(9e8)


# ---------- 成本与匹配 ----------


def test_cross_leg_cost_is_four_fills():
    # 4 笔成交 x (0.05% taker + 0.03% 滑点) = 0.32%
    assert cross_leg_cost_pct(make_cfg()) == pytest.approx(0.32)


def test_build_cross_opportunities_picks_direction_and_net():
    cfg = make_cfg()
    quotes = [
        quote("binance", "BTCUSDT", "BTC", 0.0001),
        quote("bybit", "BTCUSDT", "BTC", 0.0005),
    ]
    opps = build_cross_opportunities(quotes, cfg)
    assert len(opps) == 1
    opp = opps[0]
    # 费率高的做空，费率低的做多
    assert opp.high.exchange == "bybit"
    assert opp.low.exchange == "binance"
    assert opp.spread_rate == pytest.approx(0.0004)
    assert opp.gross_annual_pct == pytest.approx(0.0004 * 3 * 365 * 100)  # 43.8%
    expected_net = opp.gross_annual_pct - 0.32 * 365 / 30.0
    assert opp.net_annual_pct == pytest.approx(expected_net)


def test_build_cross_requires_two_exchanges():
    cfg = make_cfg()
    quotes = [
        quote("binance", "BTCUSDT", "BTC", 0.0001),
        quote("binance", "ETHUSDT", "ETH", 0.0009),
    ]
    assert build_cross_opportunities(quotes, cfg) == []


def test_build_cross_filters_low_volume_leg():
    cfg = make_cfg()
    quotes = [
        quote("binance", "BTCUSDT", "BTC", 0.0001, volume=1e9),
        quote("bybit", "BTCUSDT", "BTC", 0.0005, volume=1000),   # 流动性不足
    ]
    assert build_cross_opportunities(quotes, cfg) == []


def test_build_cross_sorts_by_net_desc():
    cfg = make_cfg()
    quotes = [
        quote("binance", "AAAUSDT", "AAA", 0.0001),
        quote("bybit", "AAAUSDT", "AAA", 0.0002),
        quote("binance", "BBBUSDT", "BBB", 0.0001),
        quote("bybit", "BBBUSDT", "BBB", 0.0009),
    ]
    opps = build_cross_opportunities(quotes, cfg)
    assert [o.base for o in opps] == ["BBB", "AAA"]


def test_format_cross_table_handles_empty_and_cjk():
    assert "没有满足" in format_cross_table([])
    cfg = make_cfg()
    opps = build_cross_opportunities(
        [quote("binance", "龙虾USDT", "龙虾", 0.0001),
         quote("bybit", "龙虾USDT", "龙虾", 0.0009)],
        cfg,
    )
    table = format_cross_table(opps)
    assert "龙虾" in table
    lines = table.splitlines()
    # 中文名占两列，表头与数据行的显示宽度必须一致
    from src.opportunity_engine import display_width

    assert display_width(lines[0]) == display_width(lines[2])


# ---------- 规则 ----------


def test_cross_rule_triggers_above_threshold():
    cfg = make_cfg()
    opps = build_cross_opportunities(
        [quote("binance", "BTCUSDT", "BTC", 0.0001),
         quote("bybit", "BTCUSDT", "BTC", 0.0009)],
        cfg,
    )
    results = evaluate_cross_exchange_rules(cfg, opps)
    assert results[0].triggered
    assert results[0].severity is Severity.INFO
    assert results[0].dedup_key == "bybit:binance:BTC"


def test_cross_rule_not_triggered_below_threshold():
    cfg = make_cfg()
    opps = build_cross_opportunities(
        [quote("binance", "BTCUSDT", "BTC", 0.0001),
         quote("bybit", "BTCUSDT", "BTC", 0.00011)],
        cfg,
    )
    results = evaluate_cross_exchange_rules(cfg, opps)
    assert not results[0].triggered


def test_cross_info_goes_to_digest(store):
    cfg = make_cfg()
    cfg.rules = {
        "cross_exchange_funding": RuleConfig("cross_exchange_funding", confirmations=1)
    }
    engine = RuleEngine(cfg, store, SGT)
    opps = build_cross_opportunities(
        [quote("binance", "BTCUSDT", "BTC", 0.0001),
         quote("bybit", "BTCUSDT", "BTC", 0.0009)],
        cfg,
    )
    decisions = engine.process(evaluate_cross_exchange_rules(cfg, opps), NOW)
    assert decisions and all(not d.immediate for d in decisions)


def test_cross_cap_respected():
    cfg = make_cfg(max_alerts_per_scan=2)
    quotes = []
    for i in range(10):
        quotes.append(quote("binance", f"S{i}USDT", f"S{i}", 0.0001))
        quotes.append(quote("bybit", f"S{i}USDT", f"S{i}", 0.0009))
    opps = build_cross_opportunities(quotes, cfg)
    assert len(opps) == 10
    assert len(evaluate_cross_exchange_rules(cfg, opps)) == 2
