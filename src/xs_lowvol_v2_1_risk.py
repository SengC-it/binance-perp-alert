"""Frozen XS-LOWVOL-V2.1 risk-layer semantic repair.

V2.1 keeps the V2 risk parameters but fixes two semantic defects from the
failed V2 engineering replay:

* an exact zero reference volatility is valid and receives scale one;
* malformed or incomplete Control observations halt the paired experiment.

The module deliberately reuses only the neutral position and weekly-return
data shapes from the old V2 layer.  It does not change or call the old V2
position-scale formula.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Mapping, Sequence

from src.xs_lowvol_v2_risk import (
    ControlWeeklyReturn,
    PositionChange,
    PositionState,
    TargetPosition,
    V2RiskScaleInvalid,
    plan_position_changes,
    scaled_target_positions,
    validate_forward_weekly_returns as _validate_legacy_forward_weekly_returns,
)

from .xs_lowvol_v2_1_spec import (
    ANNUALIZATION_FACTOR,
    MAX_SCALE,
    MIN_SCALE,
    TARGET_ANNUALIZED_VOL,
    VOL_LOOKBACK_WEEKS,
    validate_v2_1_spec,
)


UTC = timezone.utc
VALID = "VALID"
V21_VALID = VALID
V2_DATA_INTEGRITY_HALT = "V2_DATA_INTEGRITY_HALT"
DATA_INTEGRITY_HALT = V2_DATA_INTEGRITY_HALT


class V21DataIntegrityHalt(ValueError):
    """A required V1 Control observation is not valid point-in-time data."""

    code = V2_DATA_INTEGRITY_HALT

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


V2DataIntegrityHalt = V21DataIntegrityHalt


@dataclass(frozen=True)
class V21RiskScaleResult:
    """Auditable result of one V2.1 risk-layer evaluation."""

    status: str
    reference_vol: float | None = None
    position_scale: float | None = None
    selected_count: int = 0
    reason: str = ""

    @property
    def valid(self) -> bool:
        return self.status == VALID


def _halt(reason: str) -> V21DataIntegrityHalt:
    return V21DataIntegrityHalt(reason)


def _signal_cutoff(signal_time: date | datetime | None) -> datetime | None:
    if signal_time is None:
        return None
    if isinstance(signal_time, datetime):
        if signal_time.tzinfo is None or signal_time.utcoffset() is None:
            raise _halt("logical signal time must be timezone-aware")
        return signal_time.astimezone(UTC)
    if isinstance(signal_time, date):
        return datetime.combine(signal_time, time.min, tzinfo=UTC)
    raise _halt("logical signal time has an unsupported shape")


def _validate_input_records(
    weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any]],
) -> tuple[ControlWeeklyReturn, ...]:
    """Apply the strict Forward provenance checks under the V2.1 halt code."""
    if weekly_returns is None:
        raise _halt("Control weekly returns are missing")
    try:
        values = tuple(weekly_returns)
    except TypeError as exc:
        raise _halt("Control weekly returns must be a sequence") from exc
    if not values:
        raise _halt("Control weekly returns are empty")

    try:
        normalized = _validate_legacy_forward_weekly_returns(values)
    except V2RiskScaleInvalid as exc:
        raise _halt(str(exc)) from exc

    for record in normalized:
        if not record.complete:
            raise _halt("a Control weekly return is incomplete")
        try:
            if not math.isfinite(float(record.net_return)):
                raise _halt("weekly net return is not finite")
        except (TypeError, ValueError, OverflowError) as exc:
            raise _halt("weekly net return is not numeric") from exc
    return normalized


def validate_v2_1_weekly_returns(
    weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any]],
) -> tuple[ControlWeeklyReturn, ...]:
    """Validate explicit, timezone-aware, completed Control observations."""
    return _validate_input_records(weekly_returns)


def completed_control_weekly_returns(
    weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any]],
    *,
    signal_time: date | datetime | None = None,
    lookback_weeks: int = VOL_LOOKBACK_WEEKS,
) -> tuple[ControlWeeklyReturn, ...]:
    """Select the latest 13 completed observations known before the signal.

    Observations at or after the signal cutoff are excluded before the
    required-window count.  A future row therefore cannot change a scale, but
    an otherwise malformed row still fails the data-integrity contract.
    """
    if lookback_weeks != VOL_LOOKBACK_WEEKS:
        raise _halt("lookback_weeks is not the frozen value")
    cutoff = _signal_cutoff(signal_time)
    records = _validate_input_records(weekly_returns)
    candidates = [
        record
        for record in records
        if cutoff is None or record.completion_time < cutoff
    ]
    candidates.sort(key=lambda record: (record.completion_time, record.week_ending))
    if len(candidates) < lookback_weeks:
        raise _halt(
            f"fewer than {lookback_weeks} complete Control weeks before signal time"
        )
    selected = tuple(candidates[-lookback_weeks:])
    if any(not record.complete for record in selected):
        raise _halt("a required Control weekly return is incomplete")
    return selected


def reference_annualized_volatility(
    weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any]],
    *,
    signal_time: date | datetime | None = None,
) -> float:
    """Return population weekly volatility annualized by ``sqrt(52)``.

    Unlike the old V2 formula, zero is a valid, finite reference volatility.
    """
    selected = completed_control_weekly_returns(
        weekly_returns,
        signal_time=signal_time,
    )
    values = [float(record.net_return) for record in selected]
    annualization_factor = math.sqrt(52.0)
    if ANNUALIZATION_FACTOR != "sqrt(52)":
        raise _halt("annualization rule is not frozen as sqrt(52)")
    reference_vol = statistics.pstdev(values) * annualization_factor
    if not math.isfinite(reference_vol) or reference_vol < 0.0:
        raise _halt("reference volatility is negative or non-finite")
    return 0.0 if reference_vol == 0.0 else float(reference_vol)


def validate_position_scale(scale: float) -> float:
    """Validate a bounded no-leverage V2.1 scale."""
    try:
        value = float(scale)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _halt("position scale is not numeric") from exc
    if not math.isfinite(value):
        raise _halt("position scale is non-finite")
    if value < MIN_SCALE or value > MAX_SCALE:
        raise _halt("position scale is outside the frozen 0..1 range")
    return value


def position_scale_from_reference_vol(reference_vol: float) -> float:
    """Apply the frozen V2.1 zero/positive reference-volatility rule."""
    try:
        reference = float(reference_vol)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _halt("reference volatility is not numeric") from exc
    if not math.isfinite(reference) or reference < 0.0:
        raise _halt("reference volatility is negative or non-finite")
    if reference == 0.0:
        return 1.0
    scale = min(1.0, TARGET_ANNUALIZED_VOL / reference)
    return validate_position_scale(scale)


def calculate_position_scale(
    weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any]],
    *,
    signal_time: date | datetime | None = None,
) -> float:
    """Compute the V2.1 scale from strictly point-in-time Control returns."""
    reference = reference_annualized_volatility(
        weekly_returns,
        signal_time=signal_time,
    )
    return position_scale_from_reference_vol(reference)


def evaluate_risk_scale(
    weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any]] | None,
    *,
    signal_time: date | datetime | None = None,
) -> V21RiskScaleResult:
    """Return a status object so the paired engine can halt atomically."""
    try:
        selected = completed_control_weekly_returns(
            weekly_returns,
            signal_time=signal_time,
        )
        reference = reference_annualized_volatility(
            selected,
            signal_time=None,
        )
        scale = position_scale_from_reference_vol(reference)
    except V21DataIntegrityHalt as exc:
        return V21RiskScaleResult(
            status=V2_DATA_INTEGRITY_HALT,
            reason=str(exc),
        )
    return V21RiskScaleResult(
        status=VALID,
        reference_vol=reference,
        position_scale=scale,
        selected_count=len(selected),
    )


# Descriptive aliases keep the public API consistent with the earlier risk
# layer while ensuring callers land on the repaired implementation.
compute_risk_scale = calculate_position_scale
risk_scale_for_signal = calculate_position_scale


__all__ = [
    "ControlWeeklyReturn",
    "DATA_INTEGRITY_HALT",
    "MAX_SCALE",
    "MIN_SCALE",
    "PositionChange",
    "PositionState",
    "TargetPosition",
    "UTC",
    "VALID",
    "V21DataIntegrityHalt",
    "V21RiskScaleResult",
    "V21_VALID",
    "V2DataIntegrityHalt",
    "V2_DATA_INTEGRITY_HALT",
    "calculate_position_scale",
    "completed_control_weekly_returns",
    "compute_risk_scale",
    "evaluate_risk_scale",
    "plan_position_changes",
    "position_scale_from_reference_vol",
    "reference_annualized_volatility",
    "risk_scale_for_signal",
    "scaled_target_positions",
    "validate_position_scale",
    "validate_v2_1_spec",
    "validate_v2_1_weekly_returns",
]
