"""Formal M1-B frozen point-in-time validation runner.

This module is deliberately separate from the legacy cross-sectional backtest.
It acquires only the official Binance USD-M archive paths named by the frozen
protocol, keeps the complete discovery/download provenance, and runs the
pre-registered Control and secondary Shadow without changing the gate policy.

The command-line entry point requires the explicit ``START M1-B`` approval so
the M1-A preflight cannot accidentally become a result-producing run.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import hashlib
import json
import math
import pickle
import re
import statistics
import sys
import threading
import time as time_module
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlencode
from xml.etree import ElementTree

import requests

from src.directional_engine import position_scale
from src.xs_lowvol_spec import (
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
    SHADOW_SPEC_HASH,
    SHADOW_STRATEGY_ID,
    resolve_rules,
    strategy_spec_hash,
)

from .m1_gates import (
    block_bootstrap,
    compound_return,
    evaluate_frozen_gates,
    leave_one_out,
    max_drawdown_pct,
    remove_best_5pct,
    split_windowed_rows,
    symbol_concentration,
    weekly_profit_factor,
    weekly_sharpe,
)
from .m1_protocol import (
    M1_DATA_END,
    M1_DATA_START,
    M1_DISCOVERY_END,
    M1_DISCOVERY_START,
    M1_EXTERNAL_END,
    M1_EXTERNAL_START,
    M1_PROTOCOL_HASH_PATH,
    M1_PROTOCOL_PATH,
    M1_APPROVED_PROTOCOL_SHA256,
    frozen_gate_policy,
    load_protocol,
    protocol_windows,
    verify_protocol_hash,
    verified_protocol_identity,
)
from .xs_data_quality import (
    DataQualityReport,
    FundingCoverageReport,
    HoldingInterval,
    validate_dataset,
    validate_funding_coverage_for_holds,
)
from .xs_history import (
    OFFICIAL_ARCHIVE_LISTING_ENDPOINT,
    M1_NORMALIZED_CACHE,
    M1_RAW_CACHE,
    ArchiveRef,
    DailyBar,
    DiscoveredArchiveRecord,
    DiscoveredSymbolRecord,
    DiscoveryCatalog,
    DiscoveryListingPage,
    FundingEvent,
    HistoryDataError,
    PITSignal,
    PITSnapshot,
    PITUniverseResult,
    SymbolHistory,
    _parse_s3_listing_page,
    archive_url,
    build_dataset_manifest,
    build_pit_signal,
    build_pit_universe,
    discover_historical_archives,
    download_archive_record,
    file_sha256,
    is_historical_usdt_perpetual_symbol,
    month_keys,
    normalize_symbol_archives,
    parse_archive_ref,
    parse_official_checksum,
    checksum_url,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
M1_REPORT_PATH = PROJECT_ROOT / "research" / "m1" / "M1_REPORT.md"
M1_DECISION_PATH = PROJECT_ROOT / "research" / "m1" / "M1_DECISION.json"
M1_DATASET_MANIFEST_PATH = PROJECT_ROOT / "research" / "m1" / "M1_DATASET_MANIFEST.json"
M1_DATASET_FREEZE_PATH = M1_NORMALIZED_CACHE / "XS_LOWVOL_M1_DATASET.pkl"
S3_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
S3_MAX_KEYS = 1000
_ARCHIVE_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_S3_PARTITION_CHARS = tuple("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DOWNLOAD_THREAD_STATE = threading.local()


class M1BError(RuntimeError):
    """A fail-closed formal M1-B execution error."""


class M1InvalidError(M1BError):
    """A frozen protocol or strategy identity changed during the run."""


def _canonical(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda p: str(p[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        return sorted(_canonical(item) for item in value)
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _utc_day_end_ms(day: date) -> int:
    return int(datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000) - 1


def _date_range(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=index) for index in range((end - start).days + 1))


def _symbol_from_prefix(prefix: str) -> str:
    return str(prefix).strip("/").split("/")[-1].upper()


def _s3_listing_url(prefix: str, marker: str | None = None, delimiter: str | None = None) -> str:
    params: list[tuple[str, str]] = [("prefix", str(prefix)), ("max-keys", str(S3_MAX_KEYS))]
    if delimiter is not None:
        params.append(("delimiter", delimiter))
    if marker is not None:
        params.append(("marker", marker))
    return f"{OFFICIAL_ARCHIVE_LISTING_ENDPOINT}?{urlencode(params)}"


def _xml_text(root: ElementTree.Element, name: str) -> str | None:
    for child in root:
        if child.tag.rsplit("}", 1)[-1] == name:
            return (child.text or "").strip()
    return None


def _s3_items(content: bytes, *, prefix: str, marker: str | None) -> tuple[tuple[str, ...], bool, str | None]:
    """Parse one complete S3 V1 page, including CommonPrefixes when used."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise M1BError("DATA_INVALID: malformed official S3 XML page") from exc
    if root.tag.rsplit("}", 1)[-1] != "ListBucketResult":
        raise M1BError("DATA_INVALID: unexpected official S3 XML root")
    actual_prefix = _xml_text(root, "Prefix")
    if actual_prefix != prefix:
        raise M1BError(f"DATA_INVALID: S3 prefix mismatch expected={prefix!r} actual={actual_prefix!r}")
    actual_marker = _xml_text(root, "Marker") or None
    if actual_marker != marker:
        raise M1BError(f"DATA_INVALID: S3 marker mismatch expected={marker!r} actual={actual_marker!r}")
    truncated_text = (_xml_text(root, "IsTruncated") or "").lower()
    if truncated_text not in {"true", "false"}:
        raise M1BError("DATA_INVALID: S3 IsTruncated missing or invalid")
    keys: list[str] = []
    for child in root:
        local = child.tag.rsplit("}", 1)[-1]
        if local == "Contents":
            key = _xml_text(child, "Key")
            if not key or not key.startswith(prefix):
                raise M1BError("DATA_INVALID: S3 Contents key outside requested prefix")
            keys.append(key)
        elif local == "CommonPrefixes":
            common = _xml_text(child, "Prefix")
            if not common or not common.startswith(prefix):
                raise M1BError("DATA_INVALID: S3 CommonPrefixes value outside requested prefix")
            keys.append(common)
    if keys != sorted(keys):
        raise M1BError("DATA_INVALID: S3 page key ordering is not ascending")
    next_marker = _xml_text(root, "NextMarker") or None
    return tuple(keys), truncated_text == "true", next_marker


def _listing_page(
    content: bytes,
    *,
    page_number: int,
    request_url: str,
    marker: str | None,
    items: Sequence[str],
    is_truncated: bool,
    next_marker: str | None,
) -> DiscoveryListingPage:
    return DiscoveryListingPage(
        page_number=page_number,
        request_url=request_url,
        marker=marker,
        is_truncated=is_truncated,
        first_key=items[0] if items else None,
        last_key=items[-1] if items else None,
        key_count=len(items),
        page_content_sha256=hashlib.sha256(content).hexdigest(),
        next_marker=next_marker,
        keys=tuple(items),
    )


def _list_s3_prefix(
    prefix: str,
    *,
    delimiter: str | None = None,
    page_number_start: int = 1,
    session: requests.Session | None = None,
) -> tuple[tuple[str, ...], tuple[DiscoveryListingPage, ...], tuple[bytes, ...]]:
    """Strictly paginate one disjoint official S3 prefix until false."""
    expected_prefix = str(prefix)
    client = session or requests.Session()
    marker: str | None = None
    seen_markers: set[str] = set()
    pages: list[DiscoveryListingPage] = []
    raw_pages: list[bytes] = []
    all_items: list[str] = []
    previous_last: str | None = None
    for local_page in range(1, 1001):
        if marker is not None:
            if marker in seen_markers:
                raise M1BError(f"DATA_INVALID: repeated S3 marker {marker!r}")
            seen_markers.add(marker)
        request_url = _s3_listing_url(expected_prefix, marker, delimiter)
        response = None
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                response = client.get(request_url, timeout=(60, 240))
                break
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt + 1 < 5:
                    time_module.sleep(1.0 * (attempt + 1))
        if response is None:
            raise M1BError(f"DATA_INVALID: official catalog request failed prefix={expected_prefix!r}") from last_error
        if response.status_code >= 400:
            raise M1BError(f"official catalog GET failed HTTP {response.status_code} prefix={expected_prefix!r}")
        content = bytes(response.content)
        if not content:
            raise M1BError("DATA_INVALID: empty official S3 catalog page")
        items, truncated, next_marker = _s3_items(content, prefix=expected_prefix, marker=marker)
        if previous_last is not None and items and items[0] < previous_last:
            raise M1BError("DATA_INVALID: S3 catalog ordering regressed between pages")
        page = _listing_page(
            content,
            page_number=page_number_start + local_page - 1,
            request_url=request_url,
            marker=marker,
            items=items,
            is_truncated=truncated,
            next_marker=next_marker,
        )
        pages.append(page)
        raw_pages.append(content)
        all_items.extend(items)
        previous_last = page.last_key or previous_last
        if not truncated:
            return tuple(all_items), tuple(pages), tuple(raw_pages)
        next_value = next_marker or page.last_key
        if not next_value or (marker is not None and next_value <= marker):
            raise M1BError("DATA_INVALID: truncated S3 page did not provide an advancing marker")
        marker = next_value
    raise M1BError(f"DATA_INVALID: S3 prefix exceeded pagination limit {expected_prefix!r}")


def _valid_symbol_prefixes(items: Iterable[str]) -> tuple[str, ...]:
    prefixes: set[str] = set()
    for item in items:
        symbol = _symbol_from_prefix(item)
        if is_historical_usdt_perpetual_symbol(symbol):
            prefixes.add(str(item).rstrip("/") + "/")
    return tuple(sorted(prefixes))


def _archive_refs_from_keys(keys: Iterable[str]) -> tuple[ArchiveRef, ...]:
    refs: dict[tuple[str, str, str], ArchiveRef] = {}
    for key in keys:
        if not str(key).endswith(".zip"):
            continue
        ref = parse_archive_ref(str(key))
        if ref is not None:
            refs[(ref.symbol, ref.kind, ref.month)] = ref
    allowed_months = set(month_keys(M1_DATA_START, M1_DATA_END))
    return tuple(refs[key] for key in sorted(refs) if key[2] in allowed_months)


def _catalog_symbol_prefixes(
    *,
    session: requests.Session | None = None,
) -> tuple[tuple[str, ...], tuple[DiscoveryListingPage, ...], tuple[bytes, ...]]:
    """Get every kline symbol folder through complete S3 delimiter pages."""
    prefix = "data/futures/um/monthly/klines/"
    values, pages, raw_pages = _list_s3_prefix(prefix, delimiter="/", session=session)
    return _valid_symbol_prefixes(values), pages, raw_pages


def _catalog_funding_partition(
    character: str,
) -> tuple[tuple[str, ...], tuple[DiscoveryListingPage, ...], tuple[bytes, ...]]:
    prefix = f"data/futures/um/monthly/fundingRate/{character}"
    return _list_s3_prefix(prefix)


def acquire_m1_b_archive_catalog(*, workers: int = 16, retrieved_at: str | None = None) -> DiscoveryCatalog:
    """Acquire the complete official 1d/funding archive catalog.

    The S3 bucket has no server-side wildcard for ``/1d/``.  Kline symbol
    folders are therefore enumerated through complete first-character pages,
    then each valid symbol's exact ``/1d/`` prefix is paginated.  Funding is
    partitioned by first character so its complete object listing is also
    auditable without walking unrelated archive families.
    """
    if workers <= 0:
        raise M1BError("workers must be positive")
    retrieved = retrieved_at or datetime.now(timezone.utc).isoformat()
    top_prefixes, top_pages, top_raw = _catalog_symbol_prefixes()
    daily_keys: list[str] = []
    pages: list[DiscoveryListingPage] = list(top_pages)
    raw_pages: list[bytes] = list(top_raw)
    page_offset = len(pages) + 1

    def list_daily(symbol_prefix: str) -> tuple[str, tuple[str, ...], tuple[DiscoveryListingPage, ...], tuple[bytes, ...]]:
        symbol = _symbol_from_prefix(symbol_prefix)
        exact_prefix = f"data/futures/um/monthly/klines/{symbol}/1d/"
        items, local_pages, local_raw = _list_s3_prefix(exact_prefix, page_number_start=0)
        return symbol, items, local_pages, local_raw

    daily_results: list[tuple[str, tuple[str, ...], tuple[DiscoveryListingPage, ...], tuple[bytes, ...]]] = []
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(top_prefixes)))) as pool:
        futures = [pool.submit(list_daily, prefix) for prefix in top_prefixes]
        for future in as_completed(futures):
            daily_results.append(future.result())
    for symbol, items, local_pages, local_raw in sorted(daily_results, key=lambda item: item[0]):
        daily_keys.extend(items)
        for page in local_pages:
            pages.append(replace(page, page_number=page_offset))
            page_offset += 1
        raw_pages.extend(local_raw)

    funding_keys: list[str] = []
    funding_results: list[tuple[tuple[str, ...], tuple[DiscoveryListingPage, ...], tuple[bytes, ...]]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(_S3_PARTITION_CHARS))) as pool:
        futures = [pool.submit(_catalog_funding_partition, character) for character in _S3_PARTITION_CHARS]
        for future in as_completed(futures):
            funding_results.append(future.result())
    for items, local_pages, local_raw in sorted(funding_results, key=lambda item: item[1][0].request_url if item[1] else ""):
        funding_keys.extend(items)
        for page in local_pages:
            pages.append(replace(page, page_number=page_offset))
            page_offset += 1
        raw_pages.extend(local_raw)

    refs = _archive_refs_from_keys((*daily_keys, *funding_keys))
    if not refs:
        raise M1BError("DATA_INVALID: official catalog contained no required M1 archive")
    # Reuse the tested symbol-record builder through the public catalog helper.
    from .xs_history import build_discovery_catalog

    result = build_discovery_catalog(
        refs,
        source=OFFICIAL_ARCHIVE_LISTING_ENDPOINT,
        prefix="data/futures/um/monthly",
        retrieved_at=retrieved,
        content_bytes=b"".join(raw_pages),
        pages=pages,
        listing_complete=True,
    )
    # The raw XML pages are represented by their hashes in the catalog pages;
    # keeping the concatenated XML text would make the in-memory dataset needlessly large.
    return replace(result, content=None)


@dataclass(frozen=True)
class AcquisitionResult:
    catalog: DiscoveryCatalog
    archive_records: tuple[DiscoveredArchiveRecord, ...]
    errors: tuple[str, ...]


def _download_one(ref: ArchiveRef, *, retries: int = 3) -> DiscoveredArchiveRecord:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            session = getattr(_DOWNLOAD_THREAD_STATE, "session", None)
            if session is None:
                session = requests.Session()
                _DOWNLOAD_THREAD_STATE.session = session
            cached_target = M1_RAW_CACHE / ref.kind / ref.symbol / f"{ref.month}.zip"
            if cached_target.is_file():
                checksum_response = session.get(checksum_url(ref), timeout=60)
                if checksum_response.status_code < 400:
                    official = parse_official_checksum(checksum_response.text)
                    local_sha = file_sha256(cached_target)
                    if local_sha == official:
                        return DiscoveredArchiveRecord(
                            ref.symbol,
                            ref.kind,
                            ref.month,
                            ref.url,
                            local_path=str(cached_target),
                            local_sha256=local_sha,
                            official_checksum=official,
                            checksum_verified=True,
                            download_status="downloaded",
                        )
            return download_archive_record(ref, raw_dir=M1_RAW_CACHE, session=session, verify_checksum=True)
        except (requests.exceptions.RequestException, HistoryDataError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time_module.sleep(1.5 * (attempt + 1))
    reason = f"DOWNLOAD_ERROR:{type(last_error).__name__ if last_error else 'unknown'}"
    return DiscoveredArchiveRecord(
        ref.symbol,
        ref.kind,
        ref.month,
        ref.url,
        download_status="error",
        checksum_verified=False,
        exclusion_reason=reason,
    )


def download_m1_b_archives(catalog: DiscoveryCatalog, *, workers: int = 16) -> AcquisitionResult:
    """Download each discovered archive and verify its official CHECKSUM."""
    refs = tuple(ArchiveRef(record.symbol, record.kind, record.month, record.url) for record in catalog.archives)
    records: list[DiscoveredArchiveRecord] = []
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(refs)))) as pool:
        future_map = {pool.submit(_download_one, ref): ref for ref in refs}
        completed = 0
        for future in as_completed(future_map):
            records.append(future.result())
            completed += 1
            if completed % 500 == 0 or completed == len(refs):
                print(f"M1-B archive downloads: {completed}/{len(refs)}", flush=True)
    records.sort(key=lambda record: (record.symbol, record.kind, record.month))
    errors = tuple(
        f"{record.symbol} {record.kind} {record.month} {record.exclusion_reason or record.download_status}"
        for record in records
        if record.download_status != "downloaded" or record.checksum_verified is not True
    )
    return AcquisitionResult(catalog=catalog, archive_records=tuple(records), errors=errors)


@dataclass(frozen=True)
class DatasetBundle:
    """The frozen normalized dataset and all quality/provenance artifacts."""

    catalog: DiscoveryCatalog
    archive_records: tuple[DiscoveredArchiveRecord, ...]
    histories: tuple[SymbolHistory, ...]
    usable_histories: tuple[SymbolHistory, ...]
    symbol_records: tuple[DiscoveredSymbolRecord, ...]
    manifest: Mapping[str, Any]
    quality: DataQualityReport
    usable_quality: DataQualityReport
    dataset_sha256: str
    normalized_dataset_sha256: str
    archive_errors: tuple[str, ...]


def _archive_records_by_symbol(
    records: Iterable[DiscoveredArchiveRecord],
) -> dict[str, dict[str, list[DiscoveredArchiveRecord]]]:
    grouped: dict[str, dict[str, list[DiscoveredArchiveRecord]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        grouped[record.symbol.upper()][record.kind].append(record)
    for kinds in grouped.values():
        for values in kinds.values():
            values.sort(key=lambda record: record.month)
    return grouped


def _replace_archive_statuses(
    records: Iterable[DiscoveredArchiveRecord],
    *,
    normalized_symbols: set[str],
    quality_failed_symbols: set[str],
) -> tuple[DiscoveredArchiveRecord, ...]:
    output: list[DiscoveredArchiveRecord] = []
    for record in records:
        symbol = record.symbol.upper()
        if record.download_status != "downloaded":
            output.append(record)
        elif symbol not in normalized_symbols:
            output.append(replace(record, normalize_status="failed"))
        elif symbol in quality_failed_symbols:
            output.append(replace(record, normalize_status="normalized", quality_status="failed"))
        else:
            output.append(replace(record, normalize_status="normalized", quality_status="passed"))
    return tuple(sorted(output, key=lambda record: (record.symbol, record.kind, record.month)))


def _normalization_symbol_record(
    base: DiscoveredSymbolRecord,
    *,
    history: SymbolHistory | None,
    reason: str | None,
    usable: bool,
) -> DiscoveredSymbolRecord:
    if history is None:
        return replace(
            base,
            normalization_status="failed",
            lifecycle_status="not_available",
            dataset_status="excluded",
            exclusion_reason=reason or "NOT_NORMALIZED",
        )
    lifecycle_status = history.lifecycle.status
    if usable:
        return replace(
            base,
            normalization_status="normalized",
            lifecycle_status=lifecycle_status,
            dataset_status="usable",
            exclusion_reason=None,
        )
    return replace(
        base,
        normalization_status="normalized",
        lifecycle_status=lifecycle_status,
        dataset_status="excluded",
        exclusion_reason=reason or lifecycle_status or "NOT_USABLE",
    )


def normalize_m1_b_dataset(acquisition: AcquisitionResult) -> DatasetBundle:
    """Normalize every downloaded symbol before applying structural usability."""
    records = tuple(acquisition.archive_records)
    grouped = _archive_records_by_symbol(records)
    base_symbols = {record.symbol.upper(): record for record in acquisition.catalog.symbols}
    all_histories: list[SymbolHistory] = []
    normalized_symbols: set[str] = set()
    initial_records: dict[str, DiscoveredSymbolRecord] = {}
    normalization_reasons: dict[str, str] = {}

    for symbol in sorted(base_symbols):
        kinds = grouped.get(symbol, {})
        daily_records = [record for record in kinds.get("daily_klines", ()) if record.download_status == "downloaded" and record.local_path]
        funding_records = [record for record in kinds.get("funding", ()) if record.download_status == "downloaded" and record.local_path]
        symbol_download_errors = [
            record
            for record in tuple(kinds.get("daily_klines", ())) + tuple(kinds.get("funding", ()))
            if record.download_status != "downloaded"
        ]
        if not daily_records:
            normalization_reasons[symbol] = "NO_DAILY_ARCHIVE"
            initial_records[symbol] = _normalization_symbol_record(
                base_symbols[symbol], history=None, reason=normalization_reasons[symbol], usable=False
            )
            continue
        try:
            daily_paths = [record.local_path for record in daily_records if record.local_path]
            funding_paths = [record.local_path for record in funding_records if record.local_path]
            provisional = normalize_symbol_archives(
                symbol,
                daily_paths=daily_paths,
                funding_paths=funding_paths,
                currently_active=True,
            )
            last_day = max(bar.day for bar in provisional.daily_bars)
            currently_active = last_day >= M1_DATA_END
            history = normalize_symbol_archives(
                symbol,
                daily_paths=daily_paths,
                funding_paths=funding_paths,
                currently_active=currently_active,
                confirmed_absence_after_last_bar=not currently_active,
            )
            normalized_symbols.add(symbol)
            all_histories.append(history)
            reason = None
            if symbol_download_errors:
                reason = "ARCHIVE_DOWNLOAD_INCOMPLETE"
            elif not history.funding_events:
                reason = "NO_FUNDING_EVENTS"
            elif history.lifecycle.status != "OK":
                reason = history.lifecycle.status
            if reason:
                normalization_reasons[symbol] = reason
            initial_records[symbol] = _normalization_symbol_record(
                base_symbols[symbol], history=history, reason=reason, usable=False
            )
        except (HistoryDataError, OSError, ValueError) as exc:
            normalization_reasons[symbol] = f"NORMALIZE_ERROR:{type(exc).__name__}"
            initial_records[symbol] = _normalization_symbol_record(
                base_symbols[symbol], history=None, reason=normalization_reasons[symbol], usable=False
            )

    all_histories_tuple = tuple(sorted(all_histories, key=lambda history: history.symbol))
    quality = validate_dataset(all_histories_tuple, M1_DATA_START, M1_DATA_END)
    issues_by_symbol: dict[str, list[str]] = defaultdict(list)
    for issue in quality.issues:
        issues_by_symbol[issue.symbol.upper()].append(issue.code)
    blocking_codes = {
        "WRONG_QUOTE_ASSET",
        "WRONG_CONTRACT_TYPE",
        "OUT_OF_WINDOW",
        "DUPLICATE_DAILY_BAR",
        "NON_MONOTONIC_TIMESTAMP",
        "INVALID_PRICE",
        "INVALID_OHLC",
        "INVALID_QUOTE_VOLUME",
        "FUTURE_TIMESTAMP_LEAKAGE",
        "MISSING_INTERNAL_DAILY_BAR",
        "UNEXPLAINED_SYMBOL_GAP",
        "LIFECYCLE_INCONSISTENCY",
        "INVALID_FUNDING_RATE",
        "INVALID_FUNDING_INTERVAL",
        "DUPLICATE_FUNDING_EVENT",
        "FUNDING_COVERAGE_GAP",
    }
    quality_failed_symbols = {
        symbol for symbol, codes in issues_by_symbol.items() if blocking_codes.intersection(codes)
    }
    usable_histories = tuple(
        history
        for history in all_histories_tuple
        if history.symbol.upper() not in quality_failed_symbols
        and history.symbol.upper() not in normalization_reasons
        and history.funding_events
        and history.lifecycle.status == "OK"
    )
    usable_quality = validate_dataset(usable_histories, M1_DATA_START, M1_DATA_END)

    final_symbol_records: list[DiscoveredSymbolRecord] = []
    for symbol in sorted(base_symbols):
        history = next((item for item in all_histories_tuple if item.symbol == symbol), None)
        reason = normalization_reasons.get(symbol)
        if symbol in quality_failed_symbols:
            reason = "DATA_QUALITY_" + "+".join(sorted(set(issues_by_symbol[symbol])))
        usable = history is not None and history in usable_histories
        final_symbol_records.append(
            _normalization_symbol_record(
                base_symbols[symbol], history=history, reason=reason, usable=usable
            )
        )
    final_symbol_records_tuple = tuple(final_symbol_records)
    final_archive_records = _replace_archive_statuses(
        records,
        normalized_symbols=normalized_symbols,
        quality_failed_symbols=quality_failed_symbols,
    )
    manifest = build_dataset_manifest(
        all_histories_tuple,
        protocol_hash=M1_APPROVED_PROTOCOL_SHA256,
        data_start=M1_DATA_START,
        data_end=M1_DATA_END,
        catalog=acquisition.catalog,
        archive_records=final_archive_records,
        symbol_records=final_symbol_records_tuple,
    )
    normalized_sha = str(manifest["dataset_sha256"])
    provenance_for_hash = [
        {
            key: value
            for key, value in asdict(record).items()
            if key not in {"local_path", "source_url"}
        }
        for record in final_archive_records
    ]
    dataset_sha = _sha256({
        "normalized_dataset_sha256": normalized_sha,
        "catalog_content_sha256": acquisition.catalog.content_sha256,
        "catalog_content_fingerprint": acquisition.catalog.content_fingerprint,
        "archive_provenance": provenance_for_hash,
        "symbol_provenance": [asdict(record) for record in final_symbol_records_tuple],
    })
    manifest = dict(manifest)
    manifest["normalized_dataset_sha256"] = normalized_sha
    manifest["dataset_sha256"] = dataset_sha
    manifest["archive_download_errors"] = list(acquisition.errors)
    return DatasetBundle(
        catalog=acquisition.catalog,
        archive_records=final_archive_records,
        histories=all_histories_tuple,
        usable_histories=usable_histories,
        symbol_records=final_symbol_records_tuple,
        manifest=manifest,
        quality=quality,
        usable_quality=usable_quality,
        dataset_sha256=dataset_sha,
        normalized_dataset_sha256=normalized_sha,
        archive_errors=acquisition.errors,
    )


def _slim_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Keep committed dataset provenance auditable without local absolute paths."""
    output = dict(manifest)
    discovery = dict(output.get("discovery_catalog") or {})
    discovery["pages"] = [
        {
            key: page.get(key)
            for key in (
                "page_number",
                "request_url",
                "marker",
                "next_marker",
                "is_truncated",
                "first_key",
                "last_key",
                "key_count",
                "page_content_sha256",
            )
        }
        for page in discovery.get("pages", [])
    ]
    output["discovery_catalog"] = discovery
    slim_archives: list[dict[str, Any]] = []
    for record in output.get("raw_files", []):
        slim_archives.append({
            key: record.get(key)
            for key in (
                "symbol",
                "kind",
                "month",
                "url",
                "source_url",
                "official_checksum",
                "checksum_verified",
                "download_status",
                "normalize_status",
                "quality_status",
                "exclusion_reason",
            )
        })
    output["raw_files"] = slim_archives
    return output


def freeze_dataset(bundle: DatasetBundle) -> None:
    """Write a local ignored pickle and a committed, path-sanitized manifest."""
    M1_NORMALIZED_CACHE.mkdir(parents=True, exist_ok=True)
    with M1_DATASET_FREEZE_PATH.open("wb") as handle:
        pickle.dump(
            {
                "dataset_sha256": bundle.dataset_sha256,
                "normalized_dataset_sha256": bundle.normalized_dataset_sha256,
                "catalog": bundle.catalog,
                "archive_records": bundle.archive_records,
                "histories": bundle.histories,
                "usable_histories": bundle.usable_histories,
                "symbol_records": bundle.symbol_records,
                "manifest": bundle.manifest,
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    M1_DATASET_MANIFEST_PATH.write_text(
        json.dumps(_slim_manifest(bundle.manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class SignalCache:
    """All frozen signal-day snapshots used by the full run and LOO reruns."""

    signal_days: tuple[date, ...]
    universes: Mapping[date, Any]
    signals: Mapping[date, PITSignal]
    first_valid_signal_date: date | None
    no_signal_days: tuple[date, ...]


def build_signal_cache(histories: Iterable[SymbolHistory]) -> SignalCache:
    """Build signal snapshots on the pre-registered 7-calendar-day schedule."""
    histories = tuple(sorted(histories, key=lambda history: history.symbol))
    rules = resolve_rules(variant="control")
    bar_maps = {history.symbol: history.bars_by_day() for history in histories}
    first_day = M1_DATA_START + timedelta(days=rules.lookback_days)
    last_signal_day = M1_DATA_END - timedelta(days=rules.execution_lag_days)
    signal_days = tuple(
        first_day + timedelta(days=rules.rebalance_days * index)
        for index in range(((last_signal_day - first_day).days // rules.rebalance_days) + 1)
    )
    universes: dict[date, Any] = {}
    signals: dict[date, PITSignal] = {}
    no_signal_days: list[date] = []
    for signal_day in signal_days:
        universe = _build_universe_fast(histories, bar_maps, signal_day, rules)
        universes[signal_day] = universe
        signal = _signal_from_universe(universe, rules)
        if signal is None:
            no_signal_days.append(signal_day)
        else:
            signals[signal_day] = signal
    return SignalCache(
        signal_days=signal_days,
        universes=universes,
        signals=signals,
        first_valid_signal_date=min(signals, default=None),
        no_signal_days=tuple(no_signal_days),
    )


def _build_universe_fast(
    histories: Sequence[SymbolHistory],
    bar_maps: Mapping[str, Mapping[date, DailyBar]],
    signal_day: date,
    rules: Any,
) -> PITUniverseResult:
    """Equivalent PIT universe construction using precomputed daily maps."""
    active: list[str] = []
    liquid: list[str] = []
    eligible: list[PITSnapshot] = []
    exclusions: dict[str, str] = {}
    reasons: list[str] = []
    fail_closed = False
    start = signal_day - timedelta(days=rules.lookback_days)
    required_days = tuple(start + timedelta(days=index) for index in range(rules.lookback_days + 1))
    observation_ms = _utc_day_end_ms(signal_day)
    for history in histories:
        symbol = history.symbol.upper()
        if not is_historical_usdt_perpetual_symbol(symbol) or not history.active_on(signal_day):
            exclusions[symbol] = "NOT_ACTIVE_USDT_PERPETUAL_AT_SIGNAL_DAY"
            continue
        active.append(symbol)
        bars = bar_maps[symbol]
        signal_bar = bars.get(signal_day)
        if signal_bar is None:
            exclusions[symbol] = "NO_COMPLETED_SIGNAL_DAY_BAR"
            continue
        if not math.isfinite(signal_bar.quote_volume) or signal_bar.quote_volume < 0:
            exclusions[symbol] = "INVALID_QUOTE_VOLUME"
            fail_closed = True
            reasons.append(f"DATA_INVALID: {symbol} quote_volume")
            continue
        if signal_bar.quote_volume < rules.min_quote_volume_usdt:
            exclusions[symbol] = "BELOW_SIGNAL_DAY_VOLUME_THRESHOLD"
            continue
        liquid.append(symbol)
        missing = [day for day in required_days if day not in bars]
        if missing:
            first_bar = min((bar.day for bar in history.daily_bars), default=signal_day)
            if first_bar > start:
                exclusions[symbol] = "INSUFFICIENT_LOOKBACK"
            else:
                exclusions[symbol] = "MISSING_INTERNAL_DAILY_BAR"
                fail_closed = True
                reasons.append(f"DATA_INVALID: {symbol} MISSING_INTERNAL_DAILY_BAR")
            continue
        window = tuple(bars[day] for day in required_days)
        if any(max(bar.open_time_ms, bar.close_time_ms) > observation_ms for bar in window):
            exclusions[symbol] = "FUTURE_TIMESTAMP_LEAKAGE"
            fail_closed = True
            reasons.append(f"DATA_INVALID: {symbol} FUTURE_TIMESTAMP_LEAKAGE")
            continue
        closes = tuple(bar.close for bar in window)
        if any(not math.isfinite(close) or close <= 0 for close in closes):
            exclusions[symbol] = "INVALID_PRICE"
            fail_closed = True
            reasons.append(f"DATA_INVALID: {symbol} INVALID_PRICE")
            continue
        returns = tuple(closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes)))
        vol = statistics.pstdev(returns) * math.sqrt(365.0) * 100.0
        if vol <= 0:
            exclusions[symbol] = "ZERO_REALIZED_VOL"
            continue
        eligible.append(PITSnapshot(symbol, signal_day, vol, signal_bar.quote_volume, start, signal_day))
    eligible.sort(key=lambda snapshot: (snapshot.realized_vol_pct, snapshot.symbol))
    if fail_closed:
        reasons.insert(0, "NO_SIGNAL: eligible history/data quality failed closed")
    elif len(eligible) < rules.min_symbols:
        reasons.append(f"NO_SIGNAL: eligible universe {len(eligible)} < {rules.min_symbols}")
    return PITUniverseResult(
        signal_day=signal_day,
        active_symbols=tuple(active),
        liquid_symbols=tuple(liquid),
        eligible=tuple(eligible),
        exclusions=exclusions,
        fail_closed=fail_closed,
        reasons=tuple(reasons),
    )


def _signal_from_universe(universe: PITUniverseResult, rules: Any) -> PITSignal | None:
    required = max(rules.min_symbols, rules.k_long + rules.k_short)
    if universe.fail_closed or len(universe.eligible) < required:
        return None
    ranked = tuple(universe.eligible)
    return PITSignal(
        signal_day=universe.signal_day,
        execution_day=universe.signal_day + timedelta(days=rules.execution_lag_days),
        longs=tuple(snapshot.symbol for snapshot in ranked[:rules.k_long]),
        shorts=tuple(snapshot.symbol for snapshot in ranked[-rules.k_short:][::-1]),
        eligible_symbols=tuple(snapshot.symbol for snapshot in ranked),
        snapshots=ranked,
    )


def _signal_without_symbols(universe: Any, removed: frozenset[str], rules: Any) -> PITSignal | None:
    if getattr(universe, "fail_closed", False):
        return None
    eligible = tuple(snapshot for snapshot in universe.eligible if snapshot.symbol not in removed)
    if len(eligible) < max(rules.min_symbols, rules.k_long + rules.k_short):
        return None
    # _build_universe_fast stores this sequence in the frozen order, so LOO
    # only removes the candidate and then reranks the remaining sequence.
    ranked = eligible
    longs = tuple(snapshot.symbol for snapshot in ranked[:rules.k_long])
    shorts = tuple(snapshot.symbol for snapshot in ranked[-rules.k_short:][::-1])
    return PITSignal(
        signal_day=universe.signal_day,
        execution_day=universe.signal_day + timedelta(days=rules.execution_lag_days),
        longs=longs,
        shorts=shorts,
        eligible_symbols=tuple(snapshot.symbol for snapshot in ranked),
        snapshots=ranked,
    )


def signals_for_removed(cache: SignalCache, removed: frozenset[str]) -> dict[date, PITSignal]:
    """Rerank every signal date after removing a symbol; never subtract PnL."""
    if not removed:
        return dict(cache.signals)
    rules = resolve_rules(variant="control")
    output: dict[date, PITSignal] = {}
    for day in cache.signal_days:
        signal = _signal_without_symbols(cache.universes[day], removed, rules)
        if signal is not None:
            output[day] = signal
    return output


@dataclass(frozen=True)
class _Position:
    symbol: str
    direction: int
    notional: float
    entry_timestamp_ms: int
    entry_day: date


@dataclass(frozen=True)
class BacktestResult:
    """Normalized daily portfolio ledger for one frozen cost/variant run."""

    variant: str
    scenario: str
    strategy_id: str
    spec_hash: str
    capital: float
    dates: tuple[date, ...]
    equity: tuple[float, ...]
    daily_pnl: tuple[float, ...]
    daily_returns: tuple[float, ...]
    daily_price_pnl: tuple[float, ...]
    daily_funding_pnl: tuple[float, ...]
    daily_cost_pnl: tuple[float, ...]
    daily_long_pnl: tuple[float, ...]
    daily_short_pnl: tuple[float, ...]
    daily_pnl_by_symbol: tuple[Mapping[str, float], ...]
    weekly_rows: tuple[Mapping[str, Any], ...]
    pnl_by_symbol: Mapping[str, float]
    price_pnl: float
    funding_pnl: float
    cost_pnl: float
    long_pnl: float
    short_pnl: float
    turnover_notional: float
    trade_count: int
    rebalance_count: int
    targets_by_signal_day: Mapping[str, Mapping[str, int]]
    scales_by_signal_day: Mapping[str, float]
    holding_intervals: tuple[HoldingInterval, ...]
    selected_symbols: tuple[str, ...]
    complete: bool
    issues: tuple[str, ...]

    def equity_by_day(self) -> dict[date, float]:
        return dict(zip(self.dates, self.equity))


def _unique_events(history: SymbolHistory) -> tuple[FundingEvent, ...]:
    by_timestamp: dict[int, FundingEvent] = {}
    for event in history.funding_events:
        by_timestamp.setdefault(event.funding_time_ms, event)
    return tuple(sorted(by_timestamp.values(), key=lambda event: event.funding_time_ms))


def _add_pnl(
    *,
    direction: int,
    symbol: str,
    amount: float,
    pnl_by_symbol: dict[str, float],
    totals: dict[str, float],
    field: str,
) -> None:
    pnl_by_symbol[symbol] = pnl_by_symbol.get(symbol, 0.0) + amount
    totals[field] += amount
    totals["long_pnl" if direction > 0 else "short_pnl"] += amount


def _apply_cost(
    *,
    symbol: str,
    direction: int,
    notional: float,
    rate: float,
    pnl_by_symbol: dict[str, float],
    totals: dict[str, float],
) -> float:
    cost = float(notional) * float(rate)
    _add_pnl(
        direction=direction,
        symbol=symbol,
        amount=-cost,
        pnl_by_symbol=pnl_by_symbol,
        totals=totals,
        field="cost_pnl",
    )
    return cost


def _next_day(day: date) -> date:
    return day + timedelta(days=1)


def simulate_frozen_portfolio(
    histories: Iterable[SymbolHistory],
    signals: Mapping[date, PITSignal],
    *,
    variant: str,
    scenario: str,
    capital: float = 1.0,
) -> BacktestResult:
    """Run daily close-to-close price/funding accounting under frozen rules."""
    histories = tuple(sorted(histories, key=lambda history: history.symbol))
    rules = resolve_rules(variant="shadow" if variant == "shadow" else "control")
    if scenario not in {"COST_1X", "COST_2X", "COST_3X"}:
        raise M1BError(f"unknown frozen cost scenario {scenario}")
    cost_rates = {
        "COST_1X": (0.05 + 0.03) / 100.0,
        "COST_2X": (0.10 + 0.06) / 100.0,
        "COST_3X": (0.15 + 0.09) / 100.0,
    }
    cost_rate = cost_rates[scenario]
    by_symbol = {history.symbol: history for history in histories}
    bar_maps = {history.symbol: history.bars_by_day() for history in histories}
    funding_maps = {history.symbol: _unique_events(history) for history in histories}
    funding_times = {symbol: tuple(event.funding_time_ms for event in events) for symbol, events in funding_maps.items()}
    mark_prices: dict[str, float] = {}
    mark_timestamps: dict[str, int] = {}
    calendar = _date_range(M1_DATA_START, M1_DATA_END)
    slot = capital / (rules.k_long + rules.k_short)
    positions: dict[str, _Position] = {}
    pnl_by_symbol: dict[str, float] = {}
    totals = {
        "price_pnl": 0.0,
        "funding_pnl": 0.0,
        "cost_pnl": 0.0,
        "long_pnl": 0.0,
        "short_pnl": 0.0,
    }
    equity: list[float] = []
    daily_pnl: list[float] = []
    daily_returns: list[float] = []
    daily_price_pnl: list[float] = []
    daily_funding_pnl: list[float] = []
    daily_cost_pnl: list[float] = []
    daily_long_pnl: list[float] = []
    daily_short_pnl: list[float] = []
    daily_pnl_by_symbol: list[Mapping[str, float]] = []
    holding_intervals: list[HoldingInterval] = []
    targets_by_signal_day: dict[str, Mapping[str, int]] = {}
    scales_by_signal_day: dict[str, float] = {}
    selected_symbols: set[str] = set()
    issues: list[str] = []
    complete = True
    realized = 0.0
    turnover = 0.0
    trade_count = 0
    rebalance_count = 0

    def record_holding_interval(position: _Position, exit_timestamp_ms: int) -> None:
        if exit_timestamp_ms > position.entry_timestamp_ms:
            holding_intervals.append(
                HoldingInterval(position.symbol, position.entry_timestamp_ms, exit_timestamp_ms)
            )

    for day in calendar:
        day_pnl = 0.0
        day_totals_before = dict(totals)
        day_symbol_before = dict(pnl_by_symbol)
        # Mark at each completed daily close before any same-close rebalance.
        for symbol, position in tuple(positions.items()):
            bar = bar_maps[symbol].get(day)
            if bar is None:
                history = by_symbol[symbol]
                delisted_at = history.lifecycle.delisted_at
                if delisted_at is not None and day >= delisted_at and history.lifecycle.status == "OK":
                    terminal = history.last_bar_on_or_before(delisted_at - timedelta(days=1))
                    if terminal is None or terminal.close <= 0 or not math.isfinite(terminal.close):
                        complete = False
                        issues.append(f"DATA_INVALID: no terminal close {symbol}")
                    else:
                        record_holding_interval(position, terminal.close_time_ms)
                        day_pnl -= _apply_cost(
                            symbol=symbol,
                            direction=position.direction,
                            notional=position.notional,
                            rate=cost_rate,
                            pnl_by_symbol=pnl_by_symbol,
                            totals=totals,
                        )
                        turnover += position.notional
                        positions.pop(symbol, None)
                        mark_prices.pop(symbol, None)
                        mark_timestamps.pop(symbol, None)
                    continue
                complete = False
                issues.append(f"DATA_INVALID: missing completed mark {symbol} {day.isoformat()}")
                continue
            if day == position.entry_day:
                continue
            previous_day = day - timedelta(days=1)
            previous_bar = bar_maps[symbol].get(previous_day)
            if previous_bar is None:
                complete = False
                issues.append(f"DATA_INVALID: non-consecutive mark {symbol} {previous_day} -> {day}")
                continue
            # The previous bar must be the last mark. A daily data gap never
            # gets repaired by carrying its last price forward.
            # Mark prices are kept separately because _Position is immutable.
            previous_price = mark_prices.get(symbol, previous_bar.close)
            if not math.isfinite(previous_price) or previous_price <= 0:
                complete = False
                issues.append(f"DATA_INVALID: invalid mark price {symbol}")
                continue
            move = position.direction * position.notional * (bar.close / previous_price - 1.0)
            _add_pnl(
                direction=position.direction,
                symbol=symbol,
                amount=move,
                pnl_by_symbol=pnl_by_symbol,
                totals=totals,
                field="price_pnl",
            )
            day_pnl += move
            event_funding = 0.0
            events = funding_maps[symbol]
            times = funding_times[symbol]
            left = bisect_right(times, mark_timestamps[symbol])
            right = bisect_right(times, bar.close_time_ms)
            for event in events[left:right]:
                funding = -position.direction * position.notional * event.funding_rate
                _add_pnl(
                    direction=position.direction,
                    symbol=symbol,
                    amount=funding,
                    pnl_by_symbol=pnl_by_symbol,
                    totals=totals,
                    field="funding_pnl",
                )
                event_funding += funding
            day_pnl += event_funding
            mark_prices[symbol] = bar.close
            mark_timestamps[symbol] = bar.close_time_ms

        execution_signal: PITSignal | None = None
        for signal_day, candidate in signals.items():
            if candidate.execution_day == day:
                execution_signal = candidate
                break
        if execution_signal is not None:
            targets = execution_signal.targets
            selected_symbols.update(targets)
            if rules.volatility_targeting:
                scale = position_scale(
                    (snapshot.realized_vol_pct for snapshot in execution_signal.snapshots),
                    rules.target_vol_pct,
                    rules.max_scale,
                )
            else:
                scale = 1.0
            target_notional = slot * scale
            affected = {
                symbol
                for symbol in set(positions) | set(targets)
                if symbol not in targets
                or symbol not in positions
                or positions[symbol].direction != targets[symbol]
                or not math.isclose(positions[symbol].notional, target_notional, rel_tol=0.0, abs_tol=1e-15)
            }
            missing_execution = sorted(
                symbol for symbol in affected if symbol in targets and day not in bar_maps.get(symbol, {})
            )
            if missing_execution:
                complete = False
                issues.append("DATA_INVALID: missing execution prices " + ",".join(missing_execution))
            else:
                # Closing/replacing existing legs uses the actual completed
                # execution close, then opening the new target at that close.
                for symbol, old in tuple(positions.items()):
                    target_direction = targets.get(symbol)
                    same_direction = target_direction == old.direction
                    same_size = same_direction and math.isclose(old.notional, target_notional, rel_tol=0.0, abs_tol=1e-15)
                    if same_size:
                        continue
                    execution_bar = bar_maps[symbol].get(day)
                    if execution_bar is None:
                        complete = False
                        issues.append(f"DATA_INVALID: missing resize close {symbol} {day.isoformat()}")
                        continue
                    record_holding_interval(old, execution_bar.close_time_ms)
                    if same_direction:
                        delta = abs(target_notional - old.notional)
                        turnover += delta
                        day_pnl -= _apply_cost(
                            symbol=symbol,
                            direction=old.direction,
                            notional=delta,
                            rate=cost_rate,
                            pnl_by_symbol=pnl_by_symbol,
                            totals=totals,
                        )
                        positions[symbol] = _Position(
                            symbol=symbol,
                            direction=old.direction,
                            notional=target_notional,
                            entry_timestamp_ms=execution_bar.close_time_ms,
                            entry_day=day,
                        )
                        mark_prices[symbol] = execution_bar.close
                        mark_timestamps[symbol] = execution_bar.close_time_ms
                    else:
                        turnover += old.notional
                        day_pnl -= _apply_cost(
                            symbol=symbol,
                            direction=old.direction,
                            notional=old.notional,
                            rate=cost_rate,
                            pnl_by_symbol=pnl_by_symbol,
                            totals=totals,
                        )
                        positions.pop(symbol, None)
                        mark_prices.pop(symbol, None)
                        mark_timestamps.pop(symbol, None)
                for symbol, direction in targets.items():
                    if symbol in positions:
                        continue
                    execution_bar = bar_maps[symbol].get(day)
                    if execution_bar is None:
                        complete = False
                        issues.append(f"DATA_INVALID: missing entry close {symbol} {day.isoformat()}")
                        continue
                    positions[symbol] = _Position(
                        symbol=symbol,
                        direction=direction,
                        notional=target_notional,
                        entry_timestamp_ms=execution_bar.close_time_ms,
                        entry_day=day,
                    )
                    mark_prices[symbol] = execution_bar.close
                    mark_timestamps[symbol] = execution_bar.close_time_ms
                    turnover += target_notional
                    day_pnl -= _apply_cost(
                        symbol=symbol,
                        direction=direction,
                        notional=target_notional,
                        rate=cost_rate,
                        pnl_by_symbol=pnl_by_symbol,
                        totals=totals,
                    )
                    trade_count += 1
                targets_by_signal_day[execution_signal.signal_day.isoformat()] = dict(targets)
                scales_by_signal_day[execution_signal.signal_day.isoformat()] = scale
                rebalance_count += 1

        realized += day_pnl
        prior_equity = equity[-1] if equity else capital
        current_equity = capital + realized
        equity.append(current_equity)
        daily_pnl.append(day_pnl)
        daily_returns.append(current_equity / prior_equity - 1.0 if prior_equity > 0 else -1.0)
        daily_price_pnl.append(totals["price_pnl"] - day_totals_before["price_pnl"])
        daily_funding_pnl.append(totals["funding_pnl"] - day_totals_before["funding_pnl"])
        daily_cost_pnl.append(totals["cost_pnl"] - day_totals_before["cost_pnl"])
        daily_long_pnl.append(totals["long_pnl"] - day_totals_before["long_pnl"])
        daily_short_pnl.append(totals["short_pnl"] - day_totals_before["short_pnl"])
        daily_pnl_by_symbol.append({
            symbol: value - day_symbol_before.get(symbol, 0.0)
            for symbol, value in pnl_by_symbol.items()
            if not math.isclose(value, day_symbol_before.get(symbol, 0.0), rel_tol=0.0, abs_tol=1e-18)
        })

    # Positions are marked to the final completed close. No artificial end-of-
    # sample liquidation fee is charged, but every held interval is audited.
    for position in positions.values():
        terminal = bar_maps[position.symbol].get(M1_DATA_END)
        if terminal is None:
            terminal = by_symbol[position.symbol].last_bar_on_or_before(M1_DATA_END)
        if terminal is not None:
            record_holding_interval(position, terminal.close_time_ms)

    weekly_rows = _weekly_rows(calendar, equity, capital)
    return BacktestResult(
        variant="shadow" if variant == "shadow" else "control",
        scenario=scenario,
        strategy_id=rules.strategy_id,
        spec_hash=rules.spec_hash,
        capital=capital,
        dates=calendar,
        equity=tuple(equity),
        daily_pnl=tuple(daily_pnl),
        daily_returns=tuple(daily_returns),
        daily_price_pnl=tuple(daily_price_pnl),
        daily_funding_pnl=tuple(daily_funding_pnl),
        daily_cost_pnl=tuple(daily_cost_pnl),
        daily_long_pnl=tuple(daily_long_pnl),
        daily_short_pnl=tuple(daily_short_pnl),
        daily_pnl_by_symbol=tuple(daily_pnl_by_symbol),
        weekly_rows=tuple(weekly_rows),
        pnl_by_symbol=dict(sorted(pnl_by_symbol.items())),
        price_pnl=totals["price_pnl"],
        funding_pnl=totals["funding_pnl"],
        cost_pnl=totals["cost_pnl"],
        long_pnl=totals["long_pnl"],
        short_pnl=totals["short_pnl"],
        turnover_notional=turnover,
        trade_count=trade_count,
        rebalance_count=rebalance_count,
        targets_by_signal_day=targets_by_signal_day,
        scales_by_signal_day=scales_by_signal_day,
        holding_intervals=tuple(holding_intervals),
        selected_symbols=tuple(sorted(selected_symbols)),
        complete=complete,
        issues=tuple(dict.fromkeys(issues)),
    )


def _weekly_rows(calendar: Sequence[date], equity: Sequence[float], capital: float) -> list[dict[str, Any]]:
    by_day = dict(zip(calendar, equity))
    groups: dict[date, list[date]] = defaultdict(list)
    for day in calendar:
        groups[day - timedelta(days=day.weekday())].append(day)
    rows: list[dict[str, Any]] = []
    for week_start in sorted(groups):
        days = groups[week_start]
        week_end = days[-1]
        if len(days) != 7 or days[0] != week_start:
            continue
        before = by_day.get(week_start - timedelta(days=1), capital)
        after = by_day[week_end]
        rows.append({
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "return": after / before - 1.0 if before > 0 else -1.0,
            "pnl": after - before,
        })
    return rows


def _window_daily_values(result: BacktestResult, start: date, end: date) -> tuple[list[date], list[float], list[float]]:
    by_day = result.equity_by_day()
    if start not in by_day or end not in by_day:
        return [], [], []
    days = [day for day in result.dates if start <= day <= end]
    before = result.capital if start == result.dates[0] else by_day.get(start - timedelta(days=1), result.capital)
    values = [before, *(by_day[day] for day in days)]
    returns = [values[index] / values[index - 1] - 1.0 if values[index - 1] > 0 else -1.0 for index in range(1, len(values))]
    return days, values[1:], returns


def _window_summary(result: BacktestResult, start: date, end: date) -> dict[str, Any]:
    days, values, returns = _window_daily_values(result, start, end)
    if not days:
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "total_return_pct": 0.0,
            "cagr_pct": 0.0,
            "weekly_sharpe": 0.0,
            "profit_factor_weekly": 0.0,
            "max_drawdown_pct": 0.0,
            "pnl": 0.0,
            "weekly_observations": 0,
        }
    weekly = [
        float(row["return"])
        for row in result.weekly_rows
        if start <= date.fromisoformat(str(row["week_start"])) and date.fromisoformat(str(row["week_end"])) <= end
    ]
    before = result.capital if start == result.dates[0] else result.equity_by_day().get(start - timedelta(days=1), result.capital)
    final = values[-1]
    ratio = final / before if before > 0 else 0.0
    cagr = ((ratio ** (365.0 / max(1, (end - start).days))) - 1.0) * 100.0 if ratio > 0 else 0.0
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_return_pct": (ratio - 1.0) * 100.0,
        "cagr_pct": cagr,
        "weekly_sharpe": weekly_sharpe(weekly),
        "profit_factor_weekly": weekly_profit_factor(weekly),
        "max_drawdown_pct": max_drawdown_pct(returns),
        "pnl": final - before,
        "weekly_observations": len(weekly),
        "daily_observations": len(returns),
    }


def _window_attribution(result: BacktestResult, start: date, end: date) -> dict[str, Any]:
    index_by_day = {day: index for index, day in enumerate(result.dates)}
    indices = [index_by_day[day] for day in result.dates if start <= day <= end]
    def total(values: Sequence[float]) -> float:
        return sum(values[index] for index in indices)
    by_symbol: dict[str, float] = defaultdict(float)
    for index in indices:
        for symbol, value in result.daily_pnl_by_symbol[index].items():
            by_symbol[symbol] += float(value)
    return {
        "price_pnl": total(result.daily_price_pnl),
        "funding_pnl": total(result.daily_funding_pnl),
        "cost_pnl": total(result.daily_cost_pnl),
        "long_pnl": total(result.daily_long_pnl),
        "short_pnl": total(result.daily_short_pnl),
        "pnl_by_symbol": dict(sorted(by_symbol.items())),
        "net_pnl": total(result.daily_pnl),
    }


def _yearly_summary(result: BacktestResult, *, first_year: int = 2020, last_year: int = 2024) -> dict[str, Any]:
    by_day = result.equity_by_day()
    yearly: dict[str, Any] = {}
    for year in range(first_year, last_year + 1):
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        if start not in by_day or end not in by_day:
            continue
        before = result.capital if year == first_year else by_day.get(date(year - 1, 12, 31), result.capital)
        final = by_day[end]
        days, _, returns = _window_daily_values(result, start, end)
        yearly[str(year)] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "total_return_pct": (final / before - 1.0) * 100.0 if before > 0 else 0.0,
            "max_drawdown_pct": max_drawdown_pct(returns),
            "full_calendar_year": len(days) == 365 + (1 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 0),
        }
    positive = [int(year) for year, values in yearly.items() if values["full_calendar_year"] and values["total_return_pct"] > 0]
    full_returns = [values["total_return_pct"] for values in yearly.values() if values["full_calendar_year"]]
    return {
        "years": yearly,
        "positive_full_calendar_years": len(positive),
        "positive_years": positive,
        "worst_full_calendar_year_return_pct": min(full_returns) if full_returns else 0.0,
    }


def _regime_label(history: SymbolHistory | None, day: date) -> str:
    if history is None:
        return "SIDEWAYS"
    bars = history.bars_by_day()
    current = bars.get(day)
    prior = bars.get(day - timedelta(days=120))
    if current is None or prior is None or prior.close <= 0 or not math.isfinite(prior.close):
        return "SIDEWAYS"
    return "BULL" if current.close / prior.close - 1.0 > 0.20 else "BEAR" if current.close / prior.close - 1.0 < -0.20 else "SIDEWAYS"


def _regime_attribution(result: BacktestResult, histories: Iterable[SymbolHistory], start: date, end: date) -> dict[str, Any]:
    btc = next((history for history in histories if history.symbol == "BTCUSDT"), None)
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"days": 0, "pnl": 0.0, "return_sum": 0.0})
    for index, day in enumerate(result.dates):
        if not start <= day <= end:
            continue
        regime = _regime_label(btc, day)
        grouped[regime]["days"] += 1
        grouped[regime]["pnl"] += result.daily_pnl[index]
        grouped[regime]["return_sum"] += result.daily_returns[index]
    return {
        regime: {
            "days": values["days"],
            "pnl": values["pnl"],
            "return_sum_pct": values["return_sum"] * 100.0,
        }
        for regime, values in sorted(grouped.items())
    }


def _window_weekly_returns(result: BacktestResult, start: date, end: date) -> list[float]:
    return [
        float(row["return"])
        for row in result.weekly_rows
        if start <= date.fromisoformat(str(row["week_start"])) and date.fromisoformat(str(row["week_end"])) <= end
    ]


def _safe_bootstrap(values: Sequence[float], *, protocol: Mapping[str, Any]) -> dict[str, Any]:
    if not values:
        return {
            "block_length_weeks": 4,
            "rounds": 10000,
            "confidence": 0.95,
            "seed": 20260915,
            "mean_weekly_return_ci95_lower": None,
            "probability_mean_return_gt_zero": None,
            "error": "NO_WEEKLY_RETURNS",
        }
    return block_bootstrap(values, protocol=protocol)


def _loo_report(
    cache: SignalCache,
    histories: Sequence[SymbolHistory],
) -> dict[str, Any]:
    symbols = tuple(sorted(history.symbol for history in histories))
    if not symbols:
        return {
            "positive_runs": 0,
            "total_runs": 0,
            "worst_removed_symbol": None,
            "runs": [],
        }

    def rerun(remaining: frozenset[str]) -> Mapping[str, Any]:
        subset = tuple(history for history in histories if history.symbol in remaining)
        signals = signals_for_removed(cache, frozenset(symbol for symbol in symbols if symbol not in remaining))
        result = simulate_frozen_portfolio(subset, signals, variant="control", scenario="COST_1X")
        summary = _window_summary(result, M1_EXTERNAL_START, M1_EXTERNAL_END)
        return {"total_return_pct": summary["total_return_pct"]}

    print(f"M1-B leave-one-symbol-out reruns: {len(symbols)}", flush=True)
    result = leave_one_out(symbols, rerun)
    return {
        "positive_runs": result["loo_positive"],
        "negative_runs": result["loo_negative"],
        "total_runs": result["loo_total"],
        "min_return_pct": result["loo_min_return"],
        "median_return_pct": result["loo_median_return"],
        "worst_removed_symbol": result["loo_worst_removed_symbol"],
        "runs": list(result["runs"]),
    }


def _quality_summary(report: DataQualityReport) -> dict[str, Any]:
    counts = Counter(issue.code for issue in report.issues)
    by_symbol = Counter(issue.symbol for issue in report.issues)
    return {
        "passed": report.passed,
        "checked_symbols": report.checked_symbols,
        "duplicate_symbols": list(report.duplicate_symbols),
        "issue_count": len(report.issues),
        "issue_codes": list(report.issue_codes),
        "issue_counts": dict(sorted(counts.items())),
        "affected_symbols": dict(sorted(by_symbol.items())),
        "sample_issues": [
            {
                "code": issue.code,
                "symbol": issue.symbol,
                "detail": issue.detail,
                "day": issue.day.isoformat() if issue.day else None,
            }
            for issue in report.issues[:50]
        ],
    }


def _coverage_summary(report: FundingCoverageReport) -> dict[str, Any]:
    structural = Counter(issue.code for issue in report.structural_issues)
    holds = Counter(issue.code for issue in report.hold_issues)
    return {
        "passed": report.passed,
        "structural_passed": report.structural_passed,
        "holds_passed": report.holds_passed,
        "structural_issue_count": len(report.structural_issues),
        "hold_issue_count": len(report.hold_issues),
        "structural_issue_counts": dict(sorted(structural.items())),
        "hold_issue_counts": dict(sorted(holds.items())),
        "sample_issues": [
            {
                "code": issue.code,
                "symbol": issue.symbol,
                "detail": issue.detail,
                "day": issue.day.isoformat() if issue.day else None,
            }
            for issue in report.issues[:50]
        ],
    }


def _external_symbol_pnl(result: BacktestResult) -> dict[str, float]:
    return _window_attribution(result, M1_EXTERNAL_START, M1_EXTERNAL_END)["pnl_by_symbol"]


def _analysis_report(
    bundle: DatasetBundle,
    cache: SignalCache,
    control_runs: Mapping[str, BacktestResult],
    shadow_run: BacktestResult,
    coverage: FundingCoverageReport,
    loo: Mapping[str, Any],
    protocol: Mapping[str, Any],
    identities: Mapping[str, str],
) -> dict[str, Any]:
    control_1x = control_runs["COST_1X"]
    external = _window_summary(control_1x, M1_EXTERNAL_START, M1_EXTERNAL_END)
    cost_2x = _window_summary(control_runs["COST_2X"], M1_EXTERNAL_START, M1_EXTERNAL_END)
    cost_3x = _window_summary(control_runs["COST_3X"], M1_EXTERNAL_START, M1_EXTERNAL_END)
    shadow_external = _window_summary(shadow_run, M1_EXTERNAL_START, M1_EXTERNAL_END)
    external_attribution = _window_attribution(control_1x, M1_EXTERNAL_START, M1_EXTERNAL_END)
    bull = _window_summary(control_1x, date(2020, 1, 1), date(2021, 12, 31))
    bull_attribution = _window_attribution(control_1x, date(2020, 1, 1), date(2021, 12, 31))
    external_weekly = _window_weekly_returns(control_1x, M1_EXTERNAL_START, M1_EXTERNAL_END)
    bootstrap = _safe_bootstrap(external_weekly, protocol=protocol)
    sensitivity = {
        str(block): _safe_bootstrap_with_block(external_weekly, block, protocol)
        for block in (8, 13)
    }
    best5 = (
        remove_best_5pct(external_weekly, protocol=protocol)
        if external_weekly
        else {"removed_count": 0, "removed_indices": (), "compound_return_pct": None, "error": "NO_WEEKLY_RETURNS"}
    )
    yearly = _yearly_summary(control_1x)
    concentration = symbol_concentration(external_attribution["pnl_by_symbol"])
    regime = _regime_attribution(control_1x, bundle.usable_histories, M1_EXTERNAL_START, M1_EXTERNAL_END)
    quality_ok = bundle.usable_quality.passed
    no_future_issue = not any(issue.code == "FUTURE_TIMESTAMP_LEAKAGE" for issue in bundle.quality.issues)
    has_delisted = any(
        history.lifecycle.delisted_at is not None
        for history in bundle.usable_histories
    )
    data_integrity = {
        "point_in_time_universe": True,
        "delisted_included": has_delisted,
        "no_known_lookahead": no_future_issue,
        "no_unexplained_eligible_gap": bool(
            quality_ok and coverage.passed and control_1x.complete and not bundle.archive_errors
        ),
        "strategy_hash_unchanged": identities["control_spec_hash"] == CONTROL_SPEC_HASH and identities["shadow_spec_hash"] == SHADOW_SPEC_HASH,
        "protocol_hash_unchanged": identities["protocol_hash"] == M1_APPROVED_PROTOCOL_SHA256,
        "official_catalog_complete": bundle.catalog.listing_complete is True,
        "archive_checksums_all_verified": all(record.checksum_verified is True for record in bundle.archive_records),
    }
    metrics: dict[str, Any] = {
        "formal_run": True,
        "data_integrity": data_integrity,
        "external": external,
        "cost_2x": cost_2x,
        "cost_3x": cost_3x,
        "bootstrap": {
            key: value
            for key, value in bootstrap.items()
            if key in {"block_length_weeks", "rounds", "confidence", "seed", "mean_weekly_return_ci95_lower", "probability_mean_return_gt_zero", "error"}
        },
        "best_5pct": {
            key: value
            for key, value in best5.items()
            if key in {"removed_count", "compound_return_pct", "error"}
        },
        "leave_one_out": {
            "positive_runs": loo.get("positive_runs"),
            "total_runs": loo.get("total_runs"),
        },
        "concentration": {
            "top2_positive_pnl_share_pct": concentration.get("top2_positive_pnl_share_pct"),
        },
        "yearly": {
            "positive_full_calendar_years": yearly["positive_full_calendar_years"],
            "worst_full_calendar_year_return_pct": yearly["worst_full_calendar_year_return_pct"],
        },
        "bull_2020_2021": {
            "total_return_pct": bull["total_return_pct"],
            "max_drawdown_pct": bull["max_drawdown_pct"],
        },
    }
    gate_evaluation = evaluate_frozen_gates(metrics, protocol=protocol)
    external_window, discovery_window = protocol_windows(protocol)
    all_weekly_split = split_windowed_rows(
        control_1x.weekly_rows,
        external_window,
        discovery_window,
        date_key="week_end",
    )
    return {
        "decision": "M1 PASS" if gate_evaluation.decision == "PASS" else "M1 FAIL",
        "formal_run": True,
        "protocol_id": identities["protocol_id"],
        "protocol_sha256": identities["protocol_hash"],
        "control_strategy_id": identities["control_strategy_id"],
        "control_sha256": identities["control_spec_hash"],
        "shadow_strategy_id": identities["shadow_strategy_id"],
        "shadow_sha256": identities["shadow_spec_hash"],
        "dataset_sha256": bundle.dataset_sha256,
        "normalized_dataset_sha256": bundle.normalized_dataset_sha256,
        "data": {
            "number_of_symbols_discovered": bundle.manifest["number_of_symbols_discovered"],
            "number_of_usable_symbols": bundle.manifest["number_of_usable_symbols"],
            "number_of_delisted_symbols": bundle.manifest["number_of_delisted_symbols"],
            "number_of_ambiguous_symbols": bundle.manifest["number_of_ambiguous_symbols"],
            "listing_page_count": bundle.manifest["discovery_catalog"]["listing_page_count"],
            "raw_file_count": bundle.manifest["raw_file_count"],
            "daily_bar_count": bundle.manifest["daily_bar_count"],
            "funding_event_count": bundle.manifest["funding_event_count"],
            "catalog_complete": bundle.catalog.listing_complete,
            "archive_download_errors": list(bundle.archive_errors),
            "first_available_date": bundle.manifest["first_available_date"],
            "last_available_date": bundle.manifest["last_available_date"],
        },
        "quality": {
            "all_normalized": _quality_summary(bundle.quality),
            "usable_dataset": _quality_summary(bundle.usable_quality),
            "funding_coverage": _coverage_summary(coverage),
        },
        "first_valid_signal_date": cache.first_valid_signal_date.isoformat() if cache.first_valid_signal_date else None,
        "scheduled_signal_count": len(cache.signal_days),
        "no_signal_day_count": len(cache.no_signal_days),
        "rebalance_count": control_1x.rebalance_count,
        "control": {
            scenario: {
                "strategy_id": run.strategy_id,
                "spec_hash": run.spec_hash,
                "external": _window_summary(run, M1_EXTERNAL_START, M1_EXTERNAL_END),
                "discovery": _window_summary(run, M1_DISCOVERY_START, M1_DISCOVERY_END),
                "complete": run.complete,
                "issues": list(run.issues[:50]),
                "trade_count": run.trade_count,
                "rebalance_count": run.rebalance_count,
                "turnover_notional": run.turnover_notional,
                "price_pnl": run.price_pnl,
                "funding_pnl": run.funding_pnl,
                "cost_pnl": run.cost_pnl,
                "long_pnl": run.long_pnl,
                "short_pnl": run.short_pnl,
            }
            for scenario, run in control_runs.items()
        },
        "shadow": {
            "strategy_id": shadow_run.strategy_id,
            "spec_hash": shadow_run.spec_hash,
            "external": shadow_external,
            "discovery": _window_summary(shadow_run, M1_DISCOVERY_START, M1_DISCOVERY_END),
            "complete": shadow_run.complete,
            "issues": list(shadow_run.issues[:50]),
            "trade_count": shadow_run.trade_count,
            "rebalance_count": shadow_run.rebalance_count,
            "turnover_notional": shadow_run.turnover_notional,
            "price_pnl": shadow_run.price_pnl,
            "funding_pnl": shadow_run.funding_pnl,
            "cost_pnl": shadow_run.cost_pnl,
        },
        "attribution": {
            "external": external_attribution,
            "bull_2020_2021": bull_attribution,
            "regime_external": regime,
        },
        "bull_summary": bull,
        "yearly_breakdown": yearly,
        "bootstrap": {
            "primary_4_week": bootstrap,
            "sensitivity": sensitivity,
        },
        "best_5pct": best5,
        "leave_one_out": dict(loo),
        "symbol_concentration": concentration,
        "gates": dict(gate_evaluation.gates),
        "failed_gates": list(gate_evaluation.failed_gates),
        "missing_gate_metrics": list(gate_evaluation.missing_metrics),
        "window_split": {key: len(value) for key, value in all_weekly_split.items()},
        "runtime_evidence_status": "EVIDENCE_STALE",
        "live_trading": False,
        "http_methods": ["GET"],
        "primary_decision_strategy": CONTROL_STRATEGY_ID,
        "shadow_is_secondary": True,
    }


def _safe_bootstrap_with_block(values: Sequence[float], block: int, protocol: Mapping[str, Any]) -> dict[str, Any]:
    if not values:
        return {"block_length_weeks": block, "rounds": 10000, "confidence": 0.95, "seed": 20260915, "error": "NO_WEEKLY_RETURNS"}
    return block_bootstrap(values, block_length=block, protocol=protocol)


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "∞" if value > 0 else "-∞"
        return f"{value:.{digits}f}"
    return str(value)


def render_formal_report(result: Mapping[str, Any]) -> str:
    """Render the auditable M1 report with failed gates before good news."""
    data = result["data"]
    control = result["control"]
    c1 = control["COST_1X"]["external"]
    c2 = control["COST_2X"]["external"]
    c3 = control["COST_3X"]["external"]
    bull = result["attribution"]["bull_2020_2021"]
    bootstrap = result["bootstrap"]["primary_4_week"]
    loo = result["leave_one_out"]
    lines = [
        f"# {result['decision']}",
        "",
        "## Executive Decision",
        "",
        f"Primary decision strategy: `{result['primary_decision_strategy']}`.",
        "Shadow is secondary and cannot rescue a Control failure.",
        "",
        "## Failed Gates",
        "",
    ]
    lines.extend(f"- `{gate}`: FAIL" for gate in result["failed_gates"])
    if not result["failed_gates"]:
        lines.append("- None")
    lines.extend([
        "",
        "## Frozen Hashes",
        "",
        f"- Protocol SHA-256: `{result['protocol_sha256']}`",
        f"- Dataset SHA-256: `{result['dataset_sha256']}`",
        f"- Control SHA-256: `{result['control_sha256']}`",
        f"- Shadow SHA-256: `{result['shadow_sha256']}`",
        "",
        "## G0–G12",
        "",
    ])
    lines.extend(f"- `{gate}`: {'PASS' if passed else 'FAIL'}" for gate, passed in result["gates"].items())
    lines.extend([
        "",
        "## Dataset and Data Quality",
        "",
        f"- Symbols discovered / usable / delisted / ambiguous: {data['number_of_symbols_discovered']} / {data['number_of_usable_symbols']} / {data['number_of_delisted_symbols']} / {data['number_of_ambiguous_symbols']}",
        f"- Listing pages: {data['listing_page_count']}; raw archives: {data['raw_file_count']}",
        f"- Daily bars: {data['daily_bar_count']}; funding events: {data['funding_event_count']}",
        f"- First/last available date: {data['first_available_date']} / {data['last_available_date']}",
        f"- First valid signal date: {result['first_valid_signal_date']}; rebalances: {result['rebalance_count']}",
        f"- Funding held-interval coverage: {_fmt(result['quality']['funding_coverage']['passed'])}",
        "",
        "## External Validation (2020-01-01 to 2025-08-31)",
        "",
        "| Run | Return | CAGR | Weekly Sharpe | Profit Factor | Max Drawdown |",
        "|---|---:|---:|---:|---:|---:|",
        f"| Control COST_1X | {_fmt(c1['total_return_pct'])}% | {_fmt(c1['cagr_pct'])}% | {_fmt(c1['weekly_sharpe'])} | {_fmt(c1['profit_factor_weekly'])} | {_fmt(c1['max_drawdown_pct'])}% |",
        f"| Control COST_2X | {_fmt(c2['total_return_pct'])}% | {_fmt(c2['cagr_pct'])}% | {_fmt(c2['weekly_sharpe'])} | {_fmt(c2['profit_factor_weekly'])} | {_fmt(c2['max_drawdown_pct'])}% |",
        f"| Control COST_3X | {_fmt(c3['total_return_pct'])}% | {_fmt(c3['cagr_pct'])}% | {_fmt(c3['weekly_sharpe'])} | {_fmt(c3['profit_factor_weekly'])} | {_fmt(c3['max_drawdown_pct'])}% |",
        "",
        "## 2020–2021 Bull Death Test",
        "",
        f"- Return: {_fmt(result['bull_summary']['total_return_pct'])}%.",
        f"- Max Drawdown: {_fmt(result['bull_summary']['max_drawdown_pct'])}%",
        f"- Long PnL: {_fmt(bull['long_pnl'])}; Short PnL: {_fmt(bull['short_pnl'])}",
        "",
        "## Funding and Cost Attribution",
        "",
        f"- External price PnL: {_fmt(result['attribution']['external']['price_pnl'])}",
        f"- External funding PnL: {_fmt(result['attribution']['external']['funding_pnl'])}",
        f"- External cost PnL: {_fmt(result['attribution']['external']['cost_pnl'])}",
        f"- External long / short PnL: {_fmt(result['attribution']['external']['long_pnl'])} / {_fmt(result['attribution']['external']['short_pnl'])}",
        "",
        "## Bootstrap and Robustness",
        "",
        f"- 4-week block bootstrap: {bootstrap.get('rounds', 'N/A')} rounds, seed {bootstrap.get('seed', 'N/A')}",
        f"- CI95 lower: {_fmt(bootstrap.get('mean_weekly_return_ci95_lower'))}; P(mean > 0): {_fmt(bootstrap.get('probability_mean_return_gt_zero'))}",
        f"- Best 5% weeks removed compound return: {_fmt(result['best_5pct'].get('compound_return_pct'))}%",
        f"- LOO positive / total: {loo.get('positive_runs')} / {loo.get('total_runs')}; worst removed symbol: {loo.get('worst_removed_symbol')}",
        f"- Top-2 positive PnL concentration: {_fmt(result['symbol_concentration'].get('top2_positive_pnl_share_pct'))}%",
        "",
        "## Calendar Years and Regimes",
        "",
        f"- Positive full calendar years: {result['yearly_breakdown']['positive_years']}",
        f"- 2020–2024 worst full-year return: {_fmt(result['yearly_breakdown']['worst_full_calendar_year_return_pct'])}%",
        f"- Regime attribution: `{json.dumps(result['attribution']['regime_external'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Discovery Reference (2025-09-01 to 2026-08-31)",
        "",
        f"Control COST_1X discovery return: {_fmt(control['COST_1X']['discovery']['total_return_pct'])}%. This window is descriptive only and is excluded from all primary gates.",
        "",
        "## Safety Controls",
        "",
        "- Runtime Evidence remains `EVIDENCE_STALE`; no evidence publication was performed.",
        "- `LIVE_TRADING = False`; HTTP access in this run is GET-only.",
        "- No M1-B result is used to optimize V1 and no M2 work is started.",
        "",
        "## Machine-readable result",
        "",
        "```json",
        json.dumps(_json_safe(result), ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
    ])
    return "\n".join(lines)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return "Infinity" if value > 0 else "-Infinity" if value < 0 else "NaN"
    return value


def _verify_m1_identity(
    protocol_path: str | Path,
    hash_path: str | Path,
) -> tuple[Mapping[str, Any], dict[str, str]]:
    protocol = load_protocol(protocol_path)
    identities = verified_protocol_identity(protocol_path, hash_path)
    if identities["protocol_hash"] != M1_APPROVED_PROTOCOL_SHA256:
        raise M1InvalidError("M1 INVALID: protocol SHA-256 does not match the approved pin")
    if strategy_spec_hash() != CONTROL_SPEC_HASH or strategy_spec_hash(variant="shadow") != SHADOW_SPEC_HASH:
        raise M1InvalidError("M1 INVALID: frozen Strategy Spec SHA-256 changed")
    frozen_gate_policy(protocol, protocol_path=protocol_path, hash_path=hash_path)
    print("M1-B frozen identity verified", flush=True)
    print(f"Protocol SHA-256: {identities['protocol_hash']}", flush=True)
    print(f"Control SHA-256: {identities['control_spec_hash']}", flush=True)
    print(f"Shadow SHA-256: {identities['shadow_spec_hash']}", flush=True)
    return protocol, identities


def _complete_formal_m1_b(
    bundle: DatasetBundle,
    protocol: Mapping[str, Any],
    identities: Mapping[str, str],
    *,
    protocol_path: str | Path,
    hash_path: str | Path,
) -> dict[str, Any]:
    freeze_dataset(bundle)
    print(
        f"M1-B dataset frozen: {bundle.dataset_sha256} ({len(bundle.usable_histories)} usable symbols)",
        flush=True,
    )
    cache = build_signal_cache(bundle.usable_histories)
    print(
        f"M1-B signal cache built: {len(cache.signals)} valid signals / {len(cache.signal_days)} scheduled signal days",
        flush=True,
    )
    control_runs = {
        scenario: simulate_frozen_portfolio(
            bundle.usable_histories,
            cache.signals,
            variant="control",
            scenario=scenario,
        )
        for scenario in ("COST_1X", "COST_2X", "COST_3X")
    }
    shadow_run = simulate_frozen_portfolio(
        bundle.usable_histories,
        cache.signals,
        variant="shadow",
        scenario="COST_1X",
    )
    coverage = validate_funding_coverage_for_holds(
        bundle.usable_histories,
        control_runs["COST_1X"].holding_intervals,
    )
    loo = _loo_report(cache, bundle.usable_histories)
    result = _analysis_report(
        bundle,
        cache,
        control_runs,
        shadow_run,
        coverage,
        loo,
        protocol,
        identities,
    )

    final_protocol_hash = verify_protocol_hash(protocol_path, hash_path)
    final_control_hash = strategy_spec_hash()
    final_shadow_hash = strategy_spec_hash(variant="shadow")
    if (
        final_protocol_hash != identities["protocol_hash"]
        or final_control_hash != identities["control_spec_hash"]
        or final_shadow_hash != identities["shadow_spec_hash"]
    ):
        raise M1InvalidError("M1 INVALID: frozen identity changed during formal run")
    result["protocol_sha256"] = final_protocol_hash
    result["control_sha256"] = final_control_hash
    result["shadow_sha256"] = final_shadow_hash
    result["dataset_manifest_path"] = str(M1_DATASET_MANIFEST_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/")
    result["dataset_freeze_path"] = str(M1_DATASET_FREEZE_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/")
    result["ci_status"] = "PENDING_PUSH"
    M1_DECISION_PATH.write_text(
        json.dumps(_json_safe(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    M1_REPORT_PATH.write_text(render_formal_report(result), encoding="utf-8")
    return result


def run_formal_m1_b(
    *,
    approval: str,
    workers: int = 16,
    protocol_path: str | Path = M1_PROTOCOL_PATH,
    hash_path: str | Path = M1_PROTOCOL_HASH_PATH,
) -> dict[str, Any]:
    """Execute the complete frozen M1-B lifecycle and write its artifacts."""
    if approval != "START M1-B":
        raise M1BError("formal M1-B requires explicit approval 'START M1-B'")

    # This is intentionally the first operation that can lead to data writes.
    protocol, identities = _verify_m1_identity(protocol_path, hash_path)

    catalog = acquire_m1_b_archive_catalog(workers=workers)
    print(
        f"M1-B official catalog complete: {catalog.listing_page_count} pages, {len(catalog.archives)} required archives",
        flush=True,
    )
    acquisition = download_m1_b_archives(catalog, workers=workers)
    bundle = normalize_m1_b_dataset(acquisition)
    return _complete_formal_m1_b(
        bundle,
        protocol,
        identities,
        protocol_path=protocol_path,
        hash_path=hash_path,
    )


def run_formal_m1_b_from_verified_cache(
    *,
    approval: str,
    protocol_path: str | Path = M1_PROTOCOL_PATH,
    hash_path: str | Path = M1_PROTOCOL_HASH_PATH,
) -> dict[str, Any]:
    """Re-run only normalization/analysis from a previously verified cache.

    This recovery entry point is useful after an executioner-only correction:
    it still verifies all frozen identities and requires the explicit M1-B
    approval, but does not fetch any new historical data.
    """
    if approval != "START M1-B":
        raise M1BError("formal M1-B requires explicit approval 'START M1-B'")
    protocol, identities = _verify_m1_identity(protocol_path, hash_path)
    if not M1_DATASET_FREEZE_PATH.is_file():
        raise M1BError("verified M1-B dataset cache is missing")
    with M1_DATASET_FREEZE_PATH.open("rb") as handle:
        previous = pickle.load(handle)
    acquisition = AcquisitionResult(
        catalog=previous["catalog"],
        archive_records=tuple(previous["archive_records"]),
        errors=tuple(previous.get("manifest", {}).get("archive_download_errors", ())),
    )
    bundle = normalize_m1_b_dataset(acquisition)
    return _complete_formal_m1_b(
        bundle,
        protocol,
        identities,
        protocol_path=protocol_path,
        hash_path=hash_path,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the frozen XS-LOWVOL M1-B validation")
    parser.add_argument("--approval", required=True, help="must be exactly START M1-B")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--reuse-verified-cache", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        runner = run_formal_m1_b_from_verified_cache if args.reuse_verified_cache else run_formal_m1_b
        result = runner(approval=args.approval, workers=args.workers) if not args.reuse_verified_cache else runner(approval=args.approval)
    except M1InvalidError as exc:
        print("M1 INVALID")
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("M1 FAIL")
        print(f"M1-B execution error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(result["decision"])
    print(json.dumps(_json_safe({
        "protocol_sha256": result["protocol_sha256"],
        "dataset_sha256": result["dataset_sha256"],
        "control_sha256": result["control_sha256"],
        "shadow_sha256": result["shadow_sha256"],
        "failed_gates": result["failed_gates"],
    }), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
