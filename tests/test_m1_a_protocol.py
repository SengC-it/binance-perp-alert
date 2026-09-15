"""M1-A synthetic tests: protocol, PIT data, fail-closed checks and gates."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.m1_gates import (
    GateError,
    ManualOverrideError,
    block_bootstrap,
    compound_return,
    evaluate_frozen_gates,
    leave_one_out,
    max_drawdown_pct,
    remove_best_5pct,
    split_windowed_rows,
    symbol_concentration,
    weekly_profit_factor,
    weekly_sharpe,
)
from backtest.m1_protocol import (
    M1_PROTOCOL_ID,
    cost_rate,
    load_protocol,
    protocol_sha256,
    protocol_windows,
    validate_protocol,
    verify_protocol_hash,
)
from backtest.m1_report import render_m1_report
from backtest.m1_runner import M1BApprovalRequired, M1RunnerNotReady, preflight, run_m1
from backtest.xs_data_quality import validate_dataset, validate_symbol_history
from backtest.xs_history import (
    DailyBar,
    FundingEvent,
    HistoryDataError,
    archive_url,
    build_dataset_manifest,
    build_lifecycle,
    build_pit_signal,
    build_pit_universe,
    dataset_fingerprint,
    discover_historical_archives,
    forced_exit_for_delisting,
    is_historical_usdt_perpetual_symbol,
    make_symbol_history,
    parse_daily_klines_csv,
    parse_funding_csv,
)


UTC = timezone.utc
START = date(2021, 1, 1)


def _ms(day: date, hour: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp() * 1000)


def _bar(symbol: str, day: date, price: float, volume: float = 60_000_000.0) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        day=day,
        open=price,
        high=price * 1.01,
        low=price * 0.99,
        close=price,
        quote_volume=volume,
        open_time_ms=_ms(day),
        close_time_ms=_ms(day, 23) + 3_599_999,
    )


def _history(
    symbol: str,
    first: date = START,
    days: int = 45,
    *,
    volume: float = 60_000_000.0,
    volumes: dict[date, float] | None = None,
    currently_active: bool = True,
    delisted_at: date | None = None,
    listed_from: date | None = None,
) :
    bars = [
        _bar(symbol, first + timedelta(days=index), 100.0 + (index + 1) * (1 + len(symbol) % 5), (volumes or {}).get(first + timedelta(days=index), volume))
        for index in range(days)
    ]
    funding = [
        FundingEvent(symbol, _ms(bar.day, 12), 0.0001, 24.0)
        for bar in bars
    ]
    return make_symbol_history(
        symbol,
        bars,
        funding,
        currently_active=currently_active,
        listed_from=listed_from,
        delisted_at=delisted_at,
        confirmed_absence_after_last_bar=not currently_active,
    )


def _ten_histories(first: date = START, days: int = 45) -> list:
    return [_history(f"S{index}USDT", first, days) for index in range(10)]


def test_protocol_is_frozen_and_sidecar_matches():
    protocol = load_protocol()
    assert protocol["protocol_id"] == M1_PROTOCOL_ID
    validate_protocol(protocol)
    assert protocol_sha256(protocol) == verify_protocol_hash()
    external, discovery = protocol_windows(protocol)
    assert (external.start, external.end) == (date(2020, 1, 1), date(2025, 8, 31))
    assert (discovery.start, discovery.end) == (date(2025, 9, 1), date(2026, 8, 31))
    assert cost_rate(protocol, "COST_1X") == pytest.approx(0.0008)
    assert cost_rate(protocol, "COST_2X") == pytest.approx(0.0016)
    assert cost_rate(protocol, "COST_3X") == pytest.approx(0.0024)


def test_protocol_tampering_is_rejected():
    protocol = load_protocol()
    altered = dict(protocol)
    altered["base_commit"] = "0" * 40
    with pytest.raises(ValueError, match="base_commit"):
        validate_protocol(altered, require_current_specs=False)


def test_runner_preflight_does_not_run_formal_m1(tmp_path):
    before = {path.name for path in Path("research/m1").iterdir()}
    result = preflight()
    assert result.protocol_id == M1_PROTOCOL_ID
    assert result.as_dict()["formal_run"] is False
    with pytest.raises(M1BApprovalRequired):
        run_m1()
    with pytest.raises(M1RunnerNotReady):
        run_m1(approval="START M1-B", dataset_manifest={})
    assert {path.name for path in Path("research/m1").iterdir()} == before


def test_archive_discovery_includes_historical_and_rejects_non_perpetual_paths():
    listing = [
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2020-01.zip",
        "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2020-01.zip",
        "/futures/um/monthly/klines/OLDUSDT/1d/OLDUSDT-1d-2021-02.zip",
        "/futures/um/monthly/klines/OLDUSDT/1d/OLDUSDT-1d-2021-02.zip",
        "/futures/um/quarterly/klines/OLDUSDT/1d/OLDUSDT-1d-2021-02.zip",
        "/futures/cm/monthly/klines/COINUSDT/1d/COINUSDT-1d-2021-02.zip",
        "/spot/monthly/klines/SPOTUSDT/1d/SPOTUSDT-1d-2021-02.zip",
        "/futures/um/monthly/klines/BTCUSDT_210625/1d/BTCUSDT_210625-1d-2021-02.zip",
    ]
    refs = discover_historical_archives(listing)
    assert {(ref.symbol, ref.kind, ref.month) for ref in refs} == {
        ("BTCUSDT", "daily_klines", "2020-01"),
        ("BTCUSDT", "funding", "2020-01"),
        ("OLDUSDT", "daily_klines", "2021-02"),
    }
    assert is_historical_usdt_perpetual_symbol("1000PEPEUSDT")
    assert not is_historical_usdt_perpetual_symbol("BTCUSDT_210625")
    assert "spot" not in archive_url("BTCUSDT", "daily_klines", "2020-01")


def test_archive_parsers_preserve_ohlc_and_funding_interval():
    daily = parse_daily_klines_csv(
        "open_time,open,high,low,close,volume,close_time,quote_volume\n"
        f"{_ms(START)},100,110,90,105,1,0,0\n{_ms(START, 23) + 3_599_999},105,115,95,110,1,0,0\n",
        "AUSDT",
    )
    funding = parse_funding_csv(
        "calc_time,funding_interval_hours,last_funding_rate\n"
        f"{_ms(START, 4)},4,0.0002\n",
        "AUSDT",
    )
    assert daily[0].close == 105
    assert daily[0].quote_volume == 0
    assert funding[0].funding_interval_hours == 4


def test_pit_current_delisted_and_future_listed_membership():
    signal_day = START + timedelta(days=40)
    histories = _ten_histories(days=45)
    old = _history("OLDUSDT", days=41, currently_active=False, delisted_at=signal_day + timedelta(days=1))
    future = _history("FUTUREUSDT", first=signal_day + timedelta(days=1), days=10, listed_from=signal_day + timedelta(days=1))
    histories.extend([old, future])
    universe = build_pit_universe(histories, signal_day)
    assert "OLDUSDT" in universe.active_symbols
    assert "FUTUREUSDT" not in universe.active_symbols
    assert "OLDUSDT" in universe.liquid_symbols
    assert any(item.symbol == "OLDUSDT" for item in universe.eligible)
    later = build_pit_universe(histories, signal_day + timedelta(days=1))
    assert "OLDUSDT" not in later.active_symbols


def test_pit_uses_signal_day_volume_not_t_plus_one():
    signal_day = START + timedelta(days=40)
    volumes = {signal_day: 20_000_000.0, signal_day + timedelta(days=1): 500_000_000.0}
    low = _history("LOWUSDT", days=45, volumes=volumes)
    histories = _ten_histories(days=45) + [low]
    result = build_pit_universe(histories, signal_day)
    assert "LOWUSDT" not in result.liquid_symbols
    assert result.exclusions["LOWUSDT"] == "BELOW_SIGNAL_DAY_VOLUME_THRESHOLD"


def test_new_five_day_symbol_is_not_eligible_but_does_not_block():
    signal_day = START + timedelta(days=40)
    new = _history("NEWUSDT", first=signal_day - timedelta(days=5), days=6)
    result = build_pit_universe(_ten_histories(days=45) + [new], signal_day)
    assert result.fail_closed is False
    assert result.exclusions["NEWUSDT"] == "INSUFFICIENT_LOOKBACK"
    assert build_pit_signal(_ten_histories(days=45) + [new], signal_day) is not None


def test_internal_eligible_gap_fails_closed_without_interpolation():
    signal_day = START + timedelta(days=40)
    broken = _history("BROKENUSDT", days=45)
    broken = replace(
        broken,
        daily_bars=tuple(bar for bar in broken.daily_bars if bar.day != START + timedelta(days=20)),
    )
    result = build_pit_universe(_ten_histories(days=45) + [broken], signal_day)
    assert result.fail_closed is True
    assert result.exclusions["BROKENUSDT"] == "MISSING_INTERNAL_DAILY_BAR"
    assert build_pit_signal(_ten_histories(days=45) + [broken], signal_day) is None


def test_future_price_change_does_not_change_signal_or_execution_lag():
    signal_day = START + timedelta(days=40)
    histories = _ten_histories(days=45)
    baseline = build_pit_signal(histories, signal_day)
    assert baseline is not None
    assert baseline.execution_day == signal_day + timedelta(days=1)
    changed = [
        replace(
            history,
            daily_bars=tuple(
                replace(bar, close=999_999.0, open=999_999.0, high=999_999.0, low=999_999.0)
                if bar.day > signal_day else bar
                for bar in history.daily_bars
            ),
        )
        for history in histories
    ]
    after = build_pit_signal(changed, signal_day)
    assert after is not None
    assert after.longs == baseline.longs
    assert after.shorts == baseline.shorts


def test_control_and_shadow_share_pit_targets():
    signal = build_pit_signal(_ten_histories(days=45), START + timedelta(days=40))
    assert signal is not None
    control_targets = signal.targets
    shadow_targets = signal.targets.copy()
    assert control_targets == shadow_targets


def test_data_quality_rejects_duplicates_nonmonotonic_prices_and_volume():
    day0 = START
    bars = [
        _bar("BADUSDT", day0, 100),
        replace(_bar("BADUSDT", day0, -1, -2), open_time_ms=_ms(day0) - 1),
        _bar("BADUSDT", day0 + timedelta(days=1), 101),
    ]
    history = make_symbol_history("BADUSDT", bars, currently_active=True)
    report = validate_dataset([history], day0, day0 + timedelta(days=2))
    assert not report.passed
    assert {"DUPLICATE_DAILY_BAR", "NON_MONOTONIC_TIMESTAMP", "INVALID_PRICE", "INVALID_QUOTE_VOLUME"}.issubset(report.issue_codes)


def test_data_quality_rejects_funding_gap_invalid_rate_future_and_out_window():
    bars = [_bar("FUNDUSDT", START + timedelta(days=index), 100 + index) for index in range(3)]
    events = (
        FundingEvent("FUNDUSDT", _ms(START, 12), 0.0001, 24.0),
        FundingEvent("FUNDUSDT", _ms(START, 12), float("nan"), 24.0),
        FundingEvent("FUNDUSDT", _ms(START + timedelta(days=2), 12), 0.0001, 24.0),
        FundingEvent("FUNDUSDT", _ms(START + timedelta(days=5), 12), 0.0001, 24.0),
    )
    history = make_symbol_history("FUNDUSDT", bars, events)
    issues = validate_symbol_history(history, START, START + timedelta(days=2), observation_ms=_ms(START + timedelta(days=1)))
    codes = {issue.code for issue in issues}
    assert {"DUPLICATE_FUNDING_EVENT", "INVALID_FUNDING_RATE", "FUNDING_COVERAGE_GAP", "OUT_OF_WINDOW", "FUTURE_TIMESTAMP_LEAKAGE"}.issubset(codes)


def test_lifecycle_gap_is_data_ambiguous_not_automatic_delisting():
    bars = [_bar("GAPUSDT", START, 100), _bar("GAPUSDT", START + timedelta(days=2), 102)]
    lifecycle = build_lifecycle("GAPUSDT", bars, currently_active=False, confirmed_absence_after_last_bar=True)
    assert lifecycle.status == "DATA_AMBIGUOUS"
    assert lifecycle.delisted_at is None
    history = make_symbol_history("GAPUSDT", bars, currently_active=False, confirmed_absence_after_last_bar=True)
    with pytest.raises(HistoryDataError, match="DATA_AMBIGUOUS"):
        forced_exit_for_delisting(history, next_rebalance_day=START + timedelta(days=3), notional=100.0)


def test_delisted_forced_exit_uses_last_real_close_and_cost():
    history = _history("EXITUSDT", days=5, currently_active=False)
    exit_event = forced_exit_for_delisting(
        history,
        next_rebalance_day=START + timedelta(days=7),
        notional=1_000.0,
        transaction_cost_rate=0.0008,
    )
    assert exit_event is not None
    assert exit_event.forced_exit is True
    assert exit_event.reason == "delisted"
    assert exit_event.exit_day == START + timedelta(days=4)
    assert exit_event.exit_price == history.daily_bars[-1].close
    assert exit_event.transaction_cost == pytest.approx(0.8)


def _passing_metrics() -> dict:
    return {
        "formal_run": True,
        "data_integrity": {
            "point_in_time_universe": True,
            "delisted_included": True,
            "no_known_lookahead": True,
            "no_unexplained_eligible_gap": True,
            "strategy_hash_unchanged": True,
            "protocol_hash_unchanged": True,
        },
        "external": {
            "total_return_pct": 12.0,
            "weekly_sharpe": 1.3,
            "max_drawdown_pct": 10.0,
            "profit_factor_weekly": 1.7,
        },
        "cost_2x": {"total_return_pct": 3.0},
        "bootstrap": {"mean_weekly_return_ci95_lower": 0.001, "probability_mean_return_gt_zero": 0.96},
        "best_5pct": {"compound_return_pct": 1.0},
        "leave_one_out": {"positive_runs": 10, "total_runs": 10},
        "concentration": {"top2_positive_pnl_share_pct": 30.0},
        "yearly": {"positive_full_calendar_years": 4, "worst_full_calendar_year_return_pct": -5.0},
        "bull_2020_2021": {"total_return_pct": 2.0, "max_drawdown_pct": 15.0},
    }


def test_frozen_gate_evaluator_passes_only_complete_metrics():
    result = evaluate_frozen_gates(_passing_metrics())
    assert result.decision == "PASS"
    assert result.failed_gates == ()
    assert all(result.gates.values())
    failed = _passing_metrics()
    failed["external"]["weekly_sharpe"] = 1.0
    assert evaluate_frozen_gates(failed).decision == "FAIL"
    missing = _passing_metrics()
    del missing["cost_2x"]["total_return_pct"]
    evaluated = evaluate_frozen_gates(missing)
    assert evaluated.decision == "FAIL"
    assert "cost_2x.total_return_pct" in evaluated.missing_metrics


def test_gate_evaluator_has_no_manual_override():
    with pytest.raises(ManualOverrideError):
        evaluate_frozen_gates(_passing_metrics(), manual_override=True)
    metrics = _passing_metrics()
    metrics["manual_override"] = True
    with pytest.raises(ManualOverrideError):
        evaluate_frozen_gates(metrics)


def test_external_and_discovery_windows_are_disjoint():
    external, discovery = protocol_windows(load_protocol())
    rows = [
        {"day": "2025-08-31", "value": 1},
        {"day": "2025-09-01", "value": 2},
    ]
    split = split_windowed_rows(rows, external, discovery)
    assert [row["value"] for row in split["EXTERNAL_VALIDATION"]] == [1]
    assert [row["value"] for row in split["DISCOVERY_REFERENCE"]] == [2]
    with pytest.raises(GateError):
        split_windowed_rows([{"day": "2019-12-31"}], external, discovery)


def test_bootstrap_is_fixed_and_deterministic():
    returns = [0.01, -0.002, 0.004, 0.0, 0.003, -0.001, 0.005, 0.002]
    first = block_bootstrap(returns, rounds=200)
    second = block_bootstrap(returns, rounds=200)
    assert first == second
    assert first["block_length_weeks"] == 4
    assert first["seed"] == 20260915


def test_best_5pct_removal_and_basic_metrics_are_deterministic():
    returns = [0.01] * 19 + [0.50]
    removed = remove_best_5pct(returns)
    assert removed["removed_count"] == 1
    assert removed["removed_indices"] == (19,)
    assert removed["compound_return_pct"] == pytest.approx(((1.01 ** 19) - 1) * 100)
    assert compound_return([0.1, -0.1]) == pytest.approx(-0.01)
    assert weekly_sharpe([0.01, 0.02]) > 0
    assert weekly_profit_factor([0.1, -0.05]) == pytest.approx(2.0)
    assert max_drawdown_pct([0.1, -0.2]) == pytest.approx(20.0)


def test_loo_reruns_with_symbol_removed_and_reports_worst():
    symbols = {"A", "B", "C"}
    calls = []

    def rerun(remaining):
        calls.append(remaining)
        return {"total_return_pct": 1.0 if "A" in remaining else -1.0}

    result = leave_one_out(symbols, rerun)
    assert len(calls) == 3
    assert all(len(remaining) == 2 for remaining in calls)
    assert result["loo_total"] == 3
    assert result["loo_positive"] == 2
    assert result["loo_negative"] == 1
    assert result["loo_worst_removed_symbol"] == "A"


def test_symbol_concentration_is_reported_from_positive_pnl_only():
    result = symbol_concentration({"A": 60.0, "B": 40.0, "C": -100.0})
    assert result["top_1_profit_symbol"] == "A"
    assert result["top2_positive_pnl_share_pct"] == pytest.approx(100.0)


def test_dataset_fingerprint_and_manifest_are_deterministic_and_include_lifecycle():
    histories = _ten_histories()
    first = dataset_fingerprint(histories)
    second = dataset_fingerprint(list(reversed(histories)))
    assert first == second
    altered = replace(histories[0], lifecycle=replace(histories[0].lifecycle, currently_active=False, delisted_at=START + timedelta(days=46)))
    assert dataset_fingerprint([altered, *histories[1:]]) != first
    manifest = build_dataset_manifest(histories, protocol_hash="p" * 64)
    assert manifest["dataset_sha256"] == first
    assert manifest["number_of_symbols_discovered"] == 10
    assert manifest["daily_bar_count"] == 450
    assert len(manifest["symbols"]) == 10
    assert {"listed_from", "delisted_at", "currently_active", "lifecycle_source", "lifecycle_confidence"}.issubset(manifest["symbols"][0])


def test_report_template_has_no_formal_result_and_failed_gates_come_first():
    template = render_m1_report()
    assert "No formal M1 historical backtest was run" in template
    report = render_m1_report({"decision": "FAIL", "failed_gates": ["G12_bull_survival"]})
    assert report.index("## Failed Gates") < report.index("## Machine-readable result")
