"""Frozen XS-LOWVOL-V2-RISK15 hypothesis and strategy-spec identity.

V2-M0 freezes one falsifiable risk-layer hypothesis before any V2 historical
replay.  The parent V1 Control remains the source of signal and execution
semantics; this module only loads and validates the separate V2 spec.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = PROJECT_ROOT / "research" / "v2" / "XS_LOWVOL_V2_SPEC.yaml"
SPEC_HASH_PATH = PROJECT_ROOT / "research" / "v2" / "XS_LOWVOL_V2_SPEC.sha256"

V2_STRATEGY_ID = "XS-LOWVOL-V2-RISK15"
PARENT_CONTROL_STRATEGY_ID = "XS-LOWVOL-V1-Control"
PARENT_CONTROL_SHA256 = "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678"

# SHA-256 of the canonical YAML payload, frozen by this V2-M0 commit.
V2_SPEC_SHA256 = "0337c9d026c5544d31f53d6812e59b985809a46b8329b37ec2f7864aa8ea71bc"
APPROVED_V2_SPEC_SHA256 = V2_SPEC_SHA256
# Short alias kept consistent with the V1 spec module.
V2_SPEC_HASH = V2_SPEC_SHA256


class V2SpecError(ValueError):
    """A V2 strategy-spec identity or frozen invariant is invalid."""


@dataclass(frozen=True)
class V2RiskConfig:
    """The only new V2 position-sizing layer."""

    strategy_id: str = V2_STRATEGY_ID
    target_annualized_vol: float = 0.15
    vol_lookback_weeks: int = 13
    annualization_factor: float = math.sqrt(52.0)
    vol_estimator: str = "population_std"
    min_scale: float = 0.0
    max_scale: float = 1.0
    leverage_allowed: bool = False


V2_RISK_CONFIG = V2RiskConfig()


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


def canonical_spec_json(spec: Mapping[str, Any]) -> str:
    """Return key-sorted canonical JSON independent of YAML whitespace."""
    return json.dumps(
        _canonical(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _load_yaml(path: str | Path = SPEC_PATH) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise V2SpecError(f"无法读取 V2 strategy spec: {path}") from exc
    if not isinstance(raw, dict):
        raise V2SpecError("V2 strategy spec 顶层必须是 mapping")
    return raw


def load_v2_strategy_spec(path: str | Path = SPEC_PATH) -> dict[str, Any]:
    """Load a copy of the frozen V2 strategy spec."""
    return deepcopy(_load_yaml(path))


def spec_sha256(spec_or_path: Mapping[str, Any] | str | Path = SPEC_PATH) -> str:
    """Hash a parsed spec or a V2 strategy-spec YAML path."""
    spec = (
        _load_yaml(spec_or_path)
        if isinstance(spec_or_path, (str, Path))
        else spec_or_path
    )
    if not isinstance(spec, Mapping):
        raise V2SpecError("V2 strategy spec 必须是 mapping")
    return hashlib.sha256(canonical_spec_json(spec).encode("utf-8")).hexdigest()


def strategy_spec_hash(path: str | Path = SPEC_PATH) -> str:
    """Return the current on-disk V2 strategy-spec digest."""
    return spec_sha256(path)


def read_v2_spec_hash(path: str | Path = SPEC_HASH_PATH) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip().split()[0]
    except (IndexError, OSError) as exc:
        raise V2SpecError(f"缺少 V2 strategy-spec hash sidecar: {path}") from exc
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise V2SpecError("V2 strategy-spec hash sidecar 不是 64 位小写 SHA-256")
    return token


def verify_v2_spec_hash(
    path: str | Path = SPEC_PATH,
    hash_path: str | Path = SPEC_HASH_PATH,
    *,
    approved_hash: str = APPROVED_V2_SPEC_SHA256,
) -> str:
    """Require the YAML, sidecar, and source-pinned digest to agree."""
    actual = spec_sha256(path)
    recorded = read_v2_spec_hash(hash_path)
    if actual != recorded or actual != approved_hash:
        raise V2SpecError(
            "V2 strategy spec hash mismatch: "
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
        raise V2SpecError(f"{dotted} 必须冻结为 {expected!r}，实际为 {actual!r}")


def validate_v2_spec(
    spec: Mapping[str, Any] | None = None,
    *,
    require_current_v1: bool = True,
    path: str | Path = SPEC_PATH,
    hash_path: str | Path = SPEC_HASH_PATH,
    require_approved_hash: bool = True,
) -> None:
    """Validate every V2-M0 identity and fixed risk-layer parameter."""
    source = load_v2_strategy_spec(path) if spec is None else deepcopy(dict(spec))
    if require_approved_hash:
        actual = spec_sha256(source)
        recorded = read_v2_spec_hash(hash_path)
        if actual != recorded or actual != APPROVED_V2_SPEC_SHA256:
            raise V2SpecError(
                "V2 strategy spec 未通过 approved hash pin: "
                f"approved={APPROVED_V2_SPEC_SHA256}, recorded={recorded}, actual={actual}"
            )

    _require(source, ("strategy_id",), V2_STRATEGY_ID)
    _require(source, ("parent_strategy_id",), PARENT_CONTROL_STRATEGY_ID)
    _require(source, ("parent_control_sha256",), PARENT_CONTROL_SHA256)
    _require(source, ("status",), "frozen-m0-hypothesis")

    inherited = source.get("inherited_v1_control")
    if not isinstance(inherited, Mapping):
        raise V2SpecError("缺少 inherited_v1_control")
    inherited_checks = {
        ("signal", "completed_daily_returns_lookback"): 30,
        ("signal", "candle_interval"): "1d",
        ("signal", "completed_candles_only"): True,
        ("signal", "signal_day_quote_volume_min_usdt"): 50_000_000,
        ("portfolio", "long_slots"): 5,
        ("portfolio", "short_slots"): 5,
        ("portfolio", "target_notional"): "unchanged_from_parent_control",
        ("portfolio", "target_symbols"): "exactly_parent_control_targets",
        ("point_in_time_universe", "required"): True,
        ("point_in_time_universe", "quote_asset"): "USDT",
        ("point_in_time_universe", "contract_type"): "PERPETUAL",
        ("ranking", "long"): "lowest_realized_volatility",
        ("ranking", "short"): "highest_realized_volatility",
        ("ranking", "tie_break"): "symbol_ascending",
        ("rebalance", "interval_days"): 7,
        ("rebalance", "clock"): "advance_only_after_complete_successful_rebalance",
        ("rebalance", "retry_semantics"): "failed_attempt_retries_on_next_calendar_day",
        ("execution", "execution_lag_days"): 1,
        ("execution", "future_data"): "forbidden",
        ("funding", "source"): "actual_settled_funding_events",
        ("funding", "missing_settlement"): "fail_closed_no_synthetic_value",
        ("transaction_cost", "same_side_resize_basis"): "absolute_notional_delta_only",
        ("transaction_cost", "direction_change"): "close_then_open",
    }
    for path_parts, expected in inherited_checks.items():
        _require(inherited, path_parts, expected)

    risk = source.get("risk_layer")
    if not isinstance(risk, Mapping):
        raise V2SpecError("缺少 risk_layer")
    risk_checks = {
        ("target_annualized_vol",): 0.15,
        ("vol_lookback_weeks",): 13,
        ("annualization",): "sqrt(52)",
        ("annualization_factor",): "sqrt(52)",
        ("vol_estimator",): "population_std",
        ("min_scale",): 0.0,
        ("max_scale",): 1.0,
        ("leverage_allowed",): False,
        ("input_strategy",): PARENT_CONTROL_STRATEGY_ID,
        ("input_return_frequency",): "weekly",
        ("input_return_definition",): "completed_control_weekly_net_return",
        ("input_data_rule",): "only_completed_data_known_before_signal_time",
        ("invalid_input_result",): "V2_RISK_SCALE_INVALID",
        ("invalid_input_policy",): "fail_closed",
        ("zero_or_nonfinite_reference_vol",): "invalid",
        ("incomplete_lookback",): "invalid",
        ("all_legs_use_same_scale",): True,
        ("scale_above_one",): "forbidden",
    }
    for path_parts, expected in risk_checks.items():
        _require(risk, path_parts, expected)
    _require(
        risk,
        ("input_components",),
        ["price_pnl", "funding_pnl", "transaction_cost"],
    )

    forward_epoch = source.get("forward_epoch")
    if not isinstance(forward_epoch, Mapping):
        raise V2SpecError("缺少 forward_epoch")
    _require(
        forward_epoch,
        ("rule",),
        "first eligible signal_or_rebalance strictly after the independently accepted V2-M0 freeze commit",
    )
    for key in ("pre_epoch_observations_in_forward_metrics",):
        _require(forward_epoch, (key,), False)
    _require(
        forward_epoch,
        ("pre_epoch_past_data_for_13_week_lookback",),
        "allowed_when_visible_at_signal_time",
    )

    paired = source.get("paired_forward_benchmark")
    if not isinstance(paired, Mapping):
        raise V2SpecError("缺少 paired_forward_benchmark")
    for key, expected in {
        "control_strategy_id": PARENT_CONTROL_STRATEGY_ID,
        "v2_strategy_id": V2_STRATEGY_ID,
        "same_market_data": True,
        "same_point_in_time_universe": True,
        "same_targets": True,
        "same_funding": True,
        "same_execution_price": True,
        "same_transaction_cost_model": True,
        "control_is_benchmark_only": True,
        "control_repass_qualification": False,
    }.items():
        _require(paired, (key,), expected)

    safety = source.get("safety")
    if not isinstance(safety, Mapping):
        raise V2SpecError("缺少 safety")
    _require(safety, ("live_trading",), False)
    _require(safety, ("paper_only",), True)
    _require(safety, ("http_methods",), ["GET"])

    forbidden = source.get("explicitly_forbidden_v2_features")
    required_forbidden = {
        "btc_trend_filter",
        "btc_regime_filter",
        "bull_bear_switch",
        "stop_loss",
        "take_profit",
        "drawdown_throttle",
        "short_squeeze_filter",
        "funding_filter",
        "blacklist",
        "whitelist",
        "market_cap_filter",
        "dynamic_k",
        "dynamic_lookback",
        "dynamic_liquidity_threshold",
        "long_short_ratio_adjustment",
        "symbol_specific_sizing",
        "machine_learning",
        "ai_scoring",
        "parameter_sweep",
    }
    if set(forbidden or ()) != required_forbidden:
        raise V2SpecError("explicitly_forbidden_v2_features 已发生变化")

    if require_current_v1:
        from src.xs_lowvol_spec import (
            CONTROL_SPEC_HASH,
            CONTROL_STRATEGY_ID,
            strategy_spec_hash as current_v1_strategy_spec_hash,
        )

        if CONTROL_STRATEGY_ID != PARENT_CONTROL_STRATEGY_ID:
            raise V2SpecError("当前 V1 Control strategy_id 已变化")
        if CONTROL_SPEC_HASH != PARENT_CONTROL_SHA256:
            raise V2SpecError("当前 V1 Control hash 已变化")
        if current_v1_strategy_spec_hash() != PARENT_CONTROL_SHA256:
            raise V2SpecError("当前 V1 Control spec 内容已变化")


def validate_frozen_v2_spec() -> None:
    """Convenience entry point for the approved on-disk V2 spec."""
    validate_v2_spec()
