"""Independent lifecycle and economic funding overlay for V2.1-M1.3.

The V1 normalized cache is immutable.  This module therefore keeps the
correction as an in-memory provenance layer: a confirmed Binance settlement
cuts the old symbol's daily history at its last completed trading day, while
the official settlement timestamp separately bounds the economic funding
lifetime.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from backtest.xs_data_quality import HoldingInterval
from backtest.xs_history import SymbolHistory


UTC = timezone.utc
DAY_MS = 86_400_000
FUNDING_TIME_TOLERANCE_MS = 4_000
CONFIRMED_CLASSIFICATIONS = frozenset(
    {
        "LIFECYCLE_TERMINATION_CONFIRMED",
        "FUNDING_INTERVAL_TRANSITION_CONFIRMED",
        "REAL_MISSING_REQUIRED_SETTLEMENT",
    }
)


class LifecycleOverlayError(ValueError):
    """A lifecycle overlay cannot be applied safely."""


def _as_utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LifecycleOverlayError(f"invalid UTC timestamp: {value}") from exc
    else:
        raise LifecycleOverlayError("UTC timestamp must be a string or datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LifecycleOverlayError("UTC timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def timestamp_ms(value: str | datetime) -> int:
    """Convert an explicit UTC timestamp to integer milliseconds."""
    parsed = _as_utc_datetime(value)
    return int(parsed.timestamp() * 1000)


def utc_iso(timestamp: int) -> str:
    """Render a millisecond timestamp without local-time ambiguity."""
    value = datetime.fromtimestamp(timestamp / 1000.0, UTC).isoformat(
        timespec="milliseconds"
    )
    return value.replace("+00:00", "Z").replace(".000Z", "Z")


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class LifecycleOverride:
    """One official lifecycle fact set for an old USD-M perpetual symbol."""

    symbol: str
    contract_type: str
    official_source: str
    announcement_publish_time_utc: str
    economic_termination_timestamp_utc: str
    last_valid_trading_day: date
    delisted_at: date
    evidence_status: str
    classification: str
    official_evidence: str
    old_issue: str = ""
    new_symbol: str | None = None
    last_actual_completed_trading_daily_close: float | None = None
    official_source_hash: str = ""
    supplemental: bool = False

    def __post_init__(self) -> None:
        symbol = self.symbol.upper()
        object.__setattr__(self, "symbol", symbol)
        if self.new_symbol is not None:
            object.__setattr__(self, "new_symbol", self.new_symbol.upper())
        if self.delisted_at != self.last_valid_trading_day + timedelta(days=1):
            raise LifecycleOverlayError(
                f"{symbol} delisted_at must be last_valid_trading_day + 1 day"
            )
        if self.contract_type != "USD_M_PERPETUAL":
            raise LifecycleOverlayError(f"{symbol} is not a USD-M perpetual")
        _as_utc_datetime(self.announcement_publish_time_utc)
        _as_utc_datetime(self.economic_termination_timestamp_utc)
        if not self.official_source.startswith("https://www.binance.com/"):
            raise LifecycleOverlayError(f"{symbol} source is not an official Binance URL")
        if self.classification not in CONFIRMED_CLASSIFICATIONS:
            raise LifecycleOverlayError(f"{symbol} has an unresolved classification")
        expected_hash = canonical_source_hash(self)
        if self.official_source_hash and self.official_source_hash != expected_hash:
            raise LifecycleOverlayError(f"{symbol} official source hash is inconsistent")
        if not self.official_source_hash:
            object.__setattr__(self, "official_source_hash", expected_hash)

    @property
    def economic_termination_timestamp_ms(self) -> int:
        return timestamp_ms(self.economic_termination_timestamp_utc)

    @property
    def official_publish_timestamp_ms(self) -> int:
        return timestamp_ms(self.announcement_publish_time_utc)

    def officially_known_on(self, day: date) -> bool:
        """Return whether the official record existed by the UTC day."""
        return day >= datetime.fromtimestamp(
            self.official_publish_timestamp_ms / 1000.0, UTC
        ).date()

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "contract_type": self.contract_type,
            "official_source": self.official_source,
            "official_source_hash": self.official_source_hash,
            "official_source_hash_scope": "CANONICAL_OFFICIAL_FACTS_V1",
            "announcement_publish_time_utc": self.announcement_publish_time_utc,
            "economic_termination_timestamp_utc": self.economic_termination_timestamp_utc,
            "last_valid_trading_day": self.last_valid_trading_day.isoformat(),
            "delisted_at": self.delisted_at.isoformat(),
            "last_actual_completed_trading_daily_close": (
                self.last_actual_completed_trading_daily_close
            ),
            "evidence_status": self.evidence_status,
            "classification": self.classification,
            "official_evidence": self.official_evidence,
            "old_issue": self.old_issue,
            "new_symbol": self.new_symbol,
            "supplemental": self.supplemental,
        }


def canonical_source_hash(record: LifecycleOverride) -> str:
    """Hash the reproducible official fact set, not an unavailable raw page."""
    return _json_hash(
        {
            "symbol": record.symbol,
            "contract_type": record.contract_type,
            "official_source": record.official_source,
            "announcement_publish_time_utc": record.announcement_publish_time_utc,
            "economic_termination_timestamp_utc": record.economic_termination_timestamp_utc,
            "last_valid_trading_day": record.last_valid_trading_day,
            "delisted_at": record.delisted_at,
            "evidence_status": record.evidence_status,
            "classification": record.classification,
            "official_evidence": record.official_evidence,
            "new_symbol": record.new_symbol,
        }
    )


@dataclass(frozen=True)
class LifecycleOverlay:
    """Validated immutable collection of official lifecycle corrections."""

    records: tuple[LifecycleOverride, ...]

    def __post_init__(self) -> None:
        symbols = [record.symbol for record in self.records]
        if len(symbols) != len(set(symbols)):
            raise LifecycleOverlayError("overlay contains duplicate symbols")
        if tuple(symbols) != tuple(sorted(symbols)):
            raise LifecycleOverlayError("overlay records must be symbol-sorted")

    @classmethod
    def from_records(cls, records: Iterable[LifecycleOverride]) -> "LifecycleOverlay":
        return cls(tuple(sorted(records, key=lambda record: record.symbol)))

    def record_for(self, symbol: str) -> LifecycleOverride | None:
        wanted = symbol.upper()
        return next((record for record in self.records if record.symbol == wanted), None)

    def validate(self, histories: Iterable[SymbolHistory] | None = None) -> None:
        if not self.records:
            raise LifecycleOverlayError("lifecycle overlay is empty")
        history_symbols = (
            {history.symbol.upper() for history in histories} if histories is not None else None
        )
        for record in self.records:
            if history_symbols is not None and record.symbol not in history_symbols:
                raise LifecycleOverlayError(f"overlay symbol is not in frozen history: {record.symbol}")
            if record.delisted_at <= record.last_valid_trading_day:
                raise LifecycleOverlayError(f"invalid lifecycle boundary: {record.symbol}")
            if record.economic_termination_timestamp_ms <= 0:
                raise LifecycleOverlayError(f"invalid economic termination: {record.symbol}")

    def with_terminal_closes(
        self, histories: Iterable[SymbolHistory]
    ) -> "LifecycleOverlay":
        """Bind each overlay record to the frozen actual daily close."""
        by_symbol = {history.symbol.upper(): history for history in histories}
        updated: list[LifecycleOverride] = []
        for record in self.records:
            history = by_symbol.get(record.symbol)
            if history is None:
                raise LifecycleOverlayError(f"missing frozen history: {record.symbol}")
            bars = [bar for bar in history.daily_bars if bar.day == record.last_valid_trading_day]
            if len(bars) != 1:
                raise LifecycleOverlayError(
                    f"frozen history has no unique terminal daily bar: {record.symbol}"
                )
            close = float(bars[0].close)
            if not math.isfinite(close) or close <= 0:
                raise LifecycleOverlayError(f"terminal daily close is invalid: {record.symbol}")
            updated.append(replace(record, last_actual_completed_trading_daily_close=close))
        return LifecycleOverlay.from_records(updated)

    def as_dict(self) -> dict[str, Any]:
        return {
            "overlay_id": "XS-LOWVOL-V2.1-M1.3-PARENT-LIFECYCLE-OVERLAY-V1",
            "correction_layer": "IN_MEMORY_ONLY_OVER_FROZEN_NORMALIZED_CACHE",
            "records": [record.as_dict() for record in self.records],
        }


def apply_lifecycle_overlay(
    histories: Iterable[SymbolHistory], overlay: LifecycleOverlay
) -> tuple[SymbolHistory, ...]:
    """Return corrected histories without mutating or rewriting the cache."""
    original = tuple(sorted(histories, key=lambda history: history.symbol))
    overlay.validate(original)
    corrected: list[SymbolHistory] = []
    for history in original:
        record = overlay.record_for(history.symbol)
        if record is None:
            corrected.append(history)
            continue
        terminal_bars = [
            bar for bar in history.daily_bars if bar.day == record.last_valid_trading_day
        ]
        if len(terminal_bars) != 1:
            raise LifecycleOverlayError(
                f"{history.symbol} cannot establish the official terminal close"
            )
        lifecycle = replace(
            history.lifecycle,
            last_available_day=min(
                history.lifecycle.last_available_day, record.last_valid_trading_day
            ),
            delisted_at=record.delisted_at,
            currently_active=False,
            lifecycle_source="official_binance_lifecycle_overlay",
            lifecycle_confidence="official",
            status="OK",
        )
        corrected.append(
            replace(
                history,
                daily_bars=tuple(
                    bar for bar in history.daily_bars if bar.day <= record.last_valid_trading_day
                ),
                lifecycle=lifecycle,
            )
        )
    return tuple(corrected)


def terminal_daily_close(
    history: SymbolHistory, record: LifecycleOverride
) -> float:
    """Read the last actual completed daily close for a forced exit."""
    bar = next(
        (item for item in history.daily_bars if item.day == record.last_valid_trading_day),
        None,
    )
    if bar is None or not math.isfinite(bar.close) or bar.close <= 0:
        raise LifecycleOverlayError(f"invalid terminal close for {record.symbol}")
    if (
        record.last_actual_completed_trading_daily_close is not None
        and not math.isclose(
            float(record.last_actual_completed_trading_daily_close),
            float(bar.close),
            rel_tol=0.0,
            abs_tol=0.0,
        )
    ):
        raise LifecycleOverlayError(f"terminal close provenance mismatch for {record.symbol}")
    return float(bar.close)


def funding_exposure_end_ms(
    holding: HoldingInterval, overlay: LifecycleOverlay
) -> int:
    """Use the economic lifetime, not the later daily accounting boundary."""
    record = overlay.record_for(holding.symbol)
    if record is None:
        return holding.exit_timestamp_ms
    return min(holding.exit_timestamp_ms, record.economic_termination_timestamp_ms)


@dataclass(frozen=True)
class FundingCoverageIssue:
    symbol: str
    code: str
    detail: str
    expected_timestamp_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "code": self.code,
            "detail": self.detail,
            "expected_timestamp_utc": (
                utc_iso(self.expected_timestamp_ms)
                if self.expected_timestamp_ms is not None
                else None
            ),
        }


@dataclass(frozen=True)
class FundingExposureRecord:
    symbol: str
    accounting_entry_timestamp_ms: int
    accounting_exit_timestamp_ms: int
    economic_exposure_end_timestamp_ms: int
    official_termination_applied: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "accounting_entry_timestamp_utc": utc_iso(
                self.accounting_entry_timestamp_ms
            ),
            "accounting_exit_timestamp_utc": utc_iso(
                self.accounting_exit_timestamp_ms
            ),
            "economic_exposure_end_timestamp_utc": utc_iso(
                self.economic_exposure_end_timestamp_ms
            ),
            "official_termination_applied": self.official_termination_applied,
        }


@dataclass(frozen=True)
class FundingCoverageResult:
    issues: tuple[FundingCoverageIssue, ...]
    exposure_records: tuple[FundingExposureRecord, ...]

    @property
    def passed(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "PASS" if self.passed else "FAIL",
            "checked_interval_count": len(self.exposure_records),
            "issue_count": len(self.issues),
            "issues": [issue.as_dict() for issue in self.issues],
            "exposure_records": [record.as_dict() for record in self.exposure_records],
        }


def _unique_funding_events(history: SymbolHistory) -> tuple[Any, ...]:
    by_timestamp: dict[int, Any] = {}
    for event in history.funding_events:
        by_timestamp.setdefault(event.funding_time_ms, event)
    return tuple(sorted(by_timestamp.values(), key=lambda event: event.funding_time_ms))


def _interval_ms(event: Any) -> int | None:
    try:
        hours = float(event.funding_interval_hours)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(hours) or hours <= 0:
        return None
    return int(round(hours * 3_600_000.0))


def _transition_expected_ms(previous: Any, current: Any) -> int | None:
    current_ms = _interval_ms(current)
    previous_ms = _interval_ms(previous)
    delta = current.funding_time_ms - previous.funding_time_ms
    for candidate in (current_ms, previous_ms):
        if candidate is not None and abs(delta - candidate) <= FUNDING_TIME_TOLERANCE_MS:
            return current.funding_time_ms
    if current_ms is not None:
        return previous.funding_time_ms + current_ms
    return None


def validate_economic_funding_coverage(
    histories: Iterable[SymbolHistory],
    holdings: Iterable[HoldingInterval],
    overlay: LifecycleOverlay,
) -> FundingCoverageResult:
    """Check only real settlements within each holding's economic lifetime."""
    by_symbol = {history.symbol.upper(): history for history in histories}
    issues: list[FundingCoverageIssue] = []
    exposures: list[FundingExposureRecord] = []
    for holding in holdings:
        symbol = holding.symbol.upper()
        effective_end = funding_exposure_end_ms(holding, overlay)
        exposures.append(
            FundingExposureRecord(
                symbol=symbol,
                accounting_entry_timestamp_ms=holding.entry_timestamp_ms,
                accounting_exit_timestamp_ms=holding.exit_timestamp_ms,
                economic_exposure_end_timestamp_ms=effective_end,
                official_termination_applied=overlay.record_for(symbol) is not None,
            )
        )
        if effective_end <= holding.entry_timestamp_ms:
            continue
        history = by_symbol.get(symbol)
        if history is None:
            issues.append(
                FundingCoverageIssue(symbol, "UNKNOWN_SYMBOL", "holding has no frozen history")
            )
            continue
        events = _unique_funding_events(history)
        if not events:
            issues.append(
                FundingCoverageIssue(symbol, "MISSING_REQUIRED_SETTLEMENT", "no real funding event")
            )
            continue
        anchor_index = max(
            (index for index, event in enumerate(events) if event.funding_time_ms <= holding.entry_timestamp_ms),
            default=None,
        )
        if anchor_index is None:
            first = events[0]
            interval = _interval_ms(first)
            if interval is not None and first.funding_time_ms - holding.entry_timestamp_ms > interval + FUNDING_TIME_TOLERANCE_MS:
                issues.append(
                    FundingCoverageIssue(
                        symbol,
                        "MISSING_REQUIRED_SETTLEMENT",
                        "first real settlement is beyond one declared funding interval",
                        holding.entry_timestamp_ms + interval,
                    )
                )
            anchor_index = 0
        previous = events[anchor_index]
        for current in events[anchor_index + 1 :]:
            if current.funding_time_ms > effective_end + FUNDING_TIME_TOLERANCE_MS:
                break
            expected = _transition_expected_ms(previous, current)
            if expected is None:
                issues.append(
                    FundingCoverageIssue(
                        symbol,
                        "INVALID_FUNDING_INTERVAL",
                        "real funding event has invalid interval metadata",
                    )
                )
            elif current.funding_time_ms > expected + FUNDING_TIME_TOLERANCE_MS:
                issues.append(
                    FundingCoverageIssue(
                        symbol,
                        "MISSING_REQUIRED_SETTLEMENT",
                        "a scheduled settlement inside economic exposure is absent",
                        expected,
                    )
                )
            elif current.funding_time_ms < expected - FUNDING_TIME_TOLERANCE_MS:
                issues.append(
                    FundingCoverageIssue(
                        symbol,
                        "INVALID_FUNDING_INTERVAL",
                        "real funding events are earlier than the declared interval",
                        expected,
                    )
                )
            previous = current
        interval_ms = _interval_ms(previous)
        if interval_ms is None:
            issues.append(
                FundingCoverageIssue(
                    symbol,
                    "INVALID_FUNDING_INTERVAL",
                    "last observed funding event has invalid interval metadata",
                )
            )
        else:
            next_expected = previous.funding_time_ms + interval_ms
            if next_expected <= effective_end + FUNDING_TIME_TOLERANCE_MS:
                observed = next(
                    (
                        event
                        for event in events
                        if event.funding_time_ms > previous.funding_time_ms
                        and event.funding_time_ms <= effective_end + FUNDING_TIME_TOLERANCE_MS
                    ),
                    None,
                )
                if observed is None:
                    issues.append(
                        FundingCoverageIssue(
                            symbol,
                            "MISSING_REQUIRED_SETTLEMENT",
                            "next scheduled settlement is inside economic exposure and absent",
                            next_expected,
                        )
                    )
    return FundingCoverageResult(tuple(issues), tuple(exposures))

