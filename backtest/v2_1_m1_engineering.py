"""XS-LOWVOL V2.1 contaminated engineering replay.

The runner verifies the repaired V2.1 implementation against the already
frozen V1 point-in-time scheduler and normalized historical cache.  Its output
is limited to implementation traces and data-quality diagnostics.  It does
not create Forward evidence, aggregate historical strategy results, or alter
the frozen Strategy Spec, Protocol, or Anchor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import re
import statistics
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.xs_lowvol_spec import (
    resolve_rules,
    strategy_spec_hash as v1_strategy_spec_hash,
)
from src.xs_lowvol_v2_1_anchor import (
    APPROVED_V1_CONTROL_SHA256,
    APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_1_M0_COMMIT,
    APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC,
    APPROVED_V2_1_PROTOCOL_SHA256,
    APPROVED_V2_1_SPEC_SHA256,
    V21_FORWARD_ANCHOR_STATUS,
    validate_v2_1_forward_anchor,
    verify_v2_1_forward_anchor_hash,
)
from src.xs_lowvol_v2_1_risk import (
    ControlWeeklyReturn,
    DATA_INTEGRITY_HALT,
    PositionState,
    V21DataIntegrityHalt,
    V2_DATA_INTEGRITY_HALT,
    calculate_position_scale,
    completed_control_weekly_returns,
    evaluate_risk_scale,
    plan_position_changes,
    reference_annualized_volatility,
    scaled_target_positions,
)
from src.xs_lowvol_v2_1_spec import V21_STRATEGY_ID, verify_v2_1_spec_hash
from src.xs_lowvol_v2_risk import (
    V2RiskScaleInvalid,
    calculate_position_scale as old_v2_calculate_position_scale,
)

from .m1_b import (
    M1_DATA_END,
    M1_DATA_START,
    _signal_from_universe,
    build_stateful_schedule,
    simulate_frozen_portfolio,
)
from .m1_protocol import M1_APPROVED_PROTOCOL_SHA256, verify_protocol_hash
from .v2_1_protocol import verify_v2_1_protocol_hash
from .v2_1_forward import (
    INSUFFICIENT_UNIVERSE,
    MISSING_EXECUTION_PRICE,
    NO_SIGNAL,
    SUCCESS,
    TRANSITION_FAILURE,
    V21PairedEngine,
    validate_v2_1_temporal_inputs,
)
from .xs_data_quality import FundingCoverageReport, HoldingInterval, validate_funding_coverage_for_holds


PROJECT_ROOT = Path(__file__).resolve().parent.parent
V21_M1_BASE_COMMIT = "abf7338ff4af25e5fd0a12c1eade8a12a02667db"
V2_1_M1_BASE_COMMIT = V21_M1_BASE_COMMIT
V21_M1_APPROVAL = "START V2.1-M1"
V2_1_M1_APPROVAL = V21_M1_APPROVAL
V21_M1_RUN_ID = "XS-LOWVOL-V2.1-M1-ENGINEERING-1"
V2_1_M1_RUN_ID = V21_M1_RUN_ID
V21_M1_OUTPUT_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1"
V21_M1_REPORT_PATH = V21_M1_OUTPUT_DIR / "V2_1_M1_ENGINEERING_REPORT.json"
V21_M1_MARKDOWN_PATH = V21_M1_OUTPUT_DIR / "V2_1_M1_ENGINEERING_REPORT.md"
V21_M1_TRACE_MANIFEST_PATH = V21_M1_OUTPUT_DIR / "V2_1_M1_TRACE_MANIFEST.json"
V2_1_M1_REPORT_PATH = V21_M1_REPORT_PATH
V2_1_M1_MARKDOWN_PATH = V21_M1_MARKDOWN_PATH
V2_1_M1_TRACE_MANIFEST_PATH = V21_M1_TRACE_MANIFEST_PATH
M1_DATASET_FREEZE_PATH = (
    PROJECT_ROOT / "backtest" / "m1_cache" / "normalized" / "XS_LOWVOL_M1_DATASET.pkl"
)
EXPECTED_DATASET_SHA256 = (
    "f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755"
)
EXPECTED_NORMALIZED_DATASET_SHA256 = (
    "6adf9f06cba3a9de377ed59f69f422320b1524f635512271ebee60516896c387"
)
UTC = timezone.utc

_PARENT_MARK_FAILURE = "MARK_FAILURE"
_PARENT_STATUS_TO_V21 = {
    _PARENT_MARK_FAILURE: MISSING_EXECUTION_PRICE,
}
_PARENT_FAILURE_STATUSES = frozenset(
    {
        NO_SIGNAL,
        INSUFFICIENT_UNIVERSE,
        MISSING_EXECUTION_PRICE,
        TRANSITION_FAILURE,
        _PARENT_MARK_FAILURE,
    }
)
_FORBIDDEN_SCHEMA_KEYS = frozenset(
    {
        "return",
        "cagr",
        "sharpe",
        "sortino",
        "max_drawdown",
        "profit_factor",
        "annual_return",
        "monthly_return",
        "bootstrap_performance",
        "best_week_removal_performance",
        "best_week_removed_return",
        "historical_profitability",
        "pnl",
        "performance",
    }
)


class EngineeringReplayError(RuntimeError):
    """A fail-closed V2.1 engineering replay error."""


class EngineeringIdentityError(EngineeringReplayError):
    """A frozen implementation or dataset identity is not approved."""


class EngineeringProducerError(EngineeringReplayError):
    """The V1 accounting producer emitted invalid weekly-period provenance."""


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        return sorted(_canonical(item) for item in value)
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_code_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EngineeringReplayError("cannot determine code commit") from exc
    commit = result.stdout.strip()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise EngineeringReplayError("current code commit is not a full SHA-1")
    return commit


def _base_commit_is_ancestor(base_commit: str, code_commit: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_commit, code_commit],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise EngineeringReplayError("cannot verify the approved M0.2 base commit") from exc
    if result.returncode not in (0, 1):
        raise EngineeringReplayError("git could not verify the approved M0.2 base commit")
    return result.returncode == 0


def verify_frozen_identity() -> dict[str, str]:
    """Verify all V1/V2.1 identities before loading the historical cache."""
    try:
        control = v1_strategy_spec_hash()
        v2_1_spec = verify_v2_1_spec_hash()
        v2_1_protocol = verify_v2_1_protocol_hash()
        validate_v2_1_forward_anchor()
        anchor = verify_v2_1_forward_anchor_hash()
        dataset_protocol = verify_protocol_hash()
    except Exception as exc:  # noqa: BLE001 - identity must fail closed
        raise EngineeringIdentityError(f"E0_identity failed: {exc}") from exc
    actual = {
        "v1_control_sha256": control,
        "v2_1_spec_sha256": v2_1_spec,
        "v2_1_protocol_sha256": v2_1_protocol,
        "forward_anchor_sha256": anchor,
        "dataset_protocol_sha256": dataset_protocol,
    }
    expected = {
        "v1_control_sha256": APPROVED_V1_CONTROL_SHA256,
        "v2_1_spec_sha256": APPROVED_V2_1_SPEC_SHA256,
        "v2_1_protocol_sha256": APPROVED_V2_1_PROTOCOL_SHA256,
        "forward_anchor_sha256": APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
        "dataset_protocol_sha256": M1_APPROVED_PROTOCOL_SHA256,
    }
    if actual != expected:
        raise EngineeringIdentityError(
            f"E0_identity failed: expected={expected}, actual={actual}"
        )
    return actual


def _schema_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _performance_schema_violations(value: Any, path: str = "") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            key_path = f"{path}.{key_text}" if path else key_text
            if _schema_key(key_text) in _FORBIDDEN_SCHEMA_KEYS:
                violations.append(key_path)
            violations.extend(_performance_schema_violations(item, key_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            violations.extend(_performance_schema_violations(item, f"{path}[{index}]"))
    return violations


def assert_no_performance_fields(value: Mapping[str, Any]) -> None:
    """Reject aggregate historical-result fields from an M1 artifact."""
    violations = _performance_schema_violations(value)
    if violations:
        raise EngineeringReplayError(
            "forbidden historical result fields in V2.1-M1 artifact: "
            + ", ".join(violations[:10])
        )


def load_verified_dataset(path: str | Path = M1_DATASET_FREEZE_PATH) -> dict[str, Any]:
    """Load only the existing verified pickle; never download or normalize."""
    cache_path = Path(path)
    if not cache_path.is_file():
        raise EngineeringIdentityError(f"verified normalized dataset cache is missing: {cache_path}")
    try:
        with cache_path.open("rb") as handle:
            data = pickle.load(handle)
    except (OSError, pickle.PickleError, EOFError, AttributeError, ImportError) as exc:
        raise EngineeringIdentityError("cannot load the verified normalized dataset cache") from exc
    if not isinstance(data, Mapping):
        raise EngineeringIdentityError("verified dataset cache top level is not a mapping")
    if data.get("dataset_sha256") != EXPECTED_DATASET_SHA256:
        raise EngineeringIdentityError("dataset SHA-256 does not match the approved frozen cache")
    if data.get("normalized_dataset_sha256") != EXPECTED_NORMALIZED_DATASET_SHA256:
        raise EngineeringIdentityError("normalized dataset SHA-256 does not match the approved frozen cache")
    catalog = data.get("catalog")
    histories = tuple(data.get("histories", ()))
    usable_histories = tuple(data.get("usable_histories", ()))
    manifest = data.get("manifest")
    if catalog is None or catalog.listing_complete is not True:
        raise EngineeringIdentityError("dataset catalog is not complete")
    if not histories or not usable_histories or not isinstance(manifest, Mapping):
        raise EngineeringIdentityError("verified dataset cache is structurally incomplete")
    if manifest.get("dataset_sha256") != EXPECTED_DATASET_SHA256:
        raise EngineeringIdentityError("dataset manifest SHA-256 does not match the approved cache")
    if manifest.get("normalized_dataset_sha256") != EXPECTED_NORMALIZED_DATASET_SHA256:
        raise EngineeringIdentityError("normalized dataset manifest SHA-256 does not match the approved cache")
    if manifest.get("protocol_hash") != M1_APPROVED_PROTOCOL_SHA256:
        raise EngineeringIdentityError("dataset was not produced under the approved V1 M1 protocol")
    return {
        "catalog": catalog,
        "histories": histories,
        "usable_histories": usable_histories,
        "manifest": manifest,
        "dataset_sha256": str(data["dataset_sha256"]),
        "normalized_dataset_sha256": str(data["normalized_dataset_sha256"]),
        "cache_file_sha256": _file_sha256(cache_path),
    }


def dataset_metadata(dataset: Mapping[str, Any]) -> dict[str, Any]:
    manifest = dataset["manifest"]
    discovery = manifest.get("discovery_catalog", {})
    return {
        "dataset_sha256": dataset["dataset_sha256"],
        "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
        "cache_file_sha256": dataset["cache_file_sha256"],
        "protocol_hash": manifest["protocol_hash"],
        "number_of_symbols_discovered": int(manifest["number_of_symbols_discovered"]),
        "number_of_usable_symbols": int(manifest["number_of_usable_symbols"]),
        "number_of_delisted_symbols": int(manifest["number_of_delisted_symbols"]),
        "number_of_ambiguous_symbols": int(manifest["number_of_ambiguous_symbols"]),
        "listing_page_count": int(discovery["listing_page_count"]),
        "raw_file_count": int(manifest["raw_file_count"]),
        "daily_bar_count": int(manifest["daily_bar_count"]),
        "funding_event_count": int(manifest["funding_event_count"]),
        "first_available_date": str(manifest["first_available_date"]),
        "last_available_date": str(manifest["last_available_date"]),
        "catalog_complete": dataset["catalog"].listing_complete is True,
    }


def _weekly_inputs(accounting: Any) -> tuple[ControlWeeklyReturn, ...]:
    """Convert actual V1 weekly accounting rows into completed observations."""
    rows: list[ControlWeeklyReturn] = []
    seen_week_ending: set[date] = set()
    for row in accounting.weekly_rows:
        week_ending = date.fromisoformat(str(row["week_end"]))
        if week_ending in seen_week_ending:
            raise EngineeringProducerError(
                "ENGINEERING PRODUCER ERROR: duplicate week_ending "
                f"{week_ending.isoformat()} in V1 Control weekly observations"
            )
        seen_week_ending.add(week_ending)
        rows.append(
            ControlWeeklyReturn(
                week_ending=week_ending,
                net_return=float(row["return"]),
                completed_at=datetime.combine(week_ending, time.max, tzinfo=UTC),
                complete=True,
            )
        )
    if not rows:
        raise EngineeringProducerError(
            "ENGINEERING PRODUCER ERROR: V1 Control produced no weekly observations"
        )
    return tuple(rows)


def _logical_signal_time(execution_day: date) -> datetime:
    return datetime.combine(execution_day, time.min, tzinfo=UTC)


def _target_directions_for_attempt(cache: Any, attempt: Any, rules: Any) -> dict[str, int]:
    if attempt.status == NO_SIGNAL:
        return {}
    signal = cache.signals.get(attempt.signal_day)
    if signal is None:
        universe = cache.universes.get(attempt.signal_day)
        signal = _signal_from_universe(universe, rules) if universe is not None else None
    if signal is None:
        return {}
    return {
        **{str(symbol).upper(): 1 for symbol in signal.longs},
        **{str(symbol).upper(): -1 for symbol in signal.shorts},
    }


def _target_tuple(targets: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((str(symbol).upper(), int(direction)) for symbol, direction in targets.items()))


def _expected_v2_status(parent_status: str) -> str:
    return _PARENT_STATUS_TO_V21.get(parent_status, parent_status)


def _retry_diagnostics(attempts: Sequence[Any], data_end: date) -> dict[str, int]:
    retry_count = 0
    retry_boundary_violations = 0
    for index, attempt in enumerate(attempts):
        if attempt.status not in _PARENT_FAILURE_STATUSES:
            continue
        next_attempt = attempts[index + 1] if index + 1 < len(attempts) else None
        if next_attempt is not None and next_attempt.execution_day == attempt.execution_day + timedelta(days=1):
            retry_count += 1
        elif attempt.execution_day < data_end:
            retry_boundary_violations += 1
    return {
        "parent_retry_attempt_count": retry_count,
        "parent_retry_boundary_violation_count": retry_boundary_violations,
        "v21_independent_retry_count": 0,
    }


def paired_schedule_diagnostics(
    parent_attempts: Sequence[Any],
    v21_attempts: Sequence[Any | None],
    expected_target_directions: Sequence[tuple[tuple[str, int], ...]],
    expected_statuses: Sequence[str],
) -> dict[str, Any]:
    """Compare the V2.1 trace with the exact parent-Control schedule."""
    divergence_count = 0
    schedule_mismatch_count = 0
    target_mismatch_count = 0
    parent_status_mismatch_count = 0
    successful_rebalance_mismatch_count = 0
    first_divergence: dict[str, Any] | None = None
    lengths_match = len(parent_attempts) == len(v21_attempts) == len(expected_target_directions) == len(expected_statuses)
    if not lengths_match:
        return {
            "paired_schedule_divergence_count": 1,
            "parent_schedule_mismatch_count": 1,
            "target_mismatch_count": 1,
            "parent_status_mismatch_count": 1,
            "successful_rebalance_mismatch_count": 1,
            "first_divergence": {"reason": "trace lengths differ"},
        }
    for index, (parent, actual, expected_targets, expected_status) in enumerate(
        zip(parent_attempts, v21_attempts, expected_target_directions, expected_statuses)
    ):
        reasons: list[str] = []
        if actual is None:
            reasons.append("V2.1 attempt missing")
            schedule_mismatch_count += 1
            target_mismatch_count += 1
            parent_status_mismatch_count += 1
        else:
            if actual.signal_day != parent.signal_day or actual.execution_day != parent.execution_day:
                schedule_mismatch_count += 1
                reasons.append("signal/execution day mismatch")
            if actual.status != expected_status:
                schedule_mismatch_count += 1
                reasons.append(f"status {expected_status} != {actual.status}")
            if actual.parent_status != expected_status:
                parent_status_mismatch_count += 1
                reasons.append(f"parent status {expected_status} != {actual.parent_status}")
            if tuple(actual.target_directions) != expected_targets:
                target_mismatch_count += 1
                reasons.append("target direction mismatch")
            if (parent.status == SUCCESS) != (actual.status == SUCCESS):
                successful_rebalance_mismatch_count += 1
                reasons.append("successful rebalance mismatch")
        if reasons:
            divergence_count += 1
            if first_divergence is None:
                first_divergence = {
                    "index": index,
                    "signal_day": parent.signal_day.isoformat(),
                    "execution_day": parent.execution_day.isoformat(),
                    "parent_status": parent.status,
                    "expected_v2_1_status": expected_status,
                    "v2_1_status": actual.status if actual is not None else None,
                    "reason": "; ".join(reasons),
                }
    return {
        "paired_schedule_divergence_count": divergence_count,
        "parent_schedule_mismatch_count": schedule_mismatch_count,
        "target_mismatch_count": target_mismatch_count,
        "parent_status_mismatch_count": parent_status_mismatch_count,
        "successful_rebalance_mismatch_count": successful_rebalance_mismatch_count,
        "first_divergence": first_divergence,
    }


def _transition_diagnostics(
    old_positions: Mapping[str, PositionState],
    new_targets: Mapping[str, Any],
    changes: Sequence[Any],
) -> tuple[set[str], list[str]]:
    """Check open/close/resize/flip accounting without aggregating results."""
    cases: set[str] = set()
    violations: list[str] = []
    grouped: dict[str, list[Any]] = {}
    for change in changes:
        grouped.setdefault(str(change.symbol).upper(), []).append(change)
    old_symbols = {str(symbol).upper() for symbol in old_positions}
    new_symbols = {str(symbol).upper() for symbol in new_targets}

    def close_is_exact(change: Any, position: PositionState) -> bool:
        return (
            change.action == "CLOSE"
            and change.direction == position.direction
            and math.isclose(change.notional, position.notional, rel_tol=0.0, abs_tol=1e-15)
            and math.isclose(change.notional_delta, -position.notional, rel_tol=0.0, abs_tol=1e-15)
            and math.isclose(change.cost_notional, position.notional, rel_tol=0.0, abs_tol=1e-15)
        )

    for symbol in sorted(old_symbols | new_symbols):
        prior = old_positions.get(symbol)
        target = new_targets.get(symbol)
        actions = grouped.get(symbol, [])
        names = [item.action for item in actions]
        if prior is None:
            if target is None:
                if actions:
                    violations.append(f"unexpected transition {symbol}")
                continue
            if names != ["OPEN"]:
                violations.append(f"new symbol is not OPEN only {symbol}")
                continue
            change = actions[0]
            if not (
                change.direction == target.direction
                and change.notional > 0.0
                and math.isclose(change.notional_delta, change.notional, rel_tol=0.0, abs_tol=1e-15)
                and math.isclose(change.cost_notional, change.notional, rel_tol=0.0, abs_tol=1e-15)
            ):
                violations.append(f"OPEN accounting mismatch {symbol}")
            else:
                cases.add("open")
            continue
        if target is None:
            if names != ["CLOSE"] or not close_is_exact(actions[0], prior):
                violations.append(f"removed symbol is not CLOSE only {symbol}")
            else:
                cases.add("close")
            continue
        if prior.direction != target.direction:
            if names != ["CLOSE", "OPEN"] or not close_is_exact(actions[0], prior):
                violations.append(f"direction flip is not CLOSE+OPEN {symbol}")
            elif not (
                actions[1].direction == target.direction
                and actions[1].notional > 0.0
                and math.isclose(actions[1].notional_delta, actions[1].notional, rel_tol=0.0, abs_tol=1e-15)
                and math.isclose(actions[1].cost_notional, actions[1].notional, rel_tol=0.0, abs_tol=1e-15)
            ):
                violations.append(f"direction flip OPEN accounting mismatch {symbol}")
            else:
                cases.add("direction_flip")
            continue
        if not actions:
            continue
        if names != ["RESIZE"]:
            violations.append(f"same-side transition fabricated close/open {symbol}")
            continue
        change = actions[0]
        expected_delta = abs(change.notional_delta)
        if not (
            change.direction == prior.direction
            and math.isclose(change.notional, expected_delta, rel_tol=0.0, abs_tol=1e-15)
            and math.isclose(change.cost_notional, expected_delta, rel_tol=0.0, abs_tol=1e-15)
        ):
            violations.append(f"RESIZE accounting mismatch {symbol}")
        elif change.notional_delta > 0:
            cases.add("same_side_increase_resize")
        elif change.notional_delta < 0:
            cases.add("same_side_decrease_resize")
    if old_symbols - new_symbols and new_symbols - old_symbols:
        cases.add("symbol_replacement")
    return cases, violations


def _synthetic_transition_fixture() -> dict[str, Any]:
    old = {
        "INC": PositionState("INC", 1, 0.4),
        "DEC": PositionState("DEC", -1, 0.8),
        "FLIP": PositionState("FLIP", 1, 0.5),
        "OLD": PositionState("OLD", 1, 0.4),
    }
    new = scaled_target_positions(
        {"INC": 1, "DEC": -1, "FLIP": -1, "NEW": 1},
        base_notional=1.0,
        position_scale=0.6,
    )
    changes = plan_position_changes(old, new)
    cases, violations = _transition_diagnostics(old, new, changes)
    expected_cases = {
        "open",
        "close",
        "same_side_increase_resize",
        "same_side_decrease_resize",
        "direction_flip",
        "symbol_replacement",
    }
    return {
        "valid": not violations and expected_cases.issubset(cases),
        "cases": sorted(cases),
        "change_count": len(changes),
        "violations": violations,
    }


def _weekly_fixture(
    values: Sequence[float] | None = None,
    *,
    start: date = date(2019, 1, 6),
) -> tuple[ControlWeeklyReturn, ...]:
    values = tuple([0.0] * 13 if values is None else values)
    return tuple(
        ControlWeeklyReturn(
            week_ending=start + timedelta(days=7 * index),
            net_return=float(value),
            completed_at=datetime.combine(
                start + timedelta(days=7 * index), time.max, tzinfo=UTC
            ),
            complete=True,
        )
        for index, value in enumerate(values)
    )


def _zero_vol_regression() -> dict[str, Any]:
    signal_day = date(2020, 8, 13)
    execution_day = date(2020, 8, 14)
    logical_signal_time = _logical_signal_time(execution_day)
    records = _weekly_fixture()
    targets = {"BTCUSDT": 1, "ETHUSDT": -1}
    old_result = "UNEXPECTED_SUCCESS"
    try:
        old_v2_calculate_position_scale(records, signal_time=logical_signal_time)
    except V2RiskScaleInvalid as exc:
        old_result = exc.code
    v21 = V21PairedEngine(base_notional=1.0).process_parent_attempt(
        signal_day=signal_day,
        execution_day=execution_day,
        control_targets=targets,
        control_weekly_returns=records,
        parent_status=SUCCESS,
        signal_time=logical_signal_time,
    )
    expected_targets = _target_tuple(targets)
    passed = (
        old_result == "V2_RISK_SCALE_INVALID"
        and v21.status == SUCCESS
        and v21.position_scale == 1.0
        and v21.signal_day == signal_day
        and v21.execution_day == execution_day
        and tuple(v21.target_directions) == expected_targets
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "old_v2_zero_vol_result": old_result,
        "v2_1_result": v21.status,
        "v2_1_reference_vol": 0.0,
        "v2_1_position_scale": v21.position_scale,
        "same_signal_day": v21.signal_day == signal_day,
        "same_execution_day": v21.execution_day == execution_day,
        "same_targets": tuple(v21.target_directions) == expected_targets,
    }


def _terminal_halt_diagnostics() -> dict[str, Any]:
    signal_day = date(2020, 8, 13)
    execution_day = date(2020, 8, 14)
    signal_time = _logical_signal_time(execution_day)
    valid = _weekly_fixture()
    incomplete = replace(valid[-1], complete=False)
    nan_value = replace(valid[-1], net_return=math.nan)
    inf_value = replace(valid[-1], net_return=math.inf)
    missing_time = replace(valid[-1], completed_at=None)
    naive_time = replace(valid[-1], completed_at=datetime(2019, 4, 7, 23, 59, 59))
    cases: dict[str, Sequence[ControlWeeklyReturn]] = {
        "insufficient_13_rows": valid[:12],
        "required_incomplete": (*valid[:-1], incomplete),
        "required_nan": (*valid[:-1], nan_value),
        "required_inf": (*valid[:-1], inf_value),
        "missing_completed_at": (*valid[:-1], missing_time),
        "naive_completed_at": (*valid[:-1], naive_time),
    }
    rows: dict[str, Any] = {}
    all_halted = True
    all_terminal = True
    for name, records in cases.items():
        engine = V21PairedEngine(base_notional=1.0)
        first = engine.process_parent_attempt(
            signal_day=signal_day,
            execution_day=execution_day,
            control_targets={"BTCUSDT": 1},
            control_weekly_returns=records,
            parent_status=SUCCESS,
            signal_time=signal_time,
        )
        second = engine.process_parent_attempt(
            signal_day=execution_day,
            execution_day=execution_day + timedelta(days=1),
            control_targets={"BTCUSDT": 1},
            control_weekly_returns=valid,
            parent_status=SUCCESS,
            signal_time=_logical_signal_time(execution_day + timedelta(days=1)),
        )
        case_ok = first.status == V2_DATA_INTEGRITY_HALT
        terminal_ok = second.status == V2_DATA_INTEGRITY_HALT and engine.halted and engine.positions == {}
        all_halted = all_halted and case_ok
        all_terminal = all_terminal and terminal_ok
        rows[name] = {
            "first_status": first.status,
            "second_status_after_recovery_input": second.status,
            "halted": engine.halted,
            "positions_unchanged": engine.positions == {},
        }
    return {
        "status": "PASS" if all_halted and all_terminal else "FAIL",
        "cases": rows,
        "all_required_inputs_halted": all_halted,
        "terminal_halt_cannot_recover": all_terminal,
        "v2_specific_retry_created": False,
    }


def _run_no_lookahead_mutations(
    weekly_inputs: Sequence[ControlWeeklyReturn],
    signal_time: datetime,
    baseline_scale: float,
) -> dict[str, int]:
    mutations = (
        ("future_plus_90", 90.0, True),
        ("future_minus_90", -90.0, True),
        ("future_nan", math.nan, True),
        ("future_inf", math.inf, True),
        ("future_incomplete", 0.0, False),
    )
    violation_count = 0
    for _, value, complete in mutations:
        future = ControlWeeklyReturn(
            week_ending=signal_time.date(),
            net_return=value,
            completed_at=signal_time + timedelta(seconds=1),
            complete=complete,
        )
        try:
            mutated_scale = calculate_position_scale(
                tuple(weekly_inputs) + (future,), signal_time=signal_time
            )
        except (V21DataIntegrityHalt, TypeError, ValueError, OverflowError):
            violation_count += 1
            continue
        if not math.isclose(mutated_scale, baseline_scale, rel_tol=0.0, abs_tol=1e-15):
            violation_count += 1

    old_unused = ControlWeeklyReturn(
        week_ending=date(2010, 1, 3),
        net_return=math.nan,
        completed_at=datetime(2010, 1, 3, 23, 59, 59, tzinfo=UTC),
        complete=False,
    )
    try:
        old_mutated_scale = calculate_position_scale(
            (old_unused, *tuple(weekly_inputs)), signal_time=signal_time
        )
    except (V21DataIntegrityHalt, TypeError, ValueError, OverflowError):
        violation_count += 1
    else:
        if not math.isclose(old_mutated_scale, baseline_scale, rel_tol=0.0, abs_tol=1e-15):
            violation_count += 1
    return {
        "mutation_count": len(mutations),
        "old_non_required_mutation_count": 1,
        "violation_count": violation_count,
    }


def _funding_diagnostics(
    coverage: FundingCoverageReport,
    holding_intervals: Sequence[HoldingInterval],
) -> dict[str, Any]:
    hold_issues = tuple(coverage.hold_issues)
    issue_rows = [
        {
            "code": issue.code,
            "symbol": issue.symbol,
            "detail": issue.detail,
            "day": issue.day.isoformat() if issue.day else None,
        }
        for issue in sorted(
            hold_issues,
            key=lambda item: (item.symbol, item.code, item.detail, item.day or date.min),
        )
    ]
    affected_symbols = sorted({issue.symbol.upper() for issue in hold_issues})
    affected_intervals = [
        asdict(interval)
        for interval in holding_intervals
        if interval.symbol.upper() in affected_symbols
    ]
    return {
        "funding_integrity_issue_count": len(hold_issues),
        "structural_issue_count": len(coverage.structural_issues),
        "affected_symbols": affected_symbols,
        "affected_holding_interval_count": len(affected_intervals),
        "first_issue": issue_rows[0] if issue_rows else None,
        "last_issue": issue_rows[-1] if issue_rows else None,
        "issues": issue_rows,
        "no_synthetic_funding": True,
        "provenance_status": "CONTAMINATED_DATA_QUALITY_DIAGNOSTIC",
        "fail_closed": all(issue.code == "FUNDING_COVERAGE_GAP" for issue in hold_issues),
    }


def _warm_start_diagnostics(
    weekly_inputs: Sequence[ControlWeeklyReturn],
    coverage: FundingCoverageReport,
) -> dict[str, Any]:
    candidates = tuple(
        record
        for record in weekly_inputs
        if record.completion_time < APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC
    )
    selected = tuple(
        sorted(candidates, key=lambda record: (record.completion_time, record.week_ending))[-13:]
    )
    ready = len(selected) == 13 and len({record.week_ending for record in selected}) == 13 and all(
        record.complete for record in selected
    )
    provenance = [
        {
            "week_ending": record.week_ending.isoformat(),
            "completed_at": record.completion_time.astimezone(UTC).isoformat(),
            "complete": record.complete,
            "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        }
        for record in selected
    ]
    result: dict[str, Any] = {
        "status": "READY" if ready else "NOT_READY",
        "required_observation_count": 13,
        "available_observation_count": len(selected),
        "provenance": provenance,
        "initial_state_diagnostic_status": "INITIAL_STATE_DIAGNOSTIC_ONLY",
        "evidence_status": "NOT_PERFORMANCE_EVIDENCE",
        "historical_funding_issue_count": len(coverage.hold_issues),
    }
    if ready:
        result["initial_state"] = {
            "reference_vol": reference_annualized_volatility(
                selected,
                signal_time=APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC,
            ),
            "position_scale": calculate_position_scale(
                selected,
                signal_time=APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC,
            ),
        }
    return result


def _scale_behavior(rows: Sequence[Mapping[str, Any]], halt_count: int) -> dict[str, Any]:
    scales = [float(row["scale"]) for row in rows if row.get("status") == "VALID"]
    reference_vols = [float(row["reference_vol"]) for row in rows if row.get("status") == "VALID"]
    zero_count = sum(math.isclose(value, 0.0, rel_tol=0.0, abs_tol=1e-15) for value in reference_vols)
    return {
        "valid_scale_count": len(scales),
        "zero_reference_vol_count": zero_count,
        "positive_reference_vol_count": sum(value > 0.0 for value in reference_vols),
        "data_integrity_halt_count": halt_count,
        "scale_min": min(scales) if scales else None,
        "scale_median": statistics.median(scales) if scales else None,
        "scale_mean": statistics.mean(scales) if scales else None,
        "scale_max": max(scales) if scales else None,
        "fraction_scale_eq_1": (
            sum(math.isclose(value, 1.0, rel_tol=0.0, abs_tol=1e-15) for value in scales) / len(scales)
            if scales
            else None
        ),
        "fraction_scale_lt_1": (
            sum(value < 1.0 for value in scales) / len(scales) if scales else None
        ),
    }


def _render_markdown(result: Mapping[str, Any]) -> str:
    summary = {
        key: value
        for key, value in result.items()
        if key not in {"execution_trace", "risk_scale_trace", "transition_trace"}
    }
    lines = [
        "# XS-LOWVOL V2.1-M1 Contaminated Engineering Replay",
        "",
        f"## {result['final_decision']}",
        "",
        "Classification: `CONTAMINATED_DEVELOPMENT_DATA` / `NOT_PERFORMANCE_EVIDENCE` / `NOT_OOS` / `NOT_FORWARD`.",
        "",
        "This report contains implementation traces and data-quality diagnostics only.",
        "",
        "## Frozen identity",
        "",
        f"- Base commit: `{result['base_commit']}`",
        f"- Code commit: `{result['code_commit']}`",
        f"- Run ID: `{result['run_id']}`",
        f"- V1 Control SHA: `{result['frozen_identity']['v1_control_sha256']}`",
        f"- V2.1 Spec SHA: `{result['frozen_identity']['v2_1_spec_sha256']}`",
        f"- V2.1 Protocol SHA: `{result['frozen_identity']['v2_1_protocol_sha256']}`",
        f"- Forward Anchor SHA: `{result['frozen_identity']['forward_anchor_sha256']}`",
        f"- Dataset SHA: `{result['dataset']['dataset_sha256']}`",
        f"- Normalized Dataset SHA: `{result['dataset']['normalized_dataset_sha256']}`",
        "",
        "## E0-E10",
        "",
    ]
    for name, value in result["gates"].items():
        lines.append(f"- `{name}`: `{value}`")
    lines.extend(
        [
            "",
            "## Diagnostic summary",
            "",
            "```json",
            json.dumps(_canonical(summary), ensure_ascii=False, indent=2),
            "```",
            "",
            "Trace rows are engineering provenance. No Forward evidence was created.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_artifacts(result: Mapping[str, Any]) -> dict[str, Path]:
    output_dir = V21_M1_OUTPUT_DIR.resolve()
    targets = (V21_M1_REPORT_PATH, V21_M1_MARKDOWN_PATH, V21_M1_TRACE_MANIFEST_PATH)
    existing = [target for target in targets if target.exists()]
    if existing:
        raise EngineeringReplayError(
            "refusing to overwrite existing V2.1-M1 artifacts: "
            + ", ".join(str(target) for target in existing)
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    result_with_paths = dict(result)
    result_with_paths["artifacts"] = {
        "report": "research/v2_1/m1/V2_1_M1_ENGINEERING_REPORT.json",
        "markdown": "research/v2_1/m1/V2_1_M1_ENGINEERING_REPORT.md",
        "trace_manifest": "research/v2_1/m1/V2_1_M1_TRACE_MANIFEST.json",
    }
    assert_no_performance_fields(result_with_paths)
    V21_M1_REPORT_PATH.write_text(
        json.dumps(_canonical(result_with_paths), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    V21_M1_MARKDOWN_PATH.write_text(
        _render_markdown(result_with_paths), encoding="utf-8"
    )
    manifest: dict[str, Any] = {
        "schema_version": "V2.1-M1-ENGINEERING-TRACE-MANIFEST.v1",
        "run_type": "CONTAMINATED_ENGINEERING_REPLAY",
        "classification": [
            "CONTAMINATED_DEVELOPMENT_DATA",
            "NOT_PERFORMANCE_EVIDENCE",
            "NOT_OOS",
            "NOT_FORWARD",
        ],
        "base_commit": result["base_commit"],
        "code_commit": result["code_commit"],
        "run_id": result["run_id"],
        "dataset_sha256": result["dataset"]["dataset_sha256"],
        "normalized_dataset_sha256": result["dataset"]["normalized_dataset_sha256"],
        "forward_anchor_status": V21_FORWARD_ANCHOR_STATUS,
        "forward_evidence_created": False,
        "trace_files": {
            "execution_trace": {
                "storage": "embedded_in_V2_1_M1_ENGINEERING_REPORT.json",
                "count": len(result["execution_trace"]),
                "sha256": _sha256(result["execution_trace"]),
            },
            "risk_scale_trace": {
                "storage": "embedded_in_V2_1_M1_ENGINEERING_REPORT.json",
                "count": len(result["risk_scale_trace"]),
                "sha256": _sha256(result["risk_scale_trace"]),
            },
            "transition_trace": {
                "storage": "embedded_in_V2_1_M1_ENGINEERING_REPORT.json",
                "count": len(result["transition_trace"]),
                "sha256": _sha256(result["transition_trace"]),
            },
        },
        "artifacts": {
            "report": {
                "path": "research/v2_1/m1/V2_1_M1_ENGINEERING_REPORT.json",
                "sha256": _file_sha256(V21_M1_REPORT_PATH),
            },
            "markdown": {
                "path": "research/v2_1/m1/V2_1_M1_ENGINEERING_REPORT.md",
                "sha256": _file_sha256(V21_M1_MARKDOWN_PATH),
            },
        },
    }
    assert_no_performance_fields(manifest)
    V21_M1_TRACE_MANIFEST_PATH.write_text(
        json.dumps(_canonical(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "report": V21_M1_REPORT_PATH,
        "markdown": V21_M1_MARKDOWN_PATH,
        "trace_manifest": V21_M1_TRACE_MANIFEST_PATH,
    }


def run_engineering_replay(*, approval: str) -> dict[str, Any]:
    """Run the one approved V2.1-M1 engineering replay from the frozen cache."""
    if approval != V21_M1_APPROVAL:
        raise EngineeringReplayError(
            f"engineering replay requires explicit approval {V21_M1_APPROVAL!r}"
        )
    code_commit = _current_code_commit()
    if not _base_commit_is_ancestor(V21_M1_BASE_COMMIT, code_commit):
        raise EngineeringIdentityError(
            "current code commit is not based on the approved V2.1-M0.2 commit"
        )
    frozen_identity = verify_frozen_identity()
    dataset = load_verified_dataset()
    usable_histories = tuple(dataset["usable_histories"])
    rules = resolve_rules(variant="control")
    parent_cache = build_stateful_schedule(
        usable_histories,
        data_start=M1_DATA_START,
        data_end=M1_DATA_END,
    )
    # This is an in-memory V1 accounting source for completed weekly inputs and
    # held-interval provenance.  No aggregate accounting result is serialized.
    control_accounting = simulate_frozen_portfolio(
        usable_histories,
        parent_cache.signals,
        variant="control",
        scenario="COST_1X",
        data_start=M1_DATA_START,
        data_end=M1_DATA_END,
    )
    weekly_inputs = _weekly_inputs(control_accounting)
    funding_coverage = validate_funding_coverage_for_holds(
        usable_histories,
        control_accounting.holding_intervals,
    )

    engine = V21PairedEngine(base_notional=1.0)
    execution_trace: list[dict[str, Any]] = []
    risk_scale_trace: list[dict[str, Any]] = []
    transition_trace: list[dict[str, Any]] = []
    v21_attempts: list[Any | None] = []
    expected_targets: list[tuple[tuple[str, int], ...]] = []
    expected_statuses: list[str] = []
    formula_failures: list[str] = []
    transition_violations: list[str] = []
    natural_transition_cases: set[str] = set()

    for index, parent_attempt in enumerate(parent_cache.attempts):
        targets = _target_directions_for_attempt(parent_cache, parent_attempt, rules)
        target_tuple = _target_tuple(targets)
        expected_status = _expected_v2_status(parent_attempt.status)
        logical_signal_time = _logical_signal_time(parent_attempt.execution_day)
        previous_positions = dict(engine.positions)
        try:
            if parent_attempt.status == SUCCESS:
                v21_attempt = engine.process_parent_attempt(
                    signal_day=parent_attempt.signal_day,
                    execution_day=parent_attempt.execution_day,
                    control_targets=targets,
                    control_weekly_returns=weekly_inputs,
                    parent_status=SUCCESS,
                    signal_time=logical_signal_time,
                )
            elif parent_attempt.status == NO_SIGNAL:
                v21_attempt = engine.process_parent_attempt(
                    signal_day=parent_attempt.signal_day,
                    execution_day=parent_attempt.execution_day,
                    control_targets=None,
                    control_weekly_returns=None,
                    parent_status=NO_SIGNAL,
                    signal_time=logical_signal_time,
                )
            elif parent_attempt.status in _PARENT_FAILURE_STATUSES:
                v21_attempt = engine.process_parent_attempt(
                    signal_day=parent_attempt.signal_day,
                    execution_day=parent_attempt.execution_day,
                    control_targets=targets,
                    control_weekly_returns=None,
                    parent_status=_expected_v2_status(parent_attempt.status),
                    signal_time=logical_signal_time,
                )
            else:
                raise EngineeringReplayError(
                    f"unsupported V1 parent status {parent_attempt.status}"
                )
            v21_status = v21_attempt.status
            v21_target_tuple = tuple(v21_attempt.target_directions)
            v21_reason = v21_attempt.reason
            position_scale = v21_attempt.position_scale
            changes = tuple(v21_attempt.changes)
        except Exception as exc:  # noqa: BLE001 - retain the diagnostic cause
            v21_attempt = None
            v21_status = "ENGINE_EXCEPTION"
            v21_target_tuple = ()
            v21_reason = f"{type(exc).__name__}: {exc}"
            position_scale = None
            changes = ()
            formula_failures.append(f"attempt {index}: {v21_reason}")

        v21_attempts.append(v21_attempt)
        expected_targets.append(target_tuple)
        expected_statuses.append(expected_status)

        if parent_attempt.status == SUCCESS:
            risk = evaluate_risk_scale(weekly_inputs, signal_time=logical_signal_time)
            try:
                selected = completed_control_weekly_returns(
                    weekly_inputs, signal_time=logical_signal_time
                )
                reference_vol = reference_annualized_volatility(
                    weekly_inputs, signal_time=logical_signal_time
                )
                expected_scale = calculate_position_scale(
                    weekly_inputs, signal_time=logical_signal_time
                )
                selected_week_endings = [record.week_ending for record in selected]
                if len(selected) != 13:
                    raise EngineeringProducerError(
                        "ENGINEERING PRODUCER ERROR: required risk window is not 13 rows"
                    )
                if len(set(selected_week_endings)) != 13:
                    raise EngineeringProducerError(
                        "ENGINEERING PRODUCER ERROR: duplicate week_ending in required risk window"
                    )
                if any(record.completion_time >= logical_signal_time for record in selected):
                    raise EngineeringReplayError("E2 risk input contains a post-signal observation")
                if risk.status != "VALID" or risk.position_scale is None:
                    raise EngineeringReplayError(
                        f"E2 risk layer returned {risk.status}: {risk.reason}"
                    )
                if not math.isclose(float(risk.position_scale), expected_scale, rel_tol=0.0, abs_tol=1e-15):
                    raise EngineeringReplayError("E2 V2.1 risk scale differs from frozen formula")
                if position_scale is None or not math.isclose(
                    float(position_scale), expected_scale, rel_tol=0.0, abs_tol=1e-15
                ):
                    raise EngineeringReplayError("E2 V2.1 engine scale differs from frozen formula")
                risk_scale_trace.append(
                    {
                        "signal_day": parent_attempt.signal_day.isoformat(),
                        "execution_day": parent_attempt.execution_day.isoformat(),
                        "logical_signal_time": logical_signal_time.isoformat(),
                        "status": "VALID",
                        "selected_count": len(selected),
                        "distinct_week_ending_count": len(set(selected_week_endings)),
                        "latest_completed_at": max(record.completion_time for record in selected).astimezone(UTC).isoformat(),
                        "reference_vol": reference_vol,
                        "scale": expected_scale,
                        "zero_reference_vol": math.isclose(reference_vol, 0.0, rel_tol=0.0, abs_tol=1e-15),
                    }
                )
            except (V21DataIntegrityHalt, EngineeringProducerError, EngineeringReplayError, TypeError, ValueError, OverflowError) as exc:
                formula_failures.append(f"attempt {index}: {type(exc).__name__}: {exc}")
                risk_scale_trace.append(
                    {
                        "signal_day": parent_attempt.signal_day.isoformat(),
                        "execution_day": parent_attempt.execution_day.isoformat(),
                        "logical_signal_time": logical_signal_time.isoformat(),
                        "status": DATA_INTEGRITY_HALT if risk.status == DATA_INTEGRITY_HALT else "INVALID",
                        "reason": str(exc),
                    }
                )

        if v21_status == SUCCESS and v21_attempt is not None and position_scale is not None:
            new_targets = scaled_target_positions(
                targets,
                base_notional=1.0,
                position_scale=position_scale,
            )
            cases, violations = _transition_diagnostics(previous_positions, new_targets, changes)
            natural_transition_cases.update(cases)
            transition_violations.extend(violations)
            if changes:
                transition_trace.append(
                    {
                        "index": index,
                        "signal_day": parent_attempt.signal_day.isoformat(),
                        "execution_day": parent_attempt.execution_day.isoformat(),
                        "actions": [asdict(change) for change in changes],
                        "cases": sorted(cases),
                    }
                )

        execution_trace.append(
            {
                "index": index,
                "signal_day": parent_attempt.signal_day.isoformat(),
                "execution_day": parent_attempt.execution_day.isoformat(),
                "logical_signal_time": logical_signal_time.isoformat(),
                "parent_status": parent_attempt.status,
                "expected_v2_1_status": expected_status,
                "v2_1_status": v21_status,
                "v2_1_parent_status": v21_attempt.parent_status if v21_attempt is not None else None,
                "parent_target_directions": [list(item) for item in target_tuple],
                "v2_1_target_directions": [list(item) for item in v21_target_tuple],
                "position_scale": position_scale,
                "change_count": len(changes),
                "reason": v21_reason,
            }
        )

    paired = paired_schedule_diagnostics(
        parent_cache.attempts,
        v21_attempts,
        expected_targets,
        expected_statuses,
    )
    retry = _retry_diagnostics(parent_cache.attempts, M1_DATA_END)
    temporal_violation_count = 0
    for attempt in parent_cache.attempts:
        try:
            validate_v2_1_temporal_inputs(
                signal_day=attempt.signal_day,
                execution_day=attempt.execution_day,
                signal_time=_logical_signal_time(attempt.execution_day),
            )
        except Exception:
            temporal_violation_count += 1

    lookahead_test_event_count = 0
    lookahead_mutation_count = 0
    lookahead_violation_count = 0
    for row in risk_scale_trace:
        if row.get("status") != "VALID":
            continue
        lookahead_test_event_count += 1
        lookahead = _run_no_lookahead_mutations(
            weekly_inputs,
            datetime.fromisoformat(str(row["logical_signal_time"]).replace("Z", "+00:00")),
            float(row["scale"]),
        )
        lookahead_mutation_count += lookahead["mutation_count"]
        lookahead_mutation_count += lookahead["old_non_required_mutation_count"]
        lookahead_violation_count += lookahead["violation_count"]

    zero_vol_regression = _zero_vol_regression()
    terminal_halt = _terminal_halt_diagnostics()
    synthetic_transition = _synthetic_transition_fixture()
    transition_case_set = natural_transition_cases | set(synthetic_transition["cases"])
    transition_pass = (
        not transition_violations
        and synthetic_transition["valid"] is True
        and {
            "open",
            "close",
            "same_side_increase_resize",
            "same_side_decrease_resize",
            "direction_flip",
            "symbol_replacement",
        }.issubset(transition_case_set)
    )
    funding = _funding_diagnostics(
        funding_coverage,
        control_accounting.holding_intervals,
    )
    warm_start = _warm_start_diagnostics(weekly_inputs, funding_coverage)
    data_integrity_halt_count = sum(
        row.get("v2_1_status") == V2_DATA_INTEGRITY_HALT for row in execution_trace
    )
    scale_behavior = _scale_behavior(risk_scale_trace, data_integrity_halt_count)

    gates = {
        "E0_identity": "PASS",
        "E1_parent_parity": "PASS"
        if paired["parent_schedule_mismatch_count"] == 0
        and paired["target_mismatch_count"] == 0
        and paired["successful_rebalance_mismatch_count"] == 0
        else "FAIL",
        "E2_risk_formula": "PASS"
        if not formula_failures
        and scale_behavior["valid_scale_count"] == len(parent_cache.signals)
        and data_integrity_halt_count == 0
        else "FAIL",
        "E3_zero_vol_regression": zero_vol_regression["status"],
        "E4_paired_schedule": "PASS"
        if paired["paired_schedule_divergence_count"] == 0
        and retry["v21_independent_retry_count"] == 0
        else "FAIL",
        "E5_terminal_data_halt": terminal_halt["status"],
        "E6_no_lookahead": "PASS"
        if lookahead_test_event_count >= 100 and lookahead_violation_count == 0
        else "FAIL",
        "E7_transition_accounting": "PASS" if transition_pass else "FAIL",
        "E8_temporal_semantics": "PASS" if temporal_violation_count == 0 else "FAIL",
        "E9_funding_provenance": "PASS"
        if funding["no_synthetic_funding"] and funding["fail_closed"]
        else "FAIL",
        "E10_warm_start_readiness": warm_start["status"],
    }
    required_gates = tuple(name for name in gates if name != "E10_warm_start_readiness")
    failed_gates = [name for name in required_gates if gates[name] != "PASS"]
    final_decision = "V2.1-M1 ENGINEERING PASS" if not failed_gates else "V2.1-M1 ENGINEERING FAIL"
    result: dict[str, Any] = {
        "schema_version": "V2.1-M1-ENGINEERING-REPLAY.v1",
        "run_id": V21_M1_RUN_ID,
        "run_type": "CONTAMINATED_ENGINEERING_REPLAY",
        "classification": {
            "data": "CONTAMINATED_DEVELOPMENT_DATA",
            "evidence": "NOT_PERFORMANCE_EVIDENCE",
            "sample": "NOT_OOS",
            "forward": "NOT_FORWARD",
            "scope": "ENGINEERING_ONLY",
        },
        "base_commit": V21_M1_BASE_COMMIT,
        "code_commit": code_commit,
        "approved_m0_commit": APPROVED_V2_1_M0_COMMIT,
        "v2_1_strategy_id": V21_STRATEGY_ID,
        "frozen_identity": frozen_identity,
        "dataset": dataset_metadata(dataset),
        "forward_anchor_status": V21_FORWARD_ANCHOR_STATUS,
        "gates": gates,
        "failed_gates": failed_gates,
        "final_decision": final_decision,
        "parent_schedule_attempt_count": len(parent_cache.attempts),
        "parent_success_count": len(parent_cache.signals),
        "first_valid_signal_date": parent_cache.first_valid_signal_date.isoformat() if parent_cache.first_valid_signal_date else None,
        "rebalance_count": len(parent_cache.signals),
        "parent_schedule_mismatch_count": paired["parent_schedule_mismatch_count"],
        "target_mismatch_count": paired["target_mismatch_count"],
        "paired_schedule_divergence_count": paired["paired_schedule_divergence_count"],
        "parent_status_mismatch_count": paired["parent_status_mismatch_count"],
        "successful_rebalance_mismatch_count": paired["successful_rebalance_mismatch_count"],
        "first_divergence": paired["first_divergence"],
        "parent_retry": retry,
        "historical_regression_2020_08_14": zero_vol_regression,
        "terminal_data_halt": terminal_halt,
        "lookahead_test_event_count": lookahead_test_event_count,
        "lookahead_mutation_count": lookahead_mutation_count,
        "lookahead_violation_count": lookahead_violation_count,
        "temporal_violation_count": temporal_violation_count,
        "funding": funding,
        "funding_issue_count": funding["funding_integrity_issue_count"],
        "affected_symbols": funding["affected_symbols"],
        "first_issue": funding["first_issue"],
        "last_issue": funding["last_issue"],
        "scale_behavior": scale_behavior,
        "valid_scale_count": scale_behavior["valid_scale_count"],
        "zero_reference_vol_count": scale_behavior["zero_reference_vol_count"],
        "data_integrity_halt_count": scale_behavior["data_integrity_halt_count"],
        "scale_min": scale_behavior["scale_min"],
        "scale_median": scale_behavior["scale_median"],
        "scale_mean": scale_behavior["scale_mean"],
        "scale_max": scale_behavior["scale_max"],
        "fraction_scale_eq_1": scale_behavior["fraction_scale_eq_1"],
        "fraction_scale_lt_1": scale_behavior["fraction_scale_lt_1"],
        "warm_start": warm_start,
        "natural_transition_cases": sorted(natural_transition_cases),
        "synthetic_transition_fixture": synthetic_transition,
        "transition_violation_count": len(transition_violations),
        "transition_violations": transition_violations,
        "formula_failure_count": len(formula_failures),
        "formula_failures": formula_failures,
        "execution_trace": execution_trace,
        "risk_scale_trace": risk_scale_trace,
        "transition_trace": transition_trace,
        "skipped_tests": [],
        "runtime_safety": {
            "live_trading": False,
            "paper_only": True,
            "http_methods": ["GET"],
            "forward_evidence_created": False,
            "first_forward_signal_selected": False,
            "forward_clock_started": False,
        },
    }
    assert_no_performance_fields(result)
    artifacts = _write_artifacts(result)
    result["artifacts"] = {
        name: str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        for name, path in artifacts.items()
    }
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the XS-LOWVOL V2.1-M1 contaminated engineering replay"
    )
    parser.add_argument("--approval", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        result = run_engineering_replay(approval=args.approval)
    except Exception as exc:  # noqa: BLE001 - CLI is fail closed
        print("V2.1-M1 ENGINEERING FAIL")
        print(f"engineering replay error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(result["final_decision"])
    print(
        json.dumps(
            {
                "run_id": result["run_id"],
                "base_commit": result["base_commit"],
                "code_commit": result["code_commit"],
                "dataset_sha256": result["dataset"]["dataset_sha256"],
                "normalized_dataset_sha256": result["dataset"]["normalized_dataset_sha256"],
                "gates": result["gates"],
                "failed_gates": result["failed_gates"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["final_decision"] == "V2.1-M1 ENGINEERING PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXPECTED_DATASET_SHA256",
    "EXPECTED_NORMALIZED_DATASET_SHA256",
    "EngineeringIdentityError",
    "EngineeringProducerError",
    "EngineeringReplayError",
    "M1_DATASET_FREEZE_PATH",
    "V21_M1_APPROVAL",
    "V21_M1_BASE_COMMIT",
    "V21_M1_MARKDOWN_PATH",
    "V21_M1_OUTPUT_DIR",
    "V21_M1_REPORT_PATH",
    "V21_M1_RUN_ID",
    "V21_M1_TRACE_MANIFEST_PATH",
    "V2_1_M1_APPROVAL",
    "V2_1_M1_BASE_COMMIT",
    "V2_1_M1_MARKDOWN_PATH",
    "V2_1_M1_REPORT_PATH",
    "V2_1_M1_RUN_ID",
    "V2_1_M1_TRACE_MANIFEST_PATH",
    "_logical_signal_time",
    "_retry_diagnostics",
    "_run_no_lookahead_mutations",
    "_synthetic_transition_fixture",
    "_terminal_halt_diagnostics",
    "_transition_diagnostics",
    "_weekly_inputs",
    "assert_no_performance_fields",
    "dataset_metadata",
    "load_verified_dataset",
    "paired_schedule_diagnostics",
    "run_engineering_replay",
    "verify_frozen_identity",
]
