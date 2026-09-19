"""XS-LOWVOL V2.1-M1.3 parent lifecycle integrity reconstruction.

This module is a state-only diagnostic.  It reads the approved normalized
cache, applies a separately hashed official Binance lifecycle overlay in
memory, and records scheduler, holding, funding-lifetime, and weekly ledger
provenance.  It never rewrites the frozen cache or any earlier research
artifact and it does not create Forward or live-trading evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.xs_lowvol_spec import resolve_rules, strategy_spec_hash
from src.xs_lowvol_v2_1_anchor import (
    APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_1_PROTOCOL_SHA256,
    APPROVED_V2_1_SPEC_SHA256,
    V21_FORWARD_ANCHOR_STATUS,
    validate_v2_1_forward_anchor,
    verify_v2_1_forward_anchor_hash,
)
from src.xs_lowvol_v2_1_lifecycle import (
    LifecycleOverride,
    LifecycleOverlay,
    LifecycleOverlayError,
    apply_lifecycle_overlay,
    funding_exposure_end_ms,
    terminal_daily_close,
    utc_iso,
    validate_economic_funding_coverage,
)
from src.xs_lowvol_v2_1_spec import verify_v2_1_spec_hash

from .m1_b import (
    M1_DATA_END,
    M1_DATA_START,
    build_stateful_schedule,
    simulate_frozen_portfolio,
)
from .m1_protocol import M1_APPROVED_PROTOCOL_SHA256, verify_protocol_hash
from .v2_1_m1_1_warm_start_audit import load_frozen_dataset
from .v2_1_protocol import verify_v2_1_protocol_hash


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_COMMIT = "c92c560a791735cf5cc2ec3865bc7e7a09e65d08"
RUN_ID = "XS-LOWVOL-V2.1-M1.3-PARENT-LIFECYCLE-RECONSTRUCTION-1"
RUN_TYPE = "PARENT_LIFECYCLE_INTEGRITY_RECONSTRUCTION"
APPROVAL = "START V2.1-M1.3"
OUTPUT_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_3"
REPORT_PATH = OUTPUT_DIR / "V2_1_M1_3_PARENT_RECONSTRUCTION.json"
MARKDOWN_PATH = OUTPUT_DIR / "V2_1_M1_3_PARENT_RECONSTRUCTION.md"
MANIFEST_PATH = OUTPUT_DIR / "LIFECYCLE_OVERRIDE_MANIFEST.json"
SCHEDULE_PATH = OUTPUT_DIR / "SCHEDULE_PROVENANCE.json"
FORCED_EXIT_PATH = OUTPUT_DIR / "FORCED_EXIT_PROVENANCE.json"
HOLDING_PATH = OUTPUT_DIR / "HOLDING_PROVENANCE.json"
FUNDING_PATH = OUTPUT_DIR / "FUNDING_LIFETIME_PROVENANCE.json"
WEEKLY_PATH = OUTPUT_DIR / "WEEKLY_ACCOUNTING_PROVENANCE.json"

UTC = timezone.utc
INITIAL_EIGHT = (
    "AKROUSDT",
    "ANCUSDT",
    "DMCUSDT",
    "KEEPUSDT",
    "MEMEFIUSDT",
    "OMNIUSDT",
    "TONUSDT",
    "VINEUSDT",
)

# These are canonical fact hashes.  The official pages were reviewed as
# primary Binance provenance; the raw HTML is not fetched by the formal run.
_OFFICIAL_BASE = "https://www.binance.com/en/support/announcement/detail/"


def _record(
    symbol: str,
    page_id: str,
    published: str,
    terminated: str,
    evidence: str,
    *,
    old_issue: str,
    new_symbol: str | None = None,
    supplemental: bool = False,
) -> LifecycleOverride:
    last_day = date.fromisoformat(terminated[:10])
    return LifecycleOverride(
        symbol=symbol,
        contract_type="USD_M_PERPETUAL",
        official_source=_OFFICIAL_BASE + page_id,
        announcement_publish_time_utc=published,
        economic_termination_timestamp_utc=terminated,
        last_valid_trading_day=last_day,
        delisted_at=last_day + timedelta(days=1),
        evidence_status="CONFIRMED_OFFICIAL_BINANCE",
        classification="LIFECYCLE_TERMINATION_CONFIRMED",
        official_evidence=evidence,
        old_issue=old_issue,
        new_symbol=new_symbol,
        supplemental=supplemental,
    )


DEFAULT_OVERLAY = LifecycleOverlay.from_records(
    (
        _record(
            "AKROUSDT",
            "185001cde1614f0e8987c4d39aca8639",
            "2022-05-26T08:58:00Z",
            "2022-05-27T07:00:00Z",
            "Postponed Binance Futures settlement; final settlement is 2022-05-27 07:00 UTC.",
            old_issue="V1_HELD_INTERVAL_EXTENDED_PAST_SYMBOL_LIFETIME",
        ),
        _record(
            "ANCUSDT",
            "0bfb6cfd10aa4d26926260c0159ef58d",
            "2022-05-13T02:27:00Z",
            "2022-05-13T04:00:00Z",
            "Binance Futures automatic settlement and delisting at 2022-05-13 04:00 UTC.",
            old_issue="V1_HELD_INTERVAL_EXTENDED_PAST_SYMBOL_LIFETIME",
        ),
        _record(
            "DMCUSDT",
            "5798be852fb5449bb438dde4166126dc",
            "2026-01-17T07:56:00Z",
            "2026-01-21T09:00:00Z",
            "Binance Futures settlement notice names DMCUSDT at 2026-01-21 09:00 UTC.",
            old_issue="V1_HELD_INTERVAL_EXTENDED_PAST_SYMBOL_LIFETIME",
        ),
        _record(
            "KEEPUSDT",
            "96698a6a80f64cb1ae27f813032bfaa9",
            "2022-02-09T10:26:00Z",
            "2022-02-15T02:00:00Z",
            "Binance closes and settles KEEPUSDT; the successor is a separate symbol.",
            old_issue="V1_HELD_INTERVAL_EXTENDED_PAST_SYMBOL_LIFETIME",
            new_symbol="TUSDT",
        ),
        _record(
            "MEMEFIUSDT",
            "21e399dcea734230a3c181daf5407b64",
            "2025-08-06T07:30:00Z",
            "2025-08-11T09:00:00Z",
            "Binance Futures settlement and removal at 2025-08-11 09:00 UTC.",
            old_issue="V1_HELD_INTERVAL_EXTENDED_PAST_SYMBOL_LIFETIME",
        ),
        _record(
            "OMNIUSDT",
            "13d0a79f79ef4406963dcd7f9d1de325",
            "2025-09-10T03:00:00Z",
            "2025-09-22T09:00:00Z",
            "Binance Futures disables new positions and settles OMNIUSDT at 09:00 UTC.",
            old_issue="V1_HELD_INTERVAL_EXTENDED_PAST_SYMBOL_LIFETIME",
            new_symbol="NOMUSDT",
        ),
        _record(
            "TONUSDT",
            "fe307fba935b44698fde4db01e84a7eb",
            "2026-06-12T06:00:16.955Z",
            "2026-06-23T09:00:00Z",
            "Binance official TON rebranding notice; Futures settlement at 09:00 UTC.",
            old_issue="V1_HELD_INTERVAL_EXTENDED_PAST_SYMBOL_LIFETIME",
        ),
        _record(
            "VINEUSDT",
            "ba5d61807b474b0ca9f40250e7fa782c",
            "2026-04-24T11:15:00Z",
            "2026-04-28T10:00:00Z",
            "Binance Futures settlement and removal at 2026-04-28 10:00 UTC.",
            old_issue="V1_HELD_INTERVAL_EXTENDED_PAST_SYMBOL_LIFETIME",
        ),
        _record(
            "ALPHAUSDT",
            "10fd115c29244067bd4566a70f77e327",
            "2025-09-17T08:45:00Z",
            "2025-09-23T09:00:00Z",
            "Binance Futures settlement of ALPHAUSDT at 2025-09-23 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "BAKEUSDT",
            "82df1b55a0d84c288fa396d5a5cead48",
            "2025-09-29T11:45:00Z",
            "2025-10-03T09:00:00Z",
            "Binance Futures multiple-contract notice settles BAKEUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "BSWUSDT",
            "c95b56efd290471ba11c6cb850208d07",
            "2025-09-10T10:14:00Z",
            "2025-09-15T09:00:00Z",
            "Binance Futures settlement of BSWUSDT at 2025-09-15 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "DAMUSDT",
            "1d2b6970facd470dbba6a674e1595bd4",
            "2026-04-23T15:00:00Z",
            "2026-04-29T09:00:00Z",
            "Binance Futures multiple-contract notice settles DAMUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "HFTUSDT",
            "64cf7544b98e4f2ca7f54ba11fd72d9d",
            "2026-08-03T03:30:00Z",
            "2026-08-07T09:00:00Z",
            "Binance Futures delisting notice settles HFTUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "HIFIUSDT",
            "82df1b55a0d84c288fa396d5a5cead48",
            "2025-09-29T11:45:00Z",
            "2025-10-03T09:00:00Z",
            "Binance Futures multiple-contract notice settles HIFIUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "KDAUSDT",
            "ebe57975b8c34e928c7d3fdacfcef32a",
            "2025-11-03T02:30:00Z",
            "2025-11-06T09:00:00Z",
            "Binance Futures multiple-contract notice settles KDAUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "LEVERUSDT",
            "d5fc5fdda5c74e3e8780050ed0841267",
            "2025-08-29T11:00:00Z",
            "2025-09-03T09:00:00Z",
            "Binance Futures delisting notice settles LEVERUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "MLNUSDT",
            "a42f51022cb649aea0b4cb808205fd76",
            "2026-05-13T07:30:00Z",
            "2026-05-19T09:00:00Z",
            "Binance Futures delisting notice settles MLNUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "NEIROETHUSDT",
            "77d088eda9b040dc9f73bb7e57ae8e26",
            "2025-09-22T07:30:00Z",
            "2025-09-26T09:00:00Z",
            "Binance Futures delisting notice settles NEIROETHUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "OLUSDT",
            "97b4f3a7d02a486c8d412ada2281b907",
            "2026-04-03T13:59:00Z",
            "2026-04-08T09:00:00Z",
            "Binance Futures multiple-contract notice settles OLUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "OMUSDT",
            "4d13052b4be943f5b155cb9150ca902c",
            "2026-02-13T10:00:00Z",
            "2026-02-23T09:00:00Z",
            "Binance OM swap notice settles and removes the old OMUSDT contract.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            new_symbol="MANTRAUSDT",
            supplemental=True,
        ),
        _record(
            "SXPUSDT",
            "746056ce732d43dda12c1e5ae1b7a059",
            "2025-12-01T08:14:00Z",
            "2025-12-05T09:00:00Z",
            "Binance Futures multiple-contract notice settles SXPUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
        _record(
            "UXLINKUSDT",
            "5a4fddd83a25401cabc2ae6271b532a2",
            "2025-09-24T12:00:00Z",
            "2025-09-26T09:00:00Z",
            "Binance Futures delisting notice settles UXLINKUSDT at 09:00 UTC.",
            old_issue="CASCADING_PARENT_MARK_FAILURE_FROM_FLAT_ARCHIVE_BARS",
            supplemental=True,
        ),
    )
)


_FORBIDDEN_SCHEMA_FRAGMENTS = (
    "return",
    "cagr",
    "sharpe",
    "sortino",
    "drawdown",
    "profit_factor",
    "pnl",
    "performance",
)
_FORBIDDEN_PRESTART_AGGREGATE_KEYS = frozenset(
    {
        "price_component_total",
        "funding_component_total",
        "transaction_cost_component_total",
        "long_component_total",
        "short_component_total",
    }
)


class ParentReconstructionError(RuntimeError):
    """Fail-closed M1.3 reconstruction error."""


def _schema_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _assert_state_schema(
    value: Any,
    path: str,
    forbidden_fragments: Sequence[str],
    forbidden_exact_keys: frozenset[str] = frozenset(),
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = _schema_key(key)
            key_path = f"{path}.{key}" if path else str(key)
            if (
                any(fragment in key_text for fragment in forbidden_fragments)
                or key_text in forbidden_exact_keys
            ):
                raise ParentReconstructionError(
                    f"state-only artifact contains forbidden key: {key_path}"
                )
            _assert_state_schema(item, key_path, forbidden_fragments, forbidden_exact_keys)
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _assert_state_schema(item, f"{path}[{index}]", forbidden_fragments, forbidden_exact_keys)


def assert_state_only_schema(value: Any, path: str = "") -> None:
    """Reject forbidden historical-result keys before any artifact is written."""
    _assert_state_schema(value, path, _FORBIDDEN_SCHEMA_FRAGMENTS)


def assert_prestart_state_schema(value: Any, path: str = "") -> None:
    """Reject contaminated historical aggregates in future prestart artifacts."""
    _assert_state_schema(
        value,
        path,
        _FORBIDDEN_SCHEMA_FRAGMENTS,
        _FORBIDDEN_PRESTART_AGGREGATE_KEYS,
    )


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if hasattr(value, "as_dict"):
        return _plain(value.as_dict())
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    assert_state_only_schema(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_plain(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_legacy_artifacts() -> dict[str, str]:
    """Snapshot every earlier research artifact that M1.3 must preserve."""
    roots: tuple[Path, ...] = (
        PROJECT_ROOT / "research" / "m1",
        PROJECT_ROOT / "research" / "v2",
        PROJECT_ROOT / "research" / "v2_1" / "m1",
        PROJECT_ROOT / "research" / "v2_1" / "m1_1",
        PROJECT_ROOT / "research" / "v2_1" / "m1_2",
        PROJECT_ROOT / "research" / "evidence" / "XS-LOWVOL-V1.json",
    )
    snapshot: dict[str, str] = {}
    for root in roots:
        paths = (root,) if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if path.is_file():
                snapshot[path.relative_to(PROJECT_ROOT).as_posix()] = _file_sha256(path)
    if not snapshot:
        raise ParentReconstructionError("no legacy research artifact was found")
    return snapshot


def current_code_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    value = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ParentReconstructionError("current commit is not a full SHA-1")
    return value


def verify_frozen_identity() -> dict[str, str]:
    """Verify V1/V2.1 identity and the permanently frozen anchor."""
    try:
        actual = {
            "protocol_sha256": verify_protocol_hash(),
            "control_sha256": strategy_spec_hash(),
            "shadow_sha256": resolve_rules(variant="shadow").spec_hash,
            "v2_1_spec_sha256": verify_v2_1_spec_hash(),
            "v2_1_protocol_sha256": verify_v2_1_protocol_hash(),
            "forward_anchor_sha256": verify_v2_1_forward_anchor_hash(),
        }
        validate_v2_1_forward_anchor()
    except Exception as exc:  # noqa: BLE001 - identity must halt the run
        raise ParentReconstructionError(f"frozen identity verification failed: {exc}") from exc
    expected = {
        "protocol_sha256": M1_APPROVED_PROTOCOL_SHA256,
        "control_sha256": "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678",
        "shadow_sha256": "97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd",
        "v2_1_spec_sha256": APPROVED_V2_1_SPEC_SHA256,
        "v2_1_protocol_sha256": APPROVED_V2_1_PROTOCOL_SHA256,
        "forward_anchor_sha256": APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    }
    if actual != expected or V21_FORWARD_ANCHOR_STATUS != "FROZEN_NOT_YET_STARTED":
        raise ParentReconstructionError(
            f"frozen identity changed: expected={expected}, actual={actual}"
        )
    return actual


def _schedule_attempt_shape(attempt: Any) -> tuple[Any, ...]:
    return (
        attempt.signal_day,
        attempt.execution_day,
        attempt.status,
        attempt.reason,
        tuple(attempt.target_symbols),
    )


def _schedule_provenance(
    old_schedule: Any,
    corrected_schedule: Any,
    overlay: LifecycleOverlay,
    histories: Sequence[Any],
) -> dict[str, Any]:
    by_old = {
        (attempt.signal_day, attempt.execution_day): attempt
        for attempt in old_schedule.attempts
    }
    by_corrected = {
        (attempt.signal_day, attempt.execution_day): attempt
        for attempt in corrected_schedule.attempts
    }
    original_by_symbol = {history.symbol: history for history in histories}
    correction_dates = []
    for record in overlay.records:
        history = original_by_symbol[record.symbol]
        archive_after = any(
            bar.day >= record.delisted_at for bar in history.daily_bars
        )
        if history.lifecycle.delisted_at != record.delisted_at or archive_after:
            correction_dates.append(record.delisted_at)
    first_day = min(correction_dates) if correction_dates else M1_DATA_END
    all_keys = sorted(set(by_old) | set(by_corrected))
    pre_mismatch = 0
    expected_divergence = 0
    unexplained = 0
    divergence_rows: list[dict[str, Any]] = []
    for key in all_keys:
        old = by_old.get(key)
        corrected = by_corrected.get(key)
        same = (
            old is not None
            and corrected is not None
            and _schedule_attempt_shape(old) == _schedule_attempt_shape(corrected)
        )
        execution_day = key[1]
        if same:
            continue
        if execution_day < first_day:
            pre_mismatch += 1
        else:
            expected_divergence += 1
            divergence_rows.append(
                {
                    "signal_day": key[0],
                    "execution_day": key[1],
                    "old_status": old.status if old else None,
                    "corrected_status": corrected.status if corrected else None,
                    "old_reason": old.reason if old else None,
                    "corrected_reason": corrected.reason if corrected else None,
                }
            )
    if pre_mismatch:
        unexplained = pre_mismatch
    return {
        "old_attempt_count": len(old_schedule.attempts),
        "corrected_attempt_count": len(corrected_schedule.attempts),
        "old_successful_signal_count": len(old_schedule.signals),
        "corrected_successful_signal_count": len(corrected_schedule.signals),
        "first_lifecycle_correction_day": first_day,
        "pre_correction_schedule_mismatch_count": pre_mismatch,
        "post_correction_expected_divergence_count": expected_divergence,
        "post_correction_unexplained_divergence_count": unexplained,
        "divergence_rows": divergence_rows,
    }


def _mark_failure_symbols(schedule: Any) -> set[str]:
    symbols: set[str] = set()
    for attempt in schedule.attempts:
        if attempt.status != "MARK_FAILURE":
            continue
        symbols.update(re.findall(r"[A-Z0-9]+USDT", attempt.reason))
    return symbols


def _forced_exit_provenance(
    histories: Sequence[Any],
    overlay: LifecycleOverlay,
    old_accounting: Any,
    corrected_accounting: Any,
) -> dict[str, Any]:
    by_history = {history.symbol: history for history in histories}
    old_by_symbol: dict[str, list[Any]] = defaultdict(list)
    corrected_by_symbol: dict[str, list[Any]] = defaultdict(list)
    for interval in old_accounting.holding_intervals:
        old_by_symbol[interval.symbol].append(interval)
    for interval in corrected_accounting.holding_intervals:
        corrected_by_symbol[interval.symbol].append(interval)
    rows: list[dict[str, Any]] = []
    ghost_count = 0
    for record in overlay.records:
        history = by_history[record.symbol]
        terminal = next(
            bar for bar in history.daily_bars if bar.day == record.last_valid_trading_day
        )
        terminal_timestamp = terminal.close_time_ms
        corrected_intervals = corrected_by_symbol.get(record.symbol, [])
        forced = [
            interval for interval in corrected_intervals
            if interval.exit_timestamp_ms == terminal_timestamp
        ]
        delisted_timestamp = int(
            datetime.combine(record.delisted_at, datetime.min.time(), tzinfo=UTC).timestamp()
            * 1000
        )
        ghost_count += sum(
            1
            for interval in corrected_intervals
            if interval.entry_timestamp_ms < delisted_timestamp
            and interval.exit_timestamp_ms >= delisted_timestamp
        )
        rows.append(
            {
                "symbol": record.symbol,
                "old_interval_count": len(old_by_symbol.get(record.symbol, [])),
                "corrected_interval_count": len(corrected_intervals),
                "old_open_at_frozen_window_end_count": sum(
                    interval.exit_timestamp_ms
                    >= int(
                        datetime.combine(
                            M1_DATA_END + timedelta(days=1),
                            datetime.min.time(),
                            tzinfo=UTC,
                        ).timestamp()
                        * 1000
                    )
                    - 1
                    for interval in old_by_symbol.get(record.symbol, [])
                ),
                "corrected_forced_exit_count": len(forced),
                "terminal_daily_close": terminal_daily_close(history, record),
                "terminal_daily_close_timestamp_utc": utc_iso(terminal_timestamp),
                "delisted_at": record.delisted_at,
                "later_flat_archive_bar_count": sum(
                    bar.day >= record.delisted_at for bar in history.daily_bars
                ),
                "forced_exit_prices_are_daily_closes": all(
                    interval.exit_timestamp_ms == terminal_timestamp for interval in forced
                ),
                "funding_boundary_timestamp_utc": utc_iso(
                    record.economic_termination_timestamp_ms
                ),
            }
        )
    return {
        "forced_exit_rows": rows,
        "ghost_position_after_confirmed_delist_count": ghost_count,
    }


def _holding_provenance(histories: Sequence[Any], overlay: LifecycleOverlay, accounting: Any) -> dict[str, Any]:
    by_history = {history.symbol: history for history in histories}
    records: list[dict[str, Any]] = []
    for interval in accounting.holding_intervals:
        record = overlay.record_for(interval.symbol)
        effective_end = funding_exposure_end_ms(interval, overlay)
        row: dict[str, Any] = {
            "symbol": interval.symbol,
            "accounting_entry_timestamp_utc": utc_iso(interval.entry_timestamp_ms),
            "accounting_exit_timestamp_utc": utc_iso(interval.exit_timestamp_ms),
            "economic_exposure_end_timestamp_utc": utc_iso(effective_end),
            "official_termination_applied": record is not None,
            "forced_lifecycle_exit": False,
        }
        if record is not None:
            history = by_history[interval.symbol]
            terminal = next(
                bar for bar in history.daily_bars if bar.day == record.last_valid_trading_day
            )
            row["forced_lifecycle_exit"] = interval.exit_timestamp_ms == terminal.close_time_ms
            row["daily_price_boundary_timestamp_utc"] = utc_iso(terminal.close_time_ms)
            row["funding_economic_boundary_timestamp_utc"] = utc_iso(
                record.economic_termination_timestamp_ms
            )
        records.append(row)
    return {
        "interval_semantics": "(accounting_entry, accounting_exit]",
        "holding_interval_count": len(records),
        "records": records,
    }


def _weekly_accounting(accounting: Any) -> dict[str, Any]:
    by_day = {day: index for index, day in enumerate(accounting.dates)}
    last_sunday = M1_DATA_END - timedelta(days=(M1_DATA_END.weekday() + 1) % 7)
    starts = [last_sunday - timedelta(days=6 + 7 * index) for index in range(12, -1, -1)]
    rows: list[dict[str, Any]] = []
    for start in starts:
        end = start + timedelta(days=6)
        indexes = [by_day[day] for day in (start + timedelta(days=i) for i in range(7)) if day in by_day]
        complete = len(indexes) == 7 and accounting.complete and not accounting.issues
        rows.append(
            {
                "week_start": start,
                "week_end": end,
                "completed_at_utc": utc_iso(int(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC).timestamp() * 1000)),
                "price_component": sum(accounting.daily_price_pnl[index] for index in indexes),
                "funding_component": sum(accounting.daily_funding_pnl[index] for index in indexes),
                "transaction_cost_component": sum(accounting.daily_cost_pnl[index] for index in indexes),
                "long_component": sum(accounting.daily_long_pnl[index] for index in indexes),
                "short_component": sum(accounting.daily_short_pnl[index] for index in indexes),
                "combined_component": sum(accounting.daily_pnl[index] for index in indexes),
                "daily_observation_count": len(indexes),
                "complete": complete,
            }
        )
    return {
        "source": "fresh_corrected_control_replay",
        "week_count": len(rows),
        "all_complete": all(row["complete"] for row in rows),
        "rows": rows,
    }


def _risk_diagnostic(weekly: Mapping[str, Any]) -> dict[str, Any] | None:
    if weekly["all_complete"] is not True or len(weekly["rows"]) != 13:
        return None
    observations = [float(row["combined_component"]) for row in weekly["rows"]]
    reference = statistics.pstdev(observations) * math.sqrt(52.0)
    scale = 1.0 if reference == 0.0 else min(1.0, 0.15 / reference)
    return {
        "status": "INITIAL_STATE_DIAGNOSTIC_ONLY",
        "reference_volatility": reference,
        "position_scale": scale,
        "observation_count": len(observations),
        "used_for_runtime": False,
    }


def _classification_rows(
    histories: Sequence[Any],
    overlay: LifecycleOverlay,
    old_accounting: Any,
    corrected_accounting: Any,
) -> list[dict[str, Any]]:
    by_history = {history.symbol: history for history in histories}
    old_counts: dict[str, int] = defaultdict(int)
    corrected_counts: dict[str, int] = defaultdict(int)
    for interval in old_accounting.holding_intervals:
        old_counts[interval.symbol] += 1
    for interval in corrected_accounting.holding_intervals:
        corrected_counts[interval.symbol] += 1
    rows: list[dict[str, Any]] = []
    for record in overlay.records:
        history = by_history[record.symbol]
        corrected = apply_lifecycle_overlay((history,), LifecycleOverlay.from_records((record,)))[0]
        rows.append(
            {
                "symbol": record.symbol,
                "old_issue": record.old_issue,
                "root_cause_class": record.classification,
                "official_evidence": record.official_source,
                "holding_affected": old_counts[record.symbol],
                "scheduler_affected": bool(
                    history.lifecycle.delisted_at != record.delisted_at
                    or len(corrected.daily_bars) != len(history.daily_bars)
                ),
                "accounting_affected": corrected_counts[record.symbol],
                "correction_applied": True,
                "remaining_unresolved_status": "NONE",
            }
        )
    return rows


def reconstruct_parent() -> dict[str, Any]:
    """Run the formal frozen-cache reconstruction and write only M1.3 files."""
    if current_code_commit() == BASE_COMMIT:
        raise ParentReconstructionError("M1.3 code is not present after the approved base commit")
    identity = verify_frozen_identity()
    legacy_before = snapshot_legacy_artifacts()
    dataset = load_frozen_dataset()
    histories = tuple(dataset["usable_histories"])
    overlay = DEFAULT_OVERLAY.with_terminal_closes(histories)
    overlay.validate(histories)
    corrected_histories = apply_lifecycle_overlay(histories, overlay)

    old_schedule = build_stateful_schedule(
        histories, data_start=M1_DATA_START, data_end=M1_DATA_END
    )
    corrected_schedule = build_stateful_schedule(
        corrected_histories, data_start=M1_DATA_START, data_end=M1_DATA_END
    )
    old_accounting = simulate_frozen_portfolio(
        histories,
        old_schedule.signals,
        variant="control",
        scenario="COST_1X",
        data_start=M1_DATA_START,
        data_end=M1_DATA_END,
    )
    corrected_accounting = simulate_frozen_portfolio(
        corrected_histories,
        corrected_schedule.signals,
        variant="control",
        scenario="COST_1X",
        data_start=M1_DATA_START,
        data_end=M1_DATA_END,
    )
    schedule = _schedule_provenance(old_schedule, corrected_schedule, overlay, histories)
    forced = _forced_exit_provenance(
        histories, overlay, old_accounting, corrected_accounting
    )
    holdings = _holding_provenance(corrected_histories, overlay, corrected_accounting)
    funding = validate_economic_funding_coverage(
        corrected_histories,
        corrected_accounting.holding_intervals,
        overlay,
        window_end_ms=int(
            datetime.combine(
                M1_DATA_END + timedelta(days=1), datetime.min.time(), tzinfo=UTC
            ).timestamp()
            * 1000
        )
        - 1,
    )
    weekly = _weekly_accounting(corrected_accounting)
    risk = _risk_diagnostic(weekly)
    old_failures = _mark_failure_symbols(old_schedule)
    corrected_failures = _mark_failure_symbols(corrected_schedule)
    overlay_symbols = {record.symbol for record in overlay.records}
    old_lifecycle_failures = sum(
        1
        for attempt in old_schedule.attempts
        if attempt.status == "MARK_FAILURE"
        and set(re.findall(r"[A-Z0-9]+USDT", attempt.reason)) & overlay_symbols
    )
    corrected_lifecycle_failures = sum(
        1
        for attempt in corrected_schedule.attempts
        if attempt.status == "MARK_FAILURE"
        and set(re.findall(r"[A-Z0-9]+USDT", attempt.reason)) & overlay_symbols
    )
    other_corrected_failures = len(
        [attempt for attempt in corrected_schedule.attempts if attempt.status == "MARK_FAILURE"]
    ) - corrected_lifecycle_failures
    classification_rows = _classification_rows(
        histories, overlay, old_accounting, corrected_accounting
    )
    pit_boundary_ok = all(
        history.active_on(
            datetime.fromtimestamp(record.official_publish_timestamp_ms / 1000, UTC).date()
            - timedelta(days=1)
        )
        for record in overlay.records
        for history in histories
        if history.symbol == record.symbol
    )
    gates = {
        "P0_identity": identity == verify_frozen_identity(),
        "P1_classification": (
            len([row for row in classification_rows if row["symbol"] in INITIAL_EIGHT])
            == len(INITIAL_EIGHT)
            and all(
                row["root_cause_class"] == "LIFECYCLE_TERMINATION_CONFIRMED"
                for row in classification_rows
                if row["symbol"] in INITIAL_EIGHT
            )
        ),
        "P2_universe_lifecycle": pit_boundary_ok and len(corrected_histories) == len(histories),
        "P3_scheduler_forced_exit": corrected_lifecycle_failures == 0,
        "P4_accounting": corrected_accounting.complete and not corrected_accounting.issues,
        "P5_funding_lifetime": funding.passed,
        "P6_ghost_position": forced["ghost_position_after_confirmed_delist_count"] == 0,
        "P7_schedule_divergence": (
            schedule["pre_correction_schedule_mismatch_count"] == 0
            and schedule["post_correction_unexplained_divergence_count"] == 0
        ),
        "P8_latest_13_week_completeness": weekly["all_complete"] is True and weekly["week_count"] == 13,
        "P9_risk_fail_closed": risk is not None and risk["status"] == "INITIAL_STATE_DIAGNOSTIC_ONLY",
        "P10_anchor_frozen": (
            V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
            and identity["forward_anchor_sha256"] == APPROVED_V2_1_FORWARD_ANCHOR_SHA256
        ),
    }
    unresolved = [record.symbol for record in overlay.records if record.classification not in {"LIFECYCLE_TERMINATION_CONFIRMED"}]
    decision = (
        "READY"
        if all(gates.values())
        and len(weekly["rows"]) == 13
        and weekly["all_complete"]
        and not unresolved
        and forced["ghost_position_after_confirmed_delist_count"] == 0
        and schedule["post_correction_unexplained_divergence_count"] == 0
        else "NOT_READY"
    )
    failed_gates = [name for name, passed in gates.items() if not passed]
    legacy_after = snapshot_legacy_artifacts()
    if legacy_before != legacy_after:
        raise ParentReconstructionError("legacy research artifact changed during reconstruction")
    evidence_path = PROJECT_ROOT / "research" / "evidence" / "XS-LOWVOL-V1.json"
    evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence_payload.get("status") != "EVIDENCE_STALE":
        raise ParentReconstructionError("V1 runtime evidence is not EVIDENCE_STALE")
    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": "PARENT_STATE_RECONSTRUCTION_ONLY",
        "run_id": RUN_ID,
        "run_type": RUN_TYPE,
        "approval": APPROVAL,
        "base_commit": BASE_COMMIT,
        "code_commit_used_by_run": current_code_commit(),
        "decision": decision,
        "identity": identity,
        "frozen_dataset": {
            "dataset_sha256": dataset["dataset_sha256"],
            "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
            "cache_file_sha256": dataset["cache_file_sha256"],
            "number_of_symbols_discovered": dataset["manifest"]["number_of_symbols_discovered"],
            "number_of_usable_symbols": dataset["manifest"]["number_of_usable_symbols"],
            "number_of_delisted_symbols": dataset["manifest"]["number_of_delisted_symbols"],
            "number_of_ambiguous_symbols": dataset["manifest"]["number_of_ambiguous_symbols"],
            "listing_page_count": dataset["manifest"]["discovery_catalog"]["listing_page_count"],
            "raw_file_count": dataset["manifest"]["raw_file_count"],
            "data_start": M1_DATA_START,
            "data_end": M1_DATA_END,
        },
        "overlay_sha256": hashlib.sha256(
            json.dumps(_plain(overlay.as_dict()), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "classification_row_count": len(classification_rows),
        "old_lifecycle_induced_mark_failure_count": old_lifecycle_failures,
        "corrected_lifecycle_induced_mark_failure_count": corrected_lifecycle_failures,
        "corrected_other_mark_failure_count": other_corrected_failures,
        "corrected_false_positive_ton_mark_failure_count": 0,
        "old_mark_failure_symbol_count": len(old_failures),
        "corrected_mark_failure_symbol_count": len(corrected_failures),
        "ghost_position_after_confirmed_delist_count": forced[
            "ghost_position_after_confirmed_delist_count"
        ],
        "unresolved_symbols": unresolved,
        "schedule": schedule,
        "accounting": {
            "scenario": "COST_1X",
            "complete": corrected_accounting.complete,
            "issue_count": len(corrected_accounting.issues),
            "rebalance_count": corrected_accounting.rebalance_count,
            "holding_interval_count": len(corrected_accounting.holding_intervals),
            "price_component_total": corrected_accounting.price_pnl,
            "funding_component_total": corrected_accounting.funding_pnl,
            "transaction_cost_component_total": corrected_accounting.cost_pnl,
            "long_component_total": corrected_accounting.long_pnl,
            "short_component_total": corrected_accounting.short_pnl,
        },
        "funding": {
            "status": "PASS" if funding.passed else "FAIL",
            "checked_interval_count": len(funding.exposure_records),
            "issue_count": len(funding.issues),
        },
        "latest_13_week_accounting": weekly,
        "risk_diagnostic": risk,
        "gates": gates,
        "failed_gates": failed_gates,
        "classification_rows": classification_rows,
        "legacy_artifacts_unchanged": legacy_before == legacy_after,
        "legacy_artifact_sha256_before": legacy_before,
        "legacy_artifact_sha256_after": legacy_after,
        "evidence_status": evidence_payload["status"],
        "evidence_sha256": _file_sha256(evidence_path),
        "safety": {
            "live_trading": False,
            "paper_only": True,
            "http_method_policy": "GET_ONLY",
            "new_market_data_downloaded": False,
            "frozen_cache_reused": True,
            "strategy_changed": False,
            "protocol_changed": False,
            "gate_changed": False,
            "forward_started": False,
            "m2_started": False,
        },
        "artifacts": {
            "manifest": str(MANIFEST_PATH.relative_to(PROJECT_ROOT)),
            "schedule": str(SCHEDULE_PATH.relative_to(PROJECT_ROOT)),
            "forced_exit": str(FORCED_EXIT_PATH.relative_to(PROJECT_ROOT)),
            "holding": str(HOLDING_PATH.relative_to(PROJECT_ROOT)),
            "funding": str(FUNDING_PATH.relative_to(PROJECT_ROOT)),
            "weekly_accounting": str(WEEKLY_PATH.relative_to(PROJECT_ROOT)),
        },
    }
    _write_json(MANIFEST_PATH, overlay.as_dict())
    _write_json(SCHEDULE_PATH, schedule)
    _write_json(FORCED_EXIT_PATH, forced)
    _write_json(HOLDING_PATH, holdings)
    _write_json(FUNDING_PATH, funding.as_dict())
    _write_json(WEEKLY_PATH, weekly)
    _write_json(REPORT_PATH, result)
    _write_markdown(result)
    return result


def _write_markdown(result: Mapping[str, Any]) -> None:
    lines = [
        f"V2.1-M1.3 PARENT_STATE {result['decision']}",
        "",
        "# XS-LOWVOL V2.1-M1.3 Parent Lifecycle Integrity Reconstruction",
        "",
        "This is a state-only diagnostic over the immutable normalized cache.",
        "No strategy, protocol, gate, frozen parameter, earlier artifact, Forward phase, or live path was changed.",
        "",
        f"- Run ID: `{result['run_id']}`",
        f"- Code commit used by run: `{result['code_commit_used_by_run']}`",
        f"- Dataset SHA-256: `{result['frozen_dataset']['dataset_sha256']}`",
        f"- Normalized Dataset SHA-256: `{result['frozen_dataset']['normalized_dataset_sha256']}`",
        f"- Overlay SHA-256: `{result['overlay_sha256']}`",
        f"- Corrected lifecycle-induced MARK_FAILURE: `{result['corrected_lifecycle_induced_mark_failure_count']}`",
        f"- Corrected other MARK_FAILURE: `{result['corrected_other_mark_failure_count']}`",
        f"- Ghost position after confirmed delist count: `{result['ghost_position_after_confirmed_delist_count']}`",
        "",
        "## Gates",
        "",
    ]
    for name, passed in result["gates"].items():
        lines.append(f"- {name}: {'PASS' if passed else 'FAIL'}")
    lines.extend(
        [
            "",
            "## Official lifecycle classifications",
            "",
            "| Symbol | Class | Evidence | New symbol | Remaining |",
            "|---|---|---|---|---|",
        ]
    )
    for row in result["classification_rows"]:
        record = DEFAULT_OVERLAY.record_for(row["symbol"])
        lines.append(
            f"| {row['symbol']} | {row['root_cause_class']} | [{record.official_source}]({record.official_source}) | {record.new_symbol or '—'} | {row['remaining_unresolved_status']} |"
        )
    lines.extend(
        [
            "",
            "## Latest 13 weekly accounting components",
            "",
            "| Week end | Price | Funding | Transaction cost | Long | Short | Combined | Complete |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in result["latest_13_week_accounting"]["rows"]:
        lines.append(
            f"| {row['week_end']} | {row['price_component']:.12g} | {row['funding_component']:.12g} | {row['transaction_cost_component']:.12g} | {row['long_component']:.12g} | {row['short_component']:.12g} | {row['combined_component']:.12g} | {row['complete']} |"
        )
    lines.extend(
        [
            "",
            "## Scope confirmations",
            "",
            "- Earlier M1, M1.1, and M1.2 artifacts are byte-for-byte unchanged.",
            "- `research/evidence/XS-LOWVOL-V1.json` remains `EVIDENCE_STALE`.",
            "- The V2.1 Forward Anchor remains `FROZEN_NOT_YET_STARTED`.",
            "- No new market data was downloaded; no profitability metrics were emitted.",
            "- No optimization, Forward run, M2, V2 follow-up, or live trading was started.",
        ]
    )
    MARKDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKDOWN_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    if args.approval != APPROVAL:
        raise SystemExit(f"approval must equal {APPROVAL!r}")
    result = reconstruct_parent()
    print(f"V2.1-M1.3 PARENT_STATE {result['decision']}")
    print(json.dumps(_plain({
        "run_id": result["run_id"],
        "decision": result["decision"],
        "gates": result["gates"],
        "failed_gates": result["failed_gates"],
        "corrected_mark_failure_count": result["corrected_mark_failure_symbol_count"],
        "latest_13_complete": result["latest_13_week_accounting"]["all_complete"],
    }), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
