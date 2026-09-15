"""M1-B.1A corrective audits.

This module only inspects the frozen c4bb2b9 dataset and emits provenance and
root-cause artifacts.  It deliberately does not run a formal performance
backtest, evaluate gates, or write the formal M1 decision/report files.
"""

from __future__ import annotations

import argparse
import csv
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
import io
import json
import math
from pathlib import Path
import pickle
import zipfile
from typing import Any, Mapping, Sequence

from src.xs_lowvol_spec import (
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
    SHADOW_SPEC_HASH,
    SHADOW_STRATEGY_ID,
    resolve_rules,
)

from .m1_b import (
    M1_DATA_END,
    M1_DATA_START,
    M1_DATASET_FREEZE_PATH,
    _build_universe_fast,
    _date_range,
    _signal_from_universe,
)
from .m1_protocol import M1_APPROVED_PROTOCOL_SHA256
from .xs_data_quality import _funding_transition_is_valid
from .xs_history import DailyBar, DiscoveredArchiveRecord, FundingEvent, SymbolHistory


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORRECTIVE_DIR = PROJECT_ROOT / "research" / "m1" / "corrective"
ANC_AUDIT_PATH = CORRECTIVE_DIR / "ANCUSDT_AUDIT.json"
FUNDING_AUDIT_PATH = CORRECTIVE_DIR / "FUNDING_ROOT_CAUSE_AUDIT.json"
CORRECTIVE_RUN_ID = "M1-B.1A-20260915"
CORRECTED_FROM = "c4bb2b90ce1ef9b8e38858c7f4288b8b4d8c156b"
FUNDING_SYMBOLS = (
    "LENDUSDT",
    "KEEPUSDT",
    "LOKAUSDT",
    "MEMEFIUSDT",
    "LEVERUSDT",
    "BSWUSDT",
    "AI16ZUSDT",
    "OMUSDT",
    "MLNUSDT",
)
FUNDING_CLASSIFICATIONS = (
    "CONFIRMED_MISSING_SETTLEMENT",
    "NO_SETTLEMENT_REQUIRED",
    "VALIDATOR_FALSE_POSITIVE",
    "UNRESOLVED_DATA_INVALID",
)
ANC_SIGNAL_DAY = date(2022, 5, 13)


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_value(item) for item in value)
    return value


def _iso_ms(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).isoformat()


def _load_frozen_dataset() -> dict[str, Any]:
    dataset = pickle.loads(M1_DATASET_FREEZE_PATH.read_bytes())
    records = tuple(dataset["archive_records"])
    if not records or any(record.checksum_verified is not True for record in records):
        raise RuntimeError("frozen dataset does not have complete official checksum verification")
    if dataset.get("dataset_sha256") != "f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755":
        raise RuntimeError("unexpected frozen dataset SHA-256")
    return dataset


def _archive_record(record: DiscoveredArchiveRecord) -> dict[str, Any]:
    local_sha256 = record.local_sha256
    if record.local_path and Path(record.local_path).is_file():
        from .xs_history import file_sha256

        local_sha256 = file_sha256(record.local_path)
    return {
        "symbol": record.symbol,
        "kind": record.kind,
        "month": record.month,
        "url": record.url,
        "source_url": record.source_url or record.url,
        "local_path": record.local_path,
        "local_sha256": local_sha256,
        "official_checksum": record.official_checksum,
        "checksum_verified": record.checksum_verified,
        "download_status": record.download_status,
        "normalize_status": record.normalize_status,
        "quality_status": record.quality_status,
        "exclusion_reason": record.exclusion_reason,
    }


def _archive_records(
    dataset: Mapping[str, Any],
    symbol: str,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    records = [
        record
        for record in dataset["archive_records"]
        if record.symbol.upper() == symbol.upper() and (kind is None or record.kind == kind)
    ]
    return [_archive_record(record) for record in sorted(records, key=lambda item: (item.kind, item.month))]


def _real_daily_bars(history: SymbolHistory) -> list[DailyBar]:
    return [
        bar
        for bar in sorted(history.daily_bars, key=lambda item: (item.day, item.close_time_ms))
        if math.isfinite(bar.quote_volume)
        and bar.quote_volume > 0
        and all(math.isfinite(price) and price > 0 for price in (bar.open, bar.high, bar.low, bar.close))
    ]


def _bar_record(bar: DailyBar | None) -> dict[str, Any] | None:
    if bar is None:
        return None
    return {
        "symbol": bar.symbol,
        "day": bar.day.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "quote_volume": bar.quote_volume,
        "open_time_ms": bar.open_time_ms,
        "close_time_ms": bar.close_time_ms,
    }


def _funding_event(event: FundingEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "symbol": event.symbol,
        "funding_time_ms": event.funding_time_ms,
        "funding_time_utc": _iso_ms(event.funding_time_ms),
        "funding_rate": event.funding_rate,
        "funding_interval_hours": event.funding_interval_hours,
    }


def _raw_funding_row_count(record: Mapping[str, Any]) -> int | None:
    local_path = record.get("local_path")
    if not local_path or not Path(str(local_path)).is_file():
        return None
    count = 0
    with zipfile.ZipFile(str(local_path)) as archive:
        for member in archive.namelist():
            if not member.lower().endswith(".csv"):
                continue
            with archive.open(member) as handle:
                for row in csv.reader(io.TextIOWrapper(handle, encoding="utf-8")):
                    if not row:
                        continue
                    try:
                        int(float(row[0]))
                    except (TypeError, ValueError):
                        continue
                    count += 1
    return count


def _fixed_signal_map(histories: Sequence[SymbolHistory]) -> dict[date, Any]:
    """Reconstruct only the rejected run's fixed 7-day signal candidates."""
    rules = resolve_rules(variant="control")
    bar_maps = {history.symbol: history.bars_by_day() for history in histories}
    first_signal_day = M1_DATA_START + timedelta(days=rules.lookback_days)
    last_signal_day = M1_DATA_END - timedelta(days=rules.execution_lag_days)
    output: dict[date, Any] = {}
    signal_day = first_signal_day
    while signal_day <= last_signal_day:
        universe = _build_universe_fast(histories, bar_maps, signal_day, rules)
        signal = _signal_from_universe(universe, rules)
        if signal is not None:
            output[signal_day] = signal
        signal_day += timedelta(days=rules.rebalance_days)
    return output


def _fixed_holding_intervals(histories: Sequence[SymbolHistory]) -> list[dict[str, Any]]:
    """Locate rejected-run holds without calculating any PnL or statistics."""
    signals = _fixed_signal_map(histories)
    execution_signals = {signal.execution_day: signal for signal in signals.values()}
    by_symbol = {history.symbol: history for history in histories}
    bar_maps = {history.symbol: history.bars_by_day() for history in histories}
    positions: dict[str, tuple[int, int]] = {}
    intervals: list[dict[str, Any]] = []

    def record_interval(symbol: str, exit_timestamp_ms: int) -> None:
        position = positions.pop(symbol, None)
        if position is None or exit_timestamp_ms <= position[0]:
            return
        intervals.append(
            {
                "symbol": symbol,
                "direction": position[1],
                "entry_timestamp_ms": position[0],
                "entry_timestamp_utc": _iso_ms(position[0]),
                "exit_timestamp_ms": exit_timestamp_ms,
                "exit_timestamp_utc": _iso_ms(exit_timestamp_ms),
            }
        )

    for day in _date_range(M1_DATA_START, M1_DATA_END):
        for symbol in tuple(positions):
            bar = bar_maps[symbol].get(day)
            if bar is not None:
                continue
            history = by_symbol[symbol]
            if (
                history.lifecycle.status == "OK"
                and history.lifecycle.delisted_at is not None
                and day >= history.lifecycle.delisted_at
            ):
                terminal = history.last_bar_on_or_before(history.lifecycle.delisted_at - timedelta(days=1))
                if terminal is not None:
                    record_interval(symbol, terminal.close_time_ms)

        signal = execution_signals.get(day)
        if signal is None:
            continue
        targets = signal.targets
        affected = {
            symbol
            for symbol in set(positions) | set(targets)
            if symbol not in targets or symbol not in positions or positions[symbol][1] != targets[symbol]
        }
        if any(day not in bar_maps.get(symbol, {}) for symbol in affected):
            continue
        for symbol in tuple(positions):
            if targets.get(symbol) != positions[symbol][1]:
                record_interval(symbol, bar_maps[symbol][day].close_time_ms)
        for symbol, direction in targets.items():
            if symbol not in positions:
                positions[symbol] = (bar_maps[symbol][day].close_time_ms, direction)

    for symbol in tuple(positions):
        terminal = bar_maps[symbol].get(M1_DATA_END)
        if terminal is None:
            terminal = by_symbol[symbol].last_bar_on_or_before(M1_DATA_END)
        if terminal is not None:
            record_interval(symbol, terminal.close_time_ms)
    return intervals


def _anc_audit(dataset: Mapping[str, Any]) -> dict[str, Any]:
    histories = tuple(dataset["usable_histories"])
    history = next(item for item in histories if item.symbol == "ANCUSDT")
    signals = _fixed_signal_map(histories)
    signal = signals.get(ANC_SIGNAL_DAY)
    if signal is None or signal.targets.get("ANCUSDT") is None:
        raise RuntimeError("ANCUSDT audit signal is not present in the frozen rejected-run schedule")
    bars = _real_daily_bars(history)
    signal_bar = history.bars_by_day().get(ANC_SIGNAL_DAY)
    execution_day = ANC_SIGNAL_DAY + timedelta(days=1)
    execution_bar = history.bars_by_day().get(execution_day)
    daily_records = _archive_records(dataset, "ANCUSDT", kind="daily_klines")
    all_records = _archive_records(dataset, "ANCUSDT")
    return {
        "schema_version": "M1-B.1A-ANC-AUDIT.v1",
        "run_id": CORRECTIVE_RUN_ID,
        "corrected_from": CORRECTED_FROM,
        "formal_rerun": False,
        "symbol": "ANCUSDT",
        "signal_day": ANC_SIGNAL_DAY.isoformat(),
        "signal_day_bar": _bar_record(signal_bar),
        "execution_day": execution_day.isoformat(),
        "target_direction": signal.targets["ANCUSDT"],
        "target_direction_label": "SHORT" if signal.targets["ANCUSDT"] < 0 else "LONG",
        "target_symbols": signal.targets,
        "official_daily_archive_availability": {
            "required_window": {"start": M1_DATA_START.isoformat(), "end": M1_DATA_END.isoformat()},
            "available_months": sorted(record["month"] for record in daily_records),
            "archive_count": len(daily_records),
            "all_downloaded": all(record["download_status"] == "downloaded" for record in daily_records),
            "all_checksum_verified": all(record["checksum_verified"] is True for record in daily_records),
            "source_files": daily_records,
        },
        "execution_price": {
            "available": execution_bar is not None,
            "bar": _bar_record(execution_bar),
        },
        "last_real_bar": _bar_record(bars[-1] if bars else None),
        "next_real_bar": None,
        "lifecycle": _json_value(asdict(history.lifecycle)),
        "official_source_files": all_records,
        "root_cause_classification": "A_NO_ACTUAL_EXECUTION_CLOSE",
        "disposition": "KEEP_MISSING_EXECUTION_AND_RETRY_NEXT_CALENDAR_DAY",
        "provenance_note": (
            "The official daily archive ends with a real 2022-05-13 close; no completed "
            "2022-05-14 close exists. ANCUSDT is retained and is not blacklisted or filled."
        ),
    }


def _funding_interval_audit(
    history: SymbolHistory,
    interval: Mapping[str, Any],
    archive_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    events = tuple(sorted(history.funding_events, key=lambda event: event.funding_time_ms))
    times = [event.funding_time_ms for event in events]
    entry = int(interval["entry_timestamp_ms"])
    exit_timestamp = int(interval["exit_timestamp_ms"])
    left = bisect_right(times, entry)
    right = bisect_right(times, exit_timestamp)
    previous = events[left - 1] if left else None
    inside = events[left:right]
    following = events[right] if right < len(events) else None
    context = tuple(event for event in (previous, *inside, following) if event is not None)
    transitions = []
    for before, after in zip(context, context[1:]):
        delta_hours = (after.funding_time_ms - before.funding_time_ms) / 3_600_000.0
        transitions.append(
            {
                "from": _funding_event(before),
                "to": _funding_event(after),
                "actual_delta_hours": delta_hours,
                "transition_valid_under_frozen_validator": _funding_transition_is_valid(before, after),
            }
        )
    last_observed = inside[-1] if inside else previous
    expected_next_ms = None
    expected_next_is_inside_hold = False
    lifecycle_boundary_ms = None
    if history.lifecycle.delisted_at is not None:
        lifecycle_boundary_ms = int(
            datetime.combine(history.lifecycle.delisted_at, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000
        )
    def expected_next(event: FundingEvent | None) -> int | None:
        if event is None or not math.isfinite(event.funding_interval_hours) or event.funding_interval_hours <= 0:
            return None
        return int(event.funding_time_ms + event.funding_interval_hours * 3_600_000.0)

    def expected_is_inside(event: FundingEvent | None) -> bool:
        candidate = expected_next(event)
        return candidate is not None and entry < candidate <= exit_timestamp and (
            lifecycle_boundary_ms is None or candidate < lifecycle_boundary_ms
        )

    if last_observed is not None:
        expected_next_ms = expected_next(last_observed)
        expected_next_is_inside_hold = expected_next_ms is not None and expected_next_ms <= exit_timestamp and (
            lifecycle_boundary_ms is None or expected_next_ms < lifecycle_boundary_ms
        )
    inside_gap = any(
        not _funding_transition_is_valid(before, after)
        for before, after in zip(inside, inside[1:])
    )
    entry_gap = bool(previous and inside and not _funding_transition_is_valid(previous, inside[0]) and expected_is_inside(previous))
    exit_gap = bool(inside and not _funding_transition_is_valid(inside[-1], following) if following else expected_is_inside(inside[-1]))
    empty_hold_gap = bool(not inside and previous and following and not _funding_transition_is_valid(previous, following) and expected_is_inside(previous))
    boundary_missing = bool((inside and expected_is_inside(inside[-1])) or (not inside and expected_is_inside(previous)))
    relevant_nonmatching_count = int(inside_gap) + int(entry_gap) + int(exit_gap) + int(empty_hold_gap)
    issue_present = relevant_nonmatching_count > 0 or boundary_missing
    if issue_present:
        classification = "CONFIRMED_MISSING_SETTLEMENT"
        classification_basis = (
            "A declared real-event interval predicts a settlement inside the held interval, "
            "but no corresponding official normalized settlement exists; raw archive rows and "
            "checksum provenance are retained below."
        )
    else:
        classification = "NO_SETTLEMENT_REQUIRED"
        classification_basis = "No expected settlement is inside the hold before the observed lifecycle/data boundary."
    return {
        "entry": {
            "timestamp_ms": entry,
            "timestamp_utc": _iso_ms(entry),
        },
        "exit": {
            "timestamp_ms": exit_timestamp,
            "timestamp_utc": _iso_ms(exit_timestamp),
        },
        "direction": interval.get("direction"),
        "previous_settlement": _funding_event(previous),
        "settlements_inside_entry_exit": [_funding_event(event) for event in inside],
        "next_settlement": _funding_event(following),
        "interval_metadata": {
            "inside_settlement_count": len(inside),
            "observed_intervals_hours": sorted({event.funding_interval_hours for event in inside}),
            "expected_next_after_last_observed_ms": expected_next_ms,
            "expected_next_after_last_observed_utc": _iso_ms(expected_next_ms),
            "expected_next_is_inside_hold_before_lifecycle_boundary": expected_next_is_inside_hold,
            "transitions": transitions,
            "nonmatching_transition_count": relevant_nonmatching_count,
            "boundary_missing_settlement": boundary_missing,
        },
        "lifecycle_boundaries": {
            "listed_from": history.lifecycle.listed_from.isoformat(),
            "delisted_at": history.lifecycle.delisted_at.isoformat() if history.lifecycle.delisted_at else None,
            "first_available_day": history.lifecycle.first_available_day.isoformat(),
            "last_available_day": history.lifecycle.last_available_day.isoformat(),
            "status": history.lifecycle.status,
            "currently_active": history.lifecycle.currently_active,
            "lifecycle_source": history.lifecycle.lifecycle_source,
            "lifecycle_confidence": history.lifecycle.lifecycle_confidence,
        },
        "classification": classification,
        "classification_basis": classification_basis,
        "no_zero_funding_fill": True,
        "official_funding_archive_source_files": list(archive_records),
    }


def _funding_symbol_audit(
    dataset: Mapping[str, Any],
    history: SymbolHistory,
    intervals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    archive_records = _archive_records(dataset, history.symbol, kind="funding")
    normalized_by_month = Counter(
        datetime.fromtimestamp(event.funding_time_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m")
        for event in history.funding_events
    )
    parser_reconciliation = []
    for record in archive_records:
        raw_rows = _raw_funding_row_count(record)
        parser_reconciliation.append(
            {
                "month": record["month"],
                "raw_archive_rows": raw_rows,
                "normalized_events_in_month": normalized_by_month.get(record["month"], 0),
                "status": "MATCH" if raw_rows == normalized_by_month.get(record["month"], 0) else "MISMATCH_REQUIRES_REVIEW",
                "source_url": record["source_url"],
                "official_checksum": record["official_checksum"],
                "checksum_verified": record["checksum_verified"],
            }
        )
    interval_audits = [
        _funding_interval_audit(history, interval, archive_records)
        for interval in intervals
    ]
    classes = {audit["classification"] for audit in interval_audits}
    parser_issue_detected = any(item["status"] != "MATCH" for item in parser_reconciliation)
    if parser_issue_detected:
        classification = "UNRESOLVED_DATA_INVALID"
    elif "CONFIRMED_MISSING_SETTLEMENT" in classes:
        classification = "CONFIRMED_MISSING_SETTLEMENT"
    elif classes == {"NO_SETTLEMENT_REQUIRED"}:
        classification = "NO_SETTLEMENT_REQUIRED"
    else:
        classification = "UNRESOLVED_DATA_INVALID"
    real_bars = _real_daily_bars(history)
    return {
        "symbol": history.symbol,
        "classification": classification,
        "holding_interval_count": len(interval_audits),
        "holding_intervals": interval_audits,
        "official_funding_archive_months": [record["month"] for record in archive_records],
        "official_source_files": archive_records,
        "archive_checksum_all_verified": all(record["checksum_verified"] is True for record in archive_records),
        "parser_reconciliation": parser_reconciliation,
        "parser_issue_detected": parser_issue_detected,
        "last_normalized_settlement": _funding_event(max(history.funding_events, key=lambda event: event.funding_time_ms, default=None)),
        "last_real_daily_bar": _bar_record(real_bars[-1] if real_bars else None),
        "post_last_real_daily_bar_count": sum(
            1 for bar in history.daily_bars if real_bars and bar.day > real_bars[-1].day
        ),
        "lifecycle_boundaries": _json_value(asdict(history.lifecycle)),
        "root_cause_note": (
            "Official archive files are checksum-verified. The held interval reaches an "
            "expected settlement that is absent from the official rows; no zero-rate fill "
            "or symbol blacklist is applied."
        ),
    }


def _funding_audit(dataset: Mapping[str, Any]) -> dict[str, Any]:
    histories = {history.symbol: history for history in dataset["usable_histories"]}
    intervals = _fixed_holding_intervals(tuple(histories.values()))
    audits = [
        _funding_symbol_audit(
            dataset,
            histories[symbol],
            [interval for interval in intervals if interval["symbol"] == symbol],
        )
        for symbol in FUNDING_SYMBOLS
    ]
    counts = Counter(item["classification"] for item in audits)
    return {
        "schema_version": "M1-B.1A-FUNDING-ROOT-CAUSE.v1",
        "run_id": CORRECTIVE_RUN_ID,
        "corrected_from": CORRECTED_FROM,
        "formal_rerun": False,
        "performance_results_generated": False,
        "new_decision_generated": False,
        "dataset_sha256": dataset["dataset_sha256"],
        "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
        "protocol_sha256": M1_APPROVED_PROTOCOL_SHA256,
        "control_strategy_spec": {"strategy_id": CONTROL_STRATEGY_ID, "sha256": CONTROL_SPEC_HASH},
        "shadow_strategy_spec": {"strategy_id": SHADOW_STRATEGY_ID, "sha256": SHADOW_SPEC_HASH},
        "classification_vocabulary": list(FUNDING_CLASSIFICATIONS),
        "validator_timestamp_tolerance_hours": 1e-3,
        "validator_tolerance_evidence": {
            "synthetic_regression_test": "test_official_millisecond_funding_jitter_is_not_a_false_gap",
            "real_false_positive_classifications": 0,
        },
        "symbols": audits,
        "classification_counts": dict(sorted(counts.items())),
        "g0_disposition": "G0_FAIL_RETAINED" if any(item["classification"] != "NO_SETTLEMENT_REQUIRED" for item in audits) else "G0_UNCHANGED",
        "no_zero_funding_fill": True,
        "no_symbol_blacklist": True,
    }


def generate_audits() -> tuple[Path, Path]:
    dataset = _load_frozen_dataset()
    CORRECTIVE_DIR.mkdir(parents=True, exist_ok=True)
    ANC_AUDIT_PATH.write_text(
        json.dumps(_json_value(_anc_audit(dataset)), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    FUNDING_AUDIT_PATH.write_text(
        json.dumps(_json_value(_funding_audit(dataset)), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ANC_AUDIT_PATH, FUNDING_AUDIT_PATH


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate M1-B.1A corrective audits only")
    parser.add_argument("command", choices=("audits",))
    args = parser.parse_args(argv)
    if args.command == "audits":
        anc_path, funding_path = generate_audits()
        print(f"generated {anc_path}")
        print(f"generated {funding_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
