"""V2 Forward paper-engine primitives, without a historical replay path.

The engine consumes a caller-provided post-freeze Control schedule.  It keeps
the V1 target directions and execution clock paired while the separate risk
layer supplies only a bounded notional scale.  M0 tests exercise this engine
with synthetic observations; no development-market dataset is opened here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from src.xs_lowvol_v2_risk import (
    ControlWeeklyReturn,
    FundingCoverageError,
    PositionChange,
    PositionState,
    TargetPosition,
    V2RiskScaleInvalid,
    calculate_position_scale,
    plan_position_changes,
    require_funding_coverage,
    scaled_target_positions,
)

from .v2_protocol import (
    V2_BASE_COMMIT,
    V2_PROTOCOL_ID,
    V2_PROTOCOL_SHA256,
    frozen_v2_forward_gate_policy,
    validate_v2_protocol,
)


SUCCESS = "SUCCESS"
NO_SIGNAL = "NO_SIGNAL"
INSUFFICIENT_UNIVERSE = "INSUFFICIENT_UNIVERSE"
MISSING_EXECUTION_PRICE = "MISSING_EXECUTION_PRICE"
RISK_SCALE_INVALID = "V2_RISK_SCALE_INVALID"
TRANSITION_FAILURE = "TRANSITION_FAILURE"
_NON_SUCCESS = frozenset({NO_SIGNAL, INSUFFICIENT_UNIVERSE, MISSING_EXECUTION_PRICE, RISK_SCALE_INVALID, TRANSITION_FAILURE})
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ForwardEpochError(ValueError):
    """A Forward observation is not anchored to the accepted M0 epoch."""


@dataclass(frozen=True)
class ForwardEpoch:
    """The first eligible signal day after the accepted V2-M0 freeze."""

    freeze_commit: str
    first_eligible_signal_day: date

    def __post_init__(self) -> None:
        if not _COMMIT_RE.fullmatch(self.freeze_commit):
            raise ForwardEpochError("freeze_commit must be a 40-character commit SHA")

    def includes(self, signal_day: date) -> bool:
        return signal_day >= self.first_eligible_signal_day

    def rejects(self, signal_day: date) -> bool:
        return not self.includes(signal_day)


@dataclass(frozen=True)
class ForwardObservation:
    """One raw post-freeze evidence row, retained without aggregation."""

    observed_day: date
    payload: Mapping[str, Any]


class ForwardEvidenceLedger:
    """Separate pre-epoch and Forward evidence; no performance calculation."""

    def __init__(self, epoch: ForwardEpoch):
        self.epoch = epoch
        self._pre_epoch: list[ForwardObservation] = []
        self._forward: list[ForwardObservation] = []

    def record(self, observed_day: date, payload: Mapping[str, Any]) -> bool:
        observation = ForwardObservation(observed_day, dict(payload))
        if self.epoch.rejects(observed_day):
            self._pre_epoch.append(observation)
            return False
        self._forward.append(observation)
        return True

    @property
    def pre_epoch_observations(self) -> tuple[ForwardObservation, ...]:
        return tuple(self._pre_epoch)

    @property
    def forward_observations(self) -> tuple[ForwardObservation, ...]:
        return tuple(self._forward)


@dataclass(frozen=True)
class RebalanceAttempt:
    """One calendar-day attempt; only SUCCESS advances the clock."""

    signal_day: date
    execution_day: date
    status: str
    target_directions: tuple[tuple[str, int], ...] = ()
    position_scale: float | None = None
    changes: tuple[PositionChange, ...] = ()
    reason: str = ""


class SuccessfulRebalanceClock:
    """Stateful seven-calendar-day clock with retry-on-failure semantics."""

    def __init__(self, interval_days: int = 7):
        if interval_days != 7:
            raise ValueError("interval_days is frozen at 7")
        self.interval_days = interval_days
        self.last_successful_execution_day: date | None = None

    def is_due(self, execution_day: date) -> bool:
        return (
            self.last_successful_execution_day is None
            or (execution_day - self.last_successful_execution_day).days >= self.interval_days
        )

    def record(self, execution_day: date, status: str) -> None:
        if status not in _NON_SUCCESS | {SUCCESS}:
            raise ValueError(f"unknown rebalance status: {status}")
        if status != SUCCESS:
            return
        if self.last_successful_execution_day is not None and execution_day < self.last_successful_execution_day:
            raise ValueError("successful execution days must be monotonic")
        self.last_successful_execution_day = execution_day


def _direction_tuple(targets: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    output: list[tuple[str, int]] = []
    for symbol, direction in sorted(targets.items()):
        if isinstance(direction, bool) or direction not in (-1, 1):
            raise ValueError("target directions must be -1 or +1")
        output.append((str(symbol).upper(), int(direction)))
    return tuple(output)


class V2ForwardEngine:
    """Stateful forward paper transition engine for one V2 paired stream."""

    def __init__(self, *, base_notional: float = 1.0):
        validate_v2_protocol()
        self.base_notional = float(base_notional)
        if self.base_notional < 0:
            raise ValueError("base_notional must be non-negative")
        self.clock = SuccessfulRebalanceClock()
        self.positions: dict[str, PositionState] = {}
        self.attempts: list[RebalanceAttempt] = []

    def attempt(
        self,
        *,
        signal_day: date,
        execution_day: date,
        control_targets: Mapping[str, int] | None,
        control_weekly_returns: Sequence[ControlWeeklyReturn | Mapping[str, Any] | float] | None,
        signal_status: str = SUCCESS,
        execution_available: bool = True,
        transition_ok: bool = True,
        signal_time: date | datetime | None = None,
    ) -> RebalanceAttempt:
        """Process one attempted rebalance and preserve failed-day retries."""
        targets = dict(control_targets or {})
        target_directions = _direction_tuple(targets)
        status = signal_status
        reason = ""
        scale: float | None = None
        changes: tuple[PositionChange, ...] = ()

        if status == SUCCESS and not targets:
            status = NO_SIGNAL
            reason = "Control produced no target set"
        elif status == SUCCESS and not execution_available:
            status = MISSING_EXECUTION_PRICE
            reason = "required execution close is unavailable"
        elif status == SUCCESS and control_weekly_returns is None:
            status = RISK_SCALE_INVALID
            reason = "missing completed Control weekly returns"
        elif status == SUCCESS:
            try:
                scale = calculate_position_scale(control_weekly_returns, signal_time=signal_time)
            except V2RiskScaleInvalid as exc:
                status = RISK_SCALE_INVALID
                reason = str(exc)
        if status == SUCCESS and not transition_ok:
            status = TRANSITION_FAILURE
            reason = "paired transition was not complete"

        if status == SUCCESS:
            assert scale is not None
            scaled = scaled_target_positions(
                targets,
                base_notional=self.base_notional,
                position_scale=scale,
            )
            changes = plan_position_changes(self.positions, scaled)
            self.positions = {
                symbol: PositionState(position.symbol, position.direction, position.notional)
                for symbol, position in scaled.items()
                if position.notional > 0.0
            }
            self.clock.record(execution_day, SUCCESS)
        elif status not in _NON_SUCCESS:
            raise ValueError(f"unknown signal status: {status}")

        attempt = RebalanceAttempt(
            signal_day=signal_day,
            execution_day=execution_day,
            status=status,
            target_directions=target_directions,
            position_scale=scale,
            changes=changes,
            reason=reason,
        )
        self.attempts.append(attempt)
        return attempt

    @property
    def successful_attempts(self) -> tuple[RebalanceAttempt, ...]:
        return tuple(attempt for attempt in self.attempts if attempt.status == SUCCESS)


def assert_paired_schedule(
    control_attempts: Sequence[RebalanceAttempt],
    v2_attempts: Sequence[RebalanceAttempt],
) -> None:
    """Require Control and V2 to share signal day, execution day, and targets."""
    if len(control_attempts) != len(v2_attempts):
        raise ValueError("Control and V2 schedule lengths differ")
    for index, (control, v2) in enumerate(zip(control_attempts, v2_attempts)):
        if (
            control.signal_day != v2.signal_day
            or control.execution_day != v2.execution_day
            or control.target_directions != v2.target_directions
        ):
            raise ValueError(f"Control and V2 schedule diverge at attempt {index}")


def assert_same_targets_and_directions(
    control_targets: Mapping[str, int],
    v2_targets: Mapping[str, int],
) -> None:
    """Require the risk layer to preserve every V1 symbol and direction."""
    if _direction_tuple(control_targets) != _direction_tuple(v2_targets):
        raise ValueError("V2 target symbols or directions differ from V1 Control")


def build_forward_epoch(*, freeze_commit: str, first_eligible_signal_day: date) -> ForwardEpoch:
    """Create the explicit post-freeze epoch used by the Forward ledger."""
    return ForwardEpoch(freeze_commit, first_eligible_signal_day)


__all__ = [
    "ForwardEpoch",
    "ForwardEpochError",
    "ForwardEvidenceLedger",
    "ForwardObservation",
    "MISSING_EXECUTION_PRICE",
    "NO_SIGNAL",
    "INSUFFICIENT_UNIVERSE",
    "RISK_SCALE_INVALID",
    "RebalanceAttempt",
    "SuccessfulRebalanceClock",
    "TRANSITION_FAILURE",
    "V2ForwardEngine",
    "V2RiskScaleInvalid",
    "assert_paired_schedule",
    "assert_same_targets_and_directions",
    "build_forward_epoch",
    "frozen_v2_forward_gate_policy",
    "require_funding_coverage",
    "validate_v2_protocol",
]
