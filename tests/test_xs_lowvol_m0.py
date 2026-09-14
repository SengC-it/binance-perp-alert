"""XS-LOWVOL-V1 M0 冻结规格、parity 和周度 PAPER fixture。"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backtest.cross_sectional import XsParams, block_bootstrap, simulate_xs
from backtest.funding_arb import build_series
from src.config import Config, LIVE_TRADING
from src.directional_engine import (
    SymbolVol,
    build_directional_scan,
    build_signal,
    evidence_block,
    load_evidence,
    parse_completed_klines,
)
from src.scope_guard import scan
from src.store import Store
from src.forward_check import record_signal, verify_pending
from src.weekly_paper import (
    PaperDataError,
    WeeklyPaperPortfolioLedger,
    execution_day,
    scale_for_strategy,
    simulate_weekly_paper,
)
from src.xs_lowvol_spec import (
    CONTROL_RULES,
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
    SHADOW_RULES,
    SHADOW_SPEC_HASH,
    SHADOW_STRATEGY_ID,
    load_strategy_spec,
    spec_sha256,
    strategy_spec_hash,
)


def _ms(day: date, close: bool = False) -> int:
    base = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    return base + (86_400_000 - 1 if close else 0)


def _candle(day: date, close: float, *, future: bool = False) -> list[object]:
    return [_ms(day), str(close), str(close), str(close), str(close), "1", _ms(day, True), "60000000"]


def _scanner_fixture(count: int = 10, missing: str | None = None):
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    days = [now.date() - timedelta(days=i) for i in range(31, 0, -1)]
    rows = {
        f"S{i}USDT": [_candle(day, 100.0 + (i + 1) * (j % 4)) for j, day in enumerate(days)]
        for i in range(count)
    }
    rows["S0USDT"].append(_candle(now.date(), 120.0))
    tickers = [{"symbol": symbol, "quoteVolume": "60000000"} for symbol in rows]

    def loader(symbol: str, interval: str, limit: int):
        assert interval == "1d"
        return [] if symbol == missing else rows[symbol]

    return now, tickers, loader, set(rows)


def _market(symbol: str, amplitude: float):
    start = date(2025, 1, 1)
    perp: list[list[object]] = []
    spot: list[list[object]] = []
    funding: list[list[object]] = []
    price = 100.0
    for i in range(50):
        day = start + timedelta(days=i)
        price *= 1.0 + (amplitude if i % 2 else -amplitude)
        perp.append([day.isoformat(), price, 1_000_000_000.0])
        spot.append([day.isoformat(), price])
        funding.append([_ms(day), 0.0, 8.0])
    return build_series({"symbol": symbol, "perp": perp, "spot": spot, "funding": funding})


def test_spec_hash_is_stable_and_variants_are_distinct():
    assert strategy_spec_hash() == CONTROL_SPEC_HASH
    assert strategy_spec_hash() == strategy_spec_hash()
    assert strategy_spec_hash(variant="shadow") == SHADOW_SPEC_HASH
    assert CONTROL_SPEC_HASH != SHADOW_SPEC_HASH


def test_spec_parameter_change_changes_hash():
    changed = copy.deepcopy(load_strategy_spec())
    changed["ranking"]["k_long"] = 4
    assert spec_sha256(changed) != CONTROL_SPEC_HASH


def test_backtest_params_match_frozen_control():
    params = XsParams()
    assert params.lookback == CONTROL_RULES.lookback_days
    assert params.k_long == CONTROL_RULES.k_long
    assert params.k_short == CONTROL_RULES.k_short
    assert params.rebalance_days == CONTROL_RULES.rebalance_days
    assert params.min_volume_usdt_24h == CONTROL_RULES.min_quote_volume_usdt
    assert params.exec_lag_days == CONTROL_RULES.execution_lag_days
    assert params.max_scale == 1.0
    assert params.strategy_id == CONTROL_STRATEGY_ID
    assert params.spec_hash == CONTROL_SPEC_HASH


def test_backtest_control_keeps_same_direction_positions():
    markets = [_market(f"S{i}USDT", 0.001 * (i + 1)) for i in range(10)]
    result = simulate_xs(markets, "xs_lowvol", XsParams())
    assert result.complete is True
    assert result.trade_count == 10
    assert result.strategy_id == CONTROL_STRATEGY_ID
    assert result.spec_hash == CONTROL_SPEC_HASH


def test_weekly_ledger_rebalances_only_target_diff_and_keeps_pnl_by_leg():
    start = date(2026, 1, 1)
    days = [start + timedelta(days=i) for i in range(9)]
    prices = {
        day: {"L": 100.0 + i, "S": 100.0 - i, "X": 50.0 + i}
        for i, day in enumerate(days)
    }
    result = simulate_weekly_paper(
        prices,
        {
            start: {"L": 1, "S": -1},
            start + timedelta(days=7): {"L": 1, "X": -1},
        },
        {
            "L": [(start + timedelta(days=3), 0.001)],
            "S": [(start + timedelta(days=3), 0.001)],
            "X": [],
        },
    )
    rebalance = [row for row in result.nav if row.rebalance]
    assert len(rebalance) == 2
    assert set(rebalance[1].changed_symbols) == {"S", "X"}
    assert "L" not in rebalance[1].changed_symbols
    assert result.trade_count == 3
    assert result.long_pnl > 0
    assert result.short_pnl > 0
    assert result.funding_pnl == pytest.approx(0.0)
    assert result.cost_pnl == pytest.approx(-3.2)


def test_weekly_ledger_rejects_missing_mark_data():
    start = date(2026, 1, 1)
    ledger = WeeklyPaperPortfolioLedger()
    with pytest.raises(PaperDataError):
        ledger.run(
            [start, start + timedelta(days=1)],
            {start: {"L": 1}},
            {start: {"L": 100.0}},
        )


def test_funding_sign_and_fee_slippage_are_explicit():
    start = date(2026, 2, 1)
    prices = {
        start: {"L": 100.0, "S": 100.0},
        start + timedelta(days=1): {"L": 100.0, "S": 100.0},
    }
    result = simulate_weekly_paper(
        prices,
        {start: {"L": 1, "S": -1}},
        {"L": [(start + timedelta(days=1), 0.001)], "S": [(start + timedelta(days=1), 0.001)]},
    )
    assert result.long_pnl == pytest.approx(-1.8)
    assert result.short_pnl == pytest.approx(0.2)
    assert result.funding_pnl == pytest.approx(0.0)
    assert result.cost_pnl == pytest.approx(-1.6)


def test_vt80_shadow_scale_does_not_change_control():
    assert scale_for_strategy(CONTROL_STRATEGY_ID, [10.0, 20.0]) == 1.0
    assert scale_for_strategy(SHADOW_STRATEGY_ID, [10.0, 20.0]) == 3.0
    assert SHADOW_RULES.target_vol_pct == 80.0
    assert SHADOW_RULES.max_scale == 3.0
    with pytest.raises(ValueError):
        WeeklyPaperPortfolioLedger(
            strategy_id=CONTROL_STRATEGY_ID, spec_hash=SHADOW_SPEC_HASH
        )


def test_completed_candle_filter_and_execution_lag():
    now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    raw = [_candle(now.date() - timedelta(days=1), 100.0), _candle(now.date(), 101.0)]
    parsed = parse_completed_klines(raw, int(now.timestamp() * 1000))
    assert [item.day for item in parsed] == [now.date() - timedelta(days=1)]
    assert execution_day("2026-03-09") == date(2026, 3, 10)


def test_scanner_uses_complete_universe_and_no_future_candle():
    now, tickers, loader, valid = _scanner_fixture()
    result = build_directional_scan(tickers, loader, now, valid_symbols=valid)
    assert result.reason == "OK"
    assert result.signal is not None
    assert result.signal.as_of == "2026-09-13"
    assert result.signal.execution_date == "2026-09-14"
    assert result.signal.signal_timestamp is not None
    assert result.candidate_count == 10


def test_scanner_missing_history_is_no_signal():
    now, tickers, loader, valid = _scanner_fixture(missing="S3USDT")
    result = build_directional_scan(tickers, loader, now, valid_symbols=valid)
    assert result.signal is None
    assert "S3USDT" in result.missing_symbols
    assert result.reason.startswith("NO_SIGNAL")


def test_scanner_insufficient_universe_is_no_signal():
    now, tickers, loader, valid = _scanner_fixture(count=9)
    result = build_directional_scan(tickers, loader, now, valid_symbols=valid)
    assert result.signal is None
    assert "insufficient active universe" in result.reason


def test_stale_evidence_never_renders_old_statistics(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps({"strategy_id": CONTROL_STRATEGY_ID, "spec_hash": "old"}),
        encoding="utf-8",
    )
    evidence = load_evidence(path)
    text = evidence_block(evidence)
    assert evidence["status"] == "EVIDENCE_STALE"
    assert "EVIDENCE_STALE" in text
    assert "2.36" not in text


def test_block_bootstrap_exposes_required_metrics():
    returns = [0.001 if i % 3 else -0.0005 for i in range(56)]
    stats = block_bootstrap(returns, block_length=28, rounds=100, seed=7)
    assert stats["block_length"] == 28
    assert stats["rounds"] == 100
    assert len(stats["mean_return_ci"]) == 2
    assert len(stats["sharpe_ci"]) == 2
    assert 0.0 <= stats["probability_return_gt_zero"] <= 1.0


def test_paper_nav_and_rebalance_writes_are_idempotent(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        kwargs = dict(
            strategy_id=CONTROL_STRATEGY_ID,
            spec_hash=CONTROL_SPEC_HASH,
            day="2026-04-01",
            nav=10_000.0,
            daily_pnl=0.0,
            long_pnl=0.0,
            short_pnl=0.0,
            funding_pnl=0.0,
            cost_pnl=0.0,
            rebalance=True,
            changed_symbols=["L"],
            positions={"L": 1},
        )
        store.record_paper_nav(**kwargs)
        store.record_paper_nav(**kwargs)
        assert len(store.paper_nav(CONTROL_STRATEGY_ID)) == 1
        store.record_paper_rebalance(
            CONTROL_STRATEGY_ID, CONTROL_SPEC_HASH, "2026-04-01", "2026-03-31",
            "2026-04-01", {"L": 1}, [],
        )
        store.record_paper_rebalance(
            CONTROL_STRATEGY_ID, CONTROL_SPEC_HASH, "2026-04-01", "2026-03-31",
            "2026-04-01", {"L": 1}, ["L"],
        )
        row = store.paper_rebalances(CONTROL_STRATEGY_ID)[0]
        assert json.loads(row["changed_json"]) == ["L"]
    finally:
        store.close()


def test_forward_uses_weekly_ledger_when_completed_candles_are_available(tmp_path):
    start = date(2026, 1, 1)
    symbols = [SymbolVol(f"S{i}USDT", 10.0 + i, 100.0, 0.0, 0.0, 60_000_000.0, 30) for i in range(10)]
    first = build_signal(symbols, 5, 5, 30, start.isoformat())
    second = build_signal(symbols, 5, 5, 30, (start + timedelta(days=7)).isoformat())
    store = Store(tmp_path / "state.db")

    class CompleteClient:
        def exchange_info(self):
            return {"symbols": []}

        def klines(self, symbol, interval="1d", limit=1000):
            return [_candle(start + timedelta(days=i), 100.0 + i) for i in range(11)]

        def funding_history(self, symbol, start_ms, limit=200):
            return [{"fundingTime": start_ms, "fundingRate": "0"}]

    try:
        assert record_signal(store, first, Config()) is not None
        assert record_signal(store, second, Config()) is not None
        stats = verify_pending(store, Config(), CompleteClient(), today=start + timedelta(days=9))
        assert stats["verified"] == 1
        assert store.paper_trades()[0]["status"] == "verified"
        assert store.paper_trades()[0]["spec_hash"] == CONTROL_SPEC_HASH
        assert set(json.loads(store.paper_trades()[0]["entry_prices"]).values()) == {101.0}
        assert store.paper_nav(CONTROL_STRATEGY_ID)
    finally:
        store.close()


def test_scope_guard_still_passes():
    assert LIVE_TRADING is False
    assert scan().passed is True
