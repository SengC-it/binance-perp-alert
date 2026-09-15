"""The frozen, no-leverage V2 volatility-targeting layer.

This module accepts completed V1 Control weekly net-return observations and
turns them into a bounded position scale.  It contains no market-data loader
and no historical replay entry point, so V2-M0 can test the risk layer without
looking at V2 performance.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Mapping, Sequence

from .xs_lowvol_v2_spec import V2_RISK_CONFIG, V2RiskConfig


UTC = timezone.utc


class V2RiskScaleInvalid(ValueError):
    """A risk estimate is incomplete or unsafe; the rebalance must fail closed."""

    code = "V2_RISK_SCALE_INVALID"

    def __init__(self, reason: str):
        super().__init__(f"{self.code}: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class ControlWeeklyReturn:
    """One completed V1 Control weekly net return visible to the V2 layer."""

    week_ending: date
    net_return: float
    completed_at: date | datetime | None = None
    complete: bool = True

    @property
    def completion_time(self) -> datetime:
        marker = self.completed_at if self.completed_at is not None else self.week_ending
        if isinstance(marker, datetime):
            return marker if marker.tzinfo is not None else marker.replace(tzinfo=UTC)
        return datetime.combine(marker, time.min, tzinfo=UTC)


@dataclass(frozen=True)
class TargetPosition:
    """A V1 target direction with V2-scaled notional."""

    symbol: str
    direction: int
    notional: float


@dataclass(frozen=True)
class PositionState:
    """A currently held position used to plan a rebalance transition."""

    symbol: str
    direction: int
    notional: float


@dataclass(frozen=True)
class PositionChange:
    """A deterministic open, close, or same-side resize delta."""

    action: str
    symbol: str
    direction: int
    notional: float
    notional_delta: float
    cost_notional: float


def _as_date(value: Any, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise V2RiskScaleInvalid(f"{field_name} is not a valid date") from exc


def _as_completion_time(value: Any, *, field_name: str) -> date | datetime | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise V2RiskScaleInvalid(f"{field_name} is not a valid completion time") from exc


def _coerce_weekly_return(value: Any, index: int) -> ControlWeeklyReturn:
    if isinstance(value, ControlWeeklyReturn):
        record = value
    elif isinstance(value, Mapping):
        week_value = value.get("week_ending", value.get("week_end"))
        if week_value is None:
            raise V2RiskScaleInvalid("weekly return is missing week_ending")
        if "net_return" not in value:
            raise V2RiskScaleInvalid("weekly return is missing net_return")
        record = ControlWeeklyReturn(
            week_ending=_as_date(week_value, field_name="week_ending"),
            net_return=float(value["net_return"]),
            completed_at=_as_completion_time(value.get("completed_at"), field_name="completed_at"),
            complete=value.get("complete", True) is True,
        )
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # Numeric-only inputs are intentionally treated as an already ordered
        # sequence of completed observations.  Signal-time filtering requires
        # the dated record form above so future data cannot be mistaken as old.
        record = ControlWeeklyReturn(
            week_ending=date.fromordinal(date(1970, 1, 1).toordinal() + index),
            net_return=float(value),
        )
    else:
        raise V2RiskScaleInvalid("weekly return has an unsupported shape")

    try:
        finite_return = math.isfinite(float(record.net_return))
    except (TypeError, ValueError, OverflowError) as exc:
        raise V2RiskScaleInvalid("weekly net return is not numeric") from exc
    if not finite_return:
        raise V2RiskScaleInvalid("weekly net return is not finite")
    if not isinstance(record.complete, bool):
        raise V2RiskScaleInvalid("weekly return completeness flag is invalid")
    return record


def completed_control_weekly_returns(
    weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any] | float],
    *,
    signal_time: date | datetime | None = None,
    lookback_weeks: int = V2_RISK_CONFIG.vol_lookback_weeks,
) -> tuple[ControlWeeklyReturn, ...]:
    """Select the latest complete pre-signal Control weeks, fail closed."""
    if lookback_weeks != V2_RISK_CONFIG.vol_lookback_weeks:
        raise V2RiskScaleInvalid("lookback_weeks is not the frozen value")
    if not weekly_returns:
        raise V2RiskScaleInvalid("fewer than 13 complete Control weeks")

    records = tuple(_coerce_weekly_return(value, index) for index, value in enumerate(weekly_returns))
    cutoff: datetime | None = None
    if signal_time is not None:
        if isinstance(signal_time, datetime):
            cutoff = signal_time if signal_time.tzinfo is not None else signal_time.replace(tzinfo=UTC)
        else:
            cutoff = datetime.combine(signal_time, time.min, tzinfo=UTC)

    candidates: list[ControlWeeklyReturn] = []
    for record in records:
        if cutoff is not None and record.completion_time >= cutoff:
            # Future or same-time observations are not inputs to this signal.
            continue
        if not record.complete:
            raise V2RiskScaleInvalid("a required Control weekly return is incomplete")
        candidates.append(record)
    candidates.sort(key=lambda record: (record.completion_time, record.week_ending))
    if len(candidates) < lookback_weeks:
        raise V2RiskScaleInvalid("fewer than 13 complete Control weeks before signal time")
    return tuple(candidates[-lookback_weeks:])


def reference_annualized_volatility(
    weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any] | float],
    *,
    signal_time: date | datetime | None = None,
) -> float:
    """Estimate annualized population volatility from the frozen lookback."""
    selected = completed_control_weekly_returns(weekly_returns, signal_time=signal_time)
    values = [float(record.net_return) for record in selected]
    reference_vol = statistics.pstdev(values) * V2_RISK_CONFIG.annualization_factor
    if not math.isfinite(reference_vol) or reference_vol <= 0.0:
        raise V2RiskScaleInvalid("reference volatility is zero or non-finite")
    return reference_vol


def validate_position_scale(scale: float, *, config: V2RiskConfig = V2_RISK_CONFIG) -> float:
    """Validate and return a bounded no-leverage scale."""
    value = float(scale)
    if not math.isfinite(value):
        raise V2RiskScaleInvalid("position scale is non-finite")
    if value < config.min_scale or value > config.max_scale:
        raise V2RiskScaleInvalid("position scale is outside the frozen 0..1 range")
    if config.min_scale != 0.0 or config.max_scale != 1.0 or config.leverage_allowed:
        raise V2RiskScaleInvalid("risk config permits an unsafe scale")
    return value


def position_scale_from_reference_vol(
    reference_vol: float,
    *,
    config: V2RiskConfig = V2_RISK_CONFIG,
) -> float:
    """Apply the frozen min(1, target/reference) rule."""
    reference = float(reference_vol)
    target = float(config.target_annualized_vol)
    if not math.isfinite(reference) or reference <= 0.0:
        raise V2RiskScaleInvalid("reference volatility is zero or non-finite")
    if not math.isfinite(target) or target <= 0.0:
        raise V2RiskScaleInvalid("target volatility is invalid")
    scale = min(1.0, target / reference)
    return validate_position_scale(scale, config=config)


def calculate_position_scale(
    weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any] | float],
    *,
    signal_time: date | datetime | None = None,
) -> float:
    """Compute V2 scale from complete Control returns known at signal time."""
    reference = reference_annualized_volatility(weekly_returns, signal_time=signal_time)
    return position_scale_from_reference_vol(reference)


# Descriptive aliases make the frozen formula explicit to callers.
compute_risk_scale = calculate_position_scale
risk_scale_for_signal = calculate_position_scale


def _validate_direction(direction: Any) -> int:
    if isinstance(direction, bool) or direction not in (-1, 1):
        raise ValueError("direction must be -1 or +1")
    return int(direction)


def scaled_target_positions(
    target_directions: Mapping[str, int],
    *,
    base_notional: float,
    position_scale: float,
) -> dict[str, TargetPosition]:
    """Scale every inherited V1 target by exactly the same bounded amount."""
    base = float(base_notional)
    if not math.isfinite(base) or base < 0.0:
        raise ValueError("base_notional must be finite and non-negative")
    scale = validate_position_scale(position_scale)
    targets: dict[str, TargetPosition] = {}
    for raw_symbol, raw_direction in sorted(target_directions.items()):
        symbol = str(raw_symbol).upper()
        if not symbol:
            raise ValueError("target symbol cannot be empty")
        direction = _validate_direction(raw_direction)
        targets[symbol] = TargetPosition(symbol, direction, base * scale)
    return targets


def _coerce_position(value: Any, symbol: str) -> PositionState:
    if isinstance(value, PositionState):
        return value
    if isinstance(value, TargetPosition):
        return PositionState(value.symbol, value.direction, value.notional)
    if isinstance(value, Mapping):
        return PositionState(
            symbol,
            _validate_direction(value.get("direction")),
            float(value.get("notional")),
        )
    raise ValueError("position has an unsupported shape")


def plan_position_changes(
    old_positions: Mapping[str, PositionState | TargetPosition | Mapping[str, Any]],
    new_targets: Mapping[str, TargetPosition],
) -> tuple[PositionChange, ...]:
    """Plan deterministic deltas; same-side resize never fabricates a flip."""
    old = {
        str(symbol).upper(): _coerce_position(value, str(symbol).upper())
        for symbol, value in old_positions.items()
    }
    new = {str(symbol).upper(): value for symbol, value in new_targets.items()}
    changes: list[PositionChange] = []
    for symbol in sorted(set(old) | set(new)):
        prior = old.get(symbol)
        target = new.get(symbol)
        if prior is None and target is not None:
            if target.notional > 0.0:
                changes.append(PositionChange("OPEN", symbol, target.direction, target.notional, target.notional, target.notional))
            continue
        if prior is not None and target is None:
            changes.append(PositionChange("CLOSE", symbol, prior.direction, prior.notional, -prior.notional, prior.notional))
            continue
        if prior is None or target is None:
            continue
        if prior.direction != target.direction:
            changes.append(PositionChange("CLOSE", symbol, prior.direction, prior.notional, -prior.notional, prior.notional))
            if target.notional > 0.0:
                changes.append(PositionChange("OPEN", symbol, target.direction, target.notional, target.notional, target.notional))
            continue
        delta = float(target.notional) - float(prior.notional)
        if math.isclose(delta, 0.0, rel_tol=0.0, abs_tol=1e-15):
            continue
        changes.append(PositionChange("RESIZE", symbol, prior.direction, abs(delta), delta, abs(delta)))
    return tuple(changes)


@dataclass(frozen=True)
class RequiredFundingSettlement:
    """A real settlement identifier required by an observed holding interval."""

    symbol: str
    settled_at: int


class FundingCoverageError(ValueError):
    """A required funding settlement is not present; no synthetic value is allowed."""


def require_funding_coverage(
    required: Sequence[RequiredFundingSettlement],
    observed: Sequence[RequiredFundingSettlement],
) -> None:
    """Fail closed when any required real settlement is missing."""
    observed_keys = {(item.symbol.upper(), int(item.settled_at)) for item in observed}
    missing = [
        item for item in required
        if (item.symbol.upper(), int(item.settled_at)) not in observed_keys
    ]
    if missing:
        first = missing[0]
        raise FundingCoverageError(
            f"missing real funding settlement {first.symbol} at {first.settled_at}"
        )
