"""M1-A historical data pipeline and Point-in-Time universe primitives.

This module is intentionally independent from ``backtest.fetch_history``.  It
discovers symbols from official USD-M archive paths, never from the old manual
``DEFAULT_UNIVERSE``, and does not require spot data.  The download functions
are available for M1-B but are not called by any M1-A command or test.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import pstdev
from typing import Any, Iterable, Mapping, Sequence

import requests

from .m1_protocol import (
    M1_DATA_END,
    M1_DATA_START,
    M1_PROTOCOL_ID,
)

OFFICIAL_ARCHIVE_BASE = "https://data.binance.vision/data"
M1_RAW_CACHE = Path(__file__).resolve().parent / "m1_cache" / "raw"
M1_NORMALIZED_CACHE = Path(__file__).resolve().parent / "m1_cache" / "normalized"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


class HistoryDataError(ValueError):
    """Historical archive or normalized-data failure."""


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    day: date
    open: float
    high: float
    low: float
    close: float
    quote_volume: float
    open_time_ms: int
    close_time_ms: int


@dataclass(frozen=True)
class FundingEvent:
    symbol: str
    funding_time_ms: int
    funding_rate: float
    funding_interval_hours: float


@dataclass(frozen=True)
class LifecycleRecord:
    symbol: str
    first_available_day: date
    last_available_day: date
    listed_from: date
    delisted_at: date | None
    currently_active: bool
    lifecycle_source: str
    lifecycle_confidence: str
    status: str = "OK"

    def active_on(self, day: date) -> bool:
        if day < self.listed_from:
            return False
        return self.delisted_at is None or day < self.delisted_at


@dataclass(frozen=True)
class SymbolHistory:
    symbol: str
    daily_bars: tuple[DailyBar, ...]
    funding_events: tuple[FundingEvent, ...]
    lifecycle: LifecycleRecord
    quote_asset: str = "USDT"
    contract_type: str = "PERPETUAL"

    @property
    def currently_active(self) -> bool:
        return self.lifecycle.currently_active

    @property
    def listed_from(self) -> date:
        return self.lifecycle.listed_from

    @property
    def delisted_at(self) -> date | None:
        return self.lifecycle.delisted_at

    def active_on(self, day: date) -> bool:
        return (
            self.quote_asset.upper() == "USDT"
            and self.contract_type.upper() == "PERPETUAL"
            and self.lifecycle.active_on(day)
        )

    def bars_by_day(self) -> dict[date, DailyBar]:
        return {bar.day: bar for bar in self.daily_bars}

    def last_bar_on_or_before(self, day: date) -> DailyBar | None:
        bars = [bar for bar in self.daily_bars if bar.day <= day]
        return max(bars, key=lambda bar: (bar.day, bar.close_time_ms), default=None)


@dataclass(frozen=True)
class ArchiveRef:
    symbol: str
    kind: str
    month: str
    url: str


@dataclass(frozen=True)
class PITSnapshot:
    symbol: str
    signal_day: date
    realized_vol_pct: float
    quote_volume: float
    lookback_start: date
    lookback_end: date


@dataclass(frozen=True)
class PITUniverseResult:
    signal_day: date
    active_symbols: tuple[str, ...]
    liquid_symbols: tuple[str, ...]
    eligible: tuple[PITSnapshot, ...]
    exclusions: Mapping[str, str] = field(default_factory=dict)
    fail_closed: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def no_signal(self) -> bool:
        return self.fail_closed or not self.eligible


@dataclass(frozen=True)
class PITSignal:
    signal_day: date
    execution_day: date
    longs: tuple[str, ...]
    shorts: tuple[str, ...]
    eligible_symbols: tuple[str, ...]
    snapshots: tuple[PITSnapshot, ...]

    @property
    def targets(self) -> dict[str, int]:
        return {**{symbol: 1 for symbol in self.longs}, **{symbol: -1 for symbol in self.shorts}}


@dataclass(frozen=True)
class ForcedExit:
    symbol: str
    forced_exit: bool
    reason: str
    exit_day: date
    exit_timestamp_ms: int
    exit_price: float
    notional: float
    transaction_cost: float


def is_historical_usdt_perpetual_symbol(symbol: str) -> bool:
    """Reject dated/quarterly symbols while accepting ordinary USDT perps."""
    return bool(_SYMBOL_RE.fullmatch(str(symbol).upper()))


def month_keys(start: date, end: date) -> list[str]:
    """Return inclusive calendar months in ascending order."""
    if end < start:
        raise HistoryDataError("month window end precedes start")
    cursor = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    out: list[str] = []
    while cursor <= final:
        out.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    return out


def _validate_month(month: str) -> str:
    match = _MONTH_RE.fullmatch(str(month))
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise HistoryDataError(f"非法 archive month: {month}")
    return str(month)


def archive_url(symbol: str, kind: str, month: str) -> str:
    symbol = str(symbol).upper()
    if not is_historical_usdt_perpetual_symbol(symbol):
        raise HistoryDataError(f"不是 USD-M USDT perpetual symbol: {symbol}")
    month = _validate_month(month)
    if kind == "daily_klines":
        return f"{OFFICIAL_ARCHIVE_BASE}/futures/um/monthly/klines/{symbol}/1d/{symbol}-1d-{month}.zip"
    if kind == "funding":
        return f"{OFFICIAL_ARCHIVE_BASE}/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"
    raise HistoryDataError(f"未知 archive kind: {kind}")


def parse_archive_ref(value: str) -> ArchiveRef | None:
    """Parse one official archive URL/path and reject non-monthly USDT perps."""
    text = str(value).replace("\\", "/")
    daily = re.search(
        r"/futures/um/monthly/klines/([^/]+)/1d/([^/]+)-1d-(\d{4}-\d{2})\.zip(?:$|[?#])",
        text,
        re.IGNORECASE,
    )
    funding = re.search(
        r"/futures/um/monthly/fundingRate/([^/]+)/([^/]+)-fundingRate-(\d{4}-\d{2})\.zip(?:$|[?#])",
        text,
        re.IGNORECASE,
    )
    match = daily or funding
    if match is None:
        return None
    symbol = match.group(1).upper()
    filename_symbol = match.group(2).upper()
    month = _validate_month(match.group(3))
    if symbol != filename_symbol or not is_historical_usdt_perpetual_symbol(symbol):
        return None
    kind = "daily_klines" if daily is not None else "funding"
    url = archive_url(symbol, kind, month)
    return ArchiveRef(symbol=symbol, kind=kind, month=month, url=url)


def discover_historical_archives(listing: Iterable[str]) -> tuple[ArchiveRef, ...]:
    """Build a deduplicated archive manifest from an official listing response.

    The input may be HTML, newline-delimited URLs, or local manifest lines.  No
    current exchangeInfo or manually curated symbol list is consulted.
    """
    refs: dict[tuple[str, str, str], ArchiveRef] = {}
    for item in listing:
        ref = parse_archive_ref(str(item))
        if ref is not None:
            refs[(ref.symbol, ref.kind, ref.month)] = ref
    return tuple(refs[key] for key in sorted(refs))


def discovered_symbols(refs: Iterable[ArchiveRef]) -> tuple[str, ...]:
    return tuple(sorted({ref.symbol for ref in refs}))


def fetch_official_archive(
    ref: ArchiveRef,
    *,
    raw_dir: str | Path = M1_RAW_CACHE,
    session: requests.Session | None = None,
) -> Path | None:
    """Download one official archive using GET and store it outside Git.

    This function is intentionally not called by M1-A.  A 404 is represented as
    ``None`` so the manifest can record a missing month; other failures raise.
    """
    expected_url = archive_url(ref.symbol, ref.kind, ref.month)
    if ref.url != expected_url:
        raise HistoryDataError("archive URL 与已解析 symbol/kind/month 不一致")
    client = session or requests.Session()
    response = client.get(ref.url, timeout=60)
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise HistoryDataError(f"archive GET failed: HTTP {response.status_code}")
    target_dir = Path(raw_dir) / ref.kind / ref.symbol
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{ref.month}.zip"
    target.write_bytes(response.content)
    return target


def read_archive_csv(path: str | Path) -> str:
    """Read the single CSV payload from a downloaded official ZIP archive."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise HistoryDataError(f"archive 必须恰好包含一个 CSV: {path}")
            return archive.read(names[0]).decode("utf-8", "strict")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise HistoryDataError(f"无法读取 archive: {path}") from exc


def normalize_symbol_archives(
    symbol: str,
    *,
    daily_paths: Iterable[str | Path],
    funding_paths: Iterable[str | Path],
    currently_active: bool,
    listed_from: date | None = None,
    delisted_at: date | None = None,
    confirmed_absence_after_last_bar: bool = False,
) -> SymbolHistory:
    """Normalize perp daily and funding archives without requiring spot files.

    Inputs remain in archive order.  The data-quality pass must run before any
    consumer sorts or deduplicates them, so malformed chronology cannot be
    repaired silently.
    """
    symbol = str(symbol).upper()
    daily: list[DailyBar] = []
    funding: list[FundingEvent] = []
    for path in daily_paths:
        daily.extend(parse_daily_klines_csv(read_archive_csv(path), symbol))
    for path in funding_paths:
        funding.extend(parse_funding_csv(read_archive_csv(path), symbol))
    return make_symbol_history(
        symbol,
        daily,
        funding,
        currently_active=currently_active,
        listed_from=listed_from,
        delisted_at=delisted_at,
        confirmed_absence_after_last_bar=confirmed_absence_after_last_bar,
    )


def file_sha256(path: str | Path) -> str:
    """Hash a raw archive for a future manifest without embedding its bytes."""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise HistoryDataError(f"无法读取文件用于 checksum: {path}") from exc
    return digest.hexdigest()


def _timestamp_ms(value: Any) -> int:
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryDataError(f"非法时间戳: {value!r}") from exc
    if timestamp > 10**14:
        timestamp //= 1000
    return timestamp


def _is_header(row: Sequence[str]) -> bool:
    return bool(row) and row[0].strip().lower() in {"open_time", "calc_time", "fundingtime"}


def parse_daily_klines_csv(text: str, symbol: str) -> tuple[DailyBar, ...]:
    """Parse official 1d perp CSV without sorting or silently repairing it."""
    symbol = str(symbol).upper()
    if not is_historical_usdt_perpetual_symbol(symbol):
        raise HistoryDataError(f"非法 symbol: {symbol}")
    out: list[DailyBar] = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not any(cell.strip() for cell in row) or _is_header(row):
            continue
        if len(row) < 8:
            raise HistoryDataError(f"{symbol} daily row 字段不足")
        try:
            open_time_ms = _timestamp_ms(row[0])
            close_time_ms = _timestamp_ms(row[6])
            values = [float(row[index]) for index in (1, 2, 3, 4, 7)]
        except (TypeError, ValueError, OverflowError, IndexError) as exc:
            raise HistoryDataError(f"{symbol} daily row 数值非法") from exc
        out.append(
            DailyBar(
                symbol=symbol,
                day=datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date(),
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                quote_volume=values[4],
                open_time_ms=open_time_ms,
                close_time_ms=close_time_ms,
            )
        )
    return tuple(out)


def parse_funding_csv(text: str, symbol: str) -> tuple[FundingEvent, ...]:
    """Parse official monthly fundingRate CSV, preserving event order."""
    symbol = str(symbol).upper()
    if not is_historical_usdt_perpetual_symbol(symbol):
        raise HistoryDataError(f"非法 symbol: {symbol}")
    out: list[FundingEvent] = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not any(cell.strip() for cell in row) or _is_header(row):
            continue
        if len(row) < 3:
            raise HistoryDataError(f"{symbol} funding row 字段不足")
        try:
            timestamp_ms = _timestamp_ms(row[0])
            interval_hours = float(row[1])
            rate = float(row[2])
        except (TypeError, ValueError, OverflowError, IndexError) as exc:
            raise HistoryDataError(f"{symbol} funding row 数值非法") from exc
        out.append(
            FundingEvent(
                symbol=symbol,
                funding_time_ms=timestamp_ms,
                funding_rate=rate,
                funding_interval_hours=interval_hours,
            )
        )
    return tuple(out)


def _expected_daily_days(first: date, last: date) -> set[date]:
    return {first + timedelta(days=index) for index in range((last - first).days + 1)}


def build_lifecycle(
    symbol: str,
    daily_bars: Sequence[DailyBar],
    *,
    currently_active: bool,
    listed_from: date | None = None,
    delisted_at: date | None = None,
    confirmed_absence_after_last_bar: bool = False,
    lifecycle_source: str = "official_archive_first_bar",
) -> LifecycleRecord:
    """Create lifecycle metadata without hiding internal daily gaps."""
    symbol = str(symbol).upper()
    if not daily_bars:
        raise HistoryDataError(f"{symbol} 没有 daily bars，无法建立 lifecycle")
    days = [bar.day for bar in daily_bars]
    first = min(days)
    last = max(days)
    gaps = _expected_daily_days(first, last) - set(days)
    resolved_listed = listed_from or first
    resolved_delisted = delisted_at
    confidence = "high" if listed_from is not None and (currently_active or delisted_at is not None) else "inferred"
    status = "OK"
    if gaps:
        status = "DATA_AMBIGUOUS"
        confidence = "low"
        # An unexplained gap must never be converted into an automatic delisting.
        if delisted_at is None:
            resolved_delisted = None
    elif not currently_active and resolved_delisted is None:
        if not confirmed_absence_after_last_bar:
            status = "DATA_AMBIGUOUS"
            confidence = "low"
        else:
            resolved_delisted = last + timedelta(days=1)
    if resolved_listed > first or resolved_delisted is not None and resolved_delisted <= last:
        status = "DATA_AMBIGUOUS"
        confidence = "low"
    return LifecycleRecord(
        symbol=symbol,
        first_available_day=first,
        last_available_day=last,
        listed_from=resolved_listed,
        delisted_at=resolved_delisted,
        currently_active=currently_active,
        lifecycle_source=lifecycle_source,
        lifecycle_confidence=confidence,
        status=status,
    )


def make_symbol_history(
    symbol: str,
    daily_bars: Sequence[DailyBar],
    funding_events: Sequence[FundingEvent] = (),
    *,
    currently_active: bool = True,
    listed_from: date | None = None,
    delisted_at: date | None = None,
    confirmed_absence_after_last_bar: bool = False,
    quote_asset: str = "USDT",
    contract_type: str = "PERPETUAL",
) -> SymbolHistory:
    lifecycle = build_lifecycle(
        symbol,
        daily_bars,
        currently_active=currently_active,
        listed_from=listed_from,
        delisted_at=delisted_at,
        confirmed_absence_after_last_bar=confirmed_absence_after_last_bar,
    )
    return SymbolHistory(
        symbol=str(symbol).upper(),
        daily_bars=tuple(daily_bars),
        funding_events=tuple(funding_events),
        lifecycle=lifecycle,
        quote_asset=quote_asset,
        contract_type=contract_type,
    )


def dataset_fingerprint(histories: Iterable[SymbolHistory]) -> str:
    """Hash normalized bars, funding and lifecycle metadata deterministically."""
    rows: list[dict[str, Any]] = []
    for history in sorted(histories, key=lambda item: item.symbol):
        rows.append(
            {
                "symbol": history.symbol,
                "quote_asset": history.quote_asset,
                "contract_type": history.contract_type,
                "lifecycle": {
                    key: value.isoformat() if isinstance(value, date) else value
                    for key, value in asdict(history.lifecycle).items()
                },
                "daily": [
                    {
                        key: value.isoformat() if isinstance(value, date) else value
                        for key, value in asdict(bar).items()
                    }
                    for bar in sorted(history.daily_bars, key=lambda item: (item.day, item.open_time_ms))
                ],
                "funding": [asdict(event) for event in sorted(history.funding_events, key=lambda item: item.funding_time_ms)],
            }
        )
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lookback_snapshot(
    history: SymbolHistory,
    signal_day: date,
    lookback_days: int,
) -> tuple[PITSnapshot | None, str | None, bool]:
    bars = history.bars_by_day()
    start = signal_day - timedelta(days=lookback_days)
    required_days = [start + timedelta(days=index) for index in range(lookback_days + 1)]
    missing = [day for day in required_days if day not in bars]
    if missing:
        first_bar = min((bar.day for bar in history.daily_bars), default=signal_day)
        if first_bar > start:
            return None, "INSUFFICIENT_LOOKBACK", False
        return None, "MISSING_INTERNAL_DAILY_BAR", True
    window = [bars[day] for day in required_days]
    if any(bar.close_time_ms > int(datetime.combine(signal_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000) - 1 for bar in window):
        return None, "FUTURE_TIMESTAMP_LEAKAGE", True
    closes = [bar.close for bar in window]
    if any(not math.isfinite(close) or close <= 0 for close in closes):
        return None, "INVALID_PRICE", True
    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]
    vol = pstdev(returns) * math.sqrt(365.0) * 100.0
    signal_bar = bars[signal_day]
    if not math.isfinite(signal_bar.quote_volume) or signal_bar.quote_volume < 0:
        return None, "INVALID_QUOTE_VOLUME", True
    if vol <= 0:
        return None, "ZERO_REALIZED_VOL", False
    return (
        PITSnapshot(
            symbol=history.symbol,
            signal_day=signal_day,
            realized_vol_pct=vol,
            quote_volume=signal_bar.quote_volume,
            lookback_start=start,
            lookback_end=signal_day,
        ),
        None,
        False,
    )


def build_pit_universe(
    histories: Iterable[SymbolHistory],
    signal_day: date,
    *,
    min_quote_volume_usdt: float = 50_000_000.0,
    lookback_days: int = 30,
    min_symbols: int = 10,
) -> PITUniverseResult:
    """Build exactly the information available on completed signal day T."""
    if lookback_days <= 0 or min_symbols <= 0:
        raise HistoryDataError("lookback_days/min_symbols 必须为正数")
    active: list[str] = []
    liquid: list[str] = []
    eligible: list[PITSnapshot] = []
    exclusions: dict[str, str] = {}
    reasons: list[str] = []
    fail_closed = False
    for history in sorted(histories, key=lambda item: item.symbol):
        symbol = history.symbol.upper()
        if not is_historical_usdt_perpetual_symbol(symbol) or not history.active_on(signal_day):
            exclusions[symbol] = "NOT_ACTIVE_USDT_PERPETUAL_AT_SIGNAL_DAY"
            continue
        active.append(symbol)
        bar = history.bars_by_day().get(signal_day)
        if bar is None:
            exclusions[symbol] = "NO_COMPLETED_SIGNAL_DAY_BAR"
            continue
        if not math.isfinite(bar.quote_volume) or bar.quote_volume < 0:
            exclusions[symbol] = "INVALID_QUOTE_VOLUME"
            fail_closed = True
            reasons.append(f"DATA_INVALID: {symbol} quote_volume")
            continue
        if bar.quote_volume < min_quote_volume_usdt:
            exclusions[symbol] = "BELOW_SIGNAL_DAY_VOLUME_THRESHOLD"
            continue
        liquid.append(symbol)
        snapshot, reason, hard_failure = _lookback_snapshot(history, signal_day, lookback_days)
        if snapshot is not None:
            eligible.append(snapshot)
        elif reason is not None:
            exclusions[symbol] = reason
            fail_closed = fail_closed or hard_failure
            if hard_failure:
                reasons.append(f"DATA_INVALID: {symbol} {reason}")

    eligible.sort(key=lambda item: (item.realized_vol_pct, item.symbol))
    if fail_closed:
        reasons.insert(0, "NO_SIGNAL: eligible history/data quality failed closed")
    elif len(eligible) < min_symbols:
        reasons.append(f"NO_SIGNAL: eligible universe {len(eligible)} < {min_symbols}")
    return PITUniverseResult(
        signal_day=signal_day,
        active_symbols=tuple(active),
        liquid_symbols=tuple(liquid),
        eligible=tuple(eligible),
        exclusions=exclusions,
        fail_closed=fail_closed,
        reasons=tuple(reasons),
    )


def build_pit_signal(
    histories: Iterable[SymbolHistory],
    signal_day: date,
    *,
    min_quote_volume_usdt: float = 50_000_000.0,
    lookback_days: int = 30,
    k_long: int = 5,
    k_short: int = 5,
    min_symbols: int = 10,
) -> PITSignal | None:
    """Rank the PIT eligible universe; no future field is read."""
    if k_long <= 0 or k_short <= 0:
        raise HistoryDataError("k_long/k_short 必须为正数")
    universe = build_pit_universe(
        histories,
        signal_day,
        min_quote_volume_usdt=min_quote_volume_usdt,
        lookback_days=lookback_days,
        min_symbols=min_symbols,
    )
    required = max(min_symbols, k_long + k_short)
    if universe.fail_closed or len(universe.eligible) < required:
        return None
    ranked = sorted(universe.eligible, key=lambda item: (item.realized_vol_pct, item.symbol))
    longs = tuple(item.symbol for item in ranked[:k_long])
    shorts = tuple(item.symbol for item in ranked[-k_short:][::-1])
    return PITSignal(
        signal_day=signal_day,
        execution_day=signal_day + timedelta(days=1),
        longs=longs,
        shorts=shorts,
        eligible_symbols=tuple(item.symbol for item in ranked),
        snapshots=tuple(ranked),
    )


def forced_exit_for_delisting(
    history: SymbolHistory,
    *,
    next_rebalance_day: date,
    notional: float,
    transaction_cost_rate: float = 0.0008,
) -> ForcedExit | None:
    """Return a conservative terminal exit for a confirmed delisting."""
    if history.lifecycle.status == "DATA_AMBIGUOUS":
        raise HistoryDataError(f"{history.symbol} lifecycle DATA_AMBIGUOUS")
    delisted_at = history.lifecycle.delisted_at
    if delisted_at is None or delisted_at > next_rebalance_day:
        return None
    terminal = history.last_bar_on_or_before(delisted_at - timedelta(days=1))
    if terminal is None or terminal.close <= 0 or not math.isfinite(terminal.close):
        raise HistoryDataError(f"{history.symbol} 没有合理的 terminal completed close")
    return ForcedExit(
        symbol=history.symbol,
        forced_exit=True,
        reason="delisted",
        exit_day=terminal.day,
        exit_timestamp_ms=terminal.close_time_ms,
        exit_price=terminal.close,
        notional=float(notional),
        transaction_cost=float(notional) * float(transaction_cost_rate),
    )


def build_dataset_manifest(
    histories: Iterable[SymbolHistory],
    *,
    protocol_id: str = M1_PROTOCOL_ID,
    protocol_hash: str = "",
    data_start: date = M1_DATA_START,
    data_end: date = M1_DATA_END,
    raw_files: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a commit-friendly manifest; no raw archive is embedded."""
    histories = tuple(sorted(histories, key=lambda item: item.symbol))
    per_symbol: list[dict[str, Any]] = []
    for history in histories:
        days = sorted({bar.day for bar in history.daily_bars})
        expected = _expected_daily_days(days[0], days[-1]) if days else set()
        missing_days = sorted(expected - set(days))
        months = sorted({day.strftime("%Y-%m") for day in missing_days})
        source_files = dict((raw_files or {}).get(history.symbol, {}))
        source_checksums: dict[str, str] = {}
        for kind, source in source_files.items():
            source_path = Path(str(source))
            if source_path.is_file():
                source_checksums[str(kind)] = file_sha256(source_path)
        per_symbol.append(
            {
                "symbol": history.symbol,
                "listed_from": history.lifecycle.listed_from.isoformat(),
                "delisted_at": history.lifecycle.delisted_at.isoformat() if history.lifecycle.delisted_at else None,
                "current_active": history.currently_active,
                "currently_active": history.currently_active,
                "lifecycle_source": history.lifecycle.lifecycle_source,
                "lifecycle_confidence": history.lifecycle.lifecycle_confidence,
                "first_bar": days[0].isoformat() if days else None,
                "last_bar": days[-1].isoformat() if days else None,
                "daily_count": len(history.daily_bars),
                "funding_count": len(history.funding_events),
                "missing_days": [day.isoformat() for day in missing_days],
                "missing_months": months,
                "source_files": source_files,
                "source_checksums": source_checksums,
                "status": history.lifecycle.status,
                "exclusion_reason": None,
            }
        )
    current = sum(item["current_active"] is True for item in per_symbol)
    delisted = sum(item["current_active"] is False for item in per_symbol)
    usable = sum(item["status"] == "OK" for item in per_symbol)
    fingerprint = dataset_fingerprint(histories)
    return {
        "protocol_id": protocol_id,
        "protocol_hash": protocol_hash,
        "data_start": data_start.isoformat(),
        "data_end": data_end.isoformat(),
        "data_source": "Binance official data.binance.vision USD-M archives",
        "download_generated_at": None,
        "number_of_symbols_discovered": len(per_symbol),
        "number_of_current_symbols": current,
        "number_of_delisted_symbols": delisted,
        "number_of_usable_symbols": usable,
        "number_of_ambiguous_symbols": sum(item["status"] == "DATA_AMBIGUOUS" for item in per_symbol),
        "first_available_date": min((item["first_bar"] for item in per_symbol if item["first_bar"]), default=None),
        "last_available_date": max((item["last_bar"] for item in per_symbol if item["last_bar"]), default=None),
        "raw_file_count": sum(len(item) for item in (raw_files or {}).values()),
        "daily_bar_count": sum(item["daily_count"] for item in per_symbol),
        "funding_event_count": sum(item["funding_count"] for item in per_symbol),
        "dataset_sha256": fingerprint,
        "symbols": per_symbol,
    }
