"""XS-LOWVOL V2-M1 contaminated engineering replay.

The runner reuses the already verified V1 normalized cache and the accepted
stateful V1 scheduler.  It produces implementation traces and engineering
diagnostics only.  It deliberately has no archive downloader, Forward ledger,
performance aggregation, or gate implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import statistics
import subprocess
import sys
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.xs_lowvol_v2_anchor import (
    APPROVED_V1_CONTROL_SHA256,
    APPROVED_V2_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_M0_COMMIT,
    APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC,
    APPROVED_V2_PROTOCOL_SHA256,
    APPROVED_V2_SPEC_SHA256,
    FORWARD_ANCHOR_STATUS,
    validate_v2_forward_anchor,
    verify_v2_forward_anchor_hash,
)
from src.xs_lowvol_v2_risk import (
    ControlWeeklyReturn,
    PositionState,
    V2RiskScaleInvalid,
    calculate_position_scale,
    completed_control_weekly_returns,
    plan_position_changes,
    reference_annualized_volatility,
    scaled_target_positions,
    validate_forward_weekly_returns,
)
from src.xs_lowvol_v2_spec import (
    V2_STRATEGY_ID,
    verify_v2_spec_hash,
)
from src.xs_lowvol_spec import resolve_rules, strategy_spec_hash

from .m1_b import (
    M1_DATA_END,
    M1_DATA_START,
    _signal_from_universe,
    build_stateful_schedule,
    simulate_frozen_portfolio,
)
from .v2_forward import (
    MISSING_EXECUTION_PRICE,
    NO_SIGNAL,
    RISK_SCALE_INVALID,
    SUCCESS,
    V2ForwardEngine,
)
from .v2_protocol import verify_v2_protocol_hash
from .xs_data_quality import FundingCoverageReport, HoldingInterval, validate_funding_coverage_for_holds


PROJECT_ROOT = Path(__file__).resolve().parent.parent
V2_M1_BASE_COMMIT = "bacf6e351832fc9b8f2a1f619eaa04818995973d"
V2_M1_APPROVAL = "START V2-M1 ENGINEERING REPLAY"
V2_M1_OUTPUT_DIR = PROJECT_ROOT / "research" / "v2" / "m1"
V2_M1_REPORT_PATH = V2_M1_OUTPUT_DIR / "V2_M1_ENGINEERING_REPORT.json"
V2_M1_MARKDOWN_PATH = V2_M1_OUTPUT_DIR / "V2_M1_ENGINEERING_REPORT.md"
V2_M1_TRACE_MANIFEST_PATH = V2_M1_OUTPUT_DIR / "V2_M1_TRACE_MANIFEST.json"
M1_DATASET_FREEZE_PATH = PROJECT_ROOT / "backtest" / "m1_cache" / "normalized" / "XS_LOWVOL_M1_DATASET.pkl"
EXPECTED_DATASET_SHA256 = "f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755"
EXPECTED_NORMALIZED_DATASET_SHA256 = "6adf9f06cba3a9de377ed59f69f422320b1524f635512271ebee60516896c387"
UTC = timezone.utc

FORBIDDEN_PERFORMANCE_KEYS = frozenset(
    {
        "return",
        "cagr",
        "sharpe",
        "sortino",
        "max_drawdown",
        "profit_factor",
        "yearly_returns",
        "monthly_returns",
        "bootstrap_performance",
        "best_week_removed_return",
        "pnl",
    }
)


class EngineeringReplayError(RuntimeError):
    """A fail-closed engineering replay error."""


class EngineeringIdentityError(EngineeringReplayError):
    """A frozen V1/V2/Anchor or dataset identity is not the approved one."""


def _canonical(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
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
        raise EngineeringReplayError("cannot verify the approved M1 base commit") from exc
    if result.returncode not in (0, 1):
        raise EngineeringReplayError("git could not verify the approved M1 base commit")
    return result.returncode == 0


def verify_frozen_identity() -> dict[str, str]:
    """Verify all approved V2 identities before loading contaminated data."""
    try:
        control = strategy_spec_hash()
        v2_spec = verify_v2_spec_hash()
        v2_protocol = verify_v2_protocol_hash()
        validate_v2_forward_anchor()
        anchor = verify_v2_forward_anchor_hash()
    except Exception as exc:  # noqa: BLE001 - identity must fail closed
        raise EngineeringIdentityError(f"E0_identity failed: {exc}") from exc
    expected = {
        "v1_control_sha256": APPROVED_V1_CONTROL_SHA256,
        "v2_spec_sha256": APPROVED_V2_SPEC_SHA256,
        "v2_protocol_sha256": APPROVED_V2_PROTOCOL_SHA256,
        "forward_anchor_sha256": APPROVED_V2_FORWARD_ANCHOR_SHA256,
    }
    actual = {
        "v1_control_sha256": control,
        "v2_spec_sha256": v2_spec,
        "v2_protocol_sha256": v2_protocol,
        "forward_anchor_sha256": anchor,
    }
    if actual != expected:
        raise EngineeringIdentityError(
            f"E0_identity failed: expected={expected}, actual={actual}"
        )
    return actual


def _performance_schema_violations(value: Any, path: str = "") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            key_path = f"{path}.{key_text}" if path else key_text
            if key_text.lower() in FORBIDDEN_PERFORMANCE_KEYS:
                violations.append(key_path)
            violations.extend(_performance_schema_violations(item, key_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            violations.extend(_performance_schema_violations(item, f"{path}[{index}]"))
    return violations


def assert_no_performance_fields(value: Mapping[str, Any]) -> None:
    """Reject aggregate-performance fields from the engineering artifact."""
    violations = _performance_schema_violations(value)
    if violations:
        raise EngineeringReplayError(
            "forbidden performance fields in V2-M1 artifact: " + ", ".join(violations[:10])
        )


def load_verified_dataset(path: str | Path = M1_DATASET_FREEZE_PATH) -> dict[str, Any]:
    """Load only the existing frozen pickle; never normalize or download here."""
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
    usable = tuple(data.get("usable_histories", ()))
    manifest = data.get("manifest")
    if catalog is None or catalog.listing_complete is not True:
        raise EngineeringIdentityError("dataset catalog is not complete")
    if not histories or not usable or not isinstance(manifest, Mapping):
        raise EngineeringIdentityError("verified dataset cache is structurally incomplete")
    return {
        "catalog": catalog,
        "histories": histories,
        "usable_histories": usable,
        "manifest": manifest,
        "dataset_sha256": str(data["dataset_sha256"]),
        "normalized_dataset_sha256": str(data["normalized_dataset_sha256"]),
    }


def dataset_metadata(dataset: Mapping[str, Any]) -> dict[str, Any]:
    manifest = dataset["manifest"]
    catalog = dataset["catalog"]
    discovery = manifest.get("discovery_catalog", {})
    return {
        "dataset_sha256": dataset["dataset_sha256"],
        "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
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
        "catalog_complete": catalog.listing_complete is True,
    }


def _weekly_inputs(accounting: Any) -> tuple[ControlWeeklyReturn, ...]:
    """Convert the existing Control accounting ledger into dated risk inputs."""
    rows: list[ControlWeeklyReturn] = []
    for row in accounting.weekly_rows:
        week_end = date.fromisoformat(str(row["week_end"]))
        # A complete UTC daily candle closes at the final millisecond of the
        # UTC calendar day, strictly before the following day's 00:00 cutoff.
        completed_at = datetime.combine(week_end, time.max, tzinfo=UTC)
        rows.append(
            ControlWeeklyReturn(
                week_ending=week_end,
                net_return=float(row["return"]),
                completed_at=completed_at,
                complete=True,
            )
        )
    if not rows:
        raise EngineeringReplayError("E2_risk_formula produced no dated Control observations")
    return validate_forward_weekly_returns(tuple(rows))


def _targets_for_attempt(cache: Any, attempt: Any, rules: Any) -> dict[str, int]:
    signal = cache.signals.get(attempt.signal_day)
    if signal is None:
        universe = cache.universes.get(attempt.signal_day)
        signal = _signal_from_universe(universe, rules) if universe is not None else None
    return dict(signal.targets) if signal is not None else {}


def _logical_signal_time(execution_day: date) -> datetime:
    return datetime.combine(execution_day, time.min, tzinfo=UTC)


def _expected_v2_status(parent_status: str) -> str:
    if parent_status == "MARK_FAILURE":
        return MISSING_EXECUTION_PRICE
    return parent_status


def _run_no_lookahead_mutation(
    weekly_inputs: Sequence[ControlWeeklyReturn],
    signal_time: datetime,
    baseline_scale: float,
) -> bool:
    future_records = tuple(weekly_inputs) + (
        ControlWeeklyReturn(
            week_ending=signal_time.date(),
            net_return=90.0,
            completed_at=signal_time + timedelta(seconds=1),
        ),
    )
    negative_records = tuple(weekly_inputs) + (
        ControlWeeklyReturn(
            week_ending=signal_time.date(),
            net_return=-90.0,
            completed_at=signal_time + timedelta(seconds=1),
        ),
    )
    plus_scale = calculate_position_scale(future_records, signal_time=signal_time)
    minus_scale = calculate_position_scale(negative_records, signal_time=signal_time)
    return (
        math.isclose(plus_scale, baseline_scale, rel_tol=0.0, abs_tol=1e-15)
        and math.isclose(minus_scale, baseline_scale, rel_tol=0.0, abs_tol=1e-15)
    )


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
    by_symbol: dict[str, list[str]] = {}
    for change in changes:
        by_symbol.setdefault(change.symbol, []).append(change.action)
    valid = (
        by_symbol.get("INC") == ["RESIZE"]
        and by_symbol.get("DEC") == ["RESIZE"]
        and by_symbol.get("FLIP") == ["CLOSE", "OPEN"]
        and by_symbol.get("OLD") == ["CLOSE"]
        and by_symbol.get("NEW") == ["OPEN"]
        and all(
            change.cost_notional == abs(change.notional_delta)
            for change in changes
            if change.action == "RESIZE"
        )
    )
    return {
        "valid": valid,
        "cases": {
            "increase_resize": True,
            "decrease_resize": True,
            "direction_flip": True,
            "symbol_replacement": True,
        },
        "change_count": len(changes),
    }


def _transition_diagnostics(
    old_positions: Mapping[str, PositionState],
    targets: Mapping[str, int],
    changes: Sequence[Any],
) -> tuple[set[str], list[str]]:
    """Check each transition against the frozen open/close/resize semantics."""
    cases: set[str] = set()
    violations: list[str] = []
    grouped: dict[str, list[Any]] = {}
    for change in changes:
        grouped.setdefault(change.symbol, []).append(change)
    old_symbols = set(old_positions)
    new_symbols = set(targets)

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
        target_direction = targets.get(symbol)
        actions = grouped.get(symbol, [])
        names = [item.action for item in actions]
        if prior is None:
            if target_direction is None:
                if actions:
                    violations.append(f"unexpected transition {symbol}")
                continue
            if names != ["OPEN"]:
                violations.append(f"new symbol is not OPEN only {symbol}")
                continue
            change = actions[0]
            if not (
                change.direction == target_direction
                and change.notional > 0.0
                and math.isclose(change.notional_delta, change.notional, rel_tol=0.0, abs_tol=1e-15)
                and math.isclose(change.cost_notional, change.notional, rel_tol=0.0, abs_tol=1e-15)
            ):
                violations.append(f"OPEN accounting mismatch {symbol}")
            continue
        if target_direction is None:
            if names != ["CLOSE"] or not close_is_exact(actions[0], prior):
                violations.append(f"removed symbol is not CLOSE only {symbol}")
            continue
        if prior.direction != target_direction:
            if names != ["CLOSE", "OPEN"] or not close_is_exact(actions[0], prior):
                violations.append(f"direction flip is not CLOSE+OPEN {symbol}")
            elif not (
                actions[1].direction == target_direction
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
            cases.add("increase_resize")
        elif change.notional_delta < 0:
            cases.add("decrease_resize")
    if old_symbols - new_symbols and new_symbols - old_symbols:
        cases.add("symbol_replacement")
    return cases, violations


def paired_schedule_diagnostics(
    parent_attempts: Sequence[Any],
    v2_attempts: Sequence[Any | None],
    expected_target_directions: Sequence[tuple[tuple[str, int], ...]],
    expected_statuses: Sequence[str],
) -> dict[str, Any]:
    """Return one machine-readable divergence row per mismatching attempt."""
    divergence_count = 0
    schedule_mismatch_count = 0
    target_mismatch_count = 0
    first_divergence: dict[str, Any] | None = None
    if not (
        len(parent_attempts)
        == len(v2_attempts)
        == len(expected_target_directions)
        == len(expected_statuses)
    ):
        return {
            "paired_schedule_divergence_count": 1,
            "parent_schedule_mismatch_count": 1,
            "target_mismatch_count": 1,
            "first_divergence": {"reason": "trace lengths differ"},
        }
    for index, (parent, actual, expected_targets, expected_status) in enumerate(
        zip(parent_attempts, v2_attempts, expected_target_directions, expected_statuses)
    ):
        reasons: list[str] = []
        if actual is None:
            reasons.append("V2 attempt missing")
            schedule_mismatch_count += 1
            target_mismatch_count += 1
        else:
            if actual.status != expected_status:
                reasons.append(f"status {expected_status} != {actual.status}")
            if actual.signal_day != parent.signal_day or actual.execution_day != parent.execution_day:
                schedule_mismatch_count += 1
                reasons.append("signal/execution day mismatch")
            if tuple(actual.target_directions) != expected_targets:
                target_mismatch_count += 1
                reasons.append("target direction mismatch")
        if reasons:
            divergence_count += 1
            if first_divergence is None:
                first_divergence = {
                    "index": index,
                    "signal_day": parent.signal_day.isoformat(),
                    "execution_day": parent.execution_day.isoformat(),
                    "parent_status": parent.status,
                    "expected_status": expected_status,
                    "v2_status": actual.status if actual is not None else None,
                    "reason": "; ".join(reasons),
                }
    return {
        "paired_schedule_divergence_count": divergence_count,
        "parent_schedule_mismatch_count": schedule_mismatch_count,
        "target_mismatch_count": target_mismatch_count,
        "first_divergence": first_divergence,
    }


def _retry_diagnostics(attempts: Sequence[Any], data_end: date) -> dict[str, int]:
    failure_statuses = {NO_SIGNAL, MISSING_EXECUTION_PRICE, "MARK_FAILURE"}
    retry_count = 0
    retry_violations = 0
    recovered = 0
    for index, attempt in enumerate(attempts):
        if attempt.status not in failure_statuses:
            continue
        next_attempt = attempts[index + 1] if index + 1 < len(attempts) else None
        if next_attempt is not None and next_attempt.execution_day == attempt.execution_day + timedelta(days=1):
            retry_count += 1
            cursor = index + 1
            while (
                cursor < len(attempts)
                and attempts[cursor].execution_day
                == attempts[cursor - 1].execution_day + timedelta(days=1)
            ):
                if attempts[cursor].status == SUCCESS:
                    recovered += 1
                    break
                cursor += 1
        elif attempt.execution_day < data_end:
            retry_violations += 1
    return {
        "retry_count": retry_count,
        "retry_semantic_violation_count": retry_violations,
        "eventually_recovered_count": recovered,
    }


def _funding_diagnostics(
    coverage: FundingCoverageReport,
    holding_intervals: Sequence[HoldingInterval],
) -> dict[str, Any]:
    issues = tuple(coverage.issues)
    affected_symbols = sorted({issue.symbol.upper() for issue in coverage.hold_issues})
    affected_intervals = [
        asdict(interval)
        for interval in holding_intervals
        if interval.symbol.upper() in affected_symbols
    ]
    issue_rows = [
        {
            "code": issue.code,
            "symbol": issue.symbol,
            "detail": issue.detail,
            "day": issue.day.isoformat() if issue.day else None,
        }
        for issue in sorted(issues, key=lambda item: (item.symbol, item.code, item.detail))
    ]
    return {
        "funding_integrity_issue_count": len(coverage.hold_issues),
        "structural_issue_count": len(coverage.structural_issues),
        "affected_symbols": affected_symbols,
        "affected_holding_intervals": affected_intervals,
        "first_issue": issue_rows[0] if issue_rows else None,
        "last_issue": issue_rows[-1] if issue_rows else None,
        "issues": issue_rows,
        "no_synthetic_funding": True,
        "fail_closed": all(issue.code == "FUNDING_COVERAGE_GAP" for issue in coverage.hold_issues),
    }


def _warm_start_diagnostics(
    weekly_inputs: Sequence[ControlWeeklyReturn],
    coverage: FundingCoverageReport,
) -> dict[str, Any]:
    candidates = tuple(
        record
        for record in weekly_inputs
        if record.completion_time < APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC
    )
    selected = tuple(
        sorted(candidates, key=lambda record: (record.completion_time, record.week_ending))[-13:]
    )
    ready = len(selected) == 13 and all(record.complete for record in selected)
    provenance = [
        {
            "week_ending": record.week_ending.isoformat(),
            "completed_at": record.completion_time.astimezone(UTC).isoformat(),
            "complete": record.complete,
            "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        }
        for record in selected
    ]
    diagnostics: dict[str, Any] = {
        "status": "READY" if ready else "NOT_READY",
        "required_observation_count": 13,
        "available_observation_count": len(selected),
        "provenance": provenance,
        "incomplete_observations": [
            {
                "week_ending": record.week_ending.isoformat(),
                "reason": "complete flag is false",
            }
            for record in selected
            if not record.complete
        ],
        "historical_funding_issue_count": len(coverage.hold_issues),
    }
    if ready:
        diagnostics["initial_state_diagnostic"] = {
            "reference_vol": reference_annualized_volatility(
                selected,
                signal_time=APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC,
            ),
            "position_scale": calculate_position_scale(
                selected,
                signal_time=APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC,
            ),
        }
    return diagnostics


def _render_markdown(result: Mapping[str, Any]) -> str:
    gates = result["gates"]
    lines = [
        "# XS-LOWVOL V2-M1 Engineering Replay",
        "",
        f"## {result['final_decision']}",
        "",
        "Classification: `CONTAMINATED_DEVELOPMENT_DATA` / `NOT_PERFORMANCE_EVIDENCE` / `NOT_OOS` / `NOT_FORWARD`.",
        "",
        "This artifact records implementation traces and engineering diagnostics only.",
        "",
        "## Frozen identity",
        "",
        f"- Base commit: `{result['base_commit']}`",
        f"- Code commit: `{result['code_commit']}`",
        f"- V1 Control SHA: `{result['frozen_identity']['v1_control_sha256']}`",
        f"- V2 Spec SHA: `{result['frozen_identity']['v2_spec_sha256']}`",
        f"- V2 Protocol SHA: `{result['frozen_identity']['v2_protocol_sha256']}`",
        f"- Forward Anchor SHA: `{result['frozen_identity']['forward_anchor_sha256']}`",
        f"- Dataset SHA: `{result['dataset']['dataset_sha256']}`",
        f"- Normalized Dataset SHA: `{result['dataset']['normalized_dataset_sha256']}`",
        "",
        "## E0–E10",
        "",
    ]
    for name, value in gates.items():
        lines.append(f"- `{name}`: `{value}`")
    lines.extend(
        [
            "",
            "## Engineering diagnostics",
            "",
            "```json",
            json.dumps(
                _canonical(
                    {
                        key: value
                        for key, value in result.items()
                        if key not in {"execution_trace", "risk_scale_trace", "transition_trace"}
                    }
                ),
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            "## Trace manifest",
            "",
            f"- `{result['trace_manifest_path']}`",
            "- Trace rows remain engineering provenance; no Forward evidence was created.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_artifacts(result: Mapping[str, Any], output_dir: str | Path = V2_M1_OUTPUT_DIR) -> dict[str, Path]:
    path = Path(output_dir).resolve()
    if path != V2_M1_OUTPUT_DIR.resolve():
        raise EngineeringReplayError("M1 output directory is fixed at research/v2/m1")
    path.mkdir(parents=True, exist_ok=True)
    targets = (V2_M1_REPORT_PATH, V2_M1_MARKDOWN_PATH, V2_M1_TRACE_MANIFEST_PATH)
    existing = [target for target in targets if target.exists()]
    if existing:
        raise EngineeringReplayError(
            "refusing to overwrite existing V2-M1 engineering artifacts: "
            + ", ".join(str(target) for target in existing)
        )
    report_text = json.dumps(
        _canonical(result), ensure_ascii=False, indent=2
    ) + "\n"
    markdown_text = _render_markdown(result)
    V2_M1_REPORT_PATH.write_text(report_text, encoding="utf-8")
    V2_M1_MARKDOWN_PATH.write_text(markdown_text, encoding="utf-8")
    manifest = {
        "schema_version": "V2-M1-ENGINEERING-TRACE-MANIFEST.v1",
        "run_type": "CONTAMINATED_ENGINEERING_REPLAY",
        "classification": [
            "CONTAMINATED_DEVELOPMENT_DATA",
            "NOT_PERFORMANCE_EVIDENCE",
            "NOT_OOS",
            "NOT_FORWARD",
        ],
        "base_commit": result["base_commit"],
        "code_commit": result["code_commit"],
        "dataset_sha256": result["dataset"]["dataset_sha256"],
        "normalized_dataset_sha256": result["dataset"]["normalized_dataset_sha256"],
        "trace_files": {
            "execution_trace": {
                "storage": "embedded_in_V2_M1_ENGINEERING_REPORT.json",
                "count": len(result["execution_trace"]),
                "sha256": _sha256(result["execution_trace"]),
            },
            "risk_scale_trace": {
                "storage": "embedded_in_V2_M1_ENGINEERING_REPORT.json",
                "count": len(result["risk_scale_trace"]),
                "sha256": _sha256(result["risk_scale_trace"]),
            },
            "transition_trace": {
                "storage": "embedded_in_V2_M1_ENGINEERING_REPORT.json",
                "count": len(result["transition_trace"]),
                "sha256": _sha256(result["transition_trace"]),
            },
        },
        "artifacts": {
            "report": {
                "path": "research/v2/m1/V2_M1_ENGINEERING_REPORT.json",
                "sha256": _file_sha256(V2_M1_REPORT_PATH),
            },
            "markdown": {
                "path": "research/v2/m1/V2_M1_ENGINEERING_REPORT.md",
                "sha256": _file_sha256(V2_M1_MARKDOWN_PATH),
            },
        },
        "forward_anchor_status": FORWARD_ANCHOR_STATUS,
        "forward_evidence_created": False,
    }
    V2_M1_TRACE_MANIFEST_PATH.write_text(
        json.dumps(_canonical(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "report": V2_M1_REPORT_PATH,
        "markdown": V2_M1_MARKDOWN_PATH,
        "trace_manifest": V2_M1_TRACE_MANIFEST_PATH,
    }


def run_engineering_replay(
    *,
    approval: str,
    dataset_path: str | Path = M1_DATASET_FREEZE_PATH,
    output_dir: str | Path = V2_M1_OUTPUT_DIR,
) -> dict[str, Any]:
    """Run E0–E10 using only the verified historical cache."""
    if approval != V2_M1_APPROVAL:
        raise EngineeringReplayError(
            f"engineering replay requires explicit approval {V2_M1_APPROVAL!r}"
        )
    identity = verify_frozen_identity()
    dataset = load_verified_dataset(dataset_path)
    code_commit = _current_code_commit()
    if not _base_commit_is_ancestor(V2_M1_BASE_COMMIT, code_commit):
        raise EngineeringReplayError(
            "current code commit is not based on the approved V2-M0.2 base commit"
        )

    usable_histories = tuple(dataset["usable_histories"])
    rules = resolve_rules(variant="control")
    parent_cache = build_stateful_schedule(
        usable_histories,
        data_start=M1_DATA_START,
        data_end=M1_DATA_END,
    )
    # This is an accounting-only source for the frozen V1 Control weekly risk
    # inputs and held intervals.  No aggregate accounting result is serialized.
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

    engine = V2ForwardEngine(base_notional=1.0)
    execution_trace: list[dict[str, Any]] = []
    risk_scale_trace: list[dict[str, Any]] = []
    transition_trace: list[dict[str, Any]] = []
    v2_attempts: list[Any | None] = []
    expected_target_directions_by_attempt: list[tuple[tuple[str, int], ...]] = []
    expected_statuses: list[str] = []
    risk_invalid_count = 0
    unresolved_risk_invalid_count = 0
    recovered_risk_retry_count = 0
    no_lookahead_points = 0
    no_lookahead_failures = 0
    e2_failures = 0
    transition_violations: list[str] = []
    natural_transition_cases: set[str] = set()

    for index, parent_attempt in enumerate(parent_cache.attempts):
        targets = _targets_for_attempt(parent_cache, parent_attempt, rules)
        expected_target_directions = tuple(sorted((symbol.upper(), int(direction)) for symbol, direction in targets.items()))
        logical_time = _logical_signal_time(parent_attempt.execution_day)
        previous_positions = dict(engine.positions)
        try:
            if parent_attempt.status == SUCCESS:
                v2_attempt = engine.attempt(
                    signal_day=parent_attempt.signal_day,
                    execution_day=parent_attempt.execution_day,
                    control_targets=targets,
                    control_weekly_returns=weekly_inputs,
                    signal_time=logical_time,
                )
            elif parent_attempt.status == NO_SIGNAL:
                v2_attempt = engine.attempt(
                    signal_day=parent_attempt.signal_day,
                    execution_day=parent_attempt.execution_day,
                    control_targets=None,
                    control_weekly_returns=None,
                    signal_status=NO_SIGNAL,
                    signal_time=logical_time,
                )
            elif parent_attempt.status in {MISSING_EXECUTION_PRICE, "MARK_FAILURE"}:
                v2_attempt = engine.attempt(
                    signal_day=parent_attempt.signal_day,
                    execution_day=parent_attempt.execution_day,
                    control_targets=targets,
                    control_weekly_returns=None,
                    execution_available=False,
                    signal_time=logical_time,
                )
            else:
                raise EngineeringReplayError(
                    f"unsupported parent schedule status {parent_attempt.status}"
                )
            v2_status = v2_attempt.status
            v2_target_directions = tuple(v2_attempt.target_directions)
            v2_reason = v2_attempt.reason
            position_scale = v2_attempt.position_scale
            changes = tuple(v2_attempt.changes)
        except Exception as exc:  # noqa: BLE001 - preserve the diagnostic trace
            v2_attempt = None
            v2_status = "ENGINE_EXCEPTION"
            v2_target_directions = ()
            v2_reason = f"{type(exc).__name__}: {exc}"
            position_scale = None
            changes = ()

        expected_status = _expected_v2_status(parent_attempt.status)
        v2_attempts.append(v2_attempt)
        expected_target_directions_by_attempt.append(expected_target_directions)
        expected_statuses.append(expected_status)

        if parent_attempt.status == SUCCESS:
            try:
                selected = completed_control_weekly_returns(
                    weekly_inputs,
                    signal_time=logical_time,
                )
                reference_vol = reference_annualized_volatility(
                    weekly_inputs,
                    signal_time=logical_time,
                )
                expected_scale = calculate_position_scale(
                    weekly_inputs,
                    signal_time=logical_time,
                )
                if len(selected) != 13 or any(record.completion_time >= logical_time for record in selected):
                    raise V2RiskScaleInvalid("risk input cutoff or lookback count is invalid")
                if not 0.0 <= expected_scale <= 1.0:
                    raise V2RiskScaleInvalid("risk scale is outside the frozen 0..1 range")
                if position_scale is None or not math.isclose(
                    float(position_scale), expected_scale, rel_tol=0.0, abs_tol=1e-15
                ):
                    raise V2RiskScaleInvalid("V2 engine scale differs from frozen formula")
                risk_scale_trace.append(
                    {
                        "signal_day": parent_attempt.signal_day.isoformat(),
                        "execution_day": parent_attempt.execution_day.isoformat(),
                        "status": "VALID",
                        "lookback_count": len(selected),
                        "latest_completed_at": max(record.completion_time for record in selected).astimezone(UTC).isoformat(),
                        "reference_vol": reference_vol,
                        "scale": expected_scale,
                    }
                )
                no_lookahead_points += 1
                if not _run_no_lookahead_mutation(weekly_inputs, logical_time, expected_scale):
                    no_lookahead_failures += 1
            except V2RiskScaleInvalid as exc:
                risk_invalid_count += 1
                risk_scale_trace.append(
                    {
                        "signal_day": parent_attempt.signal_day.isoformat(),
                        "execution_day": parent_attempt.execution_day.isoformat(),
                        "status": "INVALID",
                        "reason": str(exc),
                    }
                )
            except (TypeError, ValueError, OverflowError) as exc:
                e2_failures += 1
                risk_invalid_count += 1
                risk_scale_trace.append(
                    {
                        "signal_day": parent_attempt.signal_day.isoformat(),
                        "execution_day": parent_attempt.execution_day.isoformat(),
                        "status": "INVALID",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )

        if v2_status == RISK_SCALE_INVALID:
            risk_invalid_count = max(risk_invalid_count, sum(1 for row in risk_scale_trace if row["status"] == "INVALID"))

        if v2_status == SUCCESS and v2_attempt is not None:
            cases, violations = _transition_diagnostics(previous_positions, targets, changes)
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
                "logical_signal_time": logical_time.isoformat(),
                "parent_status": parent_attempt.status,
                "v2_status": v2_status,
                "parent_target_directions": [list(item) for item in expected_target_directions],
                "v2_target_directions": [list(item) for item in v2_target_directions],
                "position_scale": position_scale,
                "change_count": len(changes),
                "reason": v2_reason,
            }
        )

    paired = paired_schedule_diagnostics(
        parent_cache.attempts,
        v2_attempts,
        expected_target_directions_by_attempt,
        expected_statuses,
    )
    schedule_mismatch_count = int(paired["parent_schedule_mismatch_count"])
    target_mismatch_count = int(paired["target_mismatch_count"])
    paired_divergence_count = int(paired["paired_schedule_divergence_count"])
    first_divergence = paired["first_divergence"]

    retry = _retry_diagnostics(parent_cache.attempts, M1_DATA_END)
    v2_risk_fail_indices = [
        index for index, row in enumerate(execution_trace) if row["v2_status"] == RISK_SCALE_INVALID
    ]
    for index in v2_risk_fail_indices:
        next_row = execution_trace[index + 1] if index + 1 < len(execution_trace) else None
        if next_row and date.fromisoformat(next_row["execution_day"]) == date.fromisoformat(execution_trace[index]["execution_day"]) + timedelta(days=1):
            if next_row["v2_status"] == SUCCESS:
                recovered_risk_retry_count += 1
            else:
                unresolved_risk_invalid_count += 1
        elif date.fromisoformat(execution_trace[index]["execution_day"]) < M1_DATA_END:
            unresolved_risk_invalid_count += 1
    risk_invalid_count = max(
        len(v2_risk_fail_indices),
        sum(1 for row in risk_scale_trace if row["status"] == "INVALID"),
    )

    success_days = [
        date.fromisoformat(row["execution_day"])
        for row in execution_trace
        if row["v2_status"] == SUCCESS
    ]
    early_rebalance_violations = sum(
        (right - left).days < 7 for left, right in zip(success_days, success_days[1:])
    )
    temporal_violations = sum(
        date.fromisoformat(row["execution_day"]) != date.fromisoformat(row["signal_day"]) + timedelta(days=1)
        or row["logical_signal_time"] != _logical_signal_time(date.fromisoformat(row["execution_day"])).isoformat()
        for row in execution_trace
    )

    synthetic_transition = _synthetic_transition_fixture()
    transition_case_set = natural_transition_cases | {
        name for name, value in synthetic_transition["cases"].items() if value
    }
    transition_pass = not transition_violations and synthetic_transition["valid"] and {
        "increase_resize",
        "decrease_resize",
        "direction_flip",
        "symbol_replacement",
    }.issubset(transition_case_set)

    scale_values = [float(row["scale"]) for row in risk_scale_trace if row["status"] == "VALID"]
    reference_values = [float(row["reference_vol"]) for row in risk_scale_trace if row["status"] == "VALID"]
    scale_stats = {
        "valid_scale_count": len(scale_values),
        "invalid_scale_count": risk_invalid_count,
        "scale_min": min(scale_values) if scale_values else None,
        "scale_median": statistics.median(scale_values) if scale_values else None,
        "scale_mean": statistics.mean(scale_values) if scale_values else None,
        "scale_max": max(scale_values) if scale_values else None,
        "fraction_scale_eq_1": (
            sum(math.isclose(value, 1.0, rel_tol=0.0, abs_tol=1e-15) for value in scale_values) / len(scale_values)
            if scale_values
            else None
        ),
        "fraction_scale_lt_1": (
            sum(value < 1.0 for value in scale_values) / len(scale_values)
            if scale_values
            else None
        ),
        "reference_vol_min": min(reference_values) if reference_values else None,
        "reference_vol_median": statistics.median(reference_values) if reference_values else None,
        "reference_vol_mean": statistics.mean(reference_values) if reference_values else None,
        "reference_vol_max": max(reference_values) if reference_values else None,
    }

    funding = _funding_diagnostics(funding_coverage, control_accounting.holding_intervals)
    warm_start = _warm_start_diagnostics(weekly_inputs, funding_coverage)
    gates = {
        "E0_identity": True,
        "E1_parent_schedule": schedule_mismatch_count == 0 and target_mismatch_count == 0,
        "E2_risk_formula": e2_failures == 0 and risk_invalid_count == 0 and bool(scale_values),
        "E3_no_lookahead": no_lookahead_points >= 100 and no_lookahead_failures == 0,
        "E4_transition_accounting": transition_pass,
        "E5_funding_fail_closed": bool(funding["fail_closed"]),
        "E6_retry_and_paired_schedule": paired_divergence_count == 0 and unresolved_risk_invalid_count == 0,
        "E7_rebalance_clock": early_rebalance_violations == 0 and retry["retry_semantic_violation_count"] == 0,
        "E8_temporal_semantics": temporal_violations == 0,
        "E9_behavior_trace_integrity": (
            len(execution_trace) == len(parent_cache.attempts)
            and len(v2_attempts) == len(parent_cache.attempts)
            and len(risk_scale_trace) == len(parent_cache.signals)
        ),
        "E10_warm_start_readiness": warm_start["status"],
    }
    failed_checks = [name for name, value in gates.items() if name != "E10_warm_start_readiness" and value is not True]
    final_decision = "ENGINEERING PASS" if not failed_checks else "ENGINEERING FAIL"
    result: dict[str, Any] = {
        "schema_version": "V2-M1-ENGINEERING-REPLAY.v1",
        "run_id": "XS-LOWVOL-V2-M1-ENGINEERING-REPLAY",
        "run_type": "CONTAMINATED_ENGINEERING_REPLAY",
        "classification": {
            "data": "CONTAMINATED_DEVELOPMENT_DATA",
            "evidence": "NOT_PERFORMANCE_EVIDENCE",
            "sample": "NOT_OOS",
            "forward": "NOT_FORWARD",
        },
        "base_commit": V2_M1_BASE_COMMIT,
        "code_commit": code_commit,
        "approved_m0_commit": APPROVED_V2_M0_COMMIT,
        "v2_strategy_id": V2_STRATEGY_ID,
        "frozen_identity": identity,
        "dataset": dataset_metadata(dataset),
        "forward_anchor_status": FORWARD_ANCHOR_STATUS,
        "gates": gates,
        "failed_checks": failed_checks,
        "final_decision": final_decision,
        "parent_schedule_attempt_count": len(parent_cache.attempts),
        "parent_success_count": len(parent_cache.signals),
        "first_valid_signal_date": parent_cache.first_valid_signal_date.isoformat() if parent_cache.first_valid_signal_date else None,
        "rebalance_count": len(parent_cache.signals),
        "parent_schedule_mismatch_count": schedule_mismatch_count,
        "target_mismatch_count": target_mismatch_count,
        "paired_schedule_divergence_count": paired_divergence_count,
        "first_divergence": first_divergence,
        "early_rebalance_violation_count": early_rebalance_violations,
        "retry_semantic_violation_count": retry["retry_semantic_violation_count"],
        "retry_count": retry["retry_count"],
        "eventually_recovered_count": retry["eventually_recovered_count"],
        "funding": funding,
        "funding_issue_count": funding["funding_integrity_issue_count"],
        "risk_invalid_attempt_count": risk_invalid_count,
        "recovered_retry_count": recovered_risk_retry_count,
        "unresolved_risk_invalid_count": unresolved_risk_invalid_count,
        "scale_behavior": scale_stats,
        "warm_start": warm_start,
        "temporal_violation_count": temporal_violations,
        "no_lookahead_test_count": no_lookahead_points,
        "no_lookahead_failure_count": no_lookahead_failures,
        "natural_transition_cases": sorted(natural_transition_cases),
        "synthetic_transition_fixture": synthetic_transition,
        "transition_violation_count": len(transition_violations),
        "transition_violations": transition_violations,
        "execution_trace": execution_trace,
        "risk_scale_trace": risk_scale_trace,
        "transition_trace": transition_trace,
        "trace_manifest_path": "research/v2/m1/V2_M1_TRACE_MANIFEST.json",
        "runtime_safety": {
            "live_trading": False,
            "paper_only": True,
            "http_methods": ["GET"],
            "forward_evidence_created": False,
        },
    }
    assert_no_performance_fields(result)
    artifacts = _write_artifacts(result, output_dir=output_dir)
    result["artifacts"] = {
        name: str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        for name, path in artifacts.items()
    }
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the XS-LOWVOL V2-M1 engineering replay")
    parser.add_argument("--approval", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        result = run_engineering_replay(approval=args.approval)
    except Exception as exc:  # noqa: BLE001 - CLI is fail closed
        print("V2-M1 ENGINEERING FAIL")
        print(f"engineering replay error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(result["final_decision"])
    print(
        json.dumps(
            {
                "base_commit": result["base_commit"],
                "code_commit": result["code_commit"],
                "dataset_sha256": result["dataset"]["dataset_sha256"],
                "normalized_dataset_sha256": result["dataset"]["normalized_dataset_sha256"],
                "gates": result["gates"],
                "failed_checks": result["failed_checks"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["final_decision"] == "ENGINEERING PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
