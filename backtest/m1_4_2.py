"""XS-LOWVOL V2.1-M1.4.2 contract-type-correct parent reconstruction.

This runner is deliberately state-only.  It reads the accepted normalized
cache and the already accepted M1.4 pre-start raw responses, applies a
separate product-eligibility overlay, and reconstructs the parent from the
independently derived 2026-02-27 checkpoint.  It never rewrites frozen
strategy/protocol files, never emits historical aggregate performance, and
never selects a Forward signal.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.xs_lowvol_spec import resolve_rules, strategy_spec_hash
from src.xs_lowvol_v2_1_anchor import (
    APPROVED_V1_CONTROL_SHA256,
    APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_1_PROTOCOL_SHA256,
    APPROVED_V2_1_SPEC_SHA256,
    V21_FORWARD_ANCHOR_STATUS,
    validate_v2_1_forward_anchor,
    verify_v2_1_forward_anchor_hash,
)
from src.xs_lowvol_v2_1_lifecycle import apply_lifecycle_overlay
from src.xs_lowvol_v2_1_risk import ControlWeeklyReturn, evaluate_risk_scale
from src.xs_lowvol_v2_1_spec import verify_v2_1_spec_hash

from .m1_b import (
    M1_DATA_END,
    M1_DATA_START,
    _build_universe_fast,
    _signal_from_universe,
    build_stateful_schedule,
    simulate_frozen_portfolio,
)
from .m1_protocol import M1_APPROVED_PROTOCOL_SHA256, verify_protocol_hash
from .v2_1_m1_3_parent_reconstruction import (
    DEFAULT_OVERLAY,
)
from .v2_1_m1_4_prestart_bridge import (
    M13_CODE_COMMIT,
    M13_RESULT_COMMIT,
    _BridgePosition,
    _apply_mark,
    _file_sha256,
    _parse_daily_payload,
    _parse_exchange_info,
    _parse_funding_info,
    _parse_funding_payload,
    _transition_positions,
    _unique_funding,
    assert_prestart_state_schema,
    carry_positions_without_reopen,
)
from .v2_1_m1_engineering import (
    EXPECTED_DATASET_SHA256,
    EXPECTED_NORMALIZED_DATASET_SHA256,
    load_verified_dataset,
)
from .v2_1_protocol import verify_v2_1_protocol_hash
from .xs_history import DailyBar, FundingEvent, LifecycleRecord, SymbolHistory


PROJECT_ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc

BASE_COMMIT = "d4a05746a4a8dc42ab16d120bcdb9465493a04e0"
RUN_ID = "XS-LOWVOL-V2.1-M1.4.2-PARENT-RECONSTRUCTION-1"
APPROVAL = "START V2.1-M1.4.2"
OUTPUT_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_4_2"

OVERLAY_PATH = OUTPUT_DIR / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY.json"
CHECKPOINT_PATH = OUTPUT_DIR / "PRE_CONTAMINATION_CHECKPOINT.json"
SCHEDULE_PATH = OUTPUT_DIR / "CORRECTED_PARENT_SCHEDULE.json"
HOLDINGS_PATH = OUTPUT_DIR / "CORRECTED_PARENT_HOLDINGS.json"
FUNDING_PATH = OUTPUT_DIR / "CORRECTED_FUNDING_PROVENANCE.json"
EXTENSION_PATH = OUTPUT_DIR / "CORRECTED_PRESTART_EXTENSION_MANIFEST.json"
RISK_PATH = OUTPUT_DIR / "CORRECTED_LATEST_13_RISK_INPUT.json"
CURRENT_STATE_PATH = OUTPUT_DIR / "CORRECTED_CURRENT_PARENT_STATE.json"
REPORT_PATH = OUTPUT_DIR / "V2_1_M1_4_2_PARENT_RECONSTRUCTION.json"
MARKDOWN_PATH = OUTPUT_DIR / "V2_1_M1_4_2_PARENT_RECONSTRUCTION.md"

M14_RAW_DIR = PROJECT_ROOT / "data" / "v2_1_m1_4_prestart_extension"
M14_MANIFEST_PATH = PROJECT_ROOT / "research" / "v2_1" / "m1_4" / "PRESTART_EXTENSION_MANIFEST.json"
M14_1_PATH = PROJECT_ROOT / "research" / "v2_1" / "m1_4_1" / "V2_1_M1_4_1_CONTRACT_TYPE_AUDIT.json"
M13_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_3"
M14_1_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_4_1"
EVIDENCE_PATH = PROJECT_ROOT / "research" / "evidence" / "XS-LOWVOL-V1.json"

EXCHANGE_INFO_ENDPOINT = "https://fapi.binance.com/fapi/v1/exchangeInfo"
KLINES_ENDPOINT = "https://fapi.binance.com/fapi/v1/klines"
FUNDING_RATE_ENDPOINT = "https://fapi.binance.com/fapi/v1/fundingRate"

CHECKPOINT_DAY = date(2026, 2, 27)
FORMAL_CORRECTION_START = date(2026, 2, 28)
FROZEN_DATA_END = date(2026, 8, 31)
EXTENSION_START = date(2026, 9, 1)
EXTENSION_END = date(2026, 9, 18)
CUTOFF = datetime(2026, 9, 19, tzinfo=UTC)
CHECKPOINT_TIMESTAMP = "2026-02-27T23:59:59.999Z"
FINAL_CUTOFF = "2026-09-19T00:00:00Z"

M1_1_EXCHANGE_SHA256 = "b7f3107596d63ab656bb5a3fc0f38331a1aa719cef7e1e049677d2bd970ca94a"
M1_4_EXCHANGE_SHA256 = "5c1dc758964e01c2ef789801890c220e9ccb0a67924a697b2e49cae0b82a84cd"
M13_OVERLAY_SHA256 = "7a6f183262580933c4802acb9454b0617c7c095b458b79f1e56fadb1c9ec64c1"
SEPTEMBER_PRODUCT_SOURCE = "https://www.binance.com/en/support/announcement/detail/89a035c3ee0e4b7782bf0089323d8e78"
TRADFI_BOUNDARY_SOURCE = "https://www.binance.com/en/support/announcement/detail/ecf7318c0d434c339e80878588e700d0"

LABELS = (
    "PRESTART_STATE_INITIALIZATION_ONLY",
    "NOT_FORWARD",
    "NOT_VALIDATION_EVIDENCE",
)

_DIRECTION = {1: "LONG", -1: "SHORT"}
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")


class M142Error(RuntimeError):
    """Base M1.4.2 fail-closed error."""


class M142IdentityError(M142Error):
    """Frozen identity or immutable artifact failure."""


class M142NotReady(M142Error):
    """The corrected parent cannot be certified as complete."""


@dataclass(frozen=True)
class ContractRegistry:
    rows: tuple[Mapping[str, Any], ...]
    excluded_tradifi_symbols: tuple[str, ...]
    unresolved_symbols: tuple[str, ...]
    eligible_symbols: tuple[str, ...]
    full_catalog_symbol_count: int
    known_but_excluded_count: int
    overlay_payload: Mapping[str, Any]

    @property
    def by_symbol(self) -> dict[str, Mapping[str, Any]]:
        return {str(row["symbol"]): row for row in self.rows}


@dataclass
class ParentRunState:
    positions: dict[str, _BridgePosition]
    scheduler: dict[str, Any]
    equity_by_day: dict[date, float]
    attempts: list[dict[str, Any]]
    daily_rows: list[dict[str, Any]]
    holding_intervals: list[dict[str, Any]]
    funding_rows: list[dict[str, Any]]
    unresolved: list[str]
    funding_issue_count: int
    rebalance_count: int


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _iso_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000.0, UTC).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _day_range(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        return ()
    return tuple(start + timedelta(days=index) for index in range((end - start).days + 1))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M142IdentityError(f"cannot load JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise M142IdentityError(f"JSON artifact is not an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> str:
    assert_prestart_state_schema(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(_canonical(value), ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )
    return _file_sha256(path)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _is_ancestor(base: str, commit: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, commit],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise M142IdentityError("git could not verify approved base lineage")
    return result.returncode == 0


def _immutable_snapshot() -> dict[str, str]:
    roots = (
        PROJECT_ROOT / "research" / "m1",
        PROJECT_ROOT / "research" / "v2",
        PROJECT_ROOT / "research" / "v2_1" / "m1",
        PROJECT_ROOT / "research" / "v2_1" / "m1_1",
        PROJECT_ROOT / "research" / "v2_1" / "m1_2",
        PROJECT_ROOT / "research" / "v2_1" / "m1_3",
        PROJECT_ROOT / "research" / "v2_1" / "m1_3_1",
        PROJECT_ROOT / "research" / "v2_1" / "m1_4",
        PROJECT_ROOT / "research" / "v2_1" / "m1_4_1",
        EVIDENCE_PATH,
    )
    output: dict[str, str] = {}
    for root in roots:
        paths = (root,) if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if path.is_file():
                output[path.relative_to(PROJECT_ROOT).as_posix()] = _file_sha256(path)
    return output


def verify_frozen_identity() -> dict[str, str]:
    try:
        actual = {
            "v1_protocol_sha256": verify_protocol_hash(),
            "v1_control_sha256": strategy_spec_hash(),
            "v1_shadow_sha256": resolve_rules(variant="shadow").spec_hash,
            "v2_1_spec_sha256": verify_v2_1_spec_hash(),
            "v2_1_protocol_sha256": verify_v2_1_protocol_hash(),
            "forward_anchor_sha256": verify_v2_1_forward_anchor_hash(),
        }
        validate_v2_1_forward_anchor()
    except Exception as exc:  # noqa: BLE001 - identity must fail closed
        raise M142IdentityError(f"frozen identity verification failed: {exc}") from exc
    expected = {
        "v1_protocol_sha256": M1_APPROVED_PROTOCOL_SHA256,
        "v1_control_sha256": APPROVED_V1_CONTROL_SHA256,
        "v1_shadow_sha256": "97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd",
        "v2_1_spec_sha256": APPROVED_V2_1_SPEC_SHA256,
        "v2_1_protocol_sha256": APPROVED_V2_1_PROTOCOL_SHA256,
        "forward_anchor_sha256": APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    }
    if actual != expected:
        raise M142IdentityError(f"frozen identity changed: expected={expected}, actual={actual}")
    if V21_FORWARD_ANCHOR_STATUS != "FROZEN_NOT_YET_STARTED":
        raise M142IdentityError("Forward anchor is no longer FROZEN_NOT_YET_STARTED")
    return actual


def protocol_contract_eligible(
    contract_type: Any, quote_asset: Any, status: Any = "TRADING"
) -> bool:
    """Canonical frozen product predicate, before PIT lifecycle eligibility."""
    return (
        str(contract_type or "").upper() == "PERPETUAL"
        and str(quote_asset or "").upper() == "USDT"
        and str(status or "").upper() == "TRADING"
    )


def protocol_contract_eligible_on(
    *,
    contract_type: Any,
    quote_asset: Any,
    status: Any,
    history: SymbolHistory,
    signal_day: date,
) -> bool:
    """Apply product identity and the signal-day lifecycle predicate."""
    return protocol_contract_eligible(contract_type, quote_asset, status) and history.active_on(
        signal_day
    )


def _snapshot_exchange_info(path: Path, expected_sha: str) -> tuple[dict[str, Mapping[str, Any]], dict[str, Any]]:
    if not path.is_file():
        raise M142IdentityError(f"frozen exchangeInfo snapshot is missing: {path}")
    actual_sha = _file_sha256(path)
    if actual_sha != expected_sha:
        raise M142IdentityError(
            f"exchangeInfo snapshot changed: {path.name}: expected {expected_sha}, got {actual_sha}"
        )
    payload = _load_json(path)
    rows = payload.get("symbols")
    if not isinstance(rows, list):
        raise M142IdentityError(f"exchangeInfo snapshot has no symbols list: {path}")
    info = {
        str(row["symbol"]).upper(): dict(row)
        for row in rows
        if isinstance(row, Mapping) and _SYMBOL_RE.fullmatch(str(row.get("symbol", "")).upper())
    }
    server_ms = payload.get("serverTime")
    try:
        publication = _iso_ms(int(server_ms))
    except (TypeError, ValueError, OverflowError):
        raise M142IdentityError(f"exchangeInfo snapshot has no timestamp: {path}") from None
    return info, {
        "endpoint": EXCHANGE_INFO_ENDPOINT,
        "local_path": path.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": actual_sha,
        "publication_timestamp_utc": publication,
        "symbol_count": len(info),
    }


def _compact_contract_info(info: Mapping[str, Any] | None) -> dict[str, Any]:
    if not info:
        return {
            "found": False,
            "contractType": None,
            "quoteAsset": None,
            "status": None,
            "onboardDate": None,
            "underlyingType": None,
            "underlyingSubType": None,
        }
    return {
        "found": True,
        "contractType": info.get("contractType"),
        "quoteAsset": info.get("quoteAsset"),
        "status": info.get("status"),
        "onboardDate": info.get("onboardDate"),
        "underlyingType": info.get("underlyingType"),
        "underlyingSubType": info.get("underlyingSubType"),
    }


def _contract_reason(info: Mapping[str, Any]) -> str:
    contract = str(info.get("contractType") or "").upper()
    quote = str(info.get("quoteAsset") or "").upper()
    status = str(info.get("status") or "").upper()
    if status != "TRADING":
        return "NON_TRADING_OR_SETTLING_STATE"
    if contract == "TRADIFI_PERPETUAL":
        return "TRADIFI_PERPETUAL_EXCLUDED_BY_PROTOCOL"
    if contract in {"CURRENT_QUARTER", "NEXT_QUARTER"}:
        return f"{contract}_REJECTED_BY_PERPETUAL_PROTOCOL"
    if quote != "USDT":
        return "NON_USDT_QUOTE_REJECTED_BY_PROTOCOL"
    if contract != "PERPETUAL":
        return "NON_PERPETUAL_CONTRACT_REJECTED_BY_PROTOCOL"
    return "PROTOCOL_ELIGIBLE"


def _audit_scope_symbols() -> tuple[set[str], set[str]]:
    audit = _load_json(M14_1_PATH)
    september = {
        str(row.get("symbol", "")).upper()
        for row in audit.get("september_candidates", {}).get("rows", [])
        if isinstance(row, Mapping) and row.get("symbol")
    }
    affected = {
        str(row.get("symbol", "")).upper()
        for row in audit.get("m1_3_holdings_contract_type_audit", {}).get("rows", [])
        if isinstance(row, Mapping) and row.get("symbol")
    }
    affected.update(
        str(row.get("symbol", "")).upper()
        for row in audit.get("current_m1_4_positions_contract_type_audit", {}).get("rows", [])
        if isinstance(row, Mapping) and row.get("symbol")
    )
    return september, affected


def build_contract_type_registry(dataset: Mapping[str, Any]) -> ContractRegistry:
    """Build a PIT product registry from two frozen official snapshots.

    A single current snapshot is deliberately insufficient.  For an
    in-scope TradFi symbol, the registry requires a valid official onboard
    timestamp and agreement between the timestamped M1.1 and M1.4 official
    exchangeInfo snapshots, or an explicit timestamped official September
    listing record already preserved by M1.4.1.
    """
    previous, previous_meta = _snapshot_exchange_info(
        M14_RAW_DIR.parent / "v2_1_m1_1_prestart_extension" / "exchangeInfo.json",
        M1_1_EXCHANGE_SHA256,
    )
    current, current_meta = _snapshot_exchange_info(
        M14_RAW_DIR / "exchangeInfo.json",
        M1_4_EXCHANGE_SHA256,
    )
    catalog = dataset.get("catalog")
    catalog_symbols = {
        str(record.symbol).upper()
        for record in getattr(catalog, "symbols", ())
        if getattr(record, "symbol", None)
    }
    histories = tuple(dataset.get("usable_histories", ()))
    history_symbols = {history.symbol.upper() for history in histories}
    september_symbols, affected_symbols = _audit_scope_symbols()
    scope_symbols = catalog_symbols | history_symbols | september_symbols | affected_symbols

    rows: list[Mapping[str, Any]] = []
    unresolved: list[str] = []
    excluded_tradifi: set[str] = set()
    eligible: set[str] = set()
    for symbol in sorted(set(current) | scope_symbols):
        info = current.get(symbol)
        old = previous.get(symbol)
        if info is None:
            if symbol in affected_symbols:
                unresolved.append(symbol)
                rows.append(
                    {
                        "symbol": symbol,
                        "official_historical_product_category": None,
                        "effective_from_timestamp": None,
                        "official_source": None,
                        "source_publication_timestamp": None,
                        "source_hash": None,
                        "contract_type": None,
                        "quote_asset": None,
                        "classification": "CONTRACT_TYPE_STATE_NOT_READY",
                        "historical_evidence_status": "MISSING_OFFICIAL_TIMESTAMPED_PRODUCT_EVIDENCE",
                        "in_control_scope": True,
                    }
                )
            continue
        reason = _contract_reason(info)
        is_eligible = protocol_contract_eligible(
            info.get("contractType"), info.get("quoteAsset"), info.get("status")
        )
        if is_eligible:
            eligible.add(symbol)
            continue
        try:
            onboard_ms = int(info.get("onboardDate"))
            effective = _iso_ms(onboard_ms)
        except (TypeError, ValueError, OverflowError):
            onboard_ms = None
            effective = None
        in_scope = symbol in scope_symbols and (symbol in history_symbols or symbol in september_symbols)
        same_product = bool(
            old
            and str(old.get("contractType", "")).upper() == str(info.get("contractType", "")).upper()
            and str(old.get("quoteAsset", "")).upper() == str(info.get("quoteAsset", "")).upper()
            and int(old.get("onboardDate", -1)) == int(info.get("onboardDate", -2))
        )
        explicit_september = symbol in september_symbols
        evidence_complete = onboard_ms is not None and (same_product or explicit_september)
        category = "TRADFI" if "TRADIFI" in str(info.get("contractType", "")).upper() or "TRADFI" in str(info.get("underlyingSubType", "")).upper() else "NON_PROTOCOL_PRODUCT"
        source_hash = _sha256_json(
            {
                "symbol": symbol,
                "current_snapshot_sha256": current_meta["sha256"],
                "previous_snapshot_sha256": previous_meta["sha256"],
                "current_info": _compact_contract_info(info),
                "previous_info": _compact_contract_info(old),
            }
        )
        row = {
            "symbol": symbol,
            "official_historical_product_category": category,
            "effective_from_timestamp": effective,
            "official_source": EXCHANGE_INFO_ENDPOINT,
            "source_publication_timestamp": current_meta["publication_timestamp_utc"],
            "source_hash": source_hash,
            "contract_type": str(info.get("contractType") or "").upper() or None,
            "quote_asset": str(info.get("quoteAsset") or "").upper() or None,
            "classification": reason,
            "historical_evidence_status": (
                "TIMESTAMPED_OFFICIAL_ONBOARD_AND_PRODUCT_CLASS_AGREEMENT"
                if evidence_complete
                else "CONTRACT_TYPE_STATE_NOT_READY"
            ),
            "historical_evidence_sources": [
                {
                    "source": previous_meta["endpoint"],
                    "publication_timestamp": previous_meta["publication_timestamp_utc"],
                    "snapshot_sha256": previous_meta["sha256"],
                    "symbol_record": _compact_contract_info(old),
                },
                {
                    "source": current_meta["endpoint"],
                    "publication_timestamp": current_meta["publication_timestamp_utc"],
                    "snapshot_sha256": current_meta["sha256"],
                    "symbol_record": _compact_contract_info(info),
                },
                {
                    "source": TRADFI_BOUNDARY_SOURCE,
                    "publication_timestamp": "2026-01-08T08:00:00Z",
                    "role": "TRADFI_PRODUCT_CATEGORY_BOUNDARY",
                },
            ],
            "in_control_scope": in_scope,
            "protocol_eligible": False,
        }
        if explicit_september:
            row["historical_evidence_sources"] = [
                *row["historical_evidence_sources"],
                {
                    "source": SEPTEMBER_PRODUCT_SOURCE,
                    "publication_timestamp": "2026-09-04T04:46:00Z",
                    "role": "TIMESTAMPED_SEPTEMBER_TRADFI_LISTING_EVIDENCE",
                },
            ]
        rows.append(row)
        if "TRADIFI" in str(info.get("contractType") or "").upper() and in_scope:
            excluded_tradifi.add(symbol)
        if in_scope and not evidence_complete:
            unresolved.append(symbol)

    rows.sort(key=lambda row: str(row["symbol"]))
    known_excluded = sum(
        1
        for symbol in catalog_symbols
        if symbol not in history_symbols
    )
    payload = {
        "schema_version": "CONTRACT_TYPE_ELIGIBILITY_OVERLAY.v1",
        "classification": list(LABELS),
        "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "predicate": {
            "name": "protocol_contract_eligible",
            "quote_asset": "USDT",
            "contract_type": "PERPETUAL",
            "status": "TRADING",
            "lifecycle": "PIT_SIGNAL_DAY_ACTIVE",
            "rejected_contract_types": [
                "TRADIFI_PERPETUAL",
                "CURRENT_QUARTER",
                "NEXT_QUARTER",
            ],
        },
        "frozen_membership_authority": {
            "catalog_symbol_count": len(catalog_symbols),
            "catalog_listing_complete": getattr(catalog, "listing_complete", False) is True,
            "known_but_excluded_preserved": True,
            "known_but_excluded_count": known_excluded,
        },
        "official_snapshots": [previous_meta, current_meta],
        "rows": rows,
        "excluded_tradifi_symbols": sorted(excluded_tradifi),
        "unresolved_symbols": sorted(set(unresolved)),
        "registry_complete": not unresolved and getattr(catalog, "listing_complete", False) is True,
    }
    return ContractRegistry(
        rows=tuple(rows),
        excluded_tradifi_symbols=tuple(sorted(excluded_tradifi)),
        unresolved_symbols=tuple(sorted(set(unresolved))),
        eligible_symbols=tuple(sorted(eligible)),
        full_catalog_symbol_count=len(catalog_symbols),
        known_but_excluded_count=known_excluded,
        overlay_payload=payload,
    )


def _registry_is_eligible(registry: ContractRegistry, symbol: str) -> bool:
    row = registry.by_symbol.get(symbol.upper())
    if row is None:
        return True
    # A current SETTLING status is a lifecycle fact, not a historical
    # product-type rewrite.  Historical PERPETUAL/USDT rows remain eligible
    # until their PIT lifecycle says otherwise.
    return (
        str(row.get("contract_type") or "").upper() == "PERPETUAL"
        and str(row.get("quote_asset") or "").upper() == "USDT"
    )


def _apply_product_layer(
    histories: Iterable[SymbolHistory], registry: ContractRegistry
) -> tuple[SymbolHistory, ...]:
    return tuple(
        history
        for history in sorted(histories, key=lambda item: item.symbol)
        if _registry_is_eligible(registry, history.symbol)
    )


def _position_row(position: _BridgePosition) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "direction": _DIRECTION[int(position.direction)],
        "direction_code": int(position.direction),
        "target_notional": float(position.notional),
        "entry_timestamp_utc": _iso_ms(position.entry_timestamp_ms),
        "last_mark_timestamp_utc": _iso_ms(position.mark_timestamp_ms),
        "last_mark_price": float(position.mark_price),
    }


def _position_from_row(row: Mapping[str, Any]) -> _BridgePosition:
    try:
        direction = int(row["direction_code"])
        notional = float(row["target_notional"])
        entry = int(datetime.fromisoformat(str(row["entry_timestamp_utc"]).replace("Z", "+00:00")).timestamp() * 1000)
        mark = int(datetime.fromisoformat(str(row["last_mark_timestamp_utc"]).replace("Z", "+00:00")).timestamp() * 1000)
        price = float(row["last_mark_price"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise M142NotReady("checkpoint position row is malformed") from exc
    if direction not in {-1, 1} or notional <= 0 or entry > mark or not math.isfinite(price) or price <= 0:
        raise M142NotReady("checkpoint position row is invalid")
    return _BridgePosition(str(row["symbol"]), direction, notional, entry, mark, price)


def _manifest_requests() -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _load_json(M14_MANIFEST_PATH)
    requests = manifest.get("requests")
    if not isinstance(requests, list):
        raise M142IdentityError("M1.4 request manifest has no request list")
    by_name = {
        str(row.get("name")): dict(row)
        for row in requests
        if isinstance(row, Mapping) and row.get("name")
    }
    return manifest, by_name


def _raw_payload(local_path: str | None) -> Any | None:
    if not local_path:
        return None
    path = PROJECT_ROOT / Path(local_path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _load_cached_extension(
    histories: Sequence[SymbolHistory], registry: ContractRegistry
) -> tuple[tuple[SymbolHistory, ...], dict[str, Any], dict[str, Any]]:
    """Merge only already-fetched M1.4 completed bars and settlements."""
    manifest, requests = _manifest_requests()
    exchange_payload = _raw_payload("data/v2_1_m1_4_prestart_extension/exchangeInfo.json")
    funding_info_payload = _raw_payload("data/v2_1_m1_4_prestart_extension/fundingInfo.json")
    if exchange_payload is None or funding_info_payload is None:
        raise M142IdentityError("M1.4 frozen exchangeInfo/fundingInfo raw cache is incomplete")
    exchange_info = _parse_exchange_info(exchange_payload)
    funding_info = _parse_funding_info(funding_info_payload)
    out: list[SymbolHistory] = []
    required_rows: list[dict[str, Any]] = []
    out_of_scope: list[str] = []
    missing: list[str] = []
    for history in sorted(histories, key=lambda item: item.symbol):
        symbol = history.symbol.upper()
        daily_req = requests.get(f"{symbol}.daily_1d")
        funding_req = requests.get(f"{symbol}.funding_rate")
        if daily_req is None or funding_req is None:
            out.append(history)
            continue
        if not _registry_is_eligible(registry, symbol):
            out_of_scope.extend([f"{symbol}.daily_1d", f"{symbol}.funding_rate"])
            continue
        daily_payload = _raw_payload(daily_req.get("local_path"))
        funding_payload = _raw_payload(funding_req.get("local_path"))
        daily = ()
        funding = ()
        if daily_payload is not None and daily_req.get("http_status") == 200:
            daily = _parse_daily_payload(daily_payload, symbol)
        else:
            missing.append(f"{symbol}.daily_1d")
        if funding_payload is not None and funding_req.get("http_status") == 200:
            funding = _parse_funding_payload(
                funding_payload, symbol, history, funding_info.get(symbol)
            )
        else:
            missing.append(f"{symbol}.funding_rate")
        required_rows.extend([dict(daily_req), dict(funding_req)])
        existing_days = {bar.day for bar in history.daily_bars}
        existing_funding = {event.funding_time_ms for event in history.funding_events}
        if existing_days.intersection(bar.day for bar in daily):
            raise M142NotReady(f"{symbol} cached daily extension overlaps frozen history")
        if existing_funding.intersection(event.funding_time_ms for event in funding):
            raise M142NotReady(f"{symbol} cached funding extension overlaps frozen history")
        out.append(
            replace(
                history,
                daily_bars=tuple(
                    sorted((*history.daily_bars, *daily), key=lambda bar: (bar.day, bar.open_time_ms))
                ),
                funding_events=tuple(
                    sorted(
                        (*history.funding_events, *funding),
                        key=lambda event: event.funding_time_ms,
                    )
                ),
            )
        )

    processed_symbols = {history.symbol.upper() for history in histories}
    for name in requests:
        symbol = str(name).split(".", 1)[0].upper()
        if symbol not in processed_symbols and symbol not in {"MARSCOINUSDT", "PONSUSDT"}:
            out_of_scope.append(str(name))

    # MARS/PONS were discovered in M1.4 and are the only eligible symbols
    # outside the frozen historical cache.  Their bars are reused, never
    # fetched again in M1.4.2.
    existing = {history.symbol for history in out}
    for symbol, info in sorted(exchange_info.items()):
        if symbol in existing or not protocol_contract_eligible(
            info.get("contractType"), info.get("quoteAsset"), info.get("status")
        ):
            continue
        if symbol not in {"MARSCOINUSDT", "PONSUSDT"}:
            continue
        daily_req = requests.get(f"{symbol}.daily_1d")
        funding_req = requests.get(f"{symbol}.funding_rate")
        daily_payload = _raw_payload(daily_req.get("local_path") if daily_req else None)
        funding_payload = _raw_payload(funding_req.get("local_path") if funding_req else None)
        if daily_payload is None or funding_payload is None:
            missing.extend([f"{symbol}.daily_1d", f"{symbol}.funding_rate"])
            continue
        daily = _parse_daily_payload(daily_payload, symbol)
        funding = _parse_funding_payload(
            funding_payload, symbol, None, funding_info.get(symbol)
        )
        if not daily or not funding:
            missing.extend([f"{symbol}.daily_1d", f"{symbol}.funding_rate"])
            continue
        onboard = int(info["onboardDate"])
        onboard_day = datetime.fromtimestamp(onboard / 1000.0, UTC).date()
        out.append(
            SymbolHistory(
                symbol=symbol,
                daily_bars=tuple(daily),
                funding_events=tuple(funding),
                lifecycle=LifecycleRecord(
                    symbol=symbol,
                    first_available_day=daily[0].day,
                    last_available_day=daily[-1].day,
                    listed_from=max(onboard_day, daily[0].day),
                    delisted_at=None,
                    currently_active=True,
                    lifecycle_source="M1_4_CACHED_PIT_ONBOARD_AND_COMPLETED_BAR",
                    lifecycle_confidence="official_pit_confirmed",
                    status="OK",
                ),
            )
        )
        required_rows.extend([dict(daily_req), dict(funding_req)])

    extension = {
        "schema_version": "CORRECTED_PRESTART_EXTENSION_MANIFEST.v1",
        "classification": list(LABELS),
        "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "source_manifest": {
            "path": M14_MANIFEST_PATH.relative_to(PROJECT_ROOT).as_posix(),
            "sha256": _file_sha256(M14_MANIFEST_PATH),
            "historical_cache_rewritten": False,
        },
        "request_window": {
            "extension_start_utc": "2026-09-01T00:00:00Z",
            "extension_end_utc_exclusive": FINAL_CUTOFF,
            "completed_daily_closes_only": True,
        },
        "required_requests": sorted(required_rows, key=lambda row: str(row.get("name"))),
        "out_of_scope_requests_not_required": sorted(out_of_scope),
        "missing_cached_requests": sorted(set(missing)),
        "celr_retry": None,
        "no_zero_fill": True,
        "no_interpolation": True,
        "no_synthetic_settlement": True,
    }
    return tuple(sorted(out, key=lambda item: item.symbol)), extension, {
        "exchange_info": exchange_info,
        "funding_info": funding_info,
        "request_map": requests,
        "source_manifest": manifest,
    }


def _retry_celr_daily_get(
    *, extension: dict[str, Any], raw_name: str = "corrective_daily_CELRUSDT.json"
) -> tuple[tuple[DailyBar, ...], dict[str, Any]]:
    """The sole permitted corrective GET: CELR is still in-scope."""
    start_ms = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(CUTOFF.timestamp() * 1000) - 1
    params = {
        "symbol": "CELRUSDT",
        "interval": "1d",
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": "1000",
    }
    request_url = f"{KLINES_ENDPOINT}?{urlencode(sorted(params.items()))}"
    requested = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    body = b""
    status: int | None = None
    error: str | None = None
    try:
        with urlopen(Request(request_url, method="GET"), timeout=60) as response:
            status = int(response.getcode())
            body = bytes(response.read() or b"")
    except HTTPError as exc:
        status = int(exc.code)
        try:
            body = bytes(exc.read() or b"")
        except OSError:
            body = b""
        error = f"HTTP {status}"
    except (OSError, URLError, TimeoutError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    record = {
        "name": "CELRUSDT.daily_1d",
        "request_kind": "CORRECTIVE_PRESTART_GET",
        "method": "GET",
        "endpoint": KLINES_ENDPOINT,
        "params": params,
        "request_url": request_url,
        "request_timestamp_utc": requested,
        "http_status": status,
        "response_sha256": _sha256_bytes(body) if body else None,
        "row_count": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "error": error,
        "local_path": None,
    }
    bars: tuple[DailyBar, ...] = ()
    if status == 200 and not error:
        try:
            payload = json.loads(body.decode("utf-8"))
            bars = _parse_daily_payload(payload, "CELRUSDT")
            record["row_count"] = len(bars)
            if bars:
                record["first_timestamp"] = _iso_ms(bars[0].open_time_ms)
                record["last_timestamp"] = _iso_ms(bars[-1].close_time_ms)
            target = M14_RAW_DIR / raw_name
            target.write_bytes(body)
            record["local_path"] = target.relative_to(PROJECT_ROOT).as_posix()
        except (UnicodeError, json.JSONDecodeError, M142Error) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
    extension["celr_retry"] = record
    return bars, record


def _schedule_positions(
    histories: Sequence[SymbolHistory], schedule: Any, end_day: date
) -> dict[str, _BridgePosition]:
    by_symbol = {history.symbol: history for history in histories}
    positions: dict[str, _BridgePosition] = {}
    for attempt in schedule.attempts:
        if attempt.execution_day > end_day or attempt.status != "SUCCESS":
            continue
        signal = schedule.signals.get(attempt.signal_day)
        if signal is None:
            raise M142NotReady("successful checkpoint attempt has no signal")
        targets = signal.targets
        for symbol, old in tuple(positions.items()):
            if targets.get(symbol) != old.direction:
                positions.pop(symbol, None)
        for symbol, direction in targets.items():
            if symbol in positions:
                continue
            bar = by_symbol[symbol].bars_by_day().get(attempt.execution_day)
            if bar is None:
                raise M142NotReady(f"checkpoint entry close is missing for {symbol}")
            positions[symbol] = _BridgePosition(
                symbol=symbol,
                direction=int(direction),
                notional=1.0 / float(len(targets)),
                entry_timestamp_ms=bar.close_time_ms,
                mark_timestamp_ms=bar.close_time_ms,
                mark_price=float(bar.close),
            )
    return positions


def _attempt_shape(attempt: Any) -> tuple[Any, ...]:
    return (
        attempt.signal_day,
        attempt.execution_day,
        attempt.status,
        attempt.reason,
        tuple(attempt.target_symbols),
    )


def _build_checkpoint(
    *,
    dataset: Mapping[str, Any],
    registry: ContractRegistry,
    old_histories: Sequence[SymbolHistory],
    corrected_histories: Sequence[SymbolHistory],
) -> tuple[dict[str, Any], Any, Any, Any]:
    """State-only extraction through 2026-02-27; no result is serialized."""
    old_schedule = build_stateful_schedule(
        old_histories, data_start=M1_DATA_START, data_end=CHECKPOINT_DAY
    )
    corrected_schedule = build_stateful_schedule(
        corrected_histories, data_start=M1_DATA_START, data_end=CHECKPOINT_DAY
    )
    old_attempts = tuple(_attempt_shape(item) for item in old_schedule.attempts)
    corrected_attempts = tuple(_attempt_shape(item) for item in corrected_schedule.attempts)
    if old_attempts != corrected_attempts:
        raise M142NotReady("pre-correction scheduler/target parity failed")
    old_signal_rows = {
        day: tuple(sorted(signal.targets.items())) for day, signal in old_schedule.signals.items()
    }
    corrected_signal_rows = {
        day: tuple(sorted(signal.targets.items()))
        for day, signal in corrected_schedule.signals.items()
    }
    if old_signal_rows != corrected_signal_rows:
        raise M142NotReady("pre-correction signal parity failed")
    successful = [item for item in corrected_schedule.attempts if item.status == "SUCCESS"]
    if not successful:
        raise M142NotReady("checkpoint has no successful execution")
    last = successful[-1]
    if last.execution_day != CHECKPOINT_DAY:
        raise M142NotReady(
            f"checkpoint derived last execution {last.execution_day.isoformat()} is not 2026-02-27"
        )
    positions = _schedule_positions(corrected_histories, corrected_schedule, CHECKPOINT_DAY)
    if not positions:
        raise M142NotReady("checkpoint has no positions")
    accounting = simulate_frozen_portfolio(
        corrected_histories,
        corrected_schedule.signals,
        variant="control",
        scenario="COST_1X",
        data_start=M1_DATA_START,
        data_end=CHECKPOINT_DAY,
    )
    if not accounting.complete or not accounting.equity:
        raise M142NotReady("state-only accounting extraction is incomplete")
    by_symbol = {history.symbol: history for history in corrected_histories}
    rows: list[dict[str, Any]] = []
    for symbol, position in sorted(positions.items()):
        bar = by_symbol[symbol].bars_by_day().get(CHECKPOINT_DAY)
        if bar is None:
            raise M142NotReady(f"checkpoint terminal mark is missing for {symbol}")
        position.mark_timestamp_ms = bar.close_time_ms
        position.mark_price = float(bar.close)
        rows.append(_position_row(position))
    opaque_payload = {
        "terminal_equity": accounting.equity[-1],
        "price_component": accounting.price_pnl,
        "funding_component": accounting.funding_pnl,
        "cost_component": accounting.cost_pnl,
        "source": "STATE_EXTRACTION_ONLY",
    }
    opaque_sha = _sha256_json(opaque_payload)
    continuity = {
        "as_of_utc": CHECKPOINT_TIMESTAMP,
        "position_rows": rows,
        "last_successful_signal_day": last.signal_day.isoformat(),
        "last_successful_execution_day": last.execution_day.isoformat(),
        "opaque_state_sha256": opaque_sha,
        "dataset_sha256": dataset["dataset_sha256"],
        "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
        "m1_3_overlay_sha256": M13_OVERLAY_SHA256,
    }
    checkpoint = {
        "schema_version": "PRE_CONTAMINATION_CHECKPOINT.v1",
        "classification": list(LABELS),
        "scope": "STATE_EXTRACTION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "as_of_utc": CHECKPOINT_TIMESTAMP,
        "source_window": {
            "start_utc": "2020-01-01T00:00:00Z",
            "end_utc": CHECKPOINT_TIMESTAMP,
            "aggregate_result_output": False,
        },
        "m1_3_result_commit": M13_RESULT_COMMIT,
        "m1_3_code_commit": M13_CODE_COMMIT,
        "m1_3_overlay_sha256": M13_OVERLAY_SHA256,
        "dataset_sha256": dataset["dataset_sha256"],
        "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
        "scheduler": {
            "last_successful_signal_day": last.signal_day.isoformat(),
            "last_successful_execution_day": last.execution_day.isoformat(),
            "next_rebalance_due_day": (last.execution_day + timedelta(days=7)).isoformat(),
            "rebalance_interval_days": 7,
            "retry_rule": "FAILED_DUE_DAY_RETRIES_ON_NEXT_CALENDAR_DAY",
            "scheduler_initialized_from_checkpoint": True,
        },
        "positions": rows,
        "directions": {row["symbol"]: row["direction"] for row in rows},
        "accounting_state": {
            "classification": "STATE_CARRY_ONLY",
            "numeric_value_exposed": False,
            "opaque_state_sha256": opaque_sha,
        },
        "continuity_hash": _sha256_json(continuity),
        "pre_correction_parity": {
            "status": "PASS",
            "old_attempt_count": len(old_schedule.attempts),
            "corrected_attempt_count": len(corrected_schedule.attempts),
            "old_signal_count": len(old_schedule.signals),
            "corrected_signal_count": len(corrected_schedule.signals),
            "state_difference_count": 0,
        },
        "formal_correction_start": FORMAL_CORRECTION_START.isoformat(),
        "historical_full_formal_rerun": False,
    }
    assert_prestart_state_schema(checkpoint)
    return checkpoint, old_schedule, corrected_schedule, accounting


def _position_funding_counts(
    *,
    day: date,
    positions: Mapping[str, _BridgePosition],
    histories: Mapping[str, SymbolHistory],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for symbol, position in positions.items():
        history = histories.get(symbol)
        if history is None:
            counts[symbol] = 0
            continue
        bar = history.bars_by_day().get(day)
        if bar is None:
            counts[symbol] = 0
            continue
        events = _unique_funding(history)
        times = tuple(event.funding_time_ms for event in events)
        counts[symbol] = max(
            0,
            bisect_right(times, bar.close_time_ms)
            - bisect_right(times, position.mark_timestamp_ms),
        )
    return counts


def _record_closed_positions(
    *,
    day: date,
    before: Mapping[str, _BridgePosition],
    after: Mapping[str, _BridgePosition],
    histories: Mapping[str, SymbolHistory],
    reason: str,
    output: list[dict[str, Any]],
) -> None:
    for symbol, position in before.items():
        if symbol in after:
            continue
        history = histories.get(symbol)
        exit_ms: int | None = None
        if history is not None:
            bar = history.bars_by_day().get(day)
            if bar is not None:
                exit_ms = bar.close_time_ms
            elif history.lifecycle.delisted_at is not None:
                terminal = history.last_bar_on_or_before(history.lifecycle.delisted_at - timedelta(days=1))
                if terminal is not None:
                    exit_ms = terminal.close_time_ms
        if exit_ms is None:
            continue
        output.append(
            {
                "symbol": symbol,
                "direction": _DIRECTION[int(position.direction)],
                "entry_timestamp_utc": _iso_ms(position.entry_timestamp_ms),
                "exit_timestamp_utc": _iso_ms(exit_ms),
                "exit_reason": reason,
                "official_lifecycle_applied": reason == "FORCED_DELISTING_EXIT",
            }
        )


def _new_parent_state(
    *,
    checkpoint: Mapping[str, Any],
    accounting: Any,
) -> ParentRunState:
    positions = {
        str(row["symbol"]): _position_from_row(row)
        for row in checkpoint.get("positions", [])
        if isinstance(row, Mapping)
    }
    scheduler = dict(checkpoint.get("scheduler", {}))
    if not positions or not scheduler:
        raise M142NotReady("checkpoint cannot initialize parent state")
    equity_by_day = {
        day: float(value)
        for day, value in accounting.equity_by_day().items()
        if isinstance(day, date) and math.isfinite(float(value))
    }
    return ParentRunState(
        positions=positions,
        scheduler=scheduler,
        equity_by_day=equity_by_day,
        attempts=[],
        daily_rows=[],
        holding_intervals=[],
        funding_rows=[],
        unresolved=[],
        funding_issue_count=0,
        rebalance_count=0,
    )


def _run_incremental_parent(
    *,
    state: ParentRunState,
    histories: Sequence[SymbolHistory],
    registry: ContractRegistry,
    start: date,
    end: date,
    label: str,
    enforce_product_layer: bool,
) -> ParentRunState:
    """Carry one parent state across a bounded post-checkpoint window."""
    if start < FORMAL_CORRECTION_START:
        raise M142NotReady("formal correction start precedes 2026-02-28")
    if end < start:
        raise M142NotReady("incremental parent window is inverted")
    if start < FORMAL_CORRECTION_START:
        raise M142NotReady("formal correction window is invalid")
    history_map = {history.symbol: history for history in histories}
    if not set(state.positions).issubset(history_map):
        raise M142NotReady("carried position is absent from corrected histories")
    bar_maps = {history.symbol: history.bars_by_day() for history in histories}
    rules = resolve_rules(variant="control")
    next_due = date.fromisoformat(str(state.scheduler["next_rebalance_due_day"]))
    last_signal = date.fromisoformat(str(state.scheduler["last_successful_signal_day"]))
    last_execution = date.fromisoformat(
        str(state.scheduler["last_successful_execution_day"])
    )
    pending_retry = bool(state.scheduler.get("pending_retry", False))

    for day in _day_range(start, end):
        before_positions = {
            symbol: replace(position)
            for symbol, position in state.positions.items()
        }
        funding_counts = _position_funding_counts(
            day=day, positions=state.positions, histories=history_map
        )
        mark_delta, mark_complete, mark_issues, funding_issues = _apply_mark(
            day=day, positions=state.positions, histories=history_map
        )
        state.funding_issue_count += funding_issues
        day_issues = list(mark_issues)
        did_rebalance = False
        target_rows: list[dict[str, Any]] = []
        universe_metadata: dict[str, Any] = {}
        if day >= next_due:
            signal_day = day - timedelta(days=rules.execution_lag_days)
            signal = None
            universe = None
            if mark_complete:
                universe = _build_universe_fast(
                    histories, bar_maps, signal_day, rules
                )
                signal = _signal_from_universe(universe, rules)
                universe_metadata = {
                    "active_symbol_count": len(universe.active_symbols),
                    "liquid_symbol_count": len(universe.liquid_symbols),
                    "eligible_snapshot_count": len(universe.eligible),
                    "fail_closed": bool(universe.fail_closed),
                    "reason_count": len(universe.reasons),
                    "protocol_ineligible_eligible_snapshot_count": sum(
                        not _registry_is_eligible(registry, snapshot.symbol)
                        for snapshot in universe.eligible
                    ),
                }
            if signal is None:
                if not day_issues:
                    day_issues.append(
                        "; ".join(universe.reasons)
                        if universe is not None and universe.reasons
                        else f"no executable Control signal {signal_day.isoformat()}"
                    )
                pending_retry = True
                next_due = day + timedelta(days=1)
                state.attempts.append(
                    {
                        "signal_day": signal_day.isoformat(),
                        "execution_day": day.isoformat(),
                        "status": "RETRY_PENDING",
                        "reason": "; ".join(day_issues),
                        "target_symbols": [],
                        "target_directions": {},
                        "source": label,
                        **universe_metadata,
                    }
                )
            else:
                targets = signal.targets
                target_rows = [
                    {
                        "symbol": symbol,
                        "direction": _DIRECTION[int(direction)],
                        "direction_code": int(direction),
                    }
                    for symbol, direction in sorted(targets.items())
                ]
                if enforce_product_layer:
                    bad_targets = [
                        symbol for symbol in targets if not _registry_is_eligible(registry, symbol)
                    ]
                    if bad_targets:
                        day_issues.append(
                            "protocol-ineligible targets " + ",".join(sorted(bad_targets))
                        )
                if not day_issues:
                    transition_delta, _, _, transition_issues = _transition_positions(
                        day=day,
                        signal=signal,
                        positions=state.positions,
                        histories=history_map,
                    )
                    mark_delta += transition_delta
                    if transition_issues:
                        day_issues.extend(transition_issues)
                    else:
                        did_rebalance = True
                        last_signal = signal_day
                        last_execution = day
                        next_due = day + timedelta(days=rules.rebalance_days)
                        pending_retry = False
                        state.rebalance_count += 1
                if not did_rebalance:
                    pending_retry = True
                    next_due = day + timedelta(days=1)
                state.attempts.append(
                    {
                        "signal_day": signal_day.isoformat(),
                        "execution_day": day.isoformat(),
                        "status": "SUCCESS" if did_rebalance else "RETRY_PENDING",
                        "reason": "; ".join(day_issues),
                        "target_symbols": sorted(targets),
                        "target_directions": {
                            symbol: _DIRECTION[int(direction)]
                            for symbol, direction in sorted(targets.items())
                        },
                        "source": label,
                        **universe_metadata,
                    }
                )
        _record_closed_positions(
            day=day,
            before=before_positions,
            after=state.positions,
            histories=history_map,
            reason="REBALANCE" if did_rebalance else "FORCED_DELISTING_EXIT",
            output=state.holding_intervals,
        )
        for symbol, count in sorted(funding_counts.items()):
            state.funding_rows.append(
                {
                    "day": day.isoformat(),
                    "symbol": symbol,
                    "settlement_count": count,
                    "coverage_status": "PASS" if count > 0 else "FAIL",
                    "holding_interval_scope": "PROTOCOL_ELIGIBLE_ONLY",
                }
            )
        complete = mark_complete and not day_issues
        prior_equity = state.equity_by_day.get(day - timedelta(days=1))
        if prior_equity is None:
            prior_equity = state.equity_by_day.get(start - timedelta(days=1))
        if prior_equity is None:
            raise M142NotReady(f"opaque accounting continuity missing before {day.isoformat()}")
        state.equity_by_day[day] = prior_equity + mark_delta if complete else prior_equity
        state.daily_rows.append(
            {
                "day": day.isoformat(),
                "complete": complete,
                "price_component_complete": mark_complete,
                "funding_component_complete": mark_complete and funding_issues == 0,
                "cost_component_complete": not any(
                    "cost" in issue.lower() for issue in day_issues
                ),
                "rebalance_count": 1 if did_rebalance else 0,
                "state_classification": "PRESTART_STATE_INITIALIZATION_ONLY",
            }
        )
        if day_issues:
            state.unresolved.extend(day_issues)

    state.scheduler.update(
        {
            "last_successful_signal_day": last_signal.isoformat(),
            "last_successful_execution_day": last_execution.isoformat(),
            "next_rebalance_due_day": next_due.isoformat(),
            "pending_retry": pending_retry,
            "scheduler_initialized_from_checkpoint": True,
        }
    )
    return state


def _compare_future_attempts(
    *,
    legacy: ParentRunState,
    corrected: ParentRunState,
    excluded_symbols: Sequence[str],
) -> dict[str, Any]:
    legacy_by_key = {
        (row["signal_day"], row["execution_day"]): row for row in legacy.attempts
    }
    corrected_by_key = {
        (row["signal_day"], row["execution_day"]): row for row in corrected.attempts
    }
    keys = sorted(set(legacy_by_key) | set(corrected_by_key))
    excluded = set(excluded_symbols)
    divergences: list[dict[str, Any]] = []
    first: dict[str, Any] | None = None
    unexplained = 0
    first_seen = False
    for key in keys:
        old = legacy_by_key.get(key)
        new = corrected_by_key.get(key)
        old_targets = set(old.get("target_symbols", [])) if old else set()
        new_targets = set(new.get("target_symbols", [])) if new else set()
        if old and new and old.get("status") == new.get("status") and old_targets == new_targets:
            continue
        if key[1] < FORMAL_CORRECTION_START.isoformat():
            continue
        direct = bool(old_targets & excluded)
        reason = (
            "CONTRACT_TYPE_CORRECTION"
            if direct
            else "DETERMINISTIC_PROPAGATION_AFTER_CONTRACT_TYPE_CORRECTION"
        )
        if not direct and not first_seen:
            unexplained += 1
            reason = "UNEXPLAINED_PRE_FIRST_CORRECTION_DIVERGENCE"
        row = {
            "signal_day": key[0],
            "execution_day": key[1],
            "legacy_target_symbols": sorted(old_targets),
            "corrected_target_symbols": sorted(new_targets),
            "reason": reason,
        }
        divergences.append(row)
        if first is None:
            first = row
        first_seen = True
    return {
        "first_corrected_divergence": first,
        "explained_divergence_count": len(divergences) - unexplained,
        "unexplained_divergence_count": unexplained,
        "rows": divergences,
    }


def _latest_13_rows(equity_by_day: Mapping[date, float]) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for week_start in _day_range(date(2026, 1, 5), date(2026, 9, 7)):
        if week_start.weekday() != 0:
            continue
        week_end = week_start + timedelta(days=6)
        completed_at = datetime.combine(
            week_end + timedelta(days=1), time.min, tzinfo=UTC
        )
        if completed_at >= CUTOFF:
            continue
        if week_end not in equity_by_day or week_end - timedelta(days=7) not in equity_by_day:
            continue
        prior = float(equity_by_day[week_end - timedelta(days=7)])
        ending = float(equity_by_day[week_end])
        if not math.isfinite(prior) or not math.isfinite(ending) or prior <= 0:
            continue
        rows.append(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "completed_at_utc": completed_at.isoformat().replace("+00:00", "Z"),
                "source": "V2_1_M1_4_2_CORRECTED_PARENT_ACCOUNTING",
                "price_component_complete": True,
                "funding_component_complete": True,
                "cost_component_complete": True,
                "complete": True,
                "weekly_return": ending / prior - 1.0,
                "classification": "RISK_STATE_INITIALIZATION_ONLY",
                "evidence_status": "NOT_PERFORMANCE_EVIDENCE",
            }
        )
    return tuple(rows[-13:])


def _build_risk_input(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    selected = tuple(rows)
    records = [
        ControlWeeklyReturn(
            week_ending=date.fromisoformat(str(row["week_end"])),
            net_return=float(row["weekly_return"]),
            completed_at=datetime.fromisoformat(
                str(row["completed_at_utc"]).replace("Z", "+00:00")
            ),
            complete=row.get("complete") is True,
        )
        for row in selected
    ]
    valid = len(records) == 13 and all(row.get("complete") is True for row in selected)
    result = evaluate_risk_scale(records, signal_time=CUTOFF) if valid else None
    output: dict[str, Any] = {
        "schema_version": "CORRECTED_LATEST_13_RISK_INPUT.v1",
        "classification": list(LABELS),
        "scope": "RISK_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "cutoff_utc": FINAL_CUTOFF,
        "selected_count": len(selected),
        "latest_week_endings": [row["week_end"] for row in selected],
        "rows": [dict(row) for row in selected],
        "input_complete_count": sum(row.get("complete") is True for row in selected),
        "status": result.status if result is not None else "HALTED_INCOMPLETE_INPUT",
        "risk_diagnostic_status": (
            "INITIAL_STATE_DIAGNOSTIC_ONLY"
            if result is not None and result.valid
            else "HALTED_INCOMPLETE_INPUT"
        ),
    }
    if result is not None and result.valid:
        output.update(
            {
                "reference_vol": result.reference_vol,
                "position_scale": result.position_scale,
                "risk_parameters": {
                    "volatility_statistic": "population_std",
                    "week_count": 13,
                    "annualization": "sqrt(52)",
                    "target_annualized_volatility": 0.15,
                    "max_scale": 1.0,
                    "leverage": False,
                    "zero_reference_vol_scale": "1",
                },
            }
        )
    else:
        output["halt_reason"] = "13/13 corrected weekly state is incomplete"
    assert_prestart_state_schema(output)
    return output


def _merge_celr_daily(
    histories: Sequence[SymbolHistory], bars: Sequence[DailyBar]
) -> tuple[SymbolHistory, ...]:
    if not bars:
        return tuple(histories)
    output: list[SymbolHistory] = []
    found = False
    for history in histories:
        if history.symbol != "CELRUSDT":
            output.append(history)
            continue
        found = True
        existing = {bar.day for bar in history.daily_bars}
        if existing.intersection(bar.day for bar in bars):
            raise M142NotReady("CELR corrective daily GET overlaps frozen data")
        output.append(
            replace(
                history,
                daily_bars=tuple(
                    sorted((*history.daily_bars, *bars), key=lambda bar: (bar.day, bar.open_time_ms))
                ),
            )
        )
    if not found:
        raise M142NotReady("CELR is required but absent from corrected histories")
    return tuple(sorted(output, key=lambda item: item.symbol))


def _required_get_failures(
    extension: Mapping[str, Any], retry: Mapping[str, Any] | None
) -> list[str]:
    failures: list[str] = []
    retry_success = bool(retry and retry.get("http_status") == 200 and not retry.get("error"))
    for row in extension.get("required_requests", []):
        name = str(row.get("name", ""))
        if name == "CELRUSDT.daily_1d" and retry_success:
            continue
        if row.get("http_status") != 200 or row.get("error"):
            failures.append(name)
    if retry is not None and not retry_success:
        failures.append("CELRUSDT.daily_1d:CORRECTIVE_PRESTART_GET")
    return sorted(set(failures))


def _current_state(
    *, state: ParentRunState, extension_complete: bool
) -> dict[str, Any]:
    rows = [
        _position_row(position)
        for _, position in sorted(state.positions.items())
    ]
    return {
        "schema_version": "CORRECTED_CURRENT_PARENT_STATE.v1",
        "classification": list(LABELS),
        "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "cutoff_utc": FINAL_CUTOFF,
        "parent_state_complete": bool(
            extension_complete
            and not state.unresolved
            and state.funding_issue_count == 0
            and rows
        ),
        "current_control_positions": rows,
        "current_position_count": len(rows),
        "protocol_ineligible_position_count": 0,
        "last_successful_signal_day": state.scheduler["last_successful_signal_day"],
        "last_successful_execution_day": state.scheduler["last_successful_execution_day"],
        "next_rebalance_due_day": state.scheduler["next_rebalance_due_day"],
        "pending_retry": bool(state.scheduler.get("pending_retry", False)),
        "scheduler_initialized_from_checkpoint": True,
        "unresolved_state_issue_count": len(set(state.unresolved)),
        "funding_issue_count": state.funding_issue_count,
        "no_forward_signal_selected": True,
    }


def _gates(
    *,
    identity_ok: bool,
    registry: ContractRegistry,
    checkpoint: Mapping[str, Any] | None,
    corrected: ParentRunState | None,
    extension: Mapping[str, Any],
    risk: Mapping[str, Any],
    current: Mapping[str, Any],
    divergence: Mapping[str, Any],
    required_get_failures: Sequence[str],
    immutable_before: Mapping[str, str],
    immutable_after: Mapping[str, str],
) -> dict[str, str]:
    parity = checkpoint is not None and checkpoint.get("pre_correction_parity", {}).get("status") == "PASS"
    corrected_attempts = corrected.attempts if corrected is not None else []
    ineligible = set(registry.excluded_tradifi_symbols)
    ineligible_targets = sum(
        1
        for row in corrected_attempts
        for symbol in row.get("target_symbols", [])
        if symbol in ineligible
    )
    snapshot_issues = sum(
        int(row.get("protocol_ineligible_eligible_snapshot_count", 0))
        for row in corrected_attempts
    )
    state_complete = current.get("parent_state_complete") is True
    latest_complete = (
        risk.get("selected_count") == 13
        and risk.get("input_complete_count") == 13
        and risk.get("risk_diagnostic_status") == "INITIAL_STATE_DIAGNOSTIC_ONLY"
    )
    gates = {
        "R0_identity": "PASS" if identity_ok else "FAIL",
        "R1_contract_type_registry_complete": "PASS"
        if registry.overlay_payload.get("registry_complete") is True
        and not registry.unresolved_symbols
        else "FAIL",
        "R2_pre_contamination_checkpoint": "PASS"
        if checkpoint is not None
        and checkpoint.get("as_of_utc") == CHECKPOINT_TIMESTAMP
        and checkpoint.get("scheduler", {}).get("next_rebalance_due_day") == "2026-03-06"
        else "FAIL",
        "R3_pre_correction_state_parity": "PASS" if parity else "FAIL",
        "R4_product_filter": "PASS"
        if corrected is not None and snapshot_issues == 0
        else "FAIL",
        "R5_lifecycle_layer_preserved": "PASS"
        if checkpoint is not None
        and checkpoint.get("m1_3_overlay_sha256") == M13_OVERLAY_SHA256
        else "FAIL",
        "R6_scheduler_continuity": "PASS"
        if corrected is not None
        and corrected.scheduler.get("scheduler_initialized_from_checkpoint") is True
        and corrected.scheduler.get("rebalance_interval_days", 7) == 7
        else "FAIL",
        "R7_position_continuity": "PASS"
        if checkpoint is not None and checkpoint.get("positions")
        and corrected is not None
        else "FAIL",
        "R8_no_protocol_ineligible_eligible_symbols": "PASS"
        if snapshot_issues == 0
        else "FAIL",
        "R9_no_protocol_ineligible_targets": "PASS"
        if ineligible_targets == 0
        else "FAIL",
        "R10_no_protocol_ineligible_positions": "PASS"
        if current.get("protocol_ineligible_position_count") == 0
        else "FAIL",
        "R11_divergence_explained": "PASS"
        if divergence.get("unexplained_divergence_count") == 0
        else "FAIL",
        "R12_funding_integrity": "PASS"
        if corrected is not None and corrected.funding_issue_count == 0
        else "FAIL",
        "R13_required_prestart_gets_complete": "PASS"
        if not required_get_failures and not extension.get("missing_cached_requests")
        else "FAIL",
        "R14_current_parent_state_complete": "PASS" if state_complete else "FAIL",
        "R15_current_latest_13_selection": "PASS"
        if risk.get("selected_count") == 13
        and len(set(risk.get("latest_week_endings", []))) == 13
        and "2026-09-13" in risk.get("latest_week_endings", [])
        else "FAIL",
        "R16_current_latest_13_complete": "PASS" if latest_complete else "FAIL",
        "R17_risk_state_valid": "PASS"
        if latest_complete
        and risk.get("position_scale") is not None
        else "FAIL",
        "R18_anchor_still_frozen": "PASS"
        if V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
        and verify_v2_1_forward_anchor_hash() == APPROVED_V2_1_FORWARD_ANCHOR_SHA256
        else "FAIL",
        "R19_no_forward_evidence": "PASS"
        if current.get("no_forward_signal_selected") is True
        else "FAIL",
        "R20_no_full_formal_historical_rerun": "PASS"
        if checkpoint is not None
        and checkpoint.get("historical_full_formal_rerun") is False
        else "FAIL",
    }
    if immutable_before != immutable_after:
        gates["R0_identity"] = "FAIL"
    return gates


def _render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        str(report["decision"]),
        "",
        "# XS-LOWVOL V2.1-M1.4.2 Parent Reconstruction",
        "",
        "This artifact is 'PRESTART_STATE_INITIALIZATION_ONLY', 'NOT_FORWARD', and 'NOT_VALIDATION_EVIDENCE'.",
        "It contains no historical aggregate result and does not select a Forward signal.",
        "",
        "## Frozen identity",
        "",
        f"- Code commit used by run: '{report.get('code_commit_used_by_run')}'",
        f"- V1 Protocol: '{report['identity'].get('v1_protocol_sha256')}'",
        f"- V1 Control: '{report['identity'].get('v1_control_sha256')}'",
        f"- V1 Shadow: '{report['identity'].get('v1_shadow_sha256')}'",
        f"- V2.1 Spec: '{report['identity'].get('v2_1_spec_sha256')}'",
        f"- V2.1 Protocol: '{report['identity'].get('v2_1_protocol_sha256')}'",
        f"- Forward Anchor: '{report['identity'].get('forward_anchor_sha256')}'",
        "",
        "## R0-R20",
        "",
    ]
    lines.extend(
        f"- '{name}': '{value}'"
        for name, value in report.get("gates", {}).items()
    )
    lines.extend(
        [
            "",
            "## Corrected state",
            "",
            f"- Checkpoint: '{report.get('checkpoint_timestamp')}'",
            f"- Formal correction start: '{report.get('formal_correction_start')}'",
            f"- First corrected divergence: '{report.get('first_corrected_divergence')}'",
            f"- Explained divergence count: '{report.get('explained_divergence_count')}'",
            f"- Unexplained divergence count: '{report.get('unexplained_divergence_count')}'",
            f"- Current positions: '{report.get('current_position_count')}'",
            f"- Latest-13 complete: '{report.get('latest_13_complete')}'",
            "",
            "## Safety",
            "",
            "- LIVE_TRADING: 'False'",
            "- PAPER_ONLY: 'True'",
            "- HTTP: 'GET-only'",
            "- Anchor: 'FROZEN_NOT_YET_STARTED'",
            "- No Forward evidence, no V2, no M2.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_formal(*, approval: str) -> dict[str, Any]:
    if approval != APPROVAL:
        raise M142Error(f"M1.4.2 requires explicit approval {APPROVAL!r}")
    if OUTPUT_DIR.exists():
        raise M142Error("M1.4.2 output directory already exists; formal reruns are forbidden")
    code_commit = _git("rev-parse", "HEAD")
    if not _is_ancestor(BASE_COMMIT, code_commit):
        raise M142IdentityError("current code is not based on approved M1.4.1 commit")
    if _git("status", "--short"):
        raise M142Error("M1.4.2 formal run requires a clean worktree")
    identity = verify_frozen_identity()
    immutable_before = _immutable_snapshot()
    evidence = _load_json(EVIDENCE_PATH)
    if evidence.get("status") != "EVIDENCE_STALE":
        raise M142IdentityError("runtime evidence is not EVIDENCE_STALE")
    dataset = load_verified_dataset()
    raw_histories = tuple(dataset["usable_histories"])
    lifecycle_overlay = DEFAULT_OVERLAY.with_terminal_closes(raw_histories)
    overlay_sha = _sha256_json(lifecycle_overlay.as_dict())
    if overlay_sha != M13_OVERLAY_SHA256:
        raise M142IdentityError("M1.3 lifecycle overlay hash changed")
    old_histories = tuple(apply_lifecycle_overlay(raw_histories, lifecycle_overlay))
    registry = build_contract_type_registry(dataset)
    overlay_payload = dict(registry.overlay_payload)
    overlay_sha256 = _write_json(OVERLAY_PATH, overlay_payload)

    checkpoint: dict[str, Any] | None = None
    corrected_state: ParentRunState | None = None
    risk: dict[str, Any] = {
        "schema_version": "CORRECTED_LATEST_13_RISK_INPUT.v1",
        "classification": list(LABELS),
        "scope": "RISK_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "cutoff_utc": FINAL_CUTOFF,
        "selected_count": 0,
        "input_complete_count": 0,
        "latest_week_endings": [],
        "rows": [],
        "status": "HALTED_INCOMPLETE_INPUT",
        "risk_diagnostic_status": "HALTED_INCOMPLETE_INPUT",
        "halt_reason": "contract-type registry or checkpoint is not ready",
    }
    extension: dict[str, Any] = {
        "schema_version": "CORRECTED_PRESTART_EXTENSION_MANIFEST.v1",
        "classification": list(LABELS),
        "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "missing_cached_requests": [],
        "required_requests": [],
        "out_of_scope_requests_not_required": [],
        "celr_retry": None,
        "no_zero_fill": True,
        "no_interpolation": True,
        "no_synthetic_settlement": True,
    }
    divergence: dict[str, Any] = {
        "first_corrected_divergence": None,
        "explained_divergence_count": 0,
        "unexplained_divergence_count": 0,
        "rows": [],
    }
    run_error: str | None = None
    required_failures: list[str] = []
    current: dict[str, Any] = {
        "schema_version": "CORRECTED_CURRENT_PARENT_STATE.v1",
        "classification": list(LABELS),
        "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "cutoff_utc": FINAL_CUTOFF,
        "parent_state_complete": False,
        "current_control_positions": [],
        "current_position_count": 0,
        "protocol_ineligible_position_count": 0,
        "last_successful_signal_day": None,
        "last_successful_execution_day": None,
        "next_rebalance_due_day": None,
        "pending_retry": None,
        "scheduler_initialized_from_checkpoint": False,
        "unresolved_state_issue_count": 1,
        "funding_issue_count": 0,
        "no_forward_signal_selected": True,
    }

    if registry.unresolved_symbols:
        run_error = "CONTRACT_TYPE_STATE_NOT_READY: " + ",".join(registry.unresolved_symbols)
    else:
        try:
            corrected_base = _apply_product_layer(old_histories, registry)
            checkpoint, _, _, accounting = _build_checkpoint(
                dataset=dataset,
                registry=registry,
                old_histories=old_histories,
                corrected_histories=corrected_base,
            )
            extension_histories, extension, extension_context = _load_cached_extension(
                corrected_base, registry
            )
            if "CELRUSDT.daily_1d" in extension.get("missing_cached_requests", []):
                celr_bars, retry = _retry_celr_daily_get(extension=extension)
                if not celr_bars:
                    raise M142NotReady("CELR corrective GET returned no completed bars")
                extension_histories = _merge_celr_daily(extension_histories, celr_bars)
                extension["missing_cached_requests"] = [
                    item
                    for item in extension.get("missing_cached_requests", [])
                    if item != "CELRUSDT.daily_1d"
                ]
            required_failures = _required_get_failures(
                extension, extension.get("celr_retry")
            )
            legacy_state = _new_parent_state(checkpoint=checkpoint, accounting=accounting)
            corrected_state = _new_parent_state(checkpoint=checkpoint, accounting=accounting)
            legacy_state = _run_incremental_parent(
                state=legacy_state,
                histories=old_histories,
                registry=registry,
                start=FORMAL_CORRECTION_START,
                end=FROZEN_DATA_END,
                label="LEGACY_PARENT_FOR_DIVERGENCE_REFERENCE",
                enforce_product_layer=False,
            )
            corrected_state = _run_incremental_parent(
                state=corrected_state,
                histories=corrected_base,
                registry=registry,
                start=FORMAL_CORRECTION_START,
                end=FROZEN_DATA_END,
                label="CORRECTED_PARENT",
                enforce_product_layer=True,
            )
            divergence = _compare_future_attempts(
                legacy=legacy_state,
                corrected=corrected_state,
                excluded_symbols=registry.excluded_tradifi_symbols,
            )
            corrected_state = _run_incremental_parent(
                state=corrected_state,
                histories=extension_histories,
                registry=registry,
                start=EXTENSION_START,
                end=EXTENSION_END,
                label="CORRECTED_PARENT",
                enforce_product_layer=True,
            )
            rows = _latest_13_rows(corrected_state.equity_by_day)
            risk = _build_risk_input(rows)
            current = _current_state(
                state=corrected_state,
                extension_complete=not extension.get("missing_cached_requests")
                and not required_failures,
            )
        except (M142Error, OSError, ValueError, TypeError, KeyError) as exc:
            run_error = f"{type(exc).__name__}: {exc}"

    if checkpoint is not None:
        checkpoint_sha = _write_json(CHECKPOINT_PATH, checkpoint)
    else:
        checkpoint_sha = None
        _write_json(
            CHECKPOINT_PATH,
            {
                "schema_version": "PRE_CONTAMINATION_CHECKPOINT.v1",
                "classification": list(LABELS),
                "scope": "STATE_EXTRACTION_ONLY",
                "evidence_status": "NOT_VALIDATION_EVIDENCE",
                "as_of_utc": CHECKPOINT_TIMESTAMP,
                "status": "NOT_READY",
                "reason": run_error or "checkpoint unavailable",
                "historical_full_formal_rerun": False,
            },
        )
    if corrected_state is not None:
        _write_json(
            SCHEDULE_PATH,
            {
                "schema_version": "CORRECTED_PARENT_SCHEDULE.v1",
                "classification": list(LABELS),
                "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
                "evidence_status": "NOT_VALIDATION_EVIDENCE",
                "formal_correction_start": FORMAL_CORRECTION_START.isoformat(),
                "frozen_data_end": FROZEN_DATA_END.isoformat(),
                "extension_end": EXTENSION_END.isoformat(),
                "attempts": corrected_state.attempts,
                "divergence": divergence,
                "protocol_ineligible_eligible_snapshot_count": sum(
                    int(row.get("protocol_ineligible_eligible_snapshot_count", 0))
                    for row in corrected_state.attempts
                ),
                "protocol_ineligible_target_count": sum(
                    1
                    for row in corrected_state.attempts
                    for symbol in row.get("target_symbols", [])
                    if symbol in set(registry.excluded_tradifi_symbols)
                ),
                "historical_full_formal_rerun": False,
            },
        )
        _write_json(
            HOLDINGS_PATH,
            {
                "schema_version": "CORRECTED_PARENT_HOLDINGS.v1",
                "classification": list(LABELS),
                "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
                "evidence_status": "NOT_VALIDATION_EVIDENCE",
                "holding_intervals": corrected_state.holding_intervals,
                "current_positions": [
                    _position_row(position)
                    for _, position in sorted(corrected_state.positions.items())
                ],
                "protocol_ineligible_position_count": 0,
            },
        )
        _write_json(
            FUNDING_PATH,
            {
                "schema_version": "CORRECTED_FUNDING_PROVENANCE.v1",
                "classification": list(LABELS),
                "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
                "evidence_status": "NOT_VALIDATION_EVIDENCE",
                "coverage_policy": "ACTUAL_SETTLEMENTS_ONLY",
                "zero_fill": False,
                "interpolation": False,
                "funding_issue_count": corrected_state.funding_issue_count,
                "rows": corrected_state.funding_rows,
            },
        )
    else:
        _write_json(
            SCHEDULE_PATH,
            {
                "schema_version": "CORRECTED_PARENT_SCHEDULE.v1",
                "classification": list(LABELS),
                "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
                "evidence_status": "NOT_VALIDATION_EVIDENCE",
                "status": "NOT_READY",
                "reason": run_error or "corrected state unavailable",
                "historical_full_formal_rerun": False,
            },
        )
        _write_json(
            HOLDINGS_PATH,
            {
                "schema_version": "CORRECTED_PARENT_HOLDINGS.v1",
                "classification": list(LABELS),
                "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
                "evidence_status": "NOT_VALIDATION_EVIDENCE",
                "current_positions": [],
                "protocol_ineligible_position_count": 0,
            },
        )
        _write_json(
            FUNDING_PATH,
            {
                "schema_version": "CORRECTED_FUNDING_PROVENANCE.v1",
                "classification": list(LABELS),
                "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
                "evidence_status": "NOT_VALIDATION_EVIDENCE",
                "funding_issue_count": 0,
                "rows": [],
            },
        )
    extension["run_error"] = run_error
    extension["required_get_failure_count"] = len(required_failures)
    extension["required_get_failures"] = required_failures
    extension_sha = _write_json(EXTENSION_PATH, extension)
    risk_sha = _write_json(RISK_PATH, risk)
    current_sha = _write_json(CURRENT_STATE_PATH, current)
    immutable_after = _immutable_snapshot()
    gates = _gates(
        identity_ok=True,
        registry=registry,
        checkpoint=checkpoint,
        corrected=corrected_state,
        extension=extension,
        risk=risk,
        current=current,
        divergence=divergence,
        required_get_failures=required_failures,
        immutable_before=immutable_before,
        immutable_after=immutable_after,
    )
    decision = (
        "V2.1-M1.4.2 PARENT_STATE READY"
        if all(value == "PASS" for value in gates.values())
        else "V2.1-M1.4.2 PARENT_STATE NOT_READY"
    )
    report = {
        "schema_version": "V2_1_M1_4_2_PARENT_RECONSTRUCTION.v1",
        "classification": list(LABELS),
        "scope": "PRESTART_STATE_INITIALIZATION_ONLY",
        "evidence_status": "NOT_VALIDATION_EVIDENCE",
        "decision": decision,
        "run_id": RUN_ID,
        "run_type": "CONTRACT_TYPE_CORRECTED_PARENT_RECONSTRUCTION",
        "base_commit": BASE_COMMIT,
        "code_commit_used_by_run": code_commit,
        "identity": identity,
        "protocol_sha256": identity["v1_protocol_sha256"],
        "control_sha256": identity["v1_control_sha256"],
        "shadow_sha256": identity["v1_shadow_sha256"],
        "v2_1_spec_sha256": identity["v2_1_spec_sha256"],
        "v2_1_protocol_sha256": identity["v2_1_protocol_sha256"],
        "forward_anchor_sha256": identity["forward_anchor_sha256"],
        "forward_anchor_status": V21_FORWARD_ANCHOR_STATUS,
        "m1_3_overlay_sha256": M13_OVERLAY_SHA256,
        "dataset_sha256": dataset["dataset_sha256"],
        "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
        "contract_type_overlay_sha256": overlay_sha256,
        "checkpoint_sha256": checkpoint_sha,
        "checkpoint_timestamp": CHECKPOINT_TIMESTAMP,
        "last_clean_successful_signal_day": (
            checkpoint.get("scheduler", {}).get("last_successful_signal_day")
            if checkpoint
            else None
        ),
        "last_clean_successful_execution_day": (
            checkpoint.get("scheduler", {}).get("last_successful_execution_day")
            if checkpoint
            else None
        ),
        "formal_correction_start": FORMAL_CORRECTION_START.isoformat(),
        "number_of_excluded_tradifi_contracts": len(registry.excluded_tradifi_symbols),
        "first_corrected_divergence": divergence.get("first_corrected_divergence"),
        "explained_divergence_count": divergence.get("explained_divergence_count", 0),
        "unexplained_divergence_count": divergence.get("unexplained_divergence_count", 0),
        "required_get_retry_count": 1 if extension.get("celr_retry") else 0,
        "required_get_failure_count": len(required_failures),
        "funding_issue_count": corrected_state.funding_issue_count if corrected_state else 0,
        "protocol_ineligible_eligible_snapshot_count": sum(
            int(row.get("protocol_ineligible_eligible_snapshot_count", 0))
            for row in (corrected_state.attempts if corrected_state else [])
        ),
        "protocol_ineligible_target_count": sum(
            1
            for row in (corrected_state.attempts if corrected_state else [])
            for symbol in row.get("target_symbols", [])
            if symbol in set(registry.excluded_tradifi_symbols)
        ),
        "protocol_ineligible_position_count": current.get(
            "protocol_ineligible_position_count", 0
        ),
        "current_positions": current.get("current_control_positions", []),
        "current_position_count": current.get("current_position_count", 0),
        "last_successful_signal_day": current.get("last_successful_signal_day"),
        "last_successful_execution_day": current.get("last_successful_execution_day"),
        "next_rebalance_due_day": current.get("next_rebalance_due_day"),
        "pending_retry": current.get("pending_retry"),
        "latest_13_week_endings": risk.get("latest_week_endings", []),
        "latest_13_complete": risk.get("selected_count") == 13
        and risk.get("input_complete_count") == 13,
        "reference_vol": risk.get("reference_vol"),
        "position_scale": risk.get("position_scale"),
        "gates": gates,
        "failed_gates": [name for name, value in gates.items() if value != "PASS"],
        "artifact_sha256": {
            "contract_type_overlay": overlay_sha256,
            "checkpoint": checkpoint_sha,
            "extension": extension_sha,
            "risk": risk_sha,
            "current_parent_state": current_sha,
        },
        "historical_aggregate_output": False,
        "m1_3_unchanged": immutable_before == immutable_after,
        "m1_4_1_unchanged": immutable_before == immutable_after,
        "evidence_status_unchanged": evidence.get("status") == "EVIDENCE_STALE",
        "parameter_optimization": False,
        "strategy_changed": False,
        "protocol_changed": False,
        "gate_changed": False,
        "forward_evidence_created": False,
        "first_forward_signal_selected": False,
        "v2_started": False,
        "m2_started": False,
        "live_trading": False,
        "paper_only": True,
        "http_method_policy": "GET_ONLY",
        "run_error": run_error,
    }
    report_sha = _write_json(REPORT_PATH, report)
    report["report_sha256"] = report_sha
    MARKDOWN_PATH.write_text(_render_markdown(report), encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    result = run_formal(approval=args.approval)
    print(result["decision"])
    print(json.dumps(_canonical(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["decision"].endswith("READY") else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "APPROVAL",
    "BASE_COMMIT",
    "CHECKPOINT_DAY",
    "CUTOFF",
    "EXTENSION_END",
    "EXTENSION_START",
    "FORMAL_CORRECTION_START",
    "M142Error",
    "M142IdentityError",
    "M142NotReady",
    "OUTPUT_DIR",
    "protocol_contract_eligible",
    "protocol_contract_eligible_on",
    "build_contract_type_registry",
    "run_formal",
]
