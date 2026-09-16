"""Frozen XS-LOWVOL-V2.1 semantic repair specification.

V2.1 is a new identity, not an in-place edit of the failed V2 lineage.  It
keeps the V2 risk parameters, makes exact zero reference volatility valid at
scale one, and makes the V1 Control parent the sole schedule authority.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from src.xs_lowvol_spec import (
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
    strategy_spec_hash as v1_strategy_spec_hash,
)
from src.xs_lowvol_v2_spec import (
    V2_SPEC_SHA256 as OLD_V2_SPEC_SHA256,
    V2_STRATEGY_ID as OLD_V2_STRATEGY_ID,
    verify_v2_spec_hash as verify_old_v2_spec_hash,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
V2_1_SPEC_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_SPEC.yaml"
V2_1_SPEC_HASH_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_SPEC.sha256"

V21_STRATEGY_ID = "XS-LOWVOL-V2.1-RISK15"
V2_1_STRATEGY_ID = V21_STRATEGY_ID
PARENT_V2_STRATEGY_ID = OLD_V2_STRATEGY_ID
PARENT_CONTROL_STRATEGY_ID = CONTROL_STRATEGY_ID
PARENT_CONTROL_SHA256 = "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678"
V2_1_SPEC_SHA256 = "e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c"
V21_SPEC_SHA256 = V2_1_SPEC_SHA256
APPROVED_V2_1_SPEC_SHA256 = V2_1_SPEC_SHA256
V2_1_SPEC_HASH = V2_1_SPEC_SHA256
TARGET_ANNUALIZED_VOL = 0.15
VOL_LOOKBACK_WEEKS = 13
MIN_SCALE = 0.0
MAX_SCALE = 1.0
ANNUALIZATION_FACTOR = "sqrt(52)"
ZERO_REFERENCE_VOL_SEMANTICS = "SCALE_ONE"
SCHEDULER_AUTHORITY = "V1_CONTROL_PARENT_ONLY"
V2_SPECIFIC_RETRY = "FORBIDDEN"
DATA_INTEGRITY_FAILURE_SEMANTICS = "HALT_PAIRED_EXPERIMENT"


class V21SpecError(ValueError):
    """A V2.1 spec or lineage identity is invalid."""


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


def canonical_v2_1_spec_json(spec: Mapping[str, Any]) -> str:
    return json.dumps(
        _canonical(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _load_yaml(path: str | Path = V2_1_SPEC_PATH) -> dict[str, Any]:
    try:
        import yaml

        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, ImportError) as exc:
        raise V21SpecError(f"cannot read V2.1 spec: {path}") from exc
    except Exception as exc:  # noqa: BLE001 - parser errors are identity failures
        raise V21SpecError(f"cannot parse V2.1 spec: {path}") from exc
    if not isinstance(raw, dict):
        raise V21SpecError("V2.1 spec top level must be a mapping")
    return raw


def load_v2_1_spec(path: str | Path = V2_1_SPEC_PATH) -> dict[str, Any]:
    return deepcopy(_load_yaml(path))


def v2_1_spec_sha256(spec_or_path: Mapping[str, Any] | str | Path = V2_1_SPEC_PATH) -> str:
    spec = _load_yaml(spec_or_path) if isinstance(spec_or_path, (str, Path)) else spec_or_path
    if not isinstance(spec, Mapping):
        raise V21SpecError("V2.1 spec must be a mapping")
    return hashlib.sha256(canonical_v2_1_spec_json(spec).encode("utf-8")).hexdigest()


def strategy_spec_hash(path: str | Path = V2_1_SPEC_PATH) -> str:
    """Return the current V2.1 strategy-spec digest."""
    return v2_1_spec_sha256(path)


def read_v2_1_spec_hash(path: str | Path = V2_1_SPEC_HASH_PATH) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip().split()[0]
    except (IndexError, OSError) as exc:
        raise V21SpecError(f"missing V2.1 spec hash sidecar: {path}") from exc
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise V21SpecError("V2.1 spec hash sidecar is not a lowercase SHA-256")
    return token


def verify_v2_1_spec_hash(
    path: str | Path = V2_1_SPEC_PATH,
    hash_path: str | Path = V2_1_SPEC_HASH_PATH,
    *,
    approved_hash: str = V2_1_SPEC_SHA256,
) -> str:
    actual = v2_1_spec_sha256(path)
    recorded = read_v2_1_spec_hash(hash_path)
    if actual != recorded or actual != approved_hash:
        raise V21SpecError(
            "V2.1 spec hash mismatch: "
            f"approved={approved_hash}, recorded={recorded}, actual={actual}"
        )
    return actual


def _require(mapping: Mapping[str, Any], key: str, expected: Any) -> None:
    actual = mapping.get(key)
    if actual != expected:
        raise V21SpecError(f"{key} must be frozen as {expected!r}, got {actual!r}")


def validate_v2_1_spec(
    spec: Mapping[str, Any] | None = None,
    *,
    require_approved_hash: bool = True,
    require_current_lineage: bool = True,
) -> None:
    """Validate V2.1 semantics and the immutable failed-V2 lineage."""
    source = load_v2_1_spec() if spec is None else deepcopy(dict(spec))
    if require_approved_hash:
        verify_v2_1_spec_hash()

    _require(source, "strategy_id", V21_STRATEGY_ID)
    _require(source, "status", "frozen-m0-semantic-repair")
    _require(source, "parent_strategy", PARENT_V2_STRATEGY_ID)
    _require(source, "supersedes_strategy", PARENT_V2_STRATEGY_ID)
    _require(
        source,
        "correction_reason",
        "ZERO_REFERENCE_VOL_AND_V2_SPECIFIC_RETRY_CREATED_PARENT_SCHEDULE_DIVERGENCE",
    )
    _require(source, "supersedes_engineering_run_commit", "2a88849f19897e76a229ceb0bb7840abccd80dd4")
    _require(source, "superseded_code_commit", "c7a114d0434b107a9a4487b0bacd7a97add04dc2")
    _require(source, "superseded_failure_gates", ["E2_risk_formula", "E6_retry_and_paired_schedule"])
    _require(source, "first_observed_divergence", date(2020, 8, 14))

    parent = source.get("parent_v1_control")
    if not isinstance(parent, Mapping):
        raise V21SpecError("missing parent_v1_control")
    _require(parent, "strategy_id", PARENT_CONTROL_STRATEGY_ID)
    _require(parent, "sha256", PARENT_CONTROL_SHA256)

    inherited = source.get("inherited_v1_semantics")
    if not isinstance(inherited, Mapping):
        raise V21SpecError("missing inherited_v1_semantics")
    inherited_expected = {
        "completed_daily_returns_lookback": 30,
        "candle_interval": "1d",
        "completed_candles_only": True,
        "signal_day_quote_volume_min_usdt": 50_000_000,
        "point_in_time_universe": True,
        "delisted_symbol_semantics": "inherited_unchanged",
        "lifecycle_ambiguity": "fail_closed",
        "long_slots": 5,
        "short_slots": 5,
        "ranking": "inherited_unchanged",
        "tie_break": "symbol_ascending",
        "execution_lag_days": 1,
        "execution_price": "next_completed_candle_close",
        "scheduler": SCHEDULER_AUTHORITY,
        "retry_semantics": "inherit_parent_retry_without_v2_specific_retry",
        "funding": "actual_settled_events_only",
        "transaction_cost": "inherited_taker_plus_slippage",
        "target_symbols": "exactly_parent_control_targets",
    }
    for key, expected in inherited_expected.items():
        _require(inherited, key, expected)

    risk = source.get("risk_layer")
    if not isinstance(risk, Mapping):
        raise V21SpecError("missing risk_layer")
    risk_expected = {
        "target_annualized_vol": TARGET_ANNUALIZED_VOL,
        "vol_lookback_weeks": VOL_LOOKBACK_WEEKS,
        "vol_estimator": "population_std",
        "annualization": ANNUALIZATION_FACTOR,
        "annualization_factor": ANNUALIZATION_FACTOR,
        "min_scale": MIN_SCALE,
        "max_scale": MAX_SCALE,
        "leverage_allowed": False,
        "input_strategy": PARENT_CONTROL_STRATEGY_ID,
        "input_return_frequency": "weekly",
        "input_return_definition": "completed_control_weekly_net_return",
        "completed_at_rule": "strict_before_logical_signal_time",
        "zero_reference_vol_semantics": ZERO_REFERENCE_VOL_SEMANTICS,
        "positive_reference_vol_semantics": "min(1.0, 0.15 / reference_vol)",
        "negative_or_nonfinite_reference_vol_semantics": "V2_DATA_INTEGRITY_HALT",
        "invalid_input_semantics": "V2_DATA_INTEGRITY_HALT",
        "invalid_input_policy": "halt_entire_paired_experiment",
        "future_observation_semantics": "exclude_from_required_window",
        "all_legs_use_same_scale": True,
        "scale_above_one": "forbidden",
    }
    for key, expected in risk_expected.items():
        _require(risk, key, expected)

    _require(source, "scheduler_authority", SCHEDULER_AUTHORITY)
    _require(source, "v2_specific_retry", V2_SPECIFIC_RETRY)
    _require(source, "independent_success_clock", "FORBIDDEN")
    _require(source, "data_integrity_failure_semantics", DATA_INTEGRITY_FAILURE_SEMANTICS)

    paired = source.get("paired_semantics")
    if not isinstance(paired, Mapping):
        raise V21SpecError("missing paired_semantics")
    for key, expected in {
        "parent_failure": "INHERIT_PARENT_STATUS_AND_RETRY",
        "parent_success_with_valid_risk": "PAIRED_SUCCESS_SAME_TARGETS_AND_DATES",
        "parent_success_with_data_integrity_failure": "HALT_ENTIRE_PAIRED_EXPERIMENT",
        "v1_benchmark_and_v21_must_remain_paired": True,
        "v21_changes_only_notional_scale": True,
    }.items():
        _require(paired, key, expected)

    position = source.get("position_sizing")
    if not isinstance(position, Mapping):
        raise V21SpecError("missing position_sizing")
    for key, expected in {
        "v1_target_notional": "unchanged",
        "v21_target_notional": "v1_target_notional_times_position_scale",
        "same_side_resize": "trade_absolute_notional_delta_only",
        "direction_flip": "close_old_direction_then_open_new_direction",
        "zero_scale": "allowed_without_leverage",
    }.items():
        _require(position, key, expected)

    anchor = source.get("forward_anchor")
    if not isinstance(anchor, Mapping):
        raise V21SpecError("missing forward_anchor")
    _require(anchor, "status", "NOT_CREATED_IN_V2_1_M0")
    _require(anchor, "creation", "forbidden_until_v2_1_m0_independent_acceptance")
    _require(anchor, "historical_backfill_into_forward", "forbidden")

    provenance = source.get("provenance")
    if not isinstance(provenance, Mapping):
        raise V21SpecError("missing provenance")
    for key, expected in {
        "development_data_label": "CONTAMINATED_DEVELOPMENT_DATA",
        "historical_replay_in_m0": "forbidden",
        "performance_output_in_m0": "forbidden",
        "forward_evidence_in_m0": "forbidden",
    }.items():
        _require(provenance, key, expected)

    safety = source.get("safety")
    if not isinstance(safety, Mapping):
        raise V21SpecError("missing safety")
    _require(safety, "live_trading", False)
    _require(safety, "paper_only", True)
    _require(safety, "http_methods", ["GET"])

    if require_current_lineage:
        if CONTROL_SPEC_HASH != PARENT_CONTROL_SHA256 or v1_strategy_spec_hash() != PARENT_CONTROL_SHA256:
            raise V21SpecError("current V1 Control spec hash changed")
        if verify_old_v2_spec_hash() != OLD_V2_SPEC_SHA256:
            raise V21SpecError("superseded V2 spec hash changed")


def spec_sha256(spec_or_path: Mapping[str, Any] | str | Path = V2_1_SPEC_PATH) -> str:
    """Compatibility name for the V2.1 canonical strategy-spec digest."""
    return v2_1_spec_sha256(spec_or_path)


load_strategy_spec = load_v2_1_spec
verify_spec_hash = verify_v2_1_spec_hash
validate_frozen_v2_1_spec = validate_v2_1_spec


__all__ = [
    "ANNUALIZATION_FACTOR",
    "APPROVED_V2_1_SPEC_SHA256",
    "DATA_INTEGRITY_FAILURE_SEMANTICS",
    "MAX_SCALE",
    "MIN_SCALE",
    "PARENT_CONTROL_SHA256",
    "PARENT_CONTROL_STRATEGY_ID",
    "PARENT_V2_STRATEGY_ID",
    "SCHEDULER_AUTHORITY",
    "TARGET_ANNUALIZED_VOL",
    "V21_SPEC_SHA256",
    "V21_STRATEGY_ID",
    "V2_1_SPEC_HASH_PATH",
    "V2_1_SPEC_HASH",
    "V2_1_SPEC_PATH",
    "V2_1_SPEC_SHA256",
    "V2_1_STRATEGY_ID",
    "V2_SPECIFIC_RETRY",
    "VOL_LOOKBACK_WEEKS",
    "ZERO_REFERENCE_VOL_SEMANTICS",
    "V21SpecError",
    "canonical_v2_1_spec_json",
    "load_v2_1_spec",
    "load_strategy_spec",
    "spec_sha256",
    "read_v2_1_spec_hash",
    "strategy_spec_hash",
    "validate_frozen_v2_1_spec",
    "v2_1_spec_sha256",
    "validate_v2_1_spec",
    "verify_v2_1_spec_hash",
    "verify_spec_hash",
]
