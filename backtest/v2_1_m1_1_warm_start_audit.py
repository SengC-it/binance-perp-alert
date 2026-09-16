"""XS-LOWVOL V2.1-M1.1 warm-start provenance audit.

This audit is intentionally narrower than a Forward run.  It reuses the
accepted V1 Control cache and producer, fetches only the completed pre-start
extension through the official Binance Futures read-only API, and serializes
weekly provenance for the latest thirteen completed Control weeks.  It never
selects a Forward signal, writes a Forward ledger, or emits aggregate strategy
performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import re
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

import requests

from src.xs_lowvol_v2_1_anchor import (
    APPROVED_V1_CONTROL_SHA256,
    APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_1_PROTOCOL_SHA256,
    APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC,
    APPROVED_V2_1_SPEC_SHA256,
    V21_FORWARD_ANCHOR_STATUS,
    validate_v2_1_forward_anchor,
    verify_v2_1_forward_anchor_hash,
)
from src.xs_lowvol_v2_1_risk import (
    ControlWeeklyReturn,
    V2_DATA_INTEGRITY_HALT,
    V21DataIntegrityHalt,
    evaluate_risk_scale,
)
from src.xs_lowvol_v2_1_spec import V21_STRATEGY_ID, verify_v2_1_spec_hash
from src.xs_lowvol_spec import strategy_spec_hash as v1_strategy_spec_hash

from .m1_b import (
    M1_DATA_END,
    M1_DATA_START,
    build_stateful_schedule,
    simulate_frozen_portfolio,
)
from .m1_protocol import M1_APPROVED_PROTOCOL_SHA256, verify_protocol_hash
from .v2_1_m1_engineering import (
    EXPECTED_DATASET_SHA256,
    EXPECTED_NORMALIZED_DATASET_SHA256,
    load_verified_dataset,
)
from .v2_1_protocol import verify_v2_1_protocol_hash
from .xs_data_quality import (
    FundingCoverageReport,
    HoldingInterval,
    validate_funding_coverage_for_holds,
)
from .xs_history import DailyBar, FundingEvent, SymbolHistory, file_sha256


PROJECT_ROOT = Path(__file__).resolve().parent.parent
V21_M1_1_BASE_COMMIT = "76412b9cfa3ac102ff2b8db8ee5242971bfa1480"
V2_1_M1_1_BASE_COMMIT = V21_M1_1_BASE_COMMIT
V21_M1_1_AUDIT_ID = "XS-LOWVOL-V2.1-M1.1-WARM-START-1"
V2_1_M1_1_AUDIT_ID = V21_M1_1_AUDIT_ID
V21_M1_1_APPROVAL = "START V2.1-M1.1"
V2_1_M1_1_APPROVAL = V21_M1_1_APPROVAL
V21_M1_1_CUTOFF_UTC = datetime(2026, 9, 16, tzinfo=timezone.utc)
V2_1_M1_1_CUTOFF_UTC = V21_M1_1_CUTOFF_UTC
V21_M1_1_EXTENSION_START = date(2026, 9, 1)
V21_M1_1_EXTENSION_END = V21_M1_1_CUTOFF_UTC.date() - timedelta(days=1)
V2_1_M1_1_EXTENSION_START = V21_M1_1_EXTENSION_START
V2_1_M1_1_EXTENSION_END = V21_M1_1_EXTENSION_END
V21_M1_1_OUTPUT_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_1"
V21_M1_1_REPORT_PATH = V21_M1_1_OUTPUT_DIR / "V2_1_M1_1_WARM_START_AUDIT.json"
V21_M1_1_MARKDOWN_PATH = V21_M1_1_OUTPUT_DIR / "V2_1_M1_1_WARM_START_AUDIT.md"
V21_M1_1_PRESTART_MANIFEST_PATH = V21_M1_1_OUTPUT_DIR / "PRESTART_EXTENSION_MANIFEST.json"
V2_1_M1_1_REPORT_PATH = V21_M1_1_REPORT_PATH
V2_1_M1_1_MARKDOWN_PATH = V21_M1_1_MARKDOWN_PATH
V2_1_M1_1_PRESTART_MANIFEST_PATH = V21_M1_1_PRESTART_MANIFEST_PATH
V21_M1_1_RAW_DIR = PROJECT_ROOT / "data" / "v2_1_m1_1_prestart_extension"
V21_M1_1_CUTOFF_MS = int(V21_M1_1_CUTOFF_UTC.timestamp() * 1000)
V21_M1_1_FROZEN_DATA_END = M1_DATA_END
BINANCE_FAPI_BASE = "https://fapi.binance.com"
EXCHANGE_INFO_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/exchangeInfo"
FUNDING_INFO_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/fundingInfo"
KLINES_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/klines"
FUNDING_RATE_ENDPOINT = f"{BINANCE_FAPI_BASE}/fapi/v1/fundingRate"
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PRICE_ISSUE_WORDS = (
    "missing completed mark",
    "non-consecutive mark",
    "invalid mark price",
    "missing execution price",
    "missing execution prices",
    "missing resize close",
    "missing entry close",
    "no terminal close",
)
_FORBIDDEN_FORMAL_KEYS = frozenset(
    {
        "return",
        "cagr",
        "sharpe",
        "sortino",
        "dd",
        "max_drawdown",
        "profit_factor",
        "annual_return",
        "monthly_return",
        "bootstrap",
        "best_week",
        "profitability",
        "validated_alpha",
        "historical_performance",
    }
)
UTC = timezone.utc


class WarmStartAuditError(RuntimeError):
    """A fail-closed M1.1 audit error."""


class WarmStartIdentityError(WarmStartAuditError):
    """A frozen identity, cache boundary, or legacy artifact changed."""


class WarmStartProducerError(WarmStartAuditError):
    """The weekly producer emitted ambiguous or incomplete provenance."""


@dataclass(frozen=True)
class RequestRecord:
    """One official GET response, including raw-content provenance."""

    name: str
    method: str
    endpoint: str
    request_url: str
    status_code: int | None
    fetched_at: str
    content_sha256: str | None
    byte_count: int
    local_path: str | None
    parsed_count: int = 0
    error: str | None = None


@dataclass(frozen=True)
class ExtensionSymbolResult:
    symbol: str
    daily_bars: tuple[DailyBar, ...]
    funding_events: tuple[FundingEvent, ...]
    daily_request: RequestRecord
    funding_request: RequestRecord
    interval_provenance: Mapping[str, Any]
    exchange_info: Mapping[str, Any] | None


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
    if isinstance(value, set):
        return sorted(_canonical(item) for item in value)
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _file_hash(path: str | Path) -> str:
    return file_sha256(path)


def _schema_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")


def _formal_field_violations(value: Any, path: str = "") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            text = str(key)
            item_path = f"{path}.{text}" if path else text
            if _schema_key(text) in _FORBIDDEN_FORMAL_KEYS:
                violations.append(item_path)
            violations.extend(_formal_field_violations(item, item_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            violations.extend(_formal_field_violations(item, f"{path}[{index}]"))
    return violations


def assert_no_formal_performance_fields(value: Mapping[str, Any]) -> None:
    violations = _formal_field_violations(value)
    if violations:
        raise WarmStartAuditError(
            "forbidden formal performance fields in M1.1 artifact: "
            + ", ".join(violations[:12])
        )


def _current_code_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WarmStartAuditError("cannot determine current code commit") from exc
    commit = result.stdout.strip()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise WarmStartAuditError("current code commit is not a full SHA-1")
    return commit


def _is_ancestor(base: str, commit: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, commit],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise WarmStartAuditError("cannot verify M1.1 base commit") from exc
    if result.returncode not in (0, 1):
        raise WarmStartAuditError("git could not verify M1.1 base commit")
    return result.returncode == 0


def verify_frozen_identity() -> dict[str, str]:
    try:
        actual = {
            "v1_control_sha256": v1_strategy_spec_hash(),
            "v2_1_spec_sha256": verify_v2_1_spec_hash(),
            "v2_1_protocol_sha256": verify_v2_1_protocol_hash(),
            "forward_anchor_sha256": verify_v2_1_forward_anchor_hash(),
            "dataset_protocol_sha256": verify_protocol_hash(),
        }
        validate_v2_1_forward_anchor()
    except Exception as exc:  # noqa: BLE001 - frozen identity must fail closed
        raise WarmStartIdentityError(f"W0_identity failed: {exc}") from exc
    expected = {
        "v1_control_sha256": APPROVED_V1_CONTROL_SHA256,
        "v2_1_spec_sha256": APPROVED_V2_1_SPEC_SHA256,
        "v2_1_protocol_sha256": APPROVED_V2_1_PROTOCOL_SHA256,
        "forward_anchor_sha256": APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
        "dataset_protocol_sha256": M1_APPROVED_PROTOCOL_SHA256,
    }
    if actual != expected:
        raise WarmStartIdentityError(f"W0_identity failed: expected={expected}, actual={actual}")
    return actual


def load_frozen_dataset() -> dict[str, Any]:
    dataset = load_verified_dataset()
    manifest = dataset["manifest"]
    if manifest.get("last_available_date") != V21_M1_1_FROZEN_DATA_END.isoformat():
        raise WarmStartIdentityError(
            "frozen dataset boundary changed: expected 2026-08-31"
        )
    if dataset["dataset_sha256"] != EXPECTED_DATASET_SHA256:
        raise WarmStartIdentityError("frozen dataset SHA changed")
    if dataset["normalized_dataset_sha256"] != EXPECTED_NORMALIZED_DATASET_SHA256:
        raise WarmStartIdentityError("frozen normalized dataset SHA changed")
    return dataset


def _request_url(endpoint: str, params: Mapping[str, Any] | None = None) -> str:
    if not params:
        return endpoint
    return f"{endpoint}?{urlencode(sorted((str(k), str(v)) for k, v in params.items()))}"


def _safe_raw_path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise WarmStartAuditError(f"unsafe pre-start raw filename: {name}")
    return V21_M1_1_RAW_DIR / name


def _fetch_response(
    *,
    name: str,
    endpoint: str,
    params: Mapping[str, Any] | None,
    raw_name: str,
    parse_json: bool = True,
) -> tuple[Any | None, RequestRecord]:
    request_url = _request_url(endpoint, params)
    fetched_at = datetime.now(UTC).isoformat()
    target = _safe_raw_path(raw_name)
    try:
        response = requests.get(request_url, timeout=60)
    except requests.RequestException as exc:
        return None, RequestRecord(
            name=name,
            method="GET",
            endpoint=endpoint,
            request_url=request_url,
            status_code=None,
            fetched_at=fetched_at,
            content_sha256=None,
            byte_count=0,
            local_path=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    content = bytes(response.content or b"")
    content_hash = hashlib.sha256(content).hexdigest() if content else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    error: str | None = None
    payload: Any | None = None
    if response.status_code >= 400:
        error = f"HTTP {response.status_code}"
    elif parse_json:
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = f"invalid JSON: {type(exc).__name__}"
    record = RequestRecord(
        name=name,
        method="GET",
        endpoint=endpoint,
        request_url=request_url,
        status_code=response.status_code,
        fetched_at=fetched_at,
        content_sha256=content_hash,
        byte_count=len(content),
        local_path=str(target.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        error=error,
    )
    return payload, record


def _fetch_required_json(
    *, name: str, endpoint: str, raw_name: str
) -> tuple[Any, RequestRecord]:
    payload, record = _fetch_response(
        name=name,
        endpoint=endpoint,
        params=None,
        raw_name=raw_name,
    )
    if record.status_code != 200 or record.error is not None or payload is None:
        raise WarmStartAuditError(f"required official GET failed: {record}")
    return payload, record


def _parse_exchange_info(payload: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("symbols"), list):
        raise WarmStartAuditError("exchangeInfo response has no symbols list")
    result: dict[str, Mapping[str, Any]] = {}
    for item in payload["symbols"]:
        if isinstance(item, Mapping) and item.get("symbol"):
            result[str(item["symbol"]).upper()] = {
                key: item[key]
                for key in (
                    "symbol",
                    "status",
                    "contractType",
                    "quoteAsset",
                    "baseAsset",
                    "onboardDate",
                    "deliveryDate",
                )
                if key in item
            }
    return result


def _parse_funding_info(payload: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(payload, list):
        raise WarmStartAuditError("fundingInfo response is not a list")
    result: dict[str, Mapping[str, Any]] = {}
    for item in payload:
        if isinstance(item, Mapping) and item.get("symbol"):
            result[str(item["symbol"]).upper()] = {
                key: item[key]
                for key in ("symbol", "fundingIntervalHours", "updateTime")
                if key in item
            }
    return result


def _parse_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WarmStartAuditError(f"{field} is not an integer") from exc


def _parse_daily_payload(payload: Any, symbol: str) -> tuple[DailyBar, ...]:
    if not isinstance(payload, list):
        raise WarmStartAuditError(f"{symbol} klines response is not a list")
    bars: list[DailyBar] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 8:
            raise WarmStartAuditError(f"{symbol} daily response row is malformed")
        open_time_ms = _parse_int(row[0], "open_time")
        close_time_ms = _parse_int(row[6], "close_time")
        day = datetime.fromtimestamp(open_time_ms / 1000, tz=UTC).date()
        if not V21_M1_1_EXTENSION_START <= day <= V21_M1_1_EXTENSION_END:
            raise WarmStartAuditError(
                f"{symbol} daily response contains data outside the pre-start window: {day}"
            )
        if close_time_ms >= V21_M1_1_CUTOFF_MS:
            raise WarmStartAuditError(f"{symbol} daily response contains cutoff-or-later data")
        try:
            values = [float(row[index]) for index in (1, 2, 3, 4, 7)]
        except (TypeError, ValueError, OverflowError, IndexError) as exc:
            raise WarmStartAuditError(f"{symbol} daily response contains nonnumeric data") from exc
        if (
            close_time_ms < open_time_ms
            or any(not math.isfinite(value) for value in values)
            or values[3] <= 0
        ):
            raise WarmStartAuditError(f"{symbol} daily response contains invalid price data")
        bars.append(
            DailyBar(
                symbol=symbol,
                day=day,
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                quote_volume=values[4],
                open_time_ms=open_time_ms,
                close_time_ms=close_time_ms,
            )
        )
    if len({bar.day for bar in bars}) != len(bars):
        raise WarmStartAuditError(f"{symbol} daily response contains duplicate days")
    return tuple(sorted(bars, key=lambda bar: (bar.day, bar.open_time_ms)))


def _interval_for_funding_event(
    *,
    symbol: str,
    event_time_ms: int,
    history: SymbolHistory,
    funding_info: Mapping[str, Any] | None,
) -> tuple[float, str]:
    if not history.funding_events:
        raise WarmStartAuditError(f"{symbol} has no frozen interval provenance")
    baseline = float(history.funding_events[-1].funding_interval_hours)
    if not math.isfinite(baseline) or baseline <= 0:
        raise WarmStartAuditError(f"{symbol} frozen funding interval is invalid")
    if not isinstance(funding_info, Mapping):
        return baseline, "FROZEN_PRESTART_BOUNDARY_INTERVAL"
    try:
        interval = float(funding_info.get("fundingIntervalHours"))
    except (TypeError, ValueError, OverflowError):
        return baseline, "FROZEN_PRESTART_BOUNDARY_INTERVAL"
    update_raw = funding_info.get("updateTime")
    try:
        update_time = int(update_raw) if update_raw is not None else None
    except (TypeError, ValueError, OverflowError):
        update_time = None
    if (
        math.isfinite(interval)
        and interval > 0
        and update_time is not None
        and update_time < V21_M1_1_CUTOFF_MS
        and event_time_ms >= update_time
    ):
        return interval, "OFFICIAL_FUNDING_INFO_UPDATE_BEFORE_CUTOFF"
    return baseline, "FROZEN_PRESTART_BOUNDARY_INTERVAL"


def _parse_funding_payload(
    payload: Any,
    symbol: str,
    history: SymbolHistory,
    funding_info: Mapping[str, Any] | None,
) -> tuple[tuple[FundingEvent, ...], dict[str, Any]]:
    if not isinstance(payload, list):
        raise WarmStartAuditError(f"{symbol} funding response is not a list")
    events: list[FundingEvent] = []
    sources: set[str] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            raise WarmStartAuditError(f"{symbol} funding response row is malformed")
        event_time_ms = _parse_int(item.get("fundingTime"), "fundingTime")
        if not V21_M1_1_EXTENSION_START <= datetime.fromtimestamp(
            event_time_ms / 1000, tz=UTC
        ).date() <= V21_M1_1_EXTENSION_END:
            raise WarmStartAuditError(f"{symbol} funding response contains out-of-window data")
        try:
            rate = float(item.get("fundingRate"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise WarmStartAuditError(f"{symbol} funding rate is not numeric") from exc
        if not math.isfinite(rate):
            raise WarmStartAuditError(f"{symbol} funding rate is non-finite")
        interval, source = _interval_for_funding_event(
            symbol=symbol,
            event_time_ms=event_time_ms,
            history=history,
            funding_info=funding_info,
        )
        sources.add(source)
        events.append(FundingEvent(symbol, event_time_ms, rate, interval))
    timestamps = [event.funding_time_ms for event in events]
    if len(set(timestamps)) != len(timestamps):
        raise WarmStartAuditError(f"{symbol} funding response contains duplicate timestamps")
    return tuple(sorted(events, key=lambda event: event.funding_time_ms)), {
        "frozen_boundary_interval_hours": history.funding_events[-1].funding_interval_hours,
        "official_interval_metadata": dict(funding_info or {}),
        "interval_sources": sorted(sources),
        "synthetic_settlements": False,
    }


def _with_parsed_count(record: RequestRecord, count: int) -> RequestRecord:
    return replace(record, parsed_count=count)


def _fetch_one_symbol(
    history: SymbolHistory,
    funding_info: Mapping[str, Mapping[str, Any]],
    exchange_info: Mapping[str, Mapping[str, Any]],
) -> ExtensionSymbolResult:
    symbol = history.symbol.upper()
    start_ms = int(
        datetime.combine(V21_M1_1_EXTENSION_START, time.min, tzinfo=UTC).timestamp()
        * 1000
    )
    params = {
        "symbol": symbol,
        "interval": "1d",
        "startTime": str(start_ms),
        "endTime": str(V21_M1_1_CUTOFF_MS),
        "limit": "1000",
    }
    daily_payload, daily_record = _fetch_response(
        name=f"{symbol}.daily_1d",
        endpoint=KLINES_ENDPOINT,
        params=params,
        raw_name=f"daily_{symbol}.json",
    )
    daily_bars: tuple[DailyBar, ...] = ()
    if daily_payload is not None and daily_record.error is None:
        try:
            daily_bars = _parse_daily_payload(daily_payload, symbol)
            daily_record = _with_parsed_count(daily_record, len(daily_bars))
        except WarmStartAuditError as exc:
            daily_record = replace(daily_record, error=str(exc))

    funding_params = {
        "symbol": symbol,
        "startTime": str(start_ms),
        "endTime": str(V21_M1_1_CUTOFF_MS),
        "limit": "1000",
    }
    funding_payload, funding_record = _fetch_response(
        name=f"{symbol}.funding_rate",
        endpoint=FUNDING_RATE_ENDPOINT,
        params=funding_params,
        raw_name=f"funding_{symbol}.json",
    )
    funding_events: tuple[FundingEvent, ...] = ()
    interval_provenance: dict[str, Any] = {
        "frozen_boundary_interval_hours": (
            history.funding_events[-1].funding_interval_hours
            if history.funding_events
            else None
        ),
        "official_interval_metadata": dict(funding_info.get(symbol, {})),
        "interval_sources": [],
        "synthetic_settlements": False,
    }
    if funding_payload is not None and funding_record.error is None:
        try:
            funding_events, interval_provenance = _parse_funding_payload(
                funding_payload,
                symbol,
                history,
                funding_info.get(symbol),
            )
            funding_record = _with_parsed_count(funding_record, len(funding_events))
        except WarmStartAuditError as exc:
            funding_record = replace(funding_record, error=str(exc))
    return ExtensionSymbolResult(
        symbol=symbol,
        daily_bars=daily_bars,
        funding_events=funding_events,
        daily_request=daily_record,
        funding_request=funding_record,
        interval_provenance=interval_provenance,
        exchange_info=exchange_info.get(symbol),
    )


def _archive_probe_records() -> tuple[RequestRecord, ...]:
    urls = (
        (
            "monthly_2026_09_daily_archive",
            "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2026-09.zip",
        ),
        (
            "monthly_2026_09_daily_checksum",
            "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2026-09.zip.CHECKSUM",
        ),
        (
            "monthly_2026_09_funding_archive",
            "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-09.zip",
        ),
        (
            "monthly_2026_09_funding_checksum",
            "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-09.zip.CHECKSUM",
        ),
    )
    records: list[RequestRecord] = []
    for name, endpoint in urls:
        _, record = _fetch_response(
            name=name,
            endpoint=endpoint,
            params=None,
            raw_name=f"{name}.bin",
            parse_json=False,
        )
        records.append(record)
    return tuple(records)


def fetch_prestart_extension(
    histories: Sequence[SymbolHistory],
) -> tuple[dict[str, ExtensionSymbolResult], dict[str, Any]]:
    """Fetch the completed pre-start extension without changing frozen data."""
    exchange_payload, exchange_record = _fetch_required_json(
        name="exchange_info",
        endpoint=EXCHANGE_INFO_ENDPOINT,
        raw_name="exchangeInfo.json",
    )
    funding_payload, funding_info_record = _fetch_required_json(
        name="funding_info",
        endpoint=FUNDING_INFO_ENDPOINT,
        raw_name="fundingInfo.json",
    )
    exchange_info = _parse_exchange_info(exchange_payload)
    funding_info = _parse_funding_info(funding_payload)
    active_histories = tuple(
        history
        for history in sorted(histories, key=lambda item: item.symbol.upper())
        if history.lifecycle.currently_active and history.lifecycle.delisted_at is None
    )
    results: dict[str, ExtensionSymbolResult] = {}
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="v21-m11-get") as pool:
        futures = {
            pool.submit(_fetch_one_symbol, history, funding_info, exchange_info): history.symbol
            for history in active_histories
        }
        for future in as_completed(futures):
            result = future.result()
            results[result.symbol] = result
    archive_records = _archive_probe_records()
    symbol_rows: list[dict[str, Any]] = []
    for history in sorted(histories, key=lambda item: item.symbol.upper()):
        symbol = history.symbol.upper()
        result = results.get(symbol)
        if result is None:
            symbol_rows.append(
                {
                    "symbol": symbol,
                    "scope": "FROZEN_USABLE_HISTORY_NOT_CURRENTLY_ACTIVE",
                    "frozen_lifecycle": _canonical(asdict(history.lifecycle)),
                    "exchange_info": exchange_info.get(symbol),
                    "daily_extension_row_count": 0,
                    "funding_extension_row_count": 0,
                    "price_coverage_pass": False,
                    "funding_coverage_pass": False,
                    "daily_missing_days": [],
                    "synthetic_settlements": False,
                }
            )
            continue
        observed_days = {bar.day for bar in result.daily_bars}
        expected_days = {
            V21_M1_1_EXTENSION_START + timedelta(days=index)
            for index in range((V21_M1_1_EXTENSION_END - V21_M1_1_EXTENSION_START).days + 1)
        }
        symbol_rows.append(
            {
                "symbol": symbol,
                "scope": "FROZEN_USABLE_HISTORY_CURRENTLY_ACTIVE",
                "frozen_lifecycle": _canonical(asdict(history.lifecycle)),
                "exchange_info": result.exchange_info,
                "daily_extension_row_count": len(result.daily_bars),
                "funding_extension_row_count": len(result.funding_events),
                "price_coverage_pass": (
                    result.daily_request.status_code == 200
                    and result.daily_request.error is None
                    and not (
                        {
                            V21_M1_1_EXTENSION_START
                            + timedelta(days=index)
                            for index in range(
                                (V21_M1_1_EXTENSION_END - V21_M1_1_EXTENSION_START).days
                                + 1
                            )
                        }
                        - observed_days
                    )
                ),
                "funding_coverage_pass": (
                    result.funding_request.status_code == 200
                    and result.funding_request.error is None
                    and bool(result.funding_events)
                ),
                "daily_missing_days": sorted(
                    day.isoformat() for day in expected_days - observed_days
                ),
                "daily_request_error": result.daily_request.error,
                "funding_request_error": result.funding_request.error,
                "interval_provenance": _canonical(result.interval_provenance),
                "synthetic_settlements": False,
            }
        )
    requests_all = [exchange_record, funding_info_record]
    for result in results.values():
        requests_all.extend((result.daily_request, result.funding_request))
    requests_all.extend(archive_records)
    request_dicts = [_canonical(asdict(record)) for record in sorted(requests_all, key=lambda item: item.name)]
    manifest: dict[str, Any] = {
        "schema_version": "PRESTART_EXTENSION_MANIFEST.v1",
        "manifest_type": "PRESTART_EXTENSION_MANIFEST",
        "audit_id": V21_M1_1_AUDIT_ID,
        "classification": [
            "PRESTART_EXTENSION_INITIALIZATION_ONLY",
            "NOT_FORWARD_EVIDENCE",
            "NOT_OOS_PERFORMANCE",
            "NOT_HISTORICAL_VALIDATION",
            "NO_STRATEGY_OPTIMIZATION",
        ],
        "source": {
            "venue": "Binance USD-M Futures",
            "base": BINANCE_FAPI_BASE,
            "request_method": "GET-only",
            "official_read_only_endpoints": [
                EXCHANGE_INFO_ENDPOINT,
                FUNDING_INFO_ENDPOINT,
                KLINES_ENDPOINT,
                FUNDING_RATE_ENDPOINT,
            ],
        },
        "request_window": {
            "extension_start_utc": V21_M1_1_EXTENSION_START.isoformat(),
            "extension_end_utc": V21_M1_1_EXTENSION_END.isoformat(),
            "audit_cutoff_utc": V21_M1_1_CUTOFF_UTC.isoformat(),
            "cutoff_rule": "only rows strictly before audit cutoff are eligible",
        },
        "frozen_parent": {
            "dataset_sha256": EXPECTED_DATASET_SHA256,
            "normalized_dataset_sha256": EXPECTED_NORMALIZED_DATASET_SHA256,
            "frozen_data_end": V21_M1_1_FROZEN_DATA_END.isoformat(),
            "symbol_selection": "reuse frozen usable_histories exactly",
            "lifecycle_mutation": False,
            "historical_dataset_rewritten": False,
        },
        "symbol_coverage": {
            "frozen_usable_symbol_count": len(histories),
            "active_extension_fetch_count": len(active_histories),
            "non_active_frozen_symbol_count": len(histories) - len(active_histories),
            "symbols": symbol_rows,
        },
        "archive_provenance": {
            "monthly_2026_09_probe_symbol": "BTCUSDT",
            "monthly_archive_usage": "not used when current-month archive is unavailable; API responses are retained",
            "probe_requests": [
                _canonical(asdict(record)) for record in archive_records
            ],
        },
        "requests": request_dicts,
        "fetch_summary": {
            "request_count": len(request_dicts),
            "successful_http_200_count": sum(
                record.status_code == 200 for record in requests_all
            ),
            "failed_or_not_found_count": sum(
                record.status_code != 200 for record in requests_all
            ),
            "raw_response_hashes_present": all(
                record.content_sha256 is not None
                for record in requests_all
                if record.status_code == 200
            ),
            "synthetic_data_created": False,
        },
    }
    assert_no_formal_performance_fields(manifest)
    return results, manifest


def merge_prestart_histories(
    histories: Sequence[SymbolHistory],
    extension: Mapping[str, ExtensionSymbolResult],
) -> tuple[SymbolHistory, ...]:
    merged: list[SymbolHistory] = []
    for history in sorted(histories, key=lambda item: item.symbol.upper()):
        result = extension.get(history.symbol.upper())
        if result is None:
            merged.append(history)
            continue
        existing_days = {bar.day for bar in history.daily_bars}
        if existing_days.intersection(bar.day for bar in result.daily_bars):
            raise WarmStartIdentityError(f"{history.symbol} extension overlaps frozen daily data")
        existing_funding = {event.funding_time_ms for event in history.funding_events}
        if existing_funding.intersection(event.funding_time_ms for event in result.funding_events):
            raise WarmStartIdentityError(f"{history.symbol} extension overlaps frozen funding data")
        merged.append(
            replace(
                history,
                daily_bars=tuple(
                    sorted((*history.daily_bars, *result.daily_bars), key=lambda bar: (bar.day, bar.open_time_ms))
                ),
                funding_events=tuple(
                    sorted((*history.funding_events, *result.funding_events), key=lambda event: event.funding_time_ms)
                ),
                lifecycle=history.lifecycle,
            )
        )
    return tuple(merged)


def _utc_ms(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=UTC).timestamp() * 1000)


def _clip_holding_interval(interval: HoldingInterval, week_start: date, week_end: date) -> HoldingInterval | None:
    week_start_ms = _utc_ms(week_start)
    week_end_exclusive_ms = _utc_ms(week_end + timedelta(days=1))
    if interval.entry_timestamp_ms >= week_end_exclusive_ms or interval.exit_timestamp_ms <= week_start_ms:
        return None
    clipped = HoldingInterval(
        symbol=interval.symbol,
        entry_timestamp_ms=max(interval.entry_timestamp_ms, week_start_ms),
        exit_timestamp_ms=min(interval.exit_timestamp_ms, week_end_exclusive_ms - 1),
    )
    return clipped if clipped.exit_timestamp_ms > clipped.entry_timestamp_ms else None


def clip_holding_intervals_for_week(
    intervals: Sequence[HoldingInterval], week_start: date, week_end: date
) -> tuple[HoldingInterval, ...]:
    return tuple(
        clipped
        for interval in intervals
        if (clipped := _clip_holding_interval(interval, week_start, week_end)) is not None
    )


def weekly_funding_audit(
    histories: Sequence[SymbolHistory],
    intervals: Sequence[HoldingInterval],
    week_start: date,
    week_end: date,
) -> dict[str, Any]:
    clipped = clip_holding_intervals_for_week(intervals, week_start, week_end)
    if not clipped:
        coverage = FundingCoverageReport()
    else:
        symbols = {interval.symbol.upper() for interval in clipped}
        scoped_histories = tuple(
            history for history in histories if history.symbol.upper() in symbols
        )
        coverage = validate_funding_coverage_for_holds(scoped_histories, clipped)
    issues = tuple(coverage.hold_issues)
    return {
        "overlapping_holding_interval_count": len(clipped),
        "held_symbols": sorted({interval.symbol.upper() for interval in clipped}),
        "funding_coverage_pass": not issues,
        "funding_issue_count": len(issues),
        "funding_issue_symbols": sorted({issue.symbol.upper() for issue in issues}),
        "funding_issues": [_canonical(asdict(issue)) for issue in issues],
        "funding_coverage_status": "PASS_ACTUAL_HELD_INTERVALS" if not issues else "FAIL_ACTUAL_HELD_INTERVALS",
        "synthetic_funding_used": False,
    }


def select_latest_completed_weeks(
    weekly_rows: Sequence[Mapping[str, Any]],
    *,
    cutoff: datetime,
    lookback: int = 13,
) -> tuple[dict[str, Any], ...]:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise WarmStartProducerError("audit cutoff must be timezone-aware")
    records: list[dict[str, Any]] = []
    seen: set[date] = set()
    for row in weekly_rows:
        try:
            week_start = date.fromisoformat(str(row["week_start"]))
            week_end = date.fromisoformat(str(row["week_end"]))
            value = float(row["return"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise WarmStartProducerError("weekly producer emitted malformed period") from exc
        if week_end in seen:
            raise WarmStartProducerError(
                f"duplicate week_ending rejected: {week_end.isoformat()}"
            )
        seen.add(week_end)
        if week_end - week_start != timedelta(days=6) or week_start.weekday() != 0:
            raise WarmStartProducerError("weekly period is not a complete Monday-Sunday period")
        completed_at = datetime.combine(week_end, time.max, tzinfo=UTC)
        if completed_at >= cutoff.astimezone(UTC):
            continue
        if not math.isfinite(value):
            raise WarmStartProducerError("weekly net return is non-finite")
        records.append(
            {
                "week_start": week_start,
                "week_end": week_end,
                "completed_at": completed_at,
                "producer_weekly_net_return": value,
            }
        )
    records.sort(key=lambda item: (item["completed_at"], item["week_end"]))
    if len(records) < lookback:
        raise WarmStartProducerError(
            f"only {len(records)} completed weekly periods available before cutoff"
        )
    selected = tuple(records[-lookback:])
    if len({item["week_end"] for item in selected}) != lookback:
        raise WarmStartProducerError("selected weekly periods are not distinct")
    return selected


def _accounting_issue_days(accounting: Any, schedule: Any) -> tuple[set[date], bool]:
    days: set[date] = set()
    unknown_price_issue = False
    for raw_issue in accounting.issues:
        text = str(raw_issue)
        lower = text.lower()
        if "funding" in lower or "settlement" in lower:
            continue
        if not any(word in lower for word in _PRICE_ISSUE_WORDS):
            continue
        parsed = [date.fromisoformat(match.group(0)) for match in _DATE_RE.finditer(text)]
        if parsed:
            days.update(parsed)
        else:
            unknown_price_issue = True
    for attempt in schedule.attempts:
        reason = str(getattr(attempt, "reason", "")).lower()
        is_price_failure = (
            attempt.status == "MISSING_EXECUTION_PRICE"
            or any(word in reason for word in _PRICE_ISSUE_WORDS)
        )
        if is_price_failure:
            days.add(attempt.execution_day)
    return days, unknown_price_issue


def _sum_daily_component(
    values: Sequence[Any], indices: Sequence[int]
) -> float | None:
    """Sum one accounting component, preserving missingness for fail-closed rows."""
    if any(index < 0 or index >= len(values) for index in indices):
        return None
    try:
        parsed = [float(values[index]) for index in indices]
    except (TypeError, ValueError, OverflowError):
        return None
    if any(not math.isfinite(value) for value in parsed):
        return None
    return sum(parsed)


def _week_component_provenance(
    *,
    accounting: Any,
    week: Mapping[str, Any],
    funding: Mapping[str, Any],
    price_issue_days: set[date],
    unknown_price_issue: bool,
) -> dict[str, Any]:
    week_start: date = week["week_start"]
    week_end: date = week["week_end"]
    indices = [
        index
        for index, day in enumerate(accounting.dates)
        if week_start <= day <= week_end
    ]
    if len(indices) != 7:
        raise WarmStartProducerError(f"weekly accounting has incomplete calendar: {week_end}")
    before = (
        accounting.capital
        if week_start == accounting.dates[0]
        else accounting.equity_by_day().get(week_start - timedelta(days=1))
    )
    after = accounting.equity_by_day().get(week_end)
    if before is None or after is None or not math.isfinite(float(before)) or not math.isfinite(float(after)):
        raise WarmStartProducerError(f"weekly accounting equity boundary is incomplete: {week_end}")
    price_pnl = _sum_daily_component(accounting.daily_price_pnl, indices)
    funding_pnl = _sum_daily_component(accounting.daily_funding_pnl, indices)
    cost_pnl = _sum_daily_component(accounting.daily_cost_pnl, indices)
    transaction_cost = -cost_pnl if cost_pnl is not None else None
    component_net_change = (
        price_pnl + funding_pnl + cost_pnl
        if price_pnl is not None and funding_pnl is not None and cost_pnl is not None
        else None
    )
    accounting_net_change = float(after) - float(before)
    reconciliation_delta = (
        component_net_change - accounting_net_change
        if component_net_change is not None
        else None
    )
    has_price_issue = unknown_price_issue or any(
        week_start <= day <= week_end for day in price_issue_days
    )
    price_complete = price_pnl is not None and not has_price_issue
    funding_complete = funding_pnl is not None and bool(funding["funding_coverage_pass"])
    cost_complete = cost_pnl is not None and not has_price_issue
    reconciliation_pass = (
        reconciliation_delta is not None
        and math.isclose(reconciliation_delta, 0.0, rel_tol=0.0, abs_tol=1e-12)
    )
    weekly_complete = price_complete and funding_complete and cost_complete and reconciliation_pass
    return {
        "week_ending": week_end.isoformat(),
        "completed_at": week["completed_at"].isoformat(),
        "weekly_net_return": week["producer_weekly_net_return"],
        "price_pnl": price_pnl,
        "funding_pnl": funding_pnl,
        "transaction_cost": transaction_cost,
        "component_net_change": component_net_change,
        "accounting_net_change": accounting_net_change,
        "component_reconciliation_delta": reconciliation_delta,
        "reconciliation_pass": reconciliation_pass,
        "overlapping_holding_interval_count": funding["overlapping_holding_interval_count"],
        "held_symbols": funding["held_symbols"],
        "funding_coverage_pass": funding["funding_coverage_pass"],
        "funding_issue_count": funding["funding_issue_count"],
        "funding_issue_symbols": funding["funding_issue_symbols"],
        "funding_issues": funding["funding_issues"],
        "funding_coverage_status": funding["funding_coverage_status"],
        "price_component_complete": price_complete,
        "funding_component_complete": funding_complete,
        "transaction_cost_component_complete": cost_complete,
        "weekly_return_complete": weekly_complete,
        "synthetic_funding_used": False,
    }


def build_weekly_provenance(
    *,
    histories: Sequence[SymbolHistory],
    accounting: Any,
    schedule: Any,
    cutoff: datetime,
) -> tuple[dict[str, Any], ...]:
    selected = select_latest_completed_weeks(accounting.weekly_rows, cutoff=cutoff)
    price_issue_days, unknown_price_issue = _accounting_issue_days(accounting, schedule)
    result: list[dict[str, Any]] = []
    for week in selected:
        funding = weekly_funding_audit(
            histories,
            accounting.holding_intervals,
            week["week_start"],
            week["week_end"],
        )
        result.append(
            _week_component_provenance(
                accounting=accounting,
                week=week,
                funding=funding,
                price_issue_days=price_issue_days,
                unknown_price_issue=unknown_price_issue,
            )
        )
    return tuple(result)


def build_risk_input(weekly_provenance: Sequence[Mapping[str, Any]], cutoff: datetime) -> dict[str, Any]:
    records = tuple(
        ControlWeeklyReturn(
            week_ending=date.fromisoformat(str(row["week_ending"])),
            net_return=float(row["weekly_net_return"]),
            completed_at=datetime.fromisoformat(str(row["completed_at"])),
            complete=row["weekly_return_complete"] is True,
        )
        for row in weekly_provenance
    )
    if len(records) != 13:
        raise WarmStartProducerError("risk input must contain exactly 13 audited rows")
    result = evaluate_risk_scale(records, signal_time=cutoff)
    output: dict[str, Any] = {
        "status": result.status,
        "selected_count": result.selected_count,
        "reason": result.reason,
        "input_complete_count": sum(record.complete for record in records),
        "input_row_count": len(records),
    }
    if result.status == "VALID":
        output.update(
            {
                "reference_vol": result.reference_vol,
                "position_scale": result.position_scale,
                "diagnostic_status": "INITIAL_STATE_DIAGNOSTIC_ONLY",
                "evidence_status": "NOT_PERFORMANCE_EVIDENCE",
            }
        )
    else:
        output["fail_closed_status"] = V2_DATA_INTEGRITY_HALT
    return output


def _global_funding_diagnostic(
    histories: Sequence[SymbolHistory], accounting: Any
) -> dict[str, Any]:
    coverage = validate_funding_coverage_for_holds(histories, accounting.holding_intervals)
    issues = tuple(coverage.hold_issues)
    return {
        "funding_integrity_issue_count": len(issues),
        "affected_symbols": sorted({issue.symbol.upper() for issue in issues}),
        "first_issue": _canonical(asdict(issues[0])) if issues else None,
        "last_issue": _canonical(asdict(issues[-1])) if issues else None,
        "required_window_intersection_issue_count": 0,
        "required_window_intersection_symbols": [],
        "provenance_status": "CONTAMINATED_DATA_QUALITY_DIAGNOSTIC",
        "synthetic_funding_used": False,
    }


def _warm_start_gates(
    *,
    frozen_identity_ok: bool,
    extension_manifest: Mapping[str, Any],
    weekly: Sequence[Mapping[str, Any]],
    risk: Mapping[str, Any],
    legacy_unchanged: bool,
) -> dict[str, str]:
    requests = extension_manifest["requests"]
    active_rows = [
        row
        for row in extension_manifest["symbol_coverage"]["symbols"]
        if row["scope"] == "FROZEN_USABLE_HISTORY_CURRENTLY_ACTIVE"
    ]
    required_request_names = {"exchange_info", "funding_info"}
    for row in active_rows:
        required_request_names.update(
            {f"{row['symbol']}.daily_1d", f"{row['symbol']}.funding_rate"}
        )
    request_by_name = {str(row["name"]): row for row in requests}

    def request_is_valid(name: str) -> bool:
        record = request_by_name.get(name, {})
        return (
            record.get("status_code") == 200
            and record.get("error") is None
            and record.get("method") == "GET"
            and isinstance(record.get("content_sha256"), str)
            and len(record["content_sha256"]) == 64
        )

    w1 = (
        extension_manifest["request_window"]["extension_start_utc"]
        == V21_M1_1_EXTENSION_START.isoformat()
        and extension_manifest["request_window"]["extension_end_utc"]
        == V21_M1_1_EXTENSION_END.isoformat()
        and extension_manifest["request_window"]["audit_cutoff_utc"]
        == V21_M1_1_CUTOFF_UTC.isoformat()
        and all(request_is_valid(name) for name in required_request_names)
        and all(
            row.get("price_coverage_pass") is True
            and not row["daily_missing_days"]
            for row in active_rows
        )
    )
    week_endings = [str(row["week_ending"]) for row in weekly]
    completed_at = [datetime.fromisoformat(str(row["completed_at"])) for row in weekly]
    w2 = (
        len(weekly) == 13
        and len(set(week_endings)) == 13
        and completed_at == sorted(completed_at)
        and all(item < V21_M1_1_CUTOFF_UTC for item in completed_at)
        and any(date.fromisoformat(item) > V21_M1_1_FROZEN_DATA_END for item in week_endings)
    )
    w3 = all(row["price_component_complete"] is True for row in weekly)
    w4 = all(row["funding_component_complete"] is True for row in weekly)
    w5 = all(row["transaction_cost_component_complete"] is True for row in weekly)
    w6 = all(row["weekly_return_complete"] is True for row in weekly)
    w7 = (
        risk["status"] == "VALID"
        if w6
        else risk["status"] == V2_DATA_INTEGRITY_HALT
    )
    return {
        "W0_identity": "PASS" if frozen_identity_ok and legacy_unchanged else "FAIL",
        "W1_incremental_data_provenance": "PASS" if w1 else "FAIL",
        "W2_latest_13_week_selection": "PASS" if w2 else "FAIL",
        "W3_price_component_integrity": "PASS" if w3 else "FAIL",
        "W4_funding_component_integrity": "PASS" if w4 else "FAIL",
        "W5_cost_component_integrity": "PASS" if w5 else "FAIL",
        "W6_weekly_return_completeness": "PASS" if w6 else "FAIL",
        "W7_risk_input_fail_closed": "PASS" if w7 else "FAIL",
        "W8_anchor_still_not_started": "PASS"
        if V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
        else "FAIL",
    }


def _render_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# XS-LOWVOL V2.1-M1.1 Forward Warm-Start Provenance Audit",
        "",
        f"## {result['final_decision']}",
        "",
        "Classification: `PRESTART_EXTENSION_INITIALIZATION_ONLY` / `NOT_FORWARD_EVIDENCE` / `NOT_OOS_PERFORMANCE`.",
        "",
        "No Forward signal, ledger, clock, NAV, or aggregate strategy performance was created.",
        "",
        "## Frozen identity and cutoff",
        "",
        f"- Base commit: `{result['base_commit']}`",
        f"- Code commit: `{result['code_commit']}`",
        f"- Audit ID: `{result['audit_id']}`",
        f"- Audit cutoff UTC: `{result['audit_cutoff_utc']}`",
        f"- Dataset SHA: `{result['dataset']['dataset_sha256']}`",
        f"- Normalized Dataset SHA: `{result['dataset']['normalized_dataset_sha256']}`",
        f"- PRESTART extension manifest SHA: `{result['prestart_extension_manifest_sha256']}`",
        "",
        "## W0-W8",
        "",
    ]
    lines.extend(f"- `{name}`: `{value}`" for name, value in result["gates"].items())
    lines.extend(
        [
            "",
            "## Required weekly provenance",
            "",
            "| Week ending | Completed at | Held intervals | Funding | Price | Cost | Complete |",
            "|---|---|---:|---|---|---|---|",
        ]
    )
    for row in result["weekly_provenance"]:
        lines.append(
            "| {week_ending} | {completed_at} | {overlapping_holding_interval_count} | {funding_coverage_pass} | {price_component_complete} | {transaction_cost_component_complete} | {weekly_return_complete} |".format(
                **row
            )
        )
    lines.extend(
        [
            "",
            "## Audit summary",
            "",
            "```json",
            json.dumps(_canonical({
                key: value
                for key, value in result.items()
                if key != "weekly_provenance"
            }), ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _write_artifacts(
    *,
    result: Mapping[str, Any],
    extension_manifest: Mapping[str, Any],
    legacy_hashes_before: Mapping[str, str],
) -> dict[str, Path]:
    paths = (
        V21_M1_1_PRESTART_MANIFEST_PATH,
        V21_M1_1_REPORT_PATH,
        V21_M1_1_MARKDOWN_PATH,
    )
    existing = [path for path in paths if path.exists()]
    if existing:
        raise WarmStartAuditError(
            "refusing to overwrite existing M1.1 artifacts: "
            + ", ".join(str(path) for path in existing)
        )
    V21_M1_1_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    V21_M1_1_PRESTART_MANIFEST_PATH.write_text(
        json.dumps(_canonical(extension_manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_sha = _file_hash(V21_M1_1_PRESTART_MANIFEST_PATH)
    report = dict(result)
    report["prestart_extension_manifest_sha256"] = manifest_sha
    report["artifacts"] = {
        "prestart_extension_manifest": "research/v2_1/m1_1/PRESTART_EXTENSION_MANIFEST.json",
        "report": "research/v2_1/m1_1/V2_1_M1_1_WARM_START_AUDIT.json",
        "markdown": "research/v2_1/m1_1/V2_1_M1_1_WARM_START_AUDIT.md",
    }
    assert_no_formal_performance_fields(report)
    V21_M1_1_REPORT_PATH.write_text(
        json.dumps(_canonical(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    V21_M1_1_MARKDOWN_PATH.write_text(_render_markdown(report), encoding="utf-8")
    legacy_after = {
        path: _file_hash(path)
        for path in legacy_hashes_before
    }
    if legacy_after != dict(legacy_hashes_before):
        raise WarmStartIdentityError("legacy V2.1-M1 artifact changed during M1.1 audit")
    if isinstance(result, dict):
        result["prestart_extension_manifest_sha256"] = manifest_sha
        result["legacy_m1_artifact_hashes_after"] = legacy_after
    return {
        "prestart_extension_manifest": V21_M1_1_PRESTART_MANIFEST_PATH,
        "report": V21_M1_1_REPORT_PATH,
        "markdown": V21_M1_1_MARKDOWN_PATH,
    }


def run_warm_start_audit(*, approval: str) -> dict[str, Any]:
    if approval != V21_M1_1_APPROVAL:
        raise WarmStartAuditError(
            f"M1.1 audit requires explicit approval {V21_M1_1_APPROVAL!r}"
        )
    code_commit = _current_code_commit()
    if not _is_ancestor(V21_M1_1_BASE_COMMIT, code_commit):
        raise WarmStartIdentityError("current code is not based on accepted V2.1-M1 result")
    legacy_paths = (
        PROJECT_ROOT / "research" / "v2_1" / "m1" / "V2_1_M1_ENGINEERING_REPORT.json",
        PROJECT_ROOT / "research" / "v2_1" / "m1" / "V2_1_M1_ENGINEERING_REPORT.md",
        PROJECT_ROOT / "research" / "v2_1" / "m1" / "V2_1_M1_TRACE_MANIFEST.json",
    )
    legacy_hashes_before = {str(path): _file_hash(path) for path in legacy_paths}
    frozen_identity = verify_frozen_identity()
    dataset = load_frozen_dataset()
    frozen_histories = tuple(dataset["usable_histories"])
    extension, extension_manifest = fetch_prestart_extension(frozen_histories)
    merged_histories = merge_prestart_histories(frozen_histories, extension)
    schedule = build_stateful_schedule(
        merged_histories,
        data_start=M1_DATA_START,
        data_end=V21_M1_1_EXTENSION_END,
    )
    accounting = simulate_frozen_portfolio(
        merged_histories,
        schedule.signals,
        variant="control",
        scenario="COST_1X",
        data_start=M1_DATA_START,
        data_end=V21_M1_1_EXTENSION_END,
    )
    weekly = build_weekly_provenance(
        histories=merged_histories,
        accounting=accounting,
        schedule=schedule,
        cutoff=V21_M1_1_CUTOFF_UTC,
    )
    risk = build_risk_input(weekly, V21_M1_1_CUTOFF_UTC)
    global_funding = _global_funding_diagnostic(merged_histories, accounting)
    weekly_issue_symbols = sorted(
        {
            symbol
            for row in weekly
            for symbol in row["funding_issue_symbols"]
        }
    )
    global_funding["required_window_intersection_issue_count"] = sum(
        row["funding_issue_count"] for row in weekly
    )
    global_funding["required_window_intersection_symbols"] = weekly_issue_symbols
    legacy_unchanged = all(
        _file_hash(path) == expected for path, expected in legacy_hashes_before.items()
    )
    gates = _warm_start_gates(
        frozen_identity_ok=True,
        extension_manifest=extension_manifest,
        weekly=weekly,
        risk=risk,
        legacy_unchanged=legacy_unchanged,
    )
    failed_gates = [name for name, value in gates.items() if value != "PASS"]
    warm_start_ready = not failed_gates and all(
        row["weekly_return_complete"] is True for row in weekly
    )
    result: dict[str, Any] = {
        "schema_version": "V2.1-M1.1-WARM-START-AUDIT.v1",
        "final_decision": "V2.1-M1.1 WARM_START READY"
        if warm_start_ready
        else "V2.1-M1.1 WARM_START NOT_READY",
        "classification": {
            "data": "PRESTART_EXTENSION_INITIALIZATION_ONLY",
            "evidence": "NOT_FORWARD_EVIDENCE",
            "sample": "NOT_OOS_PERFORMANCE",
            "validation": "NOT_HISTORICAL_VALIDATION",
            "scope": "WARM_START_PROVENANCE_ONLY",
        },
        "base_commit": V21_M1_1_BASE_COMMIT,
        "code_commit": code_commit,
        "audit_id": V21_M1_1_AUDIT_ID,
        "audit_cutoff_utc": V21_M1_1_CUTOFF_UTC.isoformat(),
        "v1_control_sha256": frozen_identity["v1_control_sha256"],
        "v2_1_strategy_id": V21_STRATEGY_ID,
        "v2_1_spec_sha256": frozen_identity["v2_1_spec_sha256"],
        "v2_1_protocol_sha256": frozen_identity["v2_1_protocol_sha256"],
        "forward_anchor_sha256": frozen_identity["forward_anchor_sha256"],
        "forward_anchor_status": V21_FORWARD_ANCHOR_STATUS,
        "approved_m0_commit_timestamp_utc": APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC.isoformat(),
        "dataset": {
            "dataset_sha256": dataset["dataset_sha256"],
            "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
            "cache_file_sha256": dataset["cache_file_sha256"],
            "frozen_data_end": V21_M1_1_FROZEN_DATA_END.isoformat(),
            "catalog_complete": dataset["catalog"].listing_complete is True,
            "number_of_symbols_discovered": int(
                dataset["manifest"]["number_of_symbols_discovered"]
            ),
            "number_of_usable_symbols": int(dataset["manifest"]["number_of_usable_symbols"]),
            "number_of_delisted_symbols": int(dataset["manifest"]["number_of_delisted_symbols"]),
            "number_of_ambiguous_symbols": int(dataset["manifest"]["number_of_ambiguous_symbols"]),
            "listing_page_count": int(
                dataset["manifest"]["discovery_catalog"]["listing_page_count"]
            ),
            "raw_file_count": int(dataset["manifest"]["raw_file_count"]),
            "frozen_usable_symbol_count": len(frozen_histories),
            "extension_start": V21_M1_1_EXTENSION_START.isoformat(),
            "extension_end": V21_M1_1_EXTENSION_END.isoformat(),
        },
        "prestart_extension_data_range": {
            "start_utc": V21_M1_1_EXTENSION_START.isoformat(),
            "end_utc": V21_M1_1_EXTENSION_END.isoformat(),
            "cutoff_utc": V21_M1_1_CUTOFF_UTC.isoformat(),
        },
        "prestart_extension_manifest_sha256": None,
        "latest_13_week_endings": [row["week_ending"] for row in weekly],
        "weekly_provenance": list(weekly),
        "completeness": {
            "required_week_count": 13,
            "weekly_return_complete_count": sum(
                row["weekly_return_complete"] is True for row in weekly
            ),
            "weekly_return_complete_total": 13,
        },
        "blocking": {
            "weeks": [row["week_ending"] for row in weekly if not row["weekly_return_complete"]],
            "symbols": sorted(
                {
                    symbol
                    for row in weekly
                    if not row["weekly_return_complete"]
                    for symbol in row["funding_issue_symbols"]
                }
            ),
            "data_types": sorted(
                {
                    component
                    for row in weekly
                    if not row["weekly_return_complete"]
                    for component, complete in (
                        ("price", row["price_component_complete"]),
                        ("funding", row["funding_component_complete"]),
                        ("transaction_cost", row["transaction_cost_component_complete"]),
                    )
                    if not complete
                }
            ),
            "reasons": sorted(
                {
                    reason
                    for row in weekly
                    if not row["weekly_return_complete"]
                    for reason in (
                        "price_component_incomplete" if not row["price_component_complete"] else None,
                        "funding_component_incomplete" if not row["funding_component_complete"] else None,
                        "transaction_cost_component_incomplete"
                        if not row["transaction_cost_component_complete"]
                        else None,
                    )
                    if reason is not None
                }
            ),
        },
        "risk_input": risk,
        "reference_vol": risk.get("reference_vol") if warm_start_ready else None,
        "position_scale": risk.get("position_scale") if warm_start_ready else None,
        "risk_diagnostic_status": (
            "INITIAL_STATE_DIAGNOSTIC_ONLY" if warm_start_ready else "NOT_COMPUTED_DUE_TO_INCOMPLETE_INPUT"
        ),
        "global_funding_diagnostic": global_funding,
        "gates": gates,
        "failed_gates": failed_gates,
        "legacy_m1_artifact_hashes_before": legacy_hashes_before,
        "legacy_m1_artifact_hashes_after": legacy_hashes_before,
        "legacy_m1_artifacts_unchanged": legacy_unchanged,
        "runtime_safety": {
            "live_trading": False,
            "paper_only": True,
            "http_methods": ["GET"],
            "forward_evidence_created": False,
            "first_forward_signal_selected": False,
            "forward_clock_started": False,
            "forward_ledger_created": False,
            "forward_nav_created": False,
        },
        "historical_performance_output": False,
        "parameter_optimization": False,
        "v2_1_m2_started": False,
    }
    assert_no_formal_performance_fields(result)
    artifacts = _write_artifacts(
        result=result,
        extension_manifest=extension_manifest,
        legacy_hashes_before=legacy_hashes_before,
    )
    result["artifacts"] = {
        name: str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        for name, path in artifacts.items()
    }
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the V2.1-M1.1 warm-start provenance audit")
    parser.add_argument("--approval", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        result = run_warm_start_audit(approval=args.approval)
    except Exception as exc:  # noqa: BLE001 - CLI is fail closed
        print("V2.1-M1.1 WARM_START NOT_READY")
        print(f"warm-start audit error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(result["final_decision"])
    print(
        json.dumps(
            {
                "audit_id": result["audit_id"],
                "code_commit": result["code_commit"],
                "gates": result["gates"],
                "failed_gates": result["failed_gates"],
                "weekly_return_complete_count": result["completeness"]["weekly_return_complete_count"],
                "reference_vol": result["reference_vol"],
                "position_scale": result["position_scale"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXPECTED_DATASET_SHA256",
    "EXPECTED_NORMALIZED_DATASET_SHA256",
    "V21_M1_1_APPROVAL",
    "V21_M1_1_AUDIT_ID",
    "V21_M1_1_BASE_COMMIT",
    "V21_M1_1_CUTOFF_UTC",
    "V21_M1_1_EXTENSION_END",
    "V21_M1_1_EXTENSION_START",
    "V21_M1_1_MARKDOWN_PATH",
    "V21_M1_1_OUTPUT_DIR",
    "V21_M1_1_PRESTART_MANIFEST_PATH",
    "V21_M1_1_RAW_DIR",
    "V21_M1_1_REPORT_PATH",
    "WarmStartAuditError",
    "WarmStartIdentityError",
    "WarmStartProducerError",
    "assert_no_formal_performance_fields",
    "build_risk_input",
    "build_weekly_provenance",
    "clip_holding_intervals_for_week",
    "fetch_prestart_extension",
    "load_frozen_dataset",
    "merge_prestart_histories",
    "run_warm_start_audit",
    "select_latest_completed_weeks",
    "verify_frozen_identity",
    "weekly_funding_audit",
]
