"""XS-LOWVOL-V2.1 M0 protocol identity and frozen gate registration.

This is an independent protocol module for the repaired V2.1 identity.  It
validates the old V2 lineage and gate values as immutable references, while
the V2.1 Forward anchor remains intentionally uncreated.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.xs_lowvol_v2_1_spec import (
    PARENT_CONTROL_SHA256,
    PARENT_CONTROL_STRATEGY_ID,
    V21_SPEC_SHA256,
    V21_STRATEGY_ID,
    validate_v2_1_spec,
)
from src.xs_lowvol_v2_anchor import (
    validate_v2_forward_anchor,
    verify_v2_forward_anchor_hash,
)
from .v2_protocol import (
    F_GATE_NAMES,
    V2_PROTOCOL_ID,
    V2_PROTOCOL_SHA256,
    V2_SPEC_SHA256,
    load_v2_protocol,
    verify_v2_protocol_hash,
)
from src.xs_lowvol_v2_spec import V2_STRATEGY_ID, verify_v2_spec_hash


PROJECT_ROOT = Path(__file__).resolve().parent.parent
V2_1_PROTOCOL_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_PROTOCOL.yaml"
V2_1_PROTOCOL_HASH_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_PROTOCOL.sha256"

V21_PROTOCOL_ID = "XS-LOWVOL-V2.1-FORWARD-V1"
V2_1_PROTOCOL_ID = V21_PROTOCOL_ID
V2_1_BASE_COMMIT = "2a88849f19897e76a229ceb0bb7840abccd80dd4"
V21_BASE_COMMIT = V2_1_BASE_COMMIT
V2_1_PROTOCOL_SHA256 = "6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d"
V21_PROTOCOL_SHA256 = V2_1_PROTOCOL_SHA256
APPROVED_V2_1_PROTOCOL_SHA256 = V2_1_PROTOCOL_SHA256

EXTERNAL_DATA_START = date(2020, 1, 1)
EXTERNAL_DATA_END = date(2026, 8, 31)
FORWARD_MIN_WEEKS = 52
FORWARD_MIN_REBALANCE_CYCLES = 40


class V21ProtocolError(ValueError):
    """A V2.1 protocol identity, lineage, or threshold is invalid."""


@dataclass(frozen=True)
class V21ForwardGatePolicy:
    """Thresholds registered for a future, independently anchored run."""

    minimum_completed_forward_weeks: int
    minimum_successful_rebalance_cycles: int
    net_return_pct_gt: float
    cost_2x_net_return_pct_gt: float
    weekly_sharpe_gt: float
    max_drawdown_pct_lt: float
    weekly_profit_factor_gt: float
    bootstrap_ci_lower_gt: float
    bootstrap_probability_gte: float
    best_week_compound_return_pct_gt: float
    paired_drawdown_ratio_lte: float


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
    return value


def canonical_v2_1_protocol_json(protocol: Mapping[str, Any]) -> str:
    return json.dumps(
        _canonical(protocol), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _load_yaml(path: str | Path = V2_1_PROTOCOL_PATH) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise V21ProtocolError(f"cannot read V2.1 protocol: {path}") from exc
    if not isinstance(raw, dict):
        raise V21ProtocolError("V2.1 protocol top level must be a mapping")
    return raw


def load_v2_1_protocol(path: str | Path = V2_1_PROTOCOL_PATH) -> dict[str, Any]:
    return _load_yaml(path)


def protocol_sha256(
    protocol_or_path: Mapping[str, Any] | str | Path = V2_1_PROTOCOL_PATH,
) -> str:
    protocol = (
        _load_yaml(protocol_or_path)
        if isinstance(protocol_or_path, (str, Path))
        else protocol_or_path
    )
    if not isinstance(protocol, Mapping):
        raise V21ProtocolError("V2.1 protocol must be a mapping")
    return hashlib.sha256(
        canonical_v2_1_protocol_json(protocol).encode("utf-8")
    ).hexdigest()


def read_v2_1_protocol_hash(path: str | Path = V2_1_PROTOCOL_HASH_PATH) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip().split()[0]
    except (IndexError, OSError) as exc:
        raise V21ProtocolError(f"missing V2.1 protocol hash sidecar: {path}") from exc
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise V21ProtocolError("V2.1 protocol hash sidecar is not lowercase SHA-256")
    return token


def verify_v2_1_protocol_hash(
    path: str | Path = V2_1_PROTOCOL_PATH,
    hash_path: str | Path = V2_1_PROTOCOL_HASH_PATH,
    *,
    approved_hash: str = APPROVED_V2_1_PROTOCOL_SHA256,
) -> str:
    actual = protocol_sha256(path)
    recorded = read_v2_1_protocol_hash(hash_path)
    if actual != recorded or actual != approved_hash:
        raise V21ProtocolError(
            "V2.1 protocol hash mismatch: "
            f"approved={approved_hash}, recorded={recorded}, actual={actual}"
        )
    return actual


def _value(mapping: Mapping[str, Any], *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _require(mapping: Mapping[str, Any], path: tuple[str, ...], expected: Any) -> None:
    actual = _value(mapping, *path)
    if actual != expected:
        dotted = ".".join(path)
        raise V21ProtocolError(f"{dotted} must be frozen as {expected!r}, got {actual!r}")


def _require_date(mapping: Mapping[str, Any], path: tuple[str, ...], expected: date) -> None:
    actual = _value(mapping, *path)
    if isinstance(actual, datetime):
        actual_date = actual.date()
    elif isinstance(actual, date):
        actual_date = actual
    else:
        try:
            actual_date = date.fromisoformat(str(actual))
        except ValueError as exc:
            raise V21ProtocolError(f"{'.'.join(path)} is not a date") from exc
    if actual_date != expected:
        raise V21ProtocolError(
            f"{'.'.join(path)} must be frozen as {expected.isoformat()}, got {actual!r}"
        )


def _require_number(mapping: Mapping[str, Any], path: tuple[str, ...], expected: float) -> None:
    actual = _value(mapping, *path)
    if isinstance(actual, bool):
        raise V21ProtocolError(f"{'.'.join(path)} threshold is invalid")
    try:
        value = float(actual)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V21ProtocolError(f"{'.'.join(path)} threshold is invalid") from exc
    if value != expected:
        raise V21ProtocolError(
            f"{'.'.join(path)} must be frozen as {expected!r}, got {actual!r}"
        )


def validate_v2_1_protocol(
    protocol: Mapping[str, Any] | None = None,
    *,
    require_current_lineage: bool = True,
    path: str | Path = V2_1_PROTOCOL_PATH,
    hash_path: str | Path = V2_1_PROTOCOL_HASH_PATH,
    require_approved_hash: bool = True,
) -> None:
    """Validate the independent V2.1 protocol and immutable parent gates."""
    source = load_v2_1_protocol(path) if protocol is None else dict(protocol)
    if require_approved_hash:
        actual = protocol_sha256(source)
        recorded = read_v2_1_protocol_hash(hash_path)
        if actual != recorded or actual != APPROVED_V2_1_PROTOCOL_SHA256:
            raise V21ProtocolError(
                "V2.1 protocol is not pinned: "
                f"approved={APPROVED_V2_1_PROTOCOL_SHA256}, recorded={recorded}, actual={actual}"
            )

    validate_v2_1_spec(
        require_approved_hash=require_approved_hash,
        require_current_lineage=require_current_lineage,
    )

    _require(source, ("protocol_id",), V21_PROTOCOL_ID)
    _require(source, ("status",), "frozen-m0-semantic-repair")
    _require(source, ("base_commit",), V2_1_BASE_COMMIT)
    _require(source, ("v1_control_strategy_id",), PARENT_CONTROL_STRATEGY_ID)
    _require(source, ("v1_control_sha256",), PARENT_CONTROL_SHA256)
    _require(source, ("v2_1_strategy_id",), V21_STRATEGY_ID)
    _require(source, ("v2_1_spec_sha256",), V21_SPEC_SHA256)

    lineage = source.get("lineage")
    if not isinstance(lineage, Mapping):
        raise V21ProtocolError("missing lineage")
    for key, expected in {
        "parent_strategy_id": V2_STRATEGY_ID,
        "parent_spec_sha256": V2_SPEC_SHA256,
        "parent_protocol_id": V2_PROTOCOL_ID,
        "parent_protocol_sha256": V2_PROTOCOL_SHA256,
        "superseded_engineering_run_commit": V2_1_BASE_COMMIT,
        "superseded_code_commit": "c7a114d0434b107a9a4487b0bacd7a97add04dc2",
        "superseded_failure_gates": ["E2_risk_formula", "E6_retry_and_paired_schedule"],
        "lineage_status": "SUPERSEDED_AFTER_ENGINEERING_FAIL",
    }.items():
        _require(lineage, (key,), expected)
    _require_date(lineage, ("first_observed_divergence",), date(2020, 8, 14))

    repair = source.get("semantic_repair")
    if not isinstance(repair, Mapping):
        raise V21ProtocolError("missing semantic_repair")
    for key, expected in {
        "zero_reference_volatility": "exact_zero_is_valid_scale_one",
        "positive_reference_volatility": "min_1_0_target_0_15_div_reference_volatility",
        "negative_or_nonfinite_reference_volatility": "V2_DATA_INTEGRITY_HALT",
        "invalid_input_result": "V2_DATA_INTEGRITY_HALT",
        "invalid_input_action": "halt_entire_paired_experiment",
        "scheduler_authority": "V1_CONTROL_PARENT_ONLY",
        "independent_success_clock": "FORBIDDEN",
        "v2_specific_retry": "FORBIDDEN",
        "parent_failure": "inherit_parent_status_and_retry",
        "parent_success_with_valid_risk": "paired_success_same_dates_targets",
        "parent_success_with_data_integrity_failure": "paired_experiment_halt",
    }.items():
        _require(repair, (key,), expected)

    anchor = source.get("forward_anchor")
    if not isinstance(anchor, Mapping):
        raise V21ProtocolError("missing forward_anchor")
    _require(anchor, ("status",), "NOT_CREATED_IN_V2_1_M0")
    _require(anchor, ("creation",), "forbidden_until_v2_1_m0_independent_acceptance")
    _require(anchor, ("historical_backfill_into_forward",), "forbidden")

    contaminated = source.get("contaminated_development_data")
    if not isinstance(contaminated, Mapping):
        raise V21ProtocolError("missing contaminated_development_data")
    _require_date(contaminated, ("start",), EXTERNAL_DATA_START)
    _require_date(contaminated, ("end",), EXTERNAL_DATA_END)
    _require(contaminated, ("label",), "CONTAMINATED_DEVELOPMENT_DATA")
    _require(contaminated, ("historical_replay_status",), "forbidden_as_forward_evidence")

    inherited = source.get("inherited_v1_control")
    if not isinstance(inherited, Mapping):
        raise V21ProtocolError("missing inherited_v1_control")
    for key, expected in {
        "completed_daily_returns_lookback": 30,
        "candle_interval": "1d",
        "completed_candles_only": True,
        "long_slots": 5,
        "short_slots": 5,
        "signal_day_quote_volume_min_usdt": 50_000_000,
        "point_in_time_universe": True,
        "delisted_symbol_semantics": "inherited_unchanged",
        "lifecycle_ambiguity": "fail_closed",
        "ranking": "inherited_unchanged",
        "tie_break": "symbol_ascending",
        "execution_lag_days": 1,
        "execution_price": "next_completed_candle_close",
        "scheduler": "V1_CONTROL_PARENT_ONLY",
        "retry_semantics": "inherit_parent_retry_without_v2_specific_retry",
        "funding_semantics": "actual_settled_events_only",
        "transaction_cost_semantics": "inherited_taker_plus_slippage",
        "target_symbols": "exactly_parent_control_targets",
    }.items():
        _require(inherited, (key,), expected)

    risk = source.get("v2_1_risk_layer")
    if not isinstance(risk, Mapping):
        raise V21ProtocolError("missing v2_1_risk_layer")
    for key, expected in {
        "target_annualized_vol": 0.15,
        "vol_lookback_weeks": 13,
        "annualization_factor": "sqrt(52)",
        "vol_estimator": "population_std",
        "min_scale": 0.0,
        "max_scale": 1.0,
        "leverage_allowed": False,
        "source": "completed_v1_control_weekly_net_returns",
        "signal_time_cutoff": "strict_before_signal_time",
        "zero_reference_volatility": "exact_zero_to_scale_one",
        "positive_reference_volatility": "min_1_0_target_0_15_div_reference_volatility",
        "negative_or_nonfinite_reference_volatility": "V2_DATA_INTEGRITY_HALT",
        "invalid_result": "V2_DATA_INTEGRITY_HALT",
        "invalid_policy": "halt_entire_paired_experiment",
        "future_observations": "exclude_from_required_window",
        "scale_above_one": "forbidden",
        "scale_all_legs_equally": True,
        "v1_target_notional": "unchanged",
    }.items():
        _require(risk, (key,), expected)
    _require(risk, ("return_components",), ["price_pnl", "funding_pnl", "transaction_cost"])

    paired = source.get("paired_forward")
    if not isinstance(paired, Mapping):
        raise V21ProtocolError("missing paired_forward")
    for key, expected in {
        "control_strategy_id": PARENT_CONTROL_STRATEGY_ID,
        "v2_1_strategy_id": V21_STRATEGY_ID,
        "same_market_data": True,
        "same_point_in_time_universe": True,
        "same_targets_and_directions": True,
        "same_funding_settlements": True,
        "same_execution_price": True,
        "same_transaction_cost_model": True,
        "parent_failure_inherits_status": True,
        "parent_retry_is_authoritative": True,
        "independent_v2_1_success_clock": False,
        "v2_1_specific_retry": False,
        "v1_is_benchmark_only": True,
        "v1_repass_qualification": False,
    }.items():
        _require(paired, (key,), expected)

    sample = source.get("forward_sample")
    if not isinstance(sample, Mapping):
        raise V21ProtocolError("missing forward_sample")
    _require(sample, ("minimum_completed_forward_weeks",), FORWARD_MIN_WEEKS)
    _require(sample, ("minimum_successful_rebalance_cycles",), FORWARD_MIN_REBALANCE_CYCLES)
    _require(sample, ("twenty_six_week_review",), "CONTINUE_OR_EARLY_FAIL_ONLY")
    _require(sample, ("twenty_six_week_pass",), "forbidden")

    gates = source.get("hard_gates")
    if not isinstance(gates, Mapping):
        raise V21ProtocolError("hard_gates must be a mapping")
    if set(gates) != set(F_GATE_NAMES) | {"decision", "manual_override"}:
        raise V21ProtocolError("hard_gates must contain exactly F0-F10, decision, and manual_override")
    old_gates = load_v2_protocol()["hard_gates"]
    if gates != old_gates:
        raise V21ProtocolError("V2.1 hard_gates changed from the immutable V2 registration")
    _require(gates, ("decision",), "all_F0_through_F10_must_pass")
    _require(gates, ("manual_override",), "forbidden")
    _require_number(gates, ("F2_net_return", "forward_52_week_net_return_pct_gt"), 8.0)
    _require_number(gates, ("F3_cost_robustness", "forward_52_week_net_return_pct_gt"), 0.0)
    _require_number(gates, ("F4_sharpe", "weekly_sharpe_gt"), 1.0)
    _require_number(gates, ("F5_max_drawdown", "max_drawdown_pct_lt"), 20.0)
    _require_number(gates, ("F6_profit_factor", "weekly_profit_factor_gt"), 1.2)
    _require_number(gates, ("F7_block_bootstrap", "block_length_weeks"), 4.0)
    _require_number(gates, ("F7_block_bootstrap", "rounds"), 10_000.0)
    _require_number(gates, ("F7_block_bootstrap", "confidence"), 0.95)
    _require_number(gates, ("F7_block_bootstrap", "seed"), 20_260_916.0)
    _require_number(gates, ("F7_block_bootstrap", "mean_weekly_return_ci95_lower_gt"), 0.0)
    _require_number(gates, ("F7_block_bootstrap", "probability_mean_weekly_return_gt_zero"), 0.95)
    _require_number(gates, ("F8_best_week_robustness", "remove_highest_fraction"), 0.05)
    _require_number(gates, ("F8_best_week_robustness", "compound_return_pct_gt"), 0.0)
    _require_number(gates, ("F9_paired_risk_improvement", "v2_max_drawdown_relative_to_v1_lte"), 0.8)
    _require(gates, ("F10_paired_sharpe", "v2_weekly_sharpe_gte_v1"), True)

    early = source.get("early_failure")
    if not isinstance(early, Mapping):
        raise V21ProtocolError("missing early_failure")
    _require(early, ("trigger",), "v2_max_drawdown_gte_20_percent")
    _require(early, ("result",), "EARLY_FAIL_F5")
    _require(early, ("retain_forward_evidence",), True)

    funding = source.get("funding_integrity")
    if not isinstance(funding, Mapping):
        raise V21ProtocolError("missing funding_integrity")
    _require(funding, ("required_from_first_forward_day",), True)
    _require(funding, ("missing_settlement_result",), "F0_FAIL")
    for key in ("zero_fill", "interpolation", "inferred_event", "neighboring_event_substitution"):
        _require(funding, (key,), "forbidden")

    controls = source.get("m0_controls")
    required_controls = {
        "v2_1_historical_replay",
        "v2_1_performance_output",
        "v2_1_forward_anchor",
        "v2_m2",
        "v2_1_m1",
        "parameter_selection_from_contaminated_data",
        "v1_modification",
    }
    if not isinstance(controls, Mapping) or set(controls) != required_controls or any(
        value != "forbidden" for value in controls.values()
    ):
        raise V21ProtocolError("m0_controls must be complete and all forbidden")

    safety = source.get("safety")
    if not isinstance(safety, Mapping):
        raise V21ProtocolError("missing safety")
    _require(safety, ("live_trading",), False)
    _require(safety, ("paper_only",), True)
    _require(safety, ("http_methods",), ["GET"])
    _require(safety, ("forward_mode",), "paper_only")

    if require_current_lineage:
        if verify_v2_spec_hash() != V2_SPEC_SHA256:
            raise V21ProtocolError("immutable V2 strategy spec hash changed")
        if verify_v2_protocol_hash() != V2_PROTOCOL_SHA256:
            raise V21ProtocolError("immutable V2 protocol hash changed")
        validate_v2_forward_anchor()
        verify_v2_forward_anchor_hash()


def frozen_v2_1_forward_gate_policy(
    protocol: Mapping[str, Any] | None = None,
) -> V21ForwardGatePolicy:
    """Return V2.1's frozen gates after full protocol validation."""
    validate_v2_1_protocol(protocol)
    source = load_v2_1_protocol() if protocol is None else protocol
    gates = source["hard_gates"]
    return V21ForwardGatePolicy(
        minimum_completed_forward_weeks=int(gates["F1_forward_sample"]["completed_forward_weeks_gte"]),
        minimum_successful_rebalance_cycles=int(gates["F1_forward_sample"]["successful_rebalance_cycles_gte"]),
        net_return_pct_gt=float(gates["F2_net_return"]["forward_52_week_net_return_pct_gt"]),
        cost_2x_net_return_pct_gt=float(gates["F3_cost_robustness"]["forward_52_week_net_return_pct_gt"]),
        weekly_sharpe_gt=float(gates["F4_sharpe"]["weekly_sharpe_gt"]),
        max_drawdown_pct_lt=float(gates["F5_max_drawdown"]["max_drawdown_pct_lt"]),
        weekly_profit_factor_gt=float(gates["F6_profit_factor"]["weekly_profit_factor_gt"]),
        bootstrap_ci_lower_gt=float(gates["F7_block_bootstrap"]["mean_weekly_return_ci95_lower_gt"]),
        bootstrap_probability_gte=float(gates["F7_block_bootstrap"]["probability_mean_weekly_return_gt_zero"]),
        best_week_compound_return_pct_gt=float(gates["F8_best_week_robustness"]["compound_return_pct_gt"]),
        paired_drawdown_ratio_lte=float(gates["F9_paired_risk_improvement"]["v2_max_drawdown_relative_to_v1_lte"]),
    )


def v2_1_protocol_identity() -> dict[str, str]:
    validate_v2_1_protocol()
    return {
        "protocol_id": V21_PROTOCOL_ID,
        "protocol_sha256": verify_v2_1_protocol_hash(),
        "v1_control_strategy_id": PARENT_CONTROL_STRATEGY_ID,
        "v1_control_sha256": PARENT_CONTROL_SHA256,
        "v2_1_strategy_id": V21_STRATEGY_ID,
        "v2_1_spec_sha256": V21_SPEC_SHA256,
        "superseded_v2_protocol_sha256": V2_PROTOCOL_SHA256,
    }


# Keep familiar protocol-module names available without aliasing old V2
# validation behavior.
load_protocol = load_v2_1_protocol
verify_protocol_hash = verify_v2_1_protocol_hash
protocol_identity = v2_1_protocol_identity
frozen_forward_gate_policy = frozen_v2_1_forward_gate_policy


__all__ = [
    "APPROVED_V2_1_PROTOCOL_SHA256",
    "EXTERNAL_DATA_END",
    "EXTERNAL_DATA_START",
    "FORWARD_MIN_REBALANCE_CYCLES",
    "FORWARD_MIN_WEEKS",
    "V21_BASE_COMMIT",
    "V21ForwardGatePolicy",
    "V21ProtocolError",
    "V21_PROTOCOL_ID",
    "V21_PROTOCOL_SHA256",
    "V21_STRATEGY_ID",
    "V2_1_BASE_COMMIT",
    "V2_1_PROTOCOL_HASH_PATH",
    "V2_1_PROTOCOL_ID",
    "V2_1_PROTOCOL_PATH",
    "V2_1_PROTOCOL_SHA256",
    "V2_PROTOCOL_ID",
    "V2_PROTOCOL_SHA256",
    "V2_SPEC_SHA256",
    "canonical_v2_1_protocol_json",
    "frozen_forward_gate_policy",
    "frozen_v2_1_forward_gate_policy",
    "load_protocol",
    "load_v2_1_protocol",
    "protocol_identity",
    "protocol_sha256",
    "read_v2_1_protocol_hash",
    "v2_1_protocol_identity",
    "validate_v2_1_protocol",
    "verify_protocol_hash",
    "verify_v2_1_protocol_hash",
]
