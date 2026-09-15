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
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import pstdev
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote
from xml.etree import ElementTree

import requests

from .m1_protocol import (
    M1_DATA_END,
    M1_DATA_START,
    M1_PROTOCOL_ID,
)

OFFICIAL_ARCHIVE_BASE = "https://data.binance.vision/data"
OFFICIAL_ARCHIVE_LISTING_ENDPOINT = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/"
OFFICIAL_ARCHIVE_PREFIX = "data/futures/um/monthly"
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
class DiscoveredArchiveRecord:
    """One archive from the official catalog, including download provenance."""

    symbol: str
    kind: str
    month: str
    url: str
    discovered: bool = True
    local_path: str | None = None
    local_sha256: str | None = None
    official_checksum: str | None = None
    checksum_verified: bool | None = None
    download_status: str = "not_downloaded"
    normalize_status: str = "not_attempted"
    quality_status: str = "not_attempted"
    exclusion_reason: str | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        if self.source_url is None:
            object.__setattr__(self, "source_url", self.url)


@dataclass(frozen=True)
class DiscoveredSymbolRecord:
    """Lifecycle and dataset status for every symbol in the discovery catalog."""

    symbol: str
    first_discovered_month: str
    last_discovered_month: str
    daily_archive_months: tuple[str, ...]
    funding_archive_months: tuple[str, ...]
    normalization_status: str
    lifecycle_status: str
    dataset_status: str
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class DiscoveryListingPage:
    """Auditable metadata for one official S3 ListObjects page."""

    page_number: int
    request_url: str
    marker: str | None
    is_truncated: bool
    first_key: str | None
    last_key: str | None
    key_count: int
    page_content_sha256: str
    next_marker: str | None = None
    continuation_token: str | None = None
    keys: tuple[str, ...] = ()
    key_signatures: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DiscoveryCatalog:
    """Official archive listing plus its deterministic content fingerprint."""

    source: str
    prefix: str
    retrieved_at: str
    content_sha256: str
    archives: tuple[DiscoveredArchiveRecord, ...] = ()
    symbols: tuple[DiscoveredSymbolRecord, ...] = ()
    content_fingerprint: str = ""
    content: str | None = None
    listing_page_count: int = 1
    listing_complete: bool = True
    pages: tuple[DiscoveryListingPage, ...] = ()

    @property
    def number_of_symbols_discovered(self) -> int:
        return len(self.symbols) if self.listing_complete else 0


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
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
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


_ARCHIVE_PATH_RE = re.compile(
    r"(?:https?://[^\s\"'<>]+)?/futures/um/monthly/"
    r"(?:klines/[^/]+/1d/[^/]+-1d-\d{4}-\d{2}\.zip|"
    r"fundingRate/[^/]+/[^/]+-fundingRate-\d{4}-\d{2}\.zip)",
    re.IGNORECASE,
)


def discover_historical_archives(listing: Iterable[str]) -> tuple[ArchiveRef, ...]:
    """Build a deduplicated archive manifest from an official listing response.

    The input may be HTML, newline-delimited URLs, or local manifest lines.  No
    current exchangeInfo or manually curated symbol list is consulted.
    """
    refs: dict[tuple[str, str, str], ArchiveRef] = {}
    items = [listing] if isinstance(listing, str) else list(listing)
    for item in items:
        text = str(item)
        candidates = [match.group(0) for match in _ARCHIVE_PATH_RE.finditer(text)]
        if not candidates:
            candidates = [text]
        for candidate in candidates:
            ref = parse_archive_ref(candidate)
            if ref is not None:
                refs[(ref.symbol, ref.kind, ref.month)] = ref
    return tuple(refs[key] for key in sorted(refs))


def discovered_symbols(refs: Iterable[ArchiveRef]) -> tuple[str, ...]:
    return tuple(sorted({ref.symbol for ref in refs}))


def official_archive_listing_url(prefix: str = OFFICIAL_ARCHIVE_PREFIX) -> str:
    """Return the official S3 XML listing URL for one archive prefix."""
    clean_prefix = str(prefix).strip("/")
    if not clean_prefix:
        raise HistoryDataError("official archive listing prefix 不能为空")
    return f"{OFFICIAL_ARCHIVE_LISTING_ENDPOINT}?prefix={quote(clean_prefix + '/', safe='/')}"


def _normalized_listing_prefix(prefix: str) -> str:
    clean_prefix = str(prefix).strip("/")
    if not clean_prefix:
        raise HistoryDataError("official archive listing prefix 不能为空")
    return clean_prefix + "/"


def _listing_request_url(prefix: str, marker: str | None) -> str:
    url = official_archive_listing_url(prefix)
    return f"{url}&marker={quote(marker, safe='')}" if marker is not None else url


def _xml_local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _xml_child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _xml_local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def _parse_s3_listing_page(
    content: bytes,
    *,
    page_number: int,
    request_url: str,
    marker: str | None,
    expected_prefix: str,
) -> DiscoveryListingPage:
    """Parse and strictly validate one S3 ListObjects V1 XML page."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise HistoryDataError(f"DATA_INVALID: malformed S3 listing XML page {page_number}") from exc
    if _xml_local_name(root.tag) != "ListBucketResult":
        raise HistoryDataError(f"DATA_INVALID: unexpected S3 listing root page {page_number}")
    page_prefix = _xml_child_text(root, "Prefix")
    if page_prefix != expected_prefix:
        raise HistoryDataError(
            f"DATA_INVALID: S3 listing prefix mismatch page={page_number} "
            f"expected={expected_prefix!r} actual={page_prefix!r}"
        )
    reported_marker = _xml_child_text(root, "Marker") or None
    if reported_marker is not None and reported_marker != marker:
        raise HistoryDataError(
            f"DATA_INVALID: S3 listing marker mismatch page={page_number} "
            f"expected={marker!r} actual={reported_marker!r}"
        )
    truncated_text = (_xml_child_text(root, "IsTruncated") or "").lower()
    if truncated_text not in {"true", "false"}:
        raise HistoryDataError(f"DATA_INVALID: S3 listing IsTruncated missing/invalid page {page_number}")
    is_truncated = truncated_text == "true"
    next_marker = _xml_child_text(root, "NextMarker") or None
    keys: list[str] = []
    key_signatures: list[tuple[str, str]] = []
    signatures_by_key: dict[str, str] = {}
    for element in root:
        if _xml_local_name(element.tag) != "Contents":
            continue
        key = _xml_child_text(element, "Key")
        if not key:
            raise HistoryDataError(f"DATA_INVALID: S3 listing Contents missing Key page {page_number}")
        if not key.startswith(expected_prefix):
            raise HistoryDataError(
                f"DATA_INVALID: S3 listing key outside prefix page={page_number} key={key!r}"
            )
        metadata = tuple(sorted(
            (
                str(_xml_local_name(child.tag)),
                (child.text or "").strip(),
            )
            for child in element
        ))
        signature = hashlib.sha256(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        previous_signature = signatures_by_key.get(key)
        if previous_signature is not None and previous_signature != signature:
            raise HistoryDataError(f"DATA_INVALID: conflicting duplicate S3 key {key}")
        signatures_by_key[key] = signature
        keys.append(key)
        key_signatures.append((key, signature))
    if keys != sorted(keys):
        raise HistoryDataError(f"DATA_INVALID: S3 listing key ordering abnormal page {page_number}")
    return DiscoveryListingPage(
        page_number=page_number,
        request_url=request_url,
        marker=marker,
        is_truncated=is_truncated,
        first_key=keys[0] if keys else None,
        last_key=keys[-1] if keys else None,
        key_count=len(keys),
        page_content_sha256=hashlib.sha256(content).hexdigest(),
        next_marker=next_marker,
        keys=tuple(keys),
        key_signatures=tuple(key_signatures),
    )


def _catalog_symbol_records(
    refs: Sequence[ArchiveRef],
    supplied: Iterable[DiscoveredSymbolRecord] = (),
) -> tuple[DiscoveredSymbolRecord, ...]:
    supplied_by_symbol = {record.symbol.upper(): record for record in supplied}
    grouped: dict[str, dict[str, set[str]]] = {}
    for ref in refs:
        symbol = ref.symbol.upper()
        grouped.setdefault(symbol, {"daily_klines": set(), "funding": set()})[ref.kind].add(ref.month)
    records: list[DiscoveredSymbolRecord] = []
    for symbol in sorted(grouped):
        months = sorted({month for values in grouped[symbol].values() for month in values})
        existing = supplied_by_symbol.get(symbol)
        if existing is not None:
            records.append(existing)
            continue
        records.append(
            DiscoveredSymbolRecord(
                symbol=symbol,
                first_discovered_month=months[0],
                last_discovered_month=months[-1],
                daily_archive_months=tuple(sorted(grouped[symbol]["daily_klines"])),
                funding_archive_months=tuple(sorted(grouped[symbol]["funding"])),
                normalization_status="not_attempted",
                lifecycle_status="not_attempted",
                dataset_status="not_attempted",
            )
        )
    # Explicit symbol records may describe an exclusion whose archive was not
    # returned by the mocked listing.  Retain it rather than applying
    # survivorship filtering.
    for symbol in sorted(set(supplied_by_symbol) - set(grouped)):
        records.append(supplied_by_symbol[symbol])
    return tuple(sorted(records, key=lambda record: record.symbol.upper()))


def build_discovery_catalog(
    refs_or_listing: Iterable[ArchiveRef] | Iterable[str] | str,
    *,
    source: str = OFFICIAL_ARCHIVE_LISTING_ENDPOINT,
    prefix: str = OFFICIAL_ARCHIVE_PREFIX,
    retrieved_at: str | None = None,
    content: str | bytes | None = None,
    content_bytes: bytes | None = None,
    symbol_records: Iterable[DiscoveredSymbolRecord] = (),
    pages: Iterable[DiscoveryListingPage] = (),
    listing_complete: bool = True,
) -> DiscoveryCatalog:
    """Build a catalog without dropping archives that later fail processing."""
    values = [refs_or_listing] if isinstance(refs_or_listing, str) else list(refs_or_listing)
    refs = (
        tuple(value for value in values if isinstance(value, ArchiveRef))
        if all(isinstance(value, ArchiveRef) for value in values)
        else discover_historical_archives([str(value) for value in values])
    )
    by_key = {(ref.symbol, ref.kind, ref.month): ref for ref in refs}
    archives = tuple(
        DiscoveredArchiveRecord(
            symbol=ref.symbol,
            kind=ref.kind,
            month=ref.month,
            url=ref.url,
        )
        for ref in (by_key[key] for key in sorted(by_key))
    )
    supplied_records = tuple(symbol_records)
    symbols = _catalog_symbol_records(refs, supplied_records)
    if content_bytes is None:
        if isinstance(content, bytes):
            content_bytes = content
        elif isinstance(content, str):
            content_bytes = content.encode("utf-8")
        else:
            content_bytes = "\n".join(ref.url for ref in refs).encode("utf-8")
    if isinstance(content, bytes):
        content_text = content.decode("utf-8", "replace")
    elif isinstance(content, str):
        content_text = content
    else:
        content_text = content_bytes.decode("utf-8", "replace")
    page_records = tuple(pages)
    if not page_records:
        page_records = (
            DiscoveryListingPage(
                page_number=1,
                request_url=str(source),
                marker=None,
                is_truncated=not listing_complete,
                first_key=None,
                last_key=None,
                key_count=0,
                page_content_sha256=hashlib.sha256(content_bytes).hexdigest(),
            ),
        )
    fingerprint_payload = json.dumps(
        {
            "archives": [
                {
                    "symbol": archive.symbol,
                    "kind": archive.kind,
                    "month": archive.month,
                    "url": archive.url,
                }
                for archive in archives
            ],
            "pages": [
                {
                    "page_number": page.page_number,
                    "request_url": page.request_url,
                    "marker": page.marker,
                    "next_marker": page.next_marker,
                    "is_truncated": page.is_truncated,
                    "first_key": page.first_key,
                    "last_key": page.last_key,
                    "key_count": page.key_count,
                    "page_content_sha256": page.page_content_sha256,
                }
                for page in page_records
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return DiscoveryCatalog(
        source=str(source),
        prefix=str(prefix).strip("/"),
        retrieved_at=retrieved_at or datetime.now(timezone.utc).isoformat(),
        content_sha256=hashlib.sha256(content_bytes).hexdigest(),
        archives=archives,
        symbols=symbols,
        content_fingerprint=hashlib.sha256(fingerprint_payload).hexdigest(),
        content=content_text,
        listing_page_count=len(page_records),
        listing_complete=listing_complete is True,
        pages=page_records,
    )


def catalog_with_statuses(
    catalog: DiscoveryCatalog,
    *,
    symbol_records: Iterable[DiscoveredSymbolRecord] = (),
    archive_records: Iterable[DiscoveredArchiveRecord] = (),
) -> DiscoveryCatalog:
    """Return a catalog with processing statuses merged by stable archive key."""
    archive_by_key = {
        (archive.symbol.upper(), archive.kind, archive.month): archive
        for archive in catalog.archives
    }
    archive_by_key.update({
        (record.symbol.upper(), record.kind, record.month): record
        for record in archive_records
    })
    merged_archives = tuple(archive_by_key[key] for key in sorted(archive_by_key))
    symbol_by_symbol = {record.symbol.upper(): record for record in catalog.symbols}
    symbol_by_symbol.update({
        record.symbol.upper(): record
        for record in symbol_records
    })
    merged_symbols = _catalog_symbol_records(
        tuple(
            ArchiveRef(archive.symbol, archive.kind, archive.month, archive.url)
            for archive in merged_archives
        ),
        symbol_by_symbol.values(),
    )
    return replace(catalog, archives=merged_archives, symbols=merged_symbols)


def acquire_official_archive_listing(
    *,
    prefix: str = OFFICIAL_ARCHIVE_PREFIX,
    session: requests.Session | None = None,
    retrieved_at: str | None = None,
    max_pages: int = 1000,
) -> DiscoveryCatalog:
    """GET the official archive index and return its auditable catalog.

    M1-A supplies mocked responses only; this interface is the sole intended
    discovery entry point for a later M1-B run.
    """
    if max_pages <= 0:
        raise HistoryDataError("DATA_INVALID: max_pages 必须为正数")
    expected_prefix = _normalized_listing_prefix(prefix)
    client = session or requests.Session()
    marker: str | None = None
    requested_markers: set[str] = set()
    pages: list[DiscoveryListingPage] = []
    page_contents: list[bytes] = []
    all_keys: list[str] = []
    signatures_by_key: dict[str, str] = {}
    previous_last_key: str | None = None
    listing_complete = False
    for page_number in range(1, max_pages + 1):
        if marker is not None:
            if marker in requested_markers:
                raise HistoryDataError(f"DATA_INVALID: repeated S3 listing marker {marker!r}")
            requested_markers.add(marker)
        request_url = _listing_request_url(prefix, marker)
        try:
            response = client.get(request_url, timeout=60)
        except requests.exceptions.RequestException as exc:
            raise HistoryDataError(
                f"DATA_INVALID: official archive listing request failed page={page_number}"
            ) from exc
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            raise HistoryDataError(
                f"DATA_INVALID: official archive listing response missing HTTP status page={page_number}"
            )
        if status_code >= 400:
            raise HistoryDataError(
                f"official archive listing GET failed page={page_number}: HTTP {status_code}"
            )
        raw_content = getattr(response, "content", None)
        if not isinstance(raw_content, bytes) or not raw_content:
            response_text = getattr(response, "text", None)
            if response_text is None:
                raise HistoryDataError(f"DATA_INVALID: empty S3 listing page {page_number}")
            raw_content = str(response_text).encode("utf-8")
        page = _parse_s3_listing_page(
            raw_content,
            page_number=page_number,
            request_url=request_url,
            marker=marker,
            expected_prefix=expected_prefix,
        )
        if previous_last_key is not None and any(
            key < previous_last_key for key in page.keys
        ):
            raise HistoryDataError(
                f"DATA_INVALID: S3 listing page ordering regressed at page {page_number}"
            )
        for key, signature in page.key_signatures:
            previous_signature = signatures_by_key.get(key)
            if previous_signature is not None and previous_signature != signature:
                raise HistoryDataError(f"DATA_INVALID: conflicting duplicate S3 key {key}")
            signatures_by_key[key] = signature
        pages.append(page)
        page_contents.append(raw_content)
        all_keys.extend(page.keys)
        previous_last_key = page.last_key or previous_last_key
        if not page.is_truncated:
            listing_complete = True
            break
        if page_number >= max_pages:
            raise HistoryDataError("DATA_INVALID: S3 listing exceeded max_pages before completion")
        next_marker = page.next_marker or page.last_key
        if not next_marker:
            raise HistoryDataError(
                f"DATA_INVALID: truncated S3 listing page {page_number} has no next marker"
            )
        if marker is not None and next_marker <= marker:
            raise HistoryDataError(
                f"DATA_INVALID: S3 listing marker did not advance {marker!r} -> {next_marker!r}"
            )
        if next_marker in requested_markers:
            raise HistoryDataError(f"DATA_INVALID: repeated S3 listing marker {next_marker!r}")
        marker = next_marker
    if not listing_complete:
        raise HistoryDataError("DATA_INVALID: S3 listing completion was not confirmed")
    return build_discovery_catalog(
        all_keys,
        source=official_archive_listing_url(prefix),
        prefix=prefix,
        retrieved_at=retrieved_at,
        content_bytes=b"".join(page_contents),
        pages=pages,
        listing_complete=True,
    )


def checksum_url(ref: ArchiveRef) -> str:
    """Return Binance's official checksum sidecar URL for an archive."""
    return f"{ref.url}.CHECKSUM"


def parse_official_checksum(text: str) -> str:
    """Extract the 64-hex SHA-256 token from Binance checksum text."""
    match = re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", str(text), re.IGNORECASE)
    if match is None:
        raise HistoryDataError("official checksum sidecar 缺少 64 位 SHA-256")
    return match.group(0).lower()


def download_archive_record(
    ref: ArchiveRef,
    *,
    raw_dir: str | Path = M1_RAW_CACHE,
    session: requests.Session | None = None,
    verify_checksum: bool = True,
) -> DiscoveredArchiveRecord:
    """GET one archive and its official checksum, returning auditable status."""
    expected_url = archive_url(ref.symbol, ref.kind, ref.month)
    if ref.url != expected_url:
        raise HistoryDataError("archive URL 与已解析 symbol/kind/month 不一致")
    base = DiscoveredArchiveRecord(ref.symbol, ref.kind, ref.month, ref.url)
    client = session or requests.Session()
    response = client.get(ref.url, timeout=60)
    if response.status_code == 404:
        return replace(base, download_status="not_found", exclusion_reason="ARCHIVE_HTTP_404")
    if response.status_code >= 400:
        raise HistoryDataError(f"archive GET failed: HTTP {response.status_code}")
    target_dir = Path(raw_dir) / ref.kind / ref.symbol
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{ref.month}.zip"
    target.write_bytes(response.content)
    local_sha = file_sha256(target)
    official = None
    verified: bool | None = None
    if verify_checksum:
        checksum_response = client.get(checksum_url(ref), timeout=60)
        if checksum_response.status_code == 404:
            return replace(
                base,
                local_path=str(target),
                local_sha256=local_sha,
                download_status="checksum_not_found",
                checksum_verified=False,
                exclusion_reason="CHECKSUM_HTTP_404",
            )
        if checksum_response.status_code >= 400:
            raise HistoryDataError(
                f"official checksum GET failed: HTTP {checksum_response.status_code}"
            )
        official = parse_official_checksum(checksum_response.text)
        verified = local_sha == official
        if not verified:
            raise HistoryDataError(
                "DATA_INVALID: archive SHA-256 mismatch "
                f"symbol={ref.symbol} kind={ref.kind} month={ref.month} "
                f"official={official} actual={local_sha}"
            )
    return replace(
        base,
        local_path=str(target),
        local_sha256=local_sha,
        official_checksum=official,
        checksum_verified=verified,
        download_status="downloaded",
    )


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
    record = download_archive_record(
        ref,
        raw_dir=raw_dir,
        session=session,
        verify_checksum=True,
    )
    return Path(record.local_path) if record.local_path and record.download_status == "downloaded" else None


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


def _archive_record_dict(record: DiscoveredArchiveRecord) -> dict[str, Any]:
    local_sha = record.local_sha256
    if record.local_path and Path(record.local_path).is_file():
        local_sha = file_sha256(record.local_path)
    return {
        "symbol": record.symbol,
        "kind": record.kind,
        "month": record.month,
        "url": record.url,
        "source_url": record.source_url or record.url,
        "discovered": record.discovered,
        "local_path": record.local_path,
        "local_sha256": local_sha,
        "official_checksum": record.official_checksum,
        "checksum_verified": record.checksum_verified,
        "download_status": record.download_status,
        "normalize_status": record.normalize_status,
        "quality_status": record.quality_status,
        "exclusion_reason": record.exclusion_reason,
    }


def _archive_failure_reason(records: Sequence[Mapping[str, Any]]) -> str | None:
    """Return the first fail-closed processing reason for one symbol."""
    for record in records:
        if record.get("exclusion_reason"):
            return str(record["exclusion_reason"])
        download_status = str(record.get("download_status", ""))
        if download_status not in {"", "downloaded", "not_downloaded"}:
            return f"DOWNLOAD_{download_status.upper()}"
        normalize_status = str(record.get("normalize_status", ""))
        if normalize_status not in {"", "not_attempted", "normalized", "passed"}:
            return f"NORMALIZE_{normalize_status.upper()}"
        quality_status = str(record.get("quality_status", ""))
        if quality_status not in {"", "not_attempted", "passed", "usable"}:
            return f"QUALITY_{quality_status.upper()}"
    return None


def build_dataset_manifest(
    histories: Iterable[SymbolHistory],
    *,
    protocol_id: str = M1_PROTOCOL_ID,
    protocol_hash: str = "",
    data_start: date = M1_DATA_START,
    data_end: date = M1_DATA_END,
    raw_files: Mapping[str, Mapping[str, str]] | None = None,
    catalog: DiscoveryCatalog | None = None,
    archive_records: Iterable[DiscoveredArchiveRecord] = (),
    symbol_records: Iterable[DiscoveredSymbolRecord] = (),
) -> dict[str, Any]:
    """Build a manifest while retaining every discovered/excluded symbol."""
    if catalog is not None and catalog.listing_complete is not True:
        raise HistoryDataError("DATA_INVALID: incomplete official archive listing cannot enter dataset")
    histories = tuple(sorted(histories, key=lambda item: item.symbol.upper()))
    history_by_symbol = {history.symbol.upper(): history for history in histories}
    explicit_symbol_records = {record.symbol.upper(): record for record in symbol_records}
    catalog_symbol_records = {
        record.symbol.upper(): record for record in (catalog.symbols if catalog else ())
    }
    status_records = {**catalog_symbol_records, **explicit_symbol_records}

    archive_record_values = tuple(archive_records)
    archive_by_key = {
        (record.symbol.upper(), record.kind, record.month): record
        for record in archive_record_values
    }
    if catalog is not None:
        for record in catalog.archives:
            archive_by_key.setdefault(
                (record.symbol.upper(), record.kind, record.month), record
            )
    provenance_records = tuple(
        archive_by_key[key] for key in sorted(archive_by_key)
    )
    provenance_dicts = tuple(_archive_record_dict(record) for record in provenance_records)

    legacy_raw_files = raw_files or {}
    legacy_symbols = {str(symbol).upper() for symbol in legacy_raw_files}
    symbol_names = sorted(
        set(history_by_symbol)
        | set(status_records)
        | {record.symbol.upper() for record in provenance_records}
        | legacy_symbols
    )
    per_symbol: list[dict[str, Any]] = []
    for symbol in symbol_names:
        history = history_by_symbol.get(symbol)
        status_record = status_records.get(symbol)
        days = sorted({bar.day for bar in history.daily_bars}) if history else []
        expected = _expected_daily_days(days[0], days[-1]) if days else set()
        missing_days = sorted(expected - set(days))
        months = sorted({day.strftime("%Y-%m") for day in missing_days})
        symbol_archives = [
            record
            for record in provenance_dicts
            if record["symbol"].upper() == symbol
        ]
        archive_failure = _archive_failure_reason(symbol_archives)
        if provenance_records:
            source_files: Any = symbol_archives
            source_checksums: Any = [
                {
                    key: record[key]
                    for key in (
                        "symbol",
                        "kind",
                        "month",
                        "source_url",
                        "local_path",
                        "local_sha256",
                        "official_checksum",
                        "checksum_verified",
                    )
                }
                for record in symbol_archives
            ]
        else:
            legacy_for_symbol = next(
                (value for key, value in legacy_raw_files.items() if str(key).upper() == symbol),
                {},
            )
            source_files = dict(legacy_for_symbol)
            source_checksums = {}
            for kind, source in source_files.items():
                source_path = Path(str(source))
                if source_path.is_file():
                    source_checksums[str(kind)] = file_sha256(source_path)

        if history is not None:
            default_normalization = "normalized"
            default_lifecycle = history.lifecycle.status
            default_dataset = (
                "usable"
                if history.lifecycle.status == "OK" and archive_failure is None
                else "excluded"
            )
            default_reason = (
                archive_failure
                or (None if default_dataset == "usable" else history.lifecycle.status)
            )
        else:
            default_normalization = "not_normalized"
            default_lifecycle = "not_available"
            default_dataset = "excluded"
            default_reason = archive_failure or "NOT_NORMALIZED"
        if status_record is not None:
            normalization_status = status_record.normalization_status
            lifecycle_status = status_record.lifecycle_status
            dataset_status = status_record.dataset_status
            exclusion_reason = status_record.exclusion_reason
            if archive_failure is not None:
                dataset_status = "excluded"
                exclusion_reason = archive_failure
            elif history is not None and dataset_status == "not_attempted":
                normalization_status = default_normalization
                lifecycle_status = default_lifecycle
                dataset_status = default_dataset
                exclusion_reason = default_reason
            elif dataset_status != "usable" and not exclusion_reason:
                exclusion_reason = default_reason or "NOT_USABLE"
        else:
            normalization_status = default_normalization
            lifecycle_status = default_lifecycle
            dataset_status = default_dataset
            exclusion_reason = default_reason

        discovered_months = (
            status_record.first_discovered_month,
            status_record.last_discovered_month,
        ) if status_record is not None else (None, None)
        daily_archive_months = (
            list(status_record.daily_archive_months) if status_record is not None else []
        )
        funding_archive_months = (
            list(status_record.funding_archive_months) if status_record is not None else []
        )
        if provenance_records and status_record is None:
            archive_months = sorted({record["month"] for record in symbol_archives})
            discovered_months = (archive_months[0], archive_months[-1]) if archive_months else (None, None)
            daily_archive_months = sorted(
                {record["month"] for record in symbol_archives if record["kind"] == "daily_klines"}
            )
            funding_archive_months = sorted(
                {record["month"] for record in symbol_archives if record["kind"] == "funding"}
            )
        per_symbol.append(
            {
                "symbol": symbol,
                "first_discovered_month": discovered_months[0],
                "last_discovered_month": discovered_months[1],
                "daily_archive_months": daily_archive_months,
                "funding_archive_months": funding_archive_months,
                "normalization_status": normalization_status,
                "lifecycle_status": lifecycle_status,
                "dataset_status": dataset_status,
                "listed_from": history.lifecycle.listed_from.isoformat() if history else None,
                "delisted_at": history.lifecycle.delisted_at.isoformat() if history and history.lifecycle.delisted_at else None,
                "current_active": history.currently_active if history else None,
                "currently_active": history.currently_active if history else None,
                "lifecycle_source": history.lifecycle.lifecycle_source if history else None,
                "lifecycle_confidence": history.lifecycle.lifecycle_confidence if history else None,
                "first_bar": days[0].isoformat() if days else None,
                "last_bar": days[-1].isoformat() if days else None,
                "daily_count": len(history.daily_bars) if history else 0,
                "funding_count": len(history.funding_events) if history else 0,
                "missing_days": [day.isoformat() for day in missing_days],
                "missing_months": months,
                "source_files": source_files,
                "source_checksums": source_checksums,
                "status": lifecycle_status,
                "exclusion_reason": exclusion_reason,
            }
        )
    current = sum(item["current_active"] is True for item in per_symbol)
    delisted = sum(item["current_active"] is False for item in per_symbol)
    usable = sum(item["dataset_status"] == "usable" for item in per_symbol)
    fingerprint = dataset_fingerprint(histories)
    discovery_metadata = None
    if catalog is not None:
        discovery_metadata = {
            "source": catalog.source,
            "prefix": catalog.prefix,
            "retrieved_at": catalog.retrieved_at,
            "content_sha256": catalog.content_sha256,
            "content_fingerprint": catalog.content_fingerprint,
            "listing_page_count": catalog.listing_page_count,
            "listing_complete": catalog.listing_complete,
            "pages": [asdict(page) for page in catalog.pages],
        }
    raw_file_count = (
        sum(bool(record["local_path"]) for record in provenance_dicts)
        if provenance_records
        else sum(len(item) for item in legacy_raw_files.values())
    )
    return {
        "protocol_id": protocol_id,
        "protocol_hash": protocol_hash,
        "data_start": data_start.isoformat(),
        "data_end": data_end.isoformat(),
        "data_source": "Binance official data.binance.vision USD-M archives",
        "download_generated_at": None,
        "number_of_symbols_discovered": (
            catalog.number_of_symbols_discovered if catalog is not None else len(per_symbol)
        ),
        "number_of_current_symbols": current,
        "number_of_delisted_symbols": delisted,
        "number_of_usable_symbols": usable,
        "number_of_ambiguous_symbols": sum(item["lifecycle_status"] == "DATA_AMBIGUOUS" for item in per_symbol),
        "first_available_date": min((item["first_bar"] for item in per_symbol if item["first_bar"]), default=None),
        "last_available_date": max((item["last_bar"] for item in per_symbol if item["last_bar"]), default=None),
        "raw_file_count": raw_file_count,
        "daily_bar_count": sum(item["daily_count"] for item in per_symbol),
        "funding_event_count": sum(item["funding_count"] for item in per_symbol),
        "dataset_sha256": fingerprint,
        "discovery_catalog": discovery_metadata,
        "raw_files": list(provenance_dicts),
        "symbols": per_symbol,
    }
