"""Frozen identity and temporal boundary for the V2.1 Forward phase.

This module registers V2.1's independent Forward anchor only.  It intentionally
contains no market-data loader, observation writer, PnL calculation, result
counter, or performance gate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from backtest.v2_1_protocol import V2_1_PROTOCOL_SHA256, verify_v2_1_protocol_hash
from src.xs_lowvol_spec import strategy_spec_hash as v1_strategy_spec_hash
from src.xs_lowvol_v2_1_spec import (
    V21_SPEC_SHA256,
    V21_STRATEGY_ID,
    verify_v2_1_spec_hash,
)
from src.xs_lowvol_v2_anchor import (
    validate_v2_forward_anchor,
    verify_v2_forward_anchor_hash,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
V2_1_FORWARD_ANCHOR_PATH = (
    PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_FORWARD_ANCHOR.yaml"
)
V2_1_FORWARD_ANCHOR_HASH_PATH = (
    PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_FORWARD_ANCHOR.sha256"
)

V21_FORWARD_ANCHOR_ID = "XS-LOWVOL-V2.1-FORWARD-ANCHOR-V1"
V2_1_FORWARD_ANCHOR_ID = V21_FORWARD_ANCHOR_ID
V21_FORWARD_ANCHOR_STATUS = "FROZEN_NOT_YET_STARTED"
V2_1_FORWARD_ANCHOR_STATUS = V21_FORWARD_ANCHOR_STATUS
APPROVED_V2_1_M0_COMMIT = "31fa706bf86d2177fcc251453c37d09abe8afed2"
APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC = datetime(
    2026, 9, 16, 6, 8, 22, tzinfo=timezone.utc
)
APPROVED_V2_1_M0_COMMIT_TIMESTAMP_TEXT = "2026-09-16T06:08:22Z"
APPROVED_V1_CONTROL_SHA256 = (
    "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678"
)
APPROVED_V2_1_SPEC_SHA256 = V21_SPEC_SHA256
APPROVED_V2_1_PROTOCOL_SHA256 = V2_1_PROTOCOL_SHA256

# Filled from the canonical YAML payload and sidecar after the anchor file is
# added.  The source pin is checked together with both on-disk representations.
APPROVED_V2_1_FORWARD_ANCHOR_SHA256 = "a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b"
V2_1_FORWARD_ANCHOR_SHA256 = APPROVED_V2_1_FORWARD_ANCHOR_SHA256

UTC = timezone.utc
_ANCHOR_FIELDS = frozenset(
    {
        "anchor_id",
        "status",
        "approved_v2_1_m0_commit",
        "approved_v2_1_m0_commit_timestamp_utc",
        "v1_control_sha256",
        "v2_1_spec_sha256",
        "v2_1_protocol_sha256",
        "strategy_id",
        "time_basis",
        "market_day_basis",
        "market_schedule",
        "execution_lag_days",
        "execution_day_rule",
        "logical_signal_time_rule",
        "forward_start_rule",
        "historical_backfill_into_forward",
        "forward_evidence_before_start",
        "warm_start",
        "paired_experiment",
        "temporal_validation",
        "provenance",
        "superseded_v2_anchor",
        "safety",
    }
)


class V21ForwardAnchorError(ValueError):
    """A V2.1 Forward anchor or its frozen identity is invalid."""

    code = "FORWARD_ANCHOR_INVALID"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


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


def canonical_v2_1_forward_anchor_json(anchor: Mapping[str, Any]) -> str:
    """Return the key-sorted canonical JSON representation of the anchor."""
    return json.dumps(
        _canonical(anchor), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _load_yaml(path: str | Path = V2_1_FORWARD_ANCHOR_PATH) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise V21ForwardAnchorError(f"cannot read V2.1 Forward anchor: {path}") from exc
    if not isinstance(raw, dict):
        raise V21ForwardAnchorError("V2.1 Forward anchor top level must be a mapping")
    return raw


def load_v2_1_forward_anchor(
    path: str | Path = V2_1_FORWARD_ANCHOR_PATH,
) -> dict[str, Any]:
    """Load the registered V2.1 Forward anchor without applying defaults."""
    return _load_yaml(path)


def v2_1_forward_anchor_sha256(
    anchor_or_path: Mapping[str, Any] | str | Path = V2_1_FORWARD_ANCHOR_PATH,
) -> str:
    """Hash a parsed V2.1 Forward anchor or its YAML path."""
    anchor = (
        _load_yaml(anchor_or_path)
        if isinstance(anchor_or_path, (str, Path))
        else anchor_or_path
    )
    if not isinstance(anchor, Mapping):
        raise V21ForwardAnchorError("V2.1 Forward anchor must be a mapping")
    return hashlib.sha256(
        canonical_v2_1_forward_anchor_json(anchor).encode("utf-8")
    ).hexdigest()


def read_v2_1_forward_anchor_hash(
    path: str | Path = V2_1_FORWARD_ANCHOR_HASH_PATH,
) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip().split()[0]
    except (IndexError, OSError) as exc:
        raise V21ForwardAnchorError(
            f"missing V2.1 Forward anchor hash sidecar: {path}"
        ) from exc
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise V21ForwardAnchorError(
            "V2.1 Forward anchor sidecar is not a lowercase SHA-256"
        )
    return token


def verify_v2_1_forward_anchor_hash(
    path: str | Path = V2_1_FORWARD_ANCHOR_PATH,
    hash_path: str | Path = V2_1_FORWARD_ANCHOR_HASH_PATH,
    *,
    approved_hash: str = APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
) -> str:
    """Require YAML, sidecar, and source-pinned anchor hashes to agree."""
    actual = v2_1_forward_anchor_sha256(path)
    recorded = read_v2_1_forward_anchor_hash(hash_path)
    if actual != recorded or actual != approved_hash:
        raise V21ForwardAnchorError(
            "V2.1 Forward anchor hash mismatch: "
            f"approved={approved_hash}, recorded={recorded}, actual={actual}"
        )
    return actual


def _require(anchor: Mapping[str, Any], field_name: str, expected: Any) -> None:
    actual = anchor.get(field_name)
    if actual != expected:
        raise V21ForwardAnchorError(
            f"{field_name} must be frozen as {expected!r}, got {actual!r}"
        )


def _require_mapping(
    anchor: Mapping[str, Any], field_name: str, expected: Mapping[str, Any]
) -> Mapping[str, Any]:
    actual = anchor.get(field_name)
    if not isinstance(actual, Mapping):
        raise V21ForwardAnchorError(f"{field_name} must be a mapping")
    if set(actual) != set(expected):
        raise V21ForwardAnchorError(f"{field_name} fields are not exactly frozen")
    for key, value in expected.items():
        if actual.get(key) != value:
            raise V21ForwardAnchorError(
                f"{field_name}.{key} must be frozen as {value!r}, got {actual.get(key)!r}"
            )
    return actual


def _validate_current_identities() -> None:
    if v1_strategy_spec_hash() != APPROVED_V1_CONTROL_SHA256:
        raise V21ForwardAnchorError("current V1 Control spec identity is not approved")
    if verify_v2_1_spec_hash() != APPROVED_V2_1_SPEC_SHA256:
        raise V21ForwardAnchorError("current V2.1 spec identity is not approved")
    if verify_v2_1_protocol_hash() != APPROVED_V2_1_PROTOCOL_SHA256:
        raise V21ForwardAnchorError("current V2.1 protocol identity is not approved")


def _validate_superseded_v2_anchor() -> None:
    try:
        validate_v2_forward_anchor()
        verify_v2_forward_anchor_hash()
    except Exception as exc:  # noqa: BLE001 - any old-anchor drift is fatal
        raise V21ForwardAnchorError(
            "superseded V2 Forward anchor is changed or invalid"
        ) from exc


def validate_v2_1_forward_anchor(
    anchor: Mapping[str, Any] | None = None,
    *,
    require_approved_hash: bool = True,
    require_immutable_lineage: bool = True,
    require_current_identities: bool = True,
) -> None:
    """Validate the complete independently pinned V2.1 Forward anchor."""
    source = load_v2_1_forward_anchor() if anchor is None else dict(anchor)
    if set(source) != _ANCHOR_FIELDS:
        raise V21ForwardAnchorError("V2.1 Forward anchor fields are not exactly frozen")

    actual = v2_1_forward_anchor_sha256(source)
    if anchor is None:
        recorded = read_v2_1_forward_anchor_hash()
        if recorded != actual:
            raise V21ForwardAnchorError(
                f"V2.1 Forward anchor sidecar mismatch: recorded={recorded}, actual={actual}"
            )
    if require_approved_hash and actual != APPROVED_V2_1_FORWARD_ANCHOR_SHA256:
        raise V21ForwardAnchorError(
            "V2.1 Forward anchor is not pinned to the approved SHA-256: "
            f"approved={APPROVED_V2_1_FORWARD_ANCHOR_SHA256}, actual={actual}"
        )

    _require(source, "anchor_id", V21_FORWARD_ANCHOR_ID)
    _require(source, "status", V21_FORWARD_ANCHOR_STATUS)
    _require(source, "approved_v2_1_m0_commit", APPROVED_V2_1_M0_COMMIT)
    _require(
        source,
        "approved_v2_1_m0_commit_timestamp_utc",
        APPROVED_V2_1_M0_COMMIT_TIMESTAMP_TEXT,
    )
    _require(source, "v1_control_sha256", APPROVED_V1_CONTROL_SHA256)
    _require(source, "v2_1_spec_sha256", APPROVED_V2_1_SPEC_SHA256)
    _require(source, "v2_1_protocol_sha256", APPROVED_V2_1_PROTOCOL_SHA256)
    _require(source, "strategy_id", V21_STRATEGY_ID)
    _require(source, "time_basis", "UTC")
    _require(source, "market_day_basis", "UTC_CALENDAR_DAY")
    _require(source, "market_schedule", "7x24")
    _require(source, "execution_lag_days", 1)
    _require(source, "execution_day_rule", "signal_day_plus_1_utc_calendar_day")
    _require(source, "logical_signal_time_rule", "execution_day_00:00:00Z")
    _require(source, "historical_backfill_into_forward", "FORBIDDEN")
    _require(source, "forward_evidence_before_start", "FORBIDDEN")

    _require_mapping(
        source,
        "forward_start_rule",
        {
            "minimum_logical_signal_time": "strictly_after_approved_v2_1_m0_commit_timestamp_utc",
            "first_signal_day": "NOT_SELECTED_IN_M0_2",
            "first_eligible_signal_selection": "forbidden_until_v2_1_m1_engineering_pass_and_v2_1_m2_forward_start",
            "once_selected_cannot_be_moved_later": True,
        },
    )
    _require_mapping(
        source,
        "warm_start",
        {
            "freeze_visible_completed_control_weekly_returns": "allowed_for_risk_state_initialization_only",
            "included_in_forward_pnl": False,
            "included_in_forward_return": False,
            "included_in_forward_sharpe": False,
            "included_in_forward_drawdown": False,
            "included_in_forward_profit_factor": False,
            "included_in_forward_bootstrap": False,
            "included_in_52_week_sample_count": False,
        },
    )
    _require_mapping(
        source,
        "paired_experiment",
        {
            "required": True,
            "control_strategy_id": "XS-LOWVOL-V1-Control",
            "v2_1_strategy_id": V21_STRATEGY_ID,
            "scheduler_authority": "V1_CONTROL_PARENT_ONLY",
            "independent_clock": "FORBIDDEN",
            "independent_retry": "FORBIDDEN",
            "independent_execution_decision": "FORBIDDEN",
            "risk_data_integrity_halt": "V2_DATA_INTEGRITY_HALT",
            "halt_semantics": "HALT_ENTIRE_PAIRED_EXPERIMENT",
            "collect_after_halt": "FORBIDDEN",
        },
    )
    _require_mapping(
        source,
        "temporal_validation",
        {
            "execution_day_same_as_signal_day": "FORBIDDEN",
            "execution_day_plus_2": "FORBIDDEN",
            "business_day_adjustment": "FORBIDDEN",
            "weekend_adjustment": "FORBIDDEN",
            "local_timezone_adjustment": "FORBIDDEN",
            "runtime_current_time": "FORBIDDEN",
            "caller_arbitrary_logical_signal_time": "FORBIDDEN",
        },
    )
    _require_mapping(
        source,
        "provenance",
        {
            "development_data_start": "2020-01-01",
            "development_data_end": "2026-08-31",
            "development_data_label": "CONTAMINATED_DEVELOPMENT_DATA",
            "historical_replay": "FORBIDDEN",
            "forward_backfill": "FORBIDDEN",
            "pre_start_evidence": "FORBIDDEN",
        },
    )
    _require_mapping(
        source,
        "superseded_v2_anchor",
        {
            "strategy_id": "XS-LOWVOL-V2-RISK15",
            "acceptance": "NOT_ACCEPTED_FOR_V2_1",
            "reuse": "FORBIDDEN",
        },
    )
    _require_mapping(
        source,
        "safety",
        {"live_trading": False, "paper_only": True, "http_methods": ["GET"]},
    )

    if require_immutable_lineage:
        _validate_superseded_v2_anchor()
    if require_current_identities:
        _validate_current_identities()


def _normalize_utc(logical_signal_time: datetime) -> datetime:
    if not isinstance(logical_signal_time, datetime):
        raise V21ForwardAnchorError(
            "logical_signal_time must be a timezone-aware datetime"
        )
    if logical_signal_time.tzinfo is None or logical_signal_time.utcoffset() is None:
        raise V21ForwardAnchorError(
            "logical_signal_time must be a timezone-aware datetime"
        )
    return logical_signal_time.astimezone(UTC)


def validate_forward_logical_signal_time(logical_signal_time: datetime) -> datetime:
    """Require a future observation instant strictly after the M0 freeze."""
    normalized = _normalize_utc(logical_signal_time)
    if normalized <= APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC:
        raise V21ForwardAnchorError(
            "logical_signal_time must be strictly after the approved V2.1 M0 freeze"
        )
    return normalized


def validate_forward_temporal_inputs(
    *,
    signal_day: date,
    execution_day: date,
    logical_signal_time: datetime,
) -> datetime:
    """Require +1 UTC calendar day and execution-day UTC midnight exactly."""
    if not isinstance(signal_day, date) or isinstance(signal_day, datetime):
        raise V21ForwardAnchorError("signal_day must be a UTC calendar date")
    if not isinstance(execution_day, date) or isinstance(execution_day, datetime):
        raise V21ForwardAnchorError("execution_day must be a UTC calendar date")
    if execution_day != signal_day + timedelta(days=1):
        raise V21ForwardAnchorError(
            "execution_day must equal signal_day plus exactly one UTC calendar day"
        )
    normalized = _normalize_utc(logical_signal_time)
    expected = datetime.combine(execution_day, time.min, tzinfo=UTC)
    if normalized != expected:
        raise V21ForwardAnchorError(
            "logical_signal_time must equal execution_day 00:00:00 UTC"
        )
    return validate_forward_logical_signal_time(normalized)


@dataclass(frozen=True)
class V21ForwardEpoch:
    """Immutable identity tuple for a future V2.1 Forward start."""

    approved_m0_commit: str = APPROVED_V2_1_M0_COMMIT
    approved_m0_commit_timestamp_utc: datetime = APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC
    v1_control_sha256: str = APPROVED_V1_CONTROL_SHA256
    v2_1_spec_sha256: str = APPROVED_V2_1_SPEC_SHA256
    v2_1_protocol_sha256: str = APPROVED_V2_1_PROTOCOL_SHA256
    forward_anchor_sha256: str = APPROVED_V2_1_FORWARD_ANCHOR_SHA256

    def __post_init__(self) -> None:
        expected = {
            "approved_m0_commit": APPROVED_V2_1_M0_COMMIT,
            "v1_control_sha256": APPROVED_V1_CONTROL_SHA256,
            "v2_1_spec_sha256": APPROVED_V2_1_SPEC_SHA256,
            "v2_1_protocol_sha256": APPROVED_V2_1_PROTOCOL_SHA256,
            "forward_anchor_sha256": APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
        }
        for field_name, expected_value in expected.items():
            if getattr(self, field_name) != expected_value:
                raise V21ForwardAnchorError(
                    f"{field_name} is not bound to the approved V2.1 identity"
                )
        if self.approved_m0_commit_timestamp_utc != APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC:
            raise V21ForwardAnchorError(
                "approved_m0_commit_timestamp_utc is not bound to the approved freeze"
            )
        validate_v2_1_forward_anchor()

    @property
    def approved_v2_1_m0_commit(self) -> str:
        return self.approved_m0_commit

    @property
    def approved_v2_1_m0_commit_timestamp_utc(self) -> datetime:
        return self.approved_m0_commit_timestamp_utc

    def validate_logical_signal_time(self, logical_signal_time: datetime) -> datetime:
        """Validate the strict post-freeze boundary for an observation."""
        return validate_forward_logical_signal_time(logical_signal_time)


def v2_1_forward_anchor_identity() -> dict[str, str]:
    """Return the validated immutable identity tuple used by future Forward."""
    validate_v2_1_forward_anchor()
    return {
        "anchor_id": V21_FORWARD_ANCHOR_ID,
        "status": V21_FORWARD_ANCHOR_STATUS,
        "anchor_sha256": verify_v2_1_forward_anchor_hash(),
        "approved_v2_1_m0_commit": APPROVED_V2_1_M0_COMMIT,
        "approved_v2_1_m0_commit_timestamp_utc": APPROVED_V2_1_M0_COMMIT_TIMESTAMP_TEXT,
        "v1_control_sha256": APPROVED_V1_CONTROL_SHA256,
        "v2_1_spec_sha256": APPROVED_V2_1_SPEC_SHA256,
        "v2_1_protocol_sha256": APPROVED_V2_1_PROTOCOL_SHA256,
        "strategy_id": V21_STRATEGY_ID,
    }


# Compatibility names keep the identity API discoverable without aliasing old
# V2 validation or execution behavior.
load_forward_anchor = load_v2_1_forward_anchor
forward_anchor_sha256 = v2_1_forward_anchor_sha256
read_forward_anchor_hash = read_v2_1_forward_anchor_hash
verify_forward_anchor_hash = verify_v2_1_forward_anchor_hash
validate_forward_anchor = validate_v2_1_forward_anchor
forward_anchor_identity = v2_1_forward_anchor_identity
verify_anchor_hash = verify_v2_1_forward_anchor_hash


__all__ = [
    "APPROVED_V1_CONTROL_SHA256",
    "APPROVED_V2_1_FORWARD_ANCHOR_SHA256",
    "APPROVED_V2_1_M0_COMMIT",
    "APPROVED_V2_1_M0_COMMIT_TIMESTAMP_TEXT",
    "APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC",
    "APPROVED_V2_1_PROTOCOL_SHA256",
    "APPROVED_V2_1_SPEC_SHA256",
    "V21_FORWARD_ANCHOR_ID",
    "V21_FORWARD_ANCHOR_STATUS",
    "V21ForwardAnchorError",
    "V21ForwardEpoch",
    "V2_1_FORWARD_ANCHOR_HASH_PATH",
    "V2_1_FORWARD_ANCHOR_ID",
    "V2_1_FORWARD_ANCHOR_PATH",
    "V2_1_FORWARD_ANCHOR_SHA256",
    "V2_1_FORWARD_ANCHOR_STATUS",
    "canonical_v2_1_forward_anchor_json",
    "forward_anchor_sha256",
    "forward_anchor_identity",
    "load_forward_anchor",
    "load_v2_1_forward_anchor",
    "read_forward_anchor_hash",
    "read_v2_1_forward_anchor_hash",
    "validate_forward_anchor",
    "validate_forward_logical_signal_time",
    "validate_forward_temporal_inputs",
    "validate_v2_1_forward_anchor",
    "v2_1_forward_anchor_identity",
    "v2_1_forward_anchor_sha256",
    "verify_anchor_hash",
    "verify_forward_anchor_hash",
    "verify_v2_1_forward_anchor_hash",
]
