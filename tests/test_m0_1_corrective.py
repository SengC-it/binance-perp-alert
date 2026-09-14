"""M0.1 corrective pass 的 provenance、coverage、resize 与 parity 测试。"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backtest.cross_sectional import (
    XsParams,
    dataset_fingerprint,
    generate_evidence,
    simulate_xs,
)
from backtest.funding_arb import build_series
from src.binance_client import (
    BinanceError,
    BinanceFuturesClient,
    validate_funding_coverage,
)
from src.config import Config
from src.directional_engine import (
    build_directional_scan,
    evidence_block,
    load_evidence,
)
from src.engine import AlertService
from src.forward_check import render_reconciliation
from src.store import Store
from src.weekly_paper import (
    PaperDataError,
    WeeklyPaperPortfolioLedger,
    execution_day,
    simulate_weekly_paper,
)
from src.xs_lowvol_spec import (
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
    SHADOW_SPEC_HASH,
    SHADOW_STRATEGY_ID,
)


UTC = timezone.utc
START = date(2025, 1, 1)
EIGHT_HOURS = 8 * 3_600_000
FOUR_HOURS = 4 * 3_600_000


def _ms(day: date, hour: int = 0) -> int:
    return int(datetime.combine(day, time(hour), tzinfo=UTC).timestamp() * 1000)


def _candle(day: date, close: float, quote_volume: float = 60_000_000.0) -> list[object]:
    return [
        _ms(day),
        str(close),
        str(close),
        str(close),
        str(close),
        "1",
        _ms(day, 23) + 3_599_999,
        str(quote_volume),
    ]


class _Response:
    def __init__(self, payload, status_code: int = 200, text: str = ""):
        self.payload = payload
        self.status_code = status_code
        self.text = text or json.dumps(payload)

    def json(self):
        return self.payload


class _FundingSession:
    def __init__(self, events: list[dict], *, duplicate_boundary: bool = False):
        self.events = events
        self.duplicate_boundary = duplicate_boundary
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}

    def get(self, url, *, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append(params)
        start = int(params["startTime"])
        end = int(params["endTime"])
        limit = int(params["limit"])
        if self.duplicate_boundary and len(self.calls) == 2:
            previous = [item for item in self.events if item["fundingTime"] < start][-1:]
            next_page = [item for item in self.events if start <= item["fundingTime"] <= end]
            return _Response(previous + next_page[: max(0, limit - len(previous))])
        page = [item for item in self.events if start <= item["fundingTime"] <= end]
        return _Response(page[:limit])


def _funding_events(count: int, interval_ms: int = EIGHT_HOURS) -> list[dict]:
    return [
        {
            "symbol": "S0USDT",
            "fundingTime": index * interval_ms,
            "fundingRate": "0.0001",
            "markPrice": "100",
        }
        for index in range(count)
    ]


def test_funding_history_paginates_with_explicit_window_and_dedupes_boundary():
    events = _funding_events(250)
    session = _FundingSession(events, duplicate_boundary=True)
    client = BinanceFuturesClient(session=session)
    result = client.funding_history(
        "S0USDT",
        events[0]["fundingTime"],
        limit=200,
        end_ms=events[-1]["fundingTime"],
    )
    assert [event["fundingTime"] for event in result] == [
        event["fundingTime"] for event in events
    ]
    assert len(result) == 250
    assert len(session.calls) == 2
    assert all(call["startTime"] >= events[0]["fundingTime"] for call in session.calls)
    assert all(call["endTime"] == events[-1]["fundingTime"] for call in session.calls)
    assert all(1 <= call["limit"] <= 1000 for call in session.calls)
    assert session.calls[1]["startTime"] == events[199]["fundingTime"] + 1


def test_funding_history_supports_four_hour_schedule_and_coverage_check():
    events = _funding_events(12, FOUR_HOURS)
    session = _FundingSession(events)
    client = BinanceFuturesClient(session=session)
    result = client.funding_history(
        "S0USDT",
        events[0]["fundingTime"],
        end_ms=events[-1]["fundingTime"],
        expected_interval_hours=4,
    )
    assert len(result) == 12
    assert [event["fundingTime"] for event in result] == sorted(
        event["fundingTime"] for event in events
    )


def test_funding_history_fails_closed_on_api_error():
    class FailingSession:
        headers: dict[str, str] = {}

        def get(self, url, *, params=None, timeout=None):
            return _Response({"code": -1003, "msg": "rate limit"}, 429, "rate limit")

    client = BinanceFuturesClient(session=FailingSession())
    with pytest.raises(BinanceError, match="HTTP 429"):
        client.funding_history("S0USDT", 0, end_ms=EIGHT_HOURS)


def test_funding_history_fails_closed_on_gap_and_page_limit():
    events = _funding_events(3, FOUR_HOURS)
    gap = [events[0], events[2]]
    with pytest.raises(BinanceError, match="coverage gap"):
        validate_funding_coverage(
            gap,
            events[0]["fundingTime"],
            events[2]["fundingTime"],
            expected_interval_hours=4,
        )

    session = _FundingSession(events)
    client = BinanceFuturesClient(session=session)
    with pytest.raises(BinanceError, match="最大分页数"):
        client.funding_history(
            "S0USDT",
            events[0]["fundingTime"],
            limit=1,
            max_pages=2,
            end_ms=events[-1]["fundingTime"],
        )


def test_funding_history_fails_closed_on_non_progress_and_empty_coverage():
    event = _funding_events(1)[0]

    class RepeatingSession:
        headers: dict[str, str] = {}

        def get(self, url, *, params=None, timeout=None):
            return _Response([event])

    client = BinanceFuturesClient(session=RepeatingSession())
    with pytest.raises(BinanceError, match="没有前进"):
        client.funding_history("S0USDT", 0, limit=1, max_pages=3, end_ms=EIGHT_HOURS)

    with pytest.raises(BinanceError, match="为空"):
        validate_funding_coverage([], 0, EIGHT_HOURS)


def _valid_evidence(**overrides) -> dict:
    dataset_sha = "a" * 64
    commit_sha = "b" * 40
    generated_at = "2026-09-15T00:00:00+00:00"
    result = {
        "schema_version": 2,
        "evidence_schema_version": 2,
        "artifact_type": "xs_lowvol_evidence",
        "generator": "backtest.cross_sectional.generate_evidence",
        "strategy": "xs_lowvol",
        "strategy_id": CONTROL_STRATEGY_ID,
        "variant": "Control",
        "spec_hash": CONTROL_SPEC_HASH,
        "status": "CURRENT",
        "dataset_id": "fixture-v1",
        "dataset_sha256": dataset_sha,
        "dataset_fingerprint": {"algorithm": "sha256", "sha256": dataset_sha},
        "code_commit_sha": commit_sha,
        "generated_at": generated_at,
        "provenance": {
            "dataset_id": "fixture-v1",
            "dataset_sha256": dataset_sha,
            "code_commit_sha": commit_sha,
            "generated_at": generated_at,
            "generator": "backtest.cross_sectional.generate_evidence",
        },
        "sharpe": 2.36,
        "sharpe_ci90": [0.76, 4.05],
    }
    result.update(overrides)
    return result


def test_evidence_current_requires_schema_and_provenance(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(_valid_evidence()), encoding="utf-8")
    evidence = load_evidence(path)
    assert evidence["status"] == "CURRENT"
    assert evidence["sharpe_ci90"] == (0.76, 4.05)

    legacy = _valid_evidence()
    for key in (
        "schema_version",
        "evidence_schema_version",
        "artifact_type",
        "generator",
        "dataset_sha256",
        "dataset_fingerprint",
        "code_commit_sha",
        "provenance",
    ):
        legacy.pop(key)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    stale = load_evidence(path)
    assert stale["status"] == "EVIDENCE_STALE"
    assert "sharpe" not in stale
    assert "2.36" not in evidence_block(stale)


def test_evidence_generator_emits_metadata_only_stale_without_local_dataset(tmp_path):
    artifact = generate_evidence([], code_commit_sha="c" * 40)
    assert artifact["status"] == "EVIDENCE_STALE"
    assert artifact["strategy_id"] == CONTROL_STRATEGY_ID
    assert artifact["spec_hash"] == CONTROL_SPEC_HASH
    assert len(artifact["dataset_sha256"]) == 64
    assert artifact["dataset_fingerprint"]["sha256"] == artifact["dataset_sha256"]
    assert artifact["code_commit_sha"] == "c" * 40
    assert artifact["generated_at"]
    assert "sharpe" not in artifact
    assert dataset_fingerprint([]) == artifact["dataset_sha256"]


def _scanner_with_low_volume_new_symbol():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    days = [now.date() - timedelta(days=i) for i in range(31, 0, -1)]
    rows = {
        f"S{i}USDT": [_candle(day, 100.0 + (i + 1) * (j % 4)) for j, day in enumerate(days)]
        for i in range(10)
    }
    rows["NEWUSDT"] = [
        _candle(now.date() - timedelta(days=i), 90.0 + i, 1_000_000.0)
        for i in range(5, 0, -1)
    ]
    tickers = [
        {"symbol": symbol, "quoteVolume": "60000000"}
        for symbol in rows
        if symbol != "NEWUSDT"
    ]
    tickers.append({"symbol": "NEWUSDT", "quoteVolume": "1000000"})

    def loader(symbol: str, interval: str, limit: int):
        assert interval == "1d"
        return rows[symbol]

    return now, tickers, loader, set(rows)


def test_scanner_excludes_low_volume_new_history_before_history_gate():
    now, tickers, loader, valid = _scanner_with_low_volume_new_symbol()
    result = build_directional_scan(tickers, loader, now, valid_symbols=valid)
    assert result.signal is not None
    assert result.active_count == 11
    assert result.eligible_count == 10
    assert result.candidate_count == 10
    assert "NEWUSDT" not in {
        value.symbol for value in result.signal.longs + result.signal.shorts
    }


def test_scanner_and_backtest_share_signal_day_target_fixture():
    markets = _parity_markets(days=50)
    now = datetime.combine(START + timedelta(days=49), time(12), tzinfo=UTC)
    rows = {
        market.symbol: [_candle(day, market.perp_close[day]) for day in market.dates]
        for market in markets
    }
    tickers = [
        {"symbol": market.symbol, "quoteVolume": "60000000"}
        for market in markets
    ]
    scan = build_directional_scan(
        tickers,
        lambda symbol, interval, limit: rows[symbol],
        now,
        valid_symbols={market.symbol for market in markets},
    )
    backtest = simulate_xs(markets, "xs_lowvol", XsParams())
    assert scan.signal is not None
    assert scan.signal.as_of == (START + timedelta(days=48)).isoformat()
    scanner_targets = {
        **{item.symbol: 1 for item in scan.signal.longs},
        **{item.symbol: -1 for item in scan.signal.shorts},
    }
    assert scanner_targets == backtest.targets_by_signal_day[max(backtest.targets_by_signal_day)]


def _parity_markets(days: int = 100, count: int = 10):
    markets = []
    for index in range(count):
        perp: list[list[object]] = []
        spot: list[list[object]] = []
        funding: list[list[object]] = []
        price = 100.0
        for offset in range(days):
            day = START + timedelta(days=offset)
            amplitude = 0.001 * (index + 1) if offset < 55 else 0.003 * (index + 1)
            move = amplitude if (offset + index) % 2 else -amplitude
            price *= 1.0 + move
            perp.append([day.isoformat(), price, 60_000_000.0])
            spot.append([day.isoformat(), price])
            funding.append([_ms(day, 12), 0.0001, 24.0])
        markets.append(
            build_series(
                {
                    "symbol": f"S{index}USDT",
                    "perp": perp,
                    "spot": spot,
                    "funding": funding,
                }
            )
        )
    return markets


def _paper_inputs(markets):
    prices: dict[date, dict[str, float]] = {}
    funding: dict[str, list[dict[str, object]]] = {}
    for market in markets:
        for day, price in market.perp_close.items():
            prices.setdefault(day, {})[market.symbol] = price
        funding[market.symbol] = [
            {"fundingTime": timestamp, "fundingRate": rate}
            for timestamp, rate in zip(market.funding_ts, market.funding_rate)
        ]
    return prices, funding


def test_backtest_and_weekly_paper_match_control_and_shadow_economics():
    markets = _parity_markets()
    prices, funding = _paper_inputs(markets)
    for variant in ("control", "shadow"):
        params = XsParams.for_variant(variant)
        backtest = simulate_xs(markets, "xs_lowvol", params)
        execution_targets = {
            execution_day(day, params.exec_lag_days): targets
            for day, targets in backtest.targets_by_signal_day.items()
        }
        execution_scales = {
            execution_day(day, params.exec_lag_days): scale
            for day, scale in backtest.scales_by_signal_day.items()
        }
        paper = simulate_weekly_paper(
            prices,
            execution_targets,
            funding,
            strategy_id=params.strategy_id,
            spec_hash=params.spec_hash,
            scales_by_day=execution_scales,
        )
        assert backtest.complete is True
        assert [row.day for row in paper.nav] == backtest.dates
        assert [row.nav for row in paper.nav] == pytest.approx(backtest.equity)
        expected_daily = [
            backtest.equity[0] - params.capital,
            *(
                current - previous
                for previous, current in zip(backtest.equity, backtest.equity[1:])
            ),
        ]
        assert [row.daily_pnl for row in paper.nav] == pytest.approx(expected_daily)
        assert paper.funding_pnl == pytest.approx(backtest.funding_pnl)
        assert paper.cost_pnl == pytest.approx(-backtest.cost_pnl)
        assert paper.turnover_notional == pytest.approx(backtest.turnover_notional)
        assert paper.final_nav == pytest.approx(backtest.final_equity)
        assert paper.resized_symbols == tuple(sorted(backtest.resized_symbols))
        assert paper.targets_by_day == execution_targets
        assert paper.scales_by_day == execution_scales


def test_backtest_no_signal_does_not_consume_rebalance_clock():
    markets = _parity_markets(days=50, count=11)
    failed_execution = START + timedelta(days=31)
    recovered_signal = START + timedelta(days=31)
    failed_market = markets[0]
    missing_price = failed_market.perp_close.pop(failed_execution)
    failed_market.perp_volume[failed_execution] = 0.0

    result = simulate_xs(markets, "xs_lowvol", XsParams())
    assert result.rebalance_audits
    assert result.rebalance_audits[0].execution_day == START + timedelta(days=32)
    assert any("missing execution prices" in reason for reason in result.no_signal_reasons)
    assert failed_execution not in [audit.execution_day for audit in result.rebalance_audits]
    assert result.rebalance_audits[0].signal_day == recovered_signal
    failed_market.perp_close[failed_execution] = missing_price


def test_shadow_resize_tracks_identity_turnover_and_partial_costs():
    ledger = WeeklyPaperPortfolioLedger(
        capital=1_000.0,
        strategy_id=SHADOW_STRATEGY_ID,
        spec_hash=SHADOW_SPEC_HASH,
    )
    day0 = date(2026, 1, 1)
    prices = {day0 + timedelta(days=7 * i): {"L": 100.0} for i in range(3)}
    first = ledger.rebalance(day0, {"L": 1}, prices[day0], scale=1.0)
    down = ledger.rebalance(day0 + timedelta(days=7), {"L": 1}, prices[day0 + timedelta(days=7)], scale=0.5)
    up = ledger.rebalance(day0 + timedelta(days=14), {"L": 1}, prices[day0 + timedelta(days=14)], scale=2.0)
    assert first.changed_symbols == ("L",)
    assert down.changed_symbols == ()
    assert down.resized_symbols == ("L",)
    assert down.turnover_notional == pytest.approx(50.0)
    assert up.changed_symbols == ()
    assert up.resized_symbols == ("L",)
    assert up.turnover_notional == pytest.approx(150.0)
    result = ledger.result()
    assert result.trade_count == 1
    assert result.turnover_notional == pytest.approx(300.0)
    assert result.cost_pnl == pytest.approx(-0.24)
    assert result.resized_symbols == ("L",)


def test_shadow_same_side_same_scale_is_zero_turnover_and_flip_is_close_open():
    ledger = WeeklyPaperPortfolioLedger(
        capital=1_000.0,
        strategy_id=SHADOW_STRATEGY_ID,
        spec_hash=SHADOW_SPEC_HASH,
    )
    day0 = date(2026, 2, 1)
    ledger.rebalance(day0, {"L": 1}, {"L": 100.0}, scale=1.0)
    unchanged = ledger.rebalance(day0 + timedelta(days=7), {"L": 1}, {"L": 100.0}, scale=1.0)
    flipped = ledger.rebalance(day0 + timedelta(days=14), {"L": -1}, {"L": 100.0}, scale=0.5)
    assert unchanged.changed_symbols == ()
    assert unchanged.resized_symbols == ()
    assert unchanged.turnover_notional == 0.0
    assert flipped.changed_symbols == ("L",)
    assert flipped.resized_symbols == ()
    assert flipped.turnover_notional == pytest.approx(150.0)
    assert ledger.positions == {"L": -1}


def test_control_rejects_non_frozen_scale():
    ledger = WeeklyPaperPortfolioLedger(
        capital=1_000.0,
        strategy_id=CONTROL_STRATEGY_ID,
        spec_hash=CONTROL_SPEC_HASH,
    )
    with pytest.raises(PaperDataError, match="仓位缩放"):
        ledger.rebalance(date(2026, 3, 1), {"L": 1}, {"L": 100.0}, scale=2.0)


def test_alert_service_records_independent_control_and_shadow_paper(tmp_path):
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    days = [now.date() - timedelta(days=i) for i in range(31, 0, -1)]
    symbols = [f"S{i}USDT" for i in range(10)]
    rows = {
        symbol: [_candle(day, 100.0 + (index + 1) * (offset % 4)) for offset, day in enumerate(days)]
        for index, symbol in enumerate(symbols)
    }

    class Client:
        def ticker_24hr(self):
            return [{"symbol": symbol, "quoteVolume": "60000000"} for symbol in symbols]

        def exchange_info(self):
            return {
                "symbols": [
                    {
                        "symbol": symbol,
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                    }
                    for symbol in symbols
                ]
            }

        def klines(self, symbol, interval="1d", limit=60):
            return rows[symbol]

    store = Store(tmp_path / "state.db")
    try:
        service = AlertService(
            Config(dry_run=True),
            UTC,
            store,
            Client(),
            MagicMock(),
        )
        signal = service.scan_directional(now)
        assert signal is not None
        trades = store.paper_trades()
        assert {row["strategy_id"] for row in trades} == {
            CONTROL_STRATEGY_ID,
            SHADOW_STRATEGY_ID,
        }
        assert len(trades) == 2
        assert json.loads(trades[0]["longs_json"]) == json.loads(trades[1]["longs_json"])
        assert json.loads(trades[0]["shorts_json"]) == json.loads(trades[1]["shorts_json"])
        rebalances = store.paper_rebalances()
        assert len(rebalances) == 2
        assert json.loads(rebalances[0]["targets_json"]) == json.loads(rebalances[1]["targets_json"])
        by_strategy = {row["strategy_id"]: row for row in rebalances}
        assert by_strategy[CONTROL_STRATEGY_ID]["scale"] == pytest.approx(1.0)
        assert by_strategy[SHADOW_STRATEGY_ID]["scale"] == pytest.approx(
            max(0.1, min(3.0, 80.0 / (sum(signal.universe_vols) / len(signal.universe_vols))))
        )
        report = render_reconciliation(store)
        assert "Control 记录" in report
        assert "Shadow 记录" in report
    finally:
        store.close()
