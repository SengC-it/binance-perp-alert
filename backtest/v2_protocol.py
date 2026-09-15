"""V2-M0 protocol loading, canonical hashing, and frozen gate registration.

The protocol is registration-only in M0.  This module validates identity and
future evaluation rules without opening the V1 dataset or producing metrics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.xs_lowvol_v2_spec import (
    PARENT_CONTROL_SHA256,
    PARENT_CONTROL_STRATEGY_ID,
    SPEC_HASH_PATH,
    V2_SPEC_SHA256,
    V2_STRATEGY_ID,
    validate_v2_spec,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
V2_PROTOCOL_PATH = PROJECT_ROOT / "research" / "v2" / "XS_LOWVOL_V2_PROTOCOL.yaml"
V2_PROTOCOL_HASH_PATH = PROJECT_ROOT / "research" / "v2" / "XS_LOWVOL_V2_PROTOCOL.sha256"
V2_PROTOCOL_ID = "XS-LOWVOL-V2-FORWARD-V1"
V2_BASE_COMMIT = "2066f0856575146e917e9dc7c30e17a225f8927d"

# SHA-256 of the canonical YAML payload, frozen by this V2-M0 commit.
V2_PROTOCOL_SHA256 = "2460cd4db23b81ec769307c95b1dd74b15301a3995f0326aa194714208487fd4"
APPROVED_V2_PROTOCOL_SHA256 = V2_PROTOCOL_SHA256

EXTERNAL_DATA_START = date(2020, 1, 1)
EXTERNAL_DATA_END = date(2026, 8, 31)
FORWARD_MIN_WEEKS = 52
FORWARD_MIN_REBALANCE_CYCLES = 40

F_GATE_NAMES = (
    "F0_data_integrity",
    "F1_forward_sample",
    "F2_net_return",
    "F3_cost_robustness",
    "F4_sharpe",
    "F5_max_drawdown",
    "F6_profit_factor",
    "F7_block_bootstrap",
    "F8_best_week_robustness",
    "F9_paired_risk_improvement",
    "F10_paired_sharpe",
)


class V2ProtocolError(ValueError):
    """A V2-M0 protocol or gate identity is invalid."""


@dataclass(frozen=True)
class V2ForwardGatePolicy:
    """Thresholds pre-registered for the later Forward evaluation."""

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
    return value


def canonical_protocol_json(protocol: Mapping[str, Any]) -> str:
    """Return key-sorted canonical JSON independent of YAML formatting."""
    return json.dumps(
        _canonical(protocol), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _load_yaml(path: str | Path = V2_PROTOCOL_PATH) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise V2ProtocolError(f"无法读取 V2 protocol: {path}") from exc
    if not isinstance(raw, dict):
        raise V2ProtocolError("V2 protocol 顶层必须是 mapping")
    return raw


def load_v2_protocol(path: str | Path = V2_PROTOCOL_PATH) -> dict[str, Any]:
    """Load the V2 protocol without applying defaults."""
    return _load_yaml(path)


def protocol_sha256(protocol_or_path: Mapping[str, Any] | str | Path = V2_PROTOCOL_PATH) -> str:
    """Hash a parsed V2 protocol or its YAML path."""
    protocol = (
        _load_yaml(protocol_or_path)
        if isinstance(protocol_or_path, (str, Path))
        else protocol_or_path
    )
    if not isinstance(protocol, Mapping):
        raise V2ProtocolError("V2 protocol 必须是 mapping")
    return hashlib.sha256(canonical_protocol_json(protocol).encode("utf-8")).hexdigest()


def read_v2_protocol_hash(path: str | Path = V2_PROTOCOL_HASH_PATH) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip().split()[0]
    except (IndexError, OSError) as exc:
        raise V2ProtocolError(f"缺少 V2 protocol hash sidecar: {path}") from exc
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise V2ProtocolError("V2 protocol hash sidecar 不是 64 位小写 SHA-256")
    return token


def verify_v2_protocol_hash(
    path: str | Path = V2_PROTOCOL_PATH,
    hash_path: str | Path = V2_PROTOCOL_HASH_PATH,
    *,
    approved_hash: str = APPROVED_V2_PROTOCOL_SHA256,
) -> str:
    """Require actual YAML, sidecar, and source-pinned protocol hash to agree."""
    actual = protocol_sha256(path)
    recorded = read_v2_protocol_hash(hash_path)
    if actual != recorded or actual != approved_hash:
        raise V2ProtocolError(
            "V2 protocol hash mismatch: "
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
        raise V2ProtocolError(f"{dotted} 必须冻结为 {expected!r}，实际为 {actual!r}")


def _require_number(mapping: Mapping[str, Any], path: tuple[str, ...], expected: float) -> None:
    actual = _value(mapping, *path)
    if isinstance(actual, bool):
        raise V2ProtocolError(f"{'.'.join(path)} threshold 非法")
    try:
        value = float(actual)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V2ProtocolError(f"{'.'.join(path)} threshold 非法") from exc
    if value != expected:
        raise V2ProtocolError(f"{'.'.join(path)} 必须冻结为 {expected!r}，实际为 {actual!r}")


def _require_date(mapping: Mapping[str, Any], path: tuple[str, ...], expected: str) -> None:
    actual = _value(mapping, *path)
    if isinstance(actual, datetime):
        actual_value = actual.date().isoformat()
    elif isinstance(actual, date):
        actual_value = actual.isoformat()
    else:
        actual_value = str(actual)
    if actual_value != expected:
        raise V2ProtocolError(f"{'.'.join(path)} 必须冻结为 {expected!r}，实际为 {actual!r}")


def validate_v2_protocol(
    protocol: Mapping[str, Any] | None = None,
    *,
    require_current_v1: bool = True,
    path: str | Path = V2_PROTOCOL_PATH,
    hash_path: str | Path = V2_PROTOCOL_HASH_PATH,
    require_approved_hash: bool = True,
) -> None:
    """Validate all V2-M0 identities and every F0-F10 threshold."""
    source = load_v2_protocol(path) if protocol is None else dict(protocol)
    if require_approved_hash:
        actual = protocol_sha256(source)
        recorded = read_v2_protocol_hash(hash_path)
        if actual != recorded or actual != APPROVED_V2_PROTOCOL_SHA256:
            raise V2ProtocolError(
                "V2 protocol 未通过 approved hash pin: "
                f"approved={APPROVED_V2_PROTOCOL_SHA256}, recorded={recorded}, actual={actual}"
            )

    validate_v2_spec(require_current_v1=require_current_v1)
    _require(source, ("protocol_id",), V2_PROTOCOL_ID)
    _require(source, ("status",), "frozen-m0-forward-registration")
    _require(source, ("base_commit",), V2_BASE_COMMIT)
    _require(source, ("v1_control_strategy_id",), PARENT_CONTROL_STRATEGY_ID)
    _require(source, ("v1_control_sha256",), PARENT_CONTROL_SHA256)
    _require(source, ("v2_strategy_id",), V2_STRATEGY_ID)
    _require(source, ("v2_spec_sha256",), V2_SPEC_SHA256)

    epoch = source.get("forward_epoch_rule")
    if not isinstance(epoch, Mapping):
        raise V2ProtocolError("缺少 forward_epoch_rule")
    _require(
        epoch,
        ("rule",),
        "first eligible signal_or_rebalance strictly after the independently accepted V2-M0 freeze commit",
    )
    _require(epoch, ("pre_epoch_observations_in_forward_metrics",), False)
    _require(epoch, ("pre_epoch_data_for_13_week_risk_lookback",), "allowed_only_if_visible_at_signal_time")
    for key in ("forward_pnl_start", "forward_gate_start", "forward_sharpe_start", "forward_drawdown_start"):
        _require(epoch, (key,), "after_epoch")

    contaminated = source.get("contaminated_development_data")
    if not isinstance(contaminated, Mapping):
        raise V2ProtocolError("缺少 contaminated_development_data")
    _require_date(contaminated, ("start",), "2020-01-01")
    _require_date(contaminated, ("end",), "2026-08-31")
    _require(contaminated, ("label",), "CONTAMINATED_DEVELOPMENT_DATA")
    _require(contaminated, ("historical_replay_status",), "forbidden_as_forward_evidence")

    inherited = source.get("inherited_v1_control")
    if not isinstance(inherited, Mapping):
        raise V2ProtocolError("缺少 inherited_v1_control")
    for path_parts, expected in {
        ("completed_daily_returns_lookback",): 30,
        ("long_slots",): 5,
        ("short_slots",): 5,
        ("signal_day_quote_volume_min_usdt",): 50_000_000,
        ("point_in_time_universe",): True,
        ("execution_lag_days",): 1,
        ("funding_semantics",): "actual_settled_events_only",
        ("target_symbols",): "exactly_v1_control_targets",
    }.items():
        _require(inherited, path_parts, expected)

    risk = source.get("v2_risk_layer")
    if not isinstance(risk, Mapping):
        raise V2ProtocolError("缺少 v2_risk_layer")
    for path_parts, expected in {
        ("target_annualized_vol",): 0.15,
        ("vol_lookback_weeks",): 13,
        ("annualization_factor",): "sqrt(52)",
        ("vol_estimator",): "population_std",
        ("min_scale",): 0.0,
        ("max_scale",): 1.0,
        ("leverage_allowed",): False,
        ("source",): "completed_v1_control_weekly_net_returns",
        ("signal_time_cutoff",): "strict_before_signal_time",
        ("invalid_result",): "V2_RISK_SCALE_INVALID",
        ("invalid_policy",): "fail_closed",
        ("scale_above_one",): "forbidden",
        ("scale_all_legs_equally",): True,
    }.items():
        _require(risk, path_parts, expected)
    _require(risk, ("return_components",), ["price_pnl", "funding_pnl", "transaction_cost"])

    paired = source.get("paired_forward")
    if not isinstance(paired, Mapping):
        raise V2ProtocolError("缺少 paired_forward")
    for key, expected in {
        "control_strategy_id": PARENT_CONTROL_STRATEGY_ID,
        "v2_strategy_id": V2_STRATEGY_ID,
        "same_market_data": True,
        "same_point_in_time_universe": True,
        "same_targets_and_directions": True,
        "same_funding_settlements": True,
        "same_execution_price": True,
        "same_transaction_cost_model": True,
        "v1_is_benchmark_only": True,
        "v1_repass_qualification": False,
    }.items():
        _require(paired, (key,), expected)

    sample = source.get("forward_sample")
    if not isinstance(sample, Mapping):
        raise V2ProtocolError("缺少 forward_sample")
    _require(sample, ("minimum_completed_forward_weeks",), FORWARD_MIN_WEEKS)
    _require(sample, ("minimum_successful_rebalance_cycles",), FORWARD_MIN_REBALANCE_CYCLES)
    _require(sample, ("twenty_six_week_review",), "CONTINUE_OR_EARLY_FAIL_ONLY")
    _require(sample, ("twenty_six_week_pass",), "forbidden")

    gates = source.get("hard_gates")
    if not isinstance(gates, Mapping) or set(gates) != set(F_GATE_NAMES) | {"decision", "manual_override"}:
        raise V2ProtocolError("hard_gates 必须完整包含 F0-F10、decision、manual_override")
    f0 = gates.get("F0_data_integrity")
    if not isinstance(f0, Mapping):
        raise V2ProtocolError("F0_data_integrity 必须为 mapping")
    for key in (
        "no_unresolved_held_funding_gap",
        "no_execution_price_gap",
        "no_lookahead",
        "no_lifecycle_ambiguity_affecting_holdings",
        "no_synthetic_funding",
        "no_synthetic_price",
    ):
        _require(f0, (key,), True)

    f1 = gates["F1_forward_sample"]
    if not isinstance(f1, Mapping):
        raise V2ProtocolError("F1_forward_sample 必须为 mapping")
    _require(f1, ("completed_forward_weeks_gte",), FORWARD_MIN_WEEKS)
    _require(f1, ("successful_rebalance_cycles_gte",), FORWARD_MIN_REBALANCE_CYCLES)
    _require_number(gates, ("F2_net_return", "forward_52_week_net_return_pct_gt"), 8.0)
    _require(gates, ("F3_cost_robustness", "cost_scenario"), "COST_2X")
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
    _require(gates, ("decision",), "all_F0_through_F10_must_pass")
    _require(gates, ("manual_override",), "forbidden")

    early = source.get("early_failure")
    if not isinstance(early, Mapping):
        raise V2ProtocolError("缺少 early_failure")
    _require(early, ("trigger",), "v2_max_drawdown_gte_20_percent")
    _require(early, ("result",), "EARLY_FAIL_F5")
    _require(early, ("retain_forward_evidence",), True)

    funding = source.get("funding_integrity")
    if not isinstance(funding, Mapping):
        raise V2ProtocolError("缺少 funding_integrity")
    _require(funding, ("required_from_first_forward_day",), True)
    _require(funding, ("missing_settlement_result",), "F0_FAIL")
    for key in ("zero_fill", "interpolation", "inferred_event", "neighboring_event_substitution"):
        _require(funding, (key,), "forbidden")

    controls = source.get("m0_controls")
    if not isinstance(controls, Mapping):
        raise V2ProtocolError("缺少 m0_controls")
    required_controls = {
        "v2_historical_replay",
        "v2_performance_output",
        "target_vol_comparison",
        "lookback_comparison",
        "max_scale_comparison",
        "parameter_selection_from_contaminated_data",
        "v1_modification",
        "v2_m1",
        "v2_m2",
    }
    if set(controls) != required_controls or any(value != "forbidden" for value in controls.values()):
        raise V2ProtocolError("m0_controls 必须完整且全部 forbidden")

    safety = source.get("safety")
    if not isinstance(safety, Mapping):
        raise V2ProtocolError("缺少 safety")
    _require(safety, ("live_trading",), False)
    _require(safety, ("paper_only",), True)
    _require(safety, ("http_methods",), ["GET"])
    _require(safety, ("forward_mode",), "paper_only")


def frozen_v2_forward_gate_policy(
    protocol: Mapping[str, Any] | None = None,
) -> V2ForwardGatePolicy:
    """Return the gate policy only after full protocol validation."""
    validate_v2_protocol(protocol)
    source = load_v2_protocol() if protocol is None else protocol
    gates = source["hard_gates"]
    return V2ForwardGatePolicy(
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


def v2_protocol_identity() -> dict[str, str]:
    """Return the verified V2-M0 identity tuple."""
    validate_v2_protocol()
    return {
        "protocol_id": V2_PROTOCOL_ID,
        "protocol_sha256": verify_v2_protocol_hash(),
        "v1_control_strategy_id": PARENT_CONTROL_STRATEGY_ID,
        "v1_control_sha256": PARENT_CONTROL_SHA256,
        "v2_strategy_id": V2_STRATEGY_ID,
        "v2_spec_sha256": V2_SPEC_SHA256,
    }


# Familiar names for callers that use the V1 protocol module convention.
load_protocol = load_v2_protocol
verify_protocol_hash = verify_v2_protocol_hash
protocol_identity = v2_protocol_identity
