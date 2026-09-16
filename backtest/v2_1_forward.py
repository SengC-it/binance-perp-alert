"""V2.1 paired transition engine with parent-Control scheduling.

The engine intentionally has no rebalance clock and no V2.1-specific retry
path.  A caller supplies the already-decided V1 Control attempt; V2.1 either
inherits that failure or completes the paired transition with the repaired
risk scale.  A data-integrity halt is terminal for the paired experiment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence

from src.xs_lowvol_v2_1_risk import (
    ControlWeeklyReturn,
    DATA_INTEGRITY_HALT,
    PositionChange,
    PositionState,
    V21RiskScaleResult,
    V2_DATA_INTEGRITY_HALT,
    evaluate_risk_scale,
    plan_position_changes,
    scaled_target_positions,
)

from .v2_1_protocol import validate_v2_1_protocol


UTC = timezone.utc
SUCCESS = "SUCCESS"
NO_SIGNAL = "NO_SIGNAL"
INSUFFICIENT_UNIVERSE = "INSUFFICIENT_UNIVERSE"
MISSING_EXECUTION_PRICE = "MISSING_EXECUTION_PRICE"
TRANSITION_FAILURE = "TRANSITION_FAILURE"
PAIRED_EXPERIMENT_HALTED = V2_DATA_INTEGRITY_HALT

PARENT_FAILURE_STATUSES = frozenset(
    {
        NO_SIGNAL,
        INSUFFICIENT_UNIVERSE,
        MISSING_EXECUTION_PRICE,
        TRANSITION_FAILURE,
    }
)
_KNOWN_PARENT_STATUSES = PARENT_FAILURE_STATUSES | {SUCCESS}


class V21TemporalError(ValueError):
    """The parent attempt does not carry the frozen +1 UTC-day pairing."""

    code = "V2_1_TEMPORAL_INVALID"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


class V21PairedInputError(ValueError):
    """A parent Control attempt is not a valid paired input."""

    code = "V2_1_PAIRED_INPUT_INVALID"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"{self.code}: {reason}")


def _require_aware_signal_time(signal_time: datetime | None) -> datetime:
    if signal_time is None:
        raise V21TemporalError("signal_time is required and must be timezone-aware")
    if not isinstance(signal_time, datetime):
        raise V21TemporalError("signal_time must be a timezone-aware datetime")
    if signal_time.tzinfo is None or signal_time.utcoffset() is None:
        raise V21TemporalError("signal_time must be a timezone-aware datetime")
    return signal_time.astimezone(UTC)


def validate_v2_1_temporal_inputs(
    *,
    signal_day: date,
    execution_day: date,
    signal_time: datetime | None,
) -> datetime:
    """Require the inherited one-calendar-day UTC execution relationship."""
    if execution_day != signal_day + timedelta(days=1):
        raise V21TemporalError(
            "execution_day must equal signal_day plus exactly one UTC calendar day"
        )
    normalized = _require_aware_signal_time(signal_time)
    expected = datetime.combine(execution_day, time.min, tzinfo=UTC)
    if normalized != expected:
        raise V21TemporalError("signal_time must equal execution_day 00:00:00 UTC")
    return normalized


def _direction_tuple(targets: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    output: list[tuple[str, int]] = []
    for raw_symbol, raw_direction in sorted(targets.items(), key=lambda pair: str(pair[0])):
        if isinstance(raw_direction, bool) or raw_direction not in (-1, 1):
            raise V21PairedInputError("target directions must be -1 or +1")
        symbol = str(raw_symbol).upper()
        if not symbol:
            raise V21PairedInputError("target symbol cannot be empty")
        output.append((symbol, int(raw_direction)))
    return tuple(output)


@dataclass(frozen=True)
class V21PairedAttempt:
    """One parent-Control attempt and its V2.1 paired outcome."""

    signal_day: date
    execution_day: date
    parent_status: str
    status: str
    target_directions: tuple[tuple[str, int], ...] = ()
    position_scale: float | None = None
    changes: tuple[PositionChange, ...] = ()
    reason: str = ""
    paired: bool = False
    halted: bool = False


V2_1PairedAttempt = V21PairedAttempt


class V21PairedEngine:
    """Apply V2.1 only to the schedule emitted by the V1 Control parent."""

    def __init__(self, *, base_notional: float = 1.0):
        validate_v2_1_protocol()
        self.base_notional = float(base_notional)
        if not math.isfinite(self.base_notional) or self.base_notional < 0.0:
            raise ValueError("base_notional must be finite and non-negative")
        self.positions: dict[str, PositionState] = {}
        self.attempts: list[V21PairedAttempt] = []
        self.halted = False

    def _record_halt(
        self,
        *,
        signal_day: date,
        execution_day: date,
        parent_status: str,
        target_directions: tuple[tuple[str, int], ...],
        reason: str,
    ) -> V21PairedAttempt:
        self.halted = True
        attempt = V21PairedAttempt(
            signal_day=signal_day,
            execution_day=execution_day,
            parent_status=parent_status,
            status=V2_DATA_INTEGRITY_HALT,
            target_directions=target_directions,
            reason=reason,
            paired=False,
            halted=True,
        )
        self.attempts.append(attempt)
        return attempt

    def process_parent_attempt(
        self,
        *,
        signal_day: date,
        execution_day: date,
        control_targets: Mapping[str, int] | None,
        control_weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any]] | None,
        parent_status: str = SUCCESS,
        execution_available: bool = True,
        transition_ok: bool = True,
        signal_time: datetime | None = None,
        signal_status: str | None = None,
    ) -> V21PairedAttempt:
        """Process exactly one status supplied by the V1 Control parent.

        A parent failure is returned unchanged and remains eligible for the
        parent's own retry.  V2.1 never checks elapsed time and never creates
        a next-day retry.  Once a data-integrity halt occurs, every later
        attempt is terminally halted without changing positions.
        """
        if signal_status is not None:
            parent_status = signal_status
        if parent_status not in _KNOWN_PARENT_STATUSES:
            raise V21PairedInputError(f"unknown parent status: {parent_status}")

        if parent_status == SUCCESS and (
            execution_available is not True or transition_ok is not True
        ):
            raise V21PairedInputError(
                "parent SUCCESS must not be overridden by an independent "
                "execution or transition decision"
            )

        try:
            target_directions = _direction_tuple(dict(control_targets or {}))
        except V21PairedInputError:
            if self.halted:
                target_directions = ()
            else:
                raise

        if parent_status == SUCCESS and not target_directions:
            raise V21PairedInputError(
                "parent SUCCESS must carry non-empty Control targets"
            )

        if self.halted:
            return self._record_halt(
                signal_day=signal_day,
                execution_day=execution_day,
                parent_status=parent_status,
                target_directions=target_directions,
                reason="paired experiment is already halted by V2 data-integrity failure",
            )

        if signal_time is not None:
            validate_v2_1_temporal_inputs(
                signal_day=signal_day,
                execution_day=execution_day,
                signal_time=signal_time,
            )
        elif parent_status == SUCCESS:
            raise V21TemporalError(
                "signal_time is required for a successful paired attempt"
            )
        elif execution_day != signal_day + timedelta(days=1):
            raise V21TemporalError(
                "execution_day must equal signal_day plus exactly one UTC calendar day"
            )

        if parent_status != SUCCESS:
            attempt = V21PairedAttempt(
                signal_day=signal_day,
                execution_day=execution_day,
                parent_status=parent_status,
                status=parent_status,
                target_directions=target_directions,
                reason="inherited from V1 Control parent; V2.1 adds no retry or clock",
                paired=True,
            )
            self.attempts.append(attempt)
            return attempt

        risk: V21RiskScaleResult = evaluate_risk_scale(
            control_weekly_returns,
            signal_time=signal_time,
        )
        if risk.status == DATA_INTEGRITY_HALT:
            return self._record_halt(
                signal_day=signal_day,
                execution_day=execution_day,
                parent_status=parent_status,
                target_directions=target_directions,
                reason=risk.reason,
            )

        assert risk.position_scale is not None
        scaled = scaled_target_positions(
            dict(target_directions),
            base_notional=self.base_notional,
            position_scale=risk.position_scale,
        )
        changes = plan_position_changes(self.positions, scaled)
        self.positions = {
            symbol: PositionState(position.symbol, position.direction, position.notional)
            for symbol, position in scaled.items()
            if position.notional > 0.0
        }
        attempt = V21PairedAttempt(
            signal_day=signal_day,
            execution_day=execution_day,
            parent_status=parent_status,
            status=SUCCESS,
            target_directions=target_directions,
            position_scale=risk.position_scale,
            changes=changes,
            paired=True,
        )
        self.attempts.append(attempt)
        return attempt

    # Familiar call names are aliases only; neither adds scheduling behavior.
    attempt = process_parent_attempt
    process = process_parent_attempt

    @property
    def successful_attempts(self) -> tuple[V21PairedAttempt, ...]:
        return tuple(attempt for attempt in self.attempts if attempt.status == SUCCESS)

    @property
    def paired_successful_attempts(self) -> tuple[V21PairedAttempt, ...]:
        return self.successful_attempts


def assert_paired_schedule(
    parent_attempts: Sequence[Any],
    v2_1_attempts: Sequence[V21PairedAttempt],
) -> None:
    """Require V2.1 attempts to retain the parent dates and directions."""
    if len(parent_attempts) != len(v2_1_attempts):
        raise ValueError("parent and V2.1 schedule lengths differ")
    for index, (parent, repaired) in enumerate(zip(parent_attempts, v2_1_attempts)):
        parent_targets = tuple(getattr(parent, "target_directions", ()))
        if (
            getattr(parent, "signal_day", None) != repaired.signal_day
            or getattr(parent, "execution_day", None) != repaired.execution_day
            or parent_targets != repaired.target_directions
        ):
            raise ValueError(f"parent and V2.1 schedules diverge at attempt {index}")


__all__ = [
    "DATA_INTEGRITY_HALT",
    "INSUFFICIENT_UNIVERSE",
    "MISSING_EXECUTION_PRICE",
    "NO_SIGNAL",
    "PAIRED_EXPERIMENT_HALTED",
    "PARENT_FAILURE_STATUSES",
    "PositionChange",
    "PositionState",
    "SUCCESS",
    "TRANSITION_FAILURE",
    "UTC",
    "V21PairedAttempt",
    "V21PairedEngine",
    "V21PairedInputError",
    "V21TemporalError",
    "V2_1PairedAttempt",
    "V2_DATA_INTEGRITY_HALT",
    "assert_paired_schedule",
    "validate_v2_1_temporal_inputs",
]
