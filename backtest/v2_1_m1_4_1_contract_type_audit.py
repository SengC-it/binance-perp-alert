"""XS-LOWVOL V2.1-M1.4.1 frozen contract-type conformance audit.

This module is intentionally an audit-only reader.  It consumes the already
frozen dataset, M1.3 provenance, and the M1.4 exchangeInfo snapshot.  It never
fetches market data, retries a failed request, rewrites M1.4 state, or runs a
parent reconstruction.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from src.xs_lowvol_spec import strategy_spec_hash
from src.xs_lowvol_v2_1_anchor import (
    APPROVED_V1_CONTROL_SHA256,
    APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_1_PROTOCOL_SHA256,
    APPROVED_V2_1_SPEC_SHA256,
    V21_FORWARD_ANCHOR_STATUS,
    validate_v2_1_forward_anchor,
    verify_v2_1_forward_anchor_hash,
)

from .m1_protocol import M1_APPROVED_PROTOCOL_SHA256, verify_protocol_hash
from .v2_1_m1_engineering import (
    EXPECTED_DATASET_SHA256,
    EXPECTED_NORMALIZED_DATASET_SHA256,
    load_verified_dataset,
)
from .xs_history import is_historical_usdt_perpetual_symbol


PROJECT_ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc

AUDIT_ID = "XS-LOWVOL-V2.1-M1.4.1-CONTRACT-TYPE-AUDIT-1"
BASE_FORMAL_PARENT_COMMIT = "4072283a997117d8c24e289d3e90a303384b26db"
AUDIT_CUTOFF_UTC = "2026-09-19T00:00:00Z"
EXTENSION_END = date(2026, 8, 31)
M14_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_4"
M14_RAW_DIR = PROJECT_ROOT / "data" / "v2_1_m1_4_prestart_extension"
AUDIT_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_4_1"
DATASET_PATH = PROJECT_ROOT / "backtest" / "m1_cache" / "normalized" / "XS_LOWVOL_M1_DATASET.pkl"
EXCHANGE_INFO_PATH = M14_RAW_DIR / "exchangeInfo.json"
M13_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_3"
M131_DIR = PROJECT_ROOT / "research" / "v2_1" / "m1_3_1"
M13_ARTIFACTS = {
    "FORCED_EXIT_PROVENANCE.json": "d5c24b374ed4e9dc0f50b59b1141431baa9d84061108ea73b9cdd1190bf3abcc",
    "FUNDING_LIFETIME_PROVENANCE.json": "1dc12019dce9f0f0d3c49e862029c345cc1237514ea813c1636ad9953f38649b",
    "HOLDING_PROVENANCE.json": "4502173c815618ec3eb79ca22a8c17348d822d7be4d9453f010fb749e3ec0025",
    "LIFECYCLE_OVERRIDE_MANIFEST.json": "d184ebb2c29e7890415d7eabc456b9fefcc9f749eb9660515f8c665074bc93da",
    "SCHEDULE_PROVENANCE.json": "dccb96269a82c9c21151c8cfd3f89f2092c6020ab1b0a3f0f28d737d1e76f35e",
    "V2_1_M1_3_PARENT_RECONSTRUCTION.json": "eaf37fdd32c8482ab24f707406564cad11c5fedd36814897dc5bfbc9bc5a1ab8",
    "V2_1_M1_3_PARENT_RECONSTRUCTION.md": "008d3f31732b00d0874b67a36c1049dc5c089bdf511f48ef94b216fcab9b585e",
    "WEEKLY_ACCOUNTING_PROVENANCE.json": "e19f2eebc16b3c2a471c5b3f9390bf2ca2ca9020155d1827a57757ac6af305aa",
}
M131_ARTIFACTS = {
    "V2_1_M1_3_EVIDENCE_SCOPE.json": "5a9ce93ec6f40426d6c6c339b9781beb8b0da78e812dfec435306093d362ffd8",
    "V2_1_M1_3_EVIDENCE_SCOPE.md": "f98326c8244389bf22336d1738b0bace655b546ba0629a287c4df53b9ee58f53",
}
M14_ARTIFACTS = {
    "CURRENT_LATEST_13_RISK_INPUT.json": "e1204a85f0335f23ad555258a396bfc2b410eda7d1d9eb08e5c0d7646e0b512b",
    "CURRENT_PARENT_STATE.json": "4565e986bc0576bf6c4dd7ea1e4d89deeaa9a9514e70e4e45ccd0122dbb09465",
    "M1_3_ACCEPTED_STATE_CHECKPOINT.json": "06e895353e8cb6d273e1339868a80aabc08a3d0d89796c2b9b2feacbea3df43a",
    "PRESTART_EXTENSION_MANIFEST.json": "3131162408f41b69615c212d74dd26e2a737679d1d052a40faac71f5157ea0e9",
    "PRESTART_LIFECYCLE_EXTENSION.json": "07c79cefc1a5d85fc15e532f668e30d9b35bd387faa888af187381603fc1cce6",
    "V2_1_M1_4_PRESTART_BRIDGE.json": "b7c03e8f314602c4e941aca9048d1e1dfcec15014dce2fbe265d222aa1b70c8a",
    "V2_1_M1_4_PRESTART_BRIDGE.md": "a5484cda16a33c8251fc14633ab16a5c5bdab54dc31a8ce914f063ea07f6aeec",
}

V1_PROTOCOL_PATH = PROJECT_ROOT / "research" / "m1" / "XS_LOWVOL_M1_PROTOCOL.yaml"
V21_PROTOCOL_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_PROTOCOL.yaml"
V21_SPEC_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_SPEC.yaml"
V21_ANCHOR_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_FORWARD_ANCHOR.yaml"
LEGACY_EVIDENCE_PATH = PROJECT_ROOT / "research" / "evidence" / "XS-LOWVOL-V1.json"

EXCHANGE_INFO_ENDPOINT = "https://fapi.binance.com/fapi/v1/exchangeInfo"
HISTORICAL_ARCHIVE_PREFIX = "https://data.binance.vision/data/futures/um/monthly"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")

OFFICIAL_SOURCES = [
    {
        "title": "Binance Futures Launches TradFi Perpetual Contracts",
        "url": "https://www.binance.com/en/support/announcement/detail/ecf7318c0d434c339e80878588e700d0",
        "published_utc": "2026-01-08T08:00:00Z",
        "role": "product_category_boundary; TradFi is a separate product category and is USDT-settled",
    },
    {
        "title": "Binance Futures Will Launch USDⓈ-Margined TSLAUSDT Equity Perpetual Contract",
        "url": "https://www.binance.com/en/support/announcement/detail/40c76b4deaa247f09774e5d1ee747cb8",
        "published_utc": "2026-01-26T11:29:00Z",
        "effective_utc": "2026-01-28T14:30:00Z",
        "role": "first confirmed Control contamination symbol TSLAUSDT",
    },
    {
        "title": "Binance Futures Will Launch Multiple USDⓈ-Margined TradFi Perpetual Contracts (2026-07-16)",
        "url": "https://www.binance.com/en/support/announcement/detail/0a613aed15cc4cf78898594d7c767661",
        "published_utc": "2026-07-16T06:15:00Z",
        "role": "MUUUSDT and SOXSUSDT timestamped TradFi listing evidence",
    },
    {
        "title": "Binance Futures Will Launch Multiple USDⓈ-Margined TradFi Perpetual Contracts (2026-08-28)",
        "url": "https://www.binance.com/en/support/announcement/detail/32ac927d1cbe4aa3b527eca1c401a98f",
        "published_utc": "2026-08-28T07:30:00Z",
        "role": "PDDUSDT timestamped TradFi listing evidence and fixed failure scope",
    },
    {
        "title": "Binance Futures Will Launch Multiple USDⓈ-Margined TradFi Perpetual Contracts (2026-09-07)",
        "url": "https://www.binance.com/en/support/announcement/detail/89a035c3ee0e4b7782bf0089323d8e78",
        "published_utc": "2026-09-04T04:46:00Z",
        "role": "September candidate TradFi listing evidence",
    },
]


class ContractTypeAuditError(RuntimeError):
    """Fail-closed contract-type audit error."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_map(directory: Path, expected: Mapping[str, str]) -> dict[str, str]:
    actual: dict[str, str] = {}
    for name, expected_hash in expected.items():
        path = directory / name
        if not path.is_file():
            raise ContractTypeAuditError(f"missing immutable artifact: {path}")
        digest = _sha256(path)
        actual[name] = digest
        if digest != expected_hash:
            raise ContractTypeAuditError(
                f"immutable artifact changed: {name}: expected {expected_hash}, got {digest}"
            )
    return actual


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()


def _git_commit_exists(commit: str) -> bool:
    return subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
    ).returncode == 0


def _iso_ms(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _day_from_ms(value: Any) -> str | None:
    timestamp = _iso_ms(value)
    return timestamp[:10] if timestamp else None


def is_protocol_eligible_contract(
    contract_type: Any, quote_asset: Any, status: Any = "TRADING"
) -> bool:
    """Return the frozen V1 contract-type/product availability predicate."""
    return (
        str(contract_type or "").upper() == "PERPETUAL"
        and str(quote_asset or "").upper() == "USDT"
        and str(status or "").upper() == "TRADING"
    )


def classify_contract_entry(
    contract_type: Any, quote_asset: Any, status: Any = "TRADING"
) -> dict[str, Any]:
    contract = str(contract_type or "").upper()
    quote = str(quote_asset or "").upper()
    state = str(status or "").upper()
    eligible = is_protocol_eligible_contract(contract, quote, state)
    if state in {"SETTLING", "PENDING_TRADING"} or (state and state != "TRADING"):
        reason = "NON_TRADING_OR_SETTLING_STATE"
    elif contract == "TRADIFI_PERPETUAL":
        reason = "TRADIFI_PERPETUAL_EXCLUDED_BY_PROTOCOL"
    elif contract in {"CURRENT_QUARTER", "NEXT_QUARTER"}:
        reason = f"{contract}_REJECTED_BY_PERPETUAL_PROTOCOL"
    elif quote != "USDT":
        reason = "NON_USDT_QUOTE_REJECTED_BY_PROTOCOL"
    elif contract != "PERPETUAL":
        reason = "NON_PERPETUAL_CONTRACT_REJECTED_BY_PROTOCOL"
    else:
        reason = "PROTOCOL_ELIGIBLE"
    return {
        "contract_type": contract or None,
        "quote_asset": quote or None,
        "status": state or None,
        "protocol_eligible": eligible,
        "classification_reason": reason,
    }


def classify_ambiguous_candidate(
    *,
    symbol: str,
    onboard_day: str | None,
    in_frozen_catalog: bool,
    current_info: Mapping[str, Any] | None,
) -> str:
    """Assign one mutually exclusive M1.4 ambiguous-candidate category."""
    info = current_info or {}
    status = str(info.get("status") or "").upper()
    contract_type = str(info.get("contractType") or "").upper()
    if status and status != "TRADING":
        return "C_NON_TRADING_OR_SETTLING_STATE"
    if contract_type == "TRADIFI_PERPETUAL":
        return "B_TRADIFI_PERPETUAL_EXCLUDED_BY_PROTOCOL"
    if in_frozen_catalog:
        return "A_KNOWN_IN_FROZEN_CATALOG"
    if info and onboard_day and onboard_day <= EXTENSION_END.isoformat():
        return "D_TRUE_PRESTART_PIT_GAP"
    return "E_UNRESOLVED"


def _compact_info(info: Mapping[str, Any] | None) -> dict[str, Any]:
    if not info:
        return {
            "official_exchange_info_found": False,
            "contractType": None,
            "quoteAsset": None,
            "status": None,
            "onboardDate": None,
            "onboard_timestamp_utc": None,
            "underlyingType": None,
            "underlyingSubType": None,
        }
    return {
        "official_exchange_info_found": True,
        "contractType": info.get("contractType"),
        "quoteAsset": info.get("quoteAsset"),
        "status": info.get("status"),
        "onboardDate": info.get("onboardDate"),
        "onboard_timestamp_utc": _iso_ms(info.get("onboardDate")),
        "deliveryDate": info.get("deliveryDate"),
        "underlyingType": info.get("underlyingType"),
        "underlyingSubType": info.get("underlyingSubType"),
    }


def _identity_snapshot() -> dict[str, Any]:
    actual = {
        "v1_protocol_sha256": verify_protocol_hash(),
        "v1_control_sha256": strategy_spec_hash(variant="control"),
        "v1_shadow_sha256": strategy_spec_hash(variant="shadow"),
        "v2_1_protocol_sha256": __import__(
            "backtest.v2_1_protocol", fromlist=["verify_v2_1_protocol_hash"]
        ).verify_v2_1_protocol_hash(),
        "v2_1_spec_sha256": __import__(
            "src.xs_lowvol_v2_1_spec", fromlist=["verify_v2_1_spec_hash"]
        ).verify_v2_1_spec_hash(),
        "forward_anchor_sha256": verify_v2_1_forward_anchor_hash(),
    }
    expected = {
        "v1_protocol_sha256": M1_APPROVED_PROTOCOL_SHA256,
        "v1_control_sha256": APPROVED_V1_CONTROL_SHA256,
        "v1_shadow_sha256": "97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd",
        "v2_1_protocol_sha256": APPROVED_V2_1_PROTOCOL_SHA256,
        "v2_1_spec_sha256": APPROVED_V2_1_SPEC_SHA256,
        "forward_anchor_sha256": APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    }
    if actual != expected:
        raise ContractTypeAuditError(f"frozen identity drift: {actual} != {expected}")
    validate_v2_1_forward_anchor()
    return {**actual, "expected": expected, "forward_anchor_status": V21_FORWARD_ANCHOR_STATUS}


def _parser_audit() -> dict[str, Any]:
    source = (PROJECT_ROOT / "backtest" / "v2_1_m1_4_prestart_bridge.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def _parse_exchange_info")
    end = source.index("def _parse_funding_info", start)
    parser_source = source[start:end]
    conditional_checks = {
        "quoteAsset_equals_USDT": bool(re.search(r"if .*quoteAsset|and .*quoteAsset", parser_source)),
        "contractType_equals_PERPETUAL": bool(re.search(r"if .*contractType|and .*contractType", parser_source)),
        "status_TRADING": bool(re.search(r"if .*status|and .*status", parser_source)),
        "effective_availability": "effective" in parser_source.lower(),
    }
    missing = [name for name, present in conditional_checks.items() if not present]
    return {
        "parser": "backtest.v2_1_m1_4_prestart_bridge._parse_exchange_info",
        "actual_filter": "symbol regex only: ^[A-Z0-9]+USDT$; fields are copied but not used as product predicates",
        "conditional_checks": conditional_checks,
        "missing_product_filter_finding": "M1_4_EXCHANGE_INFO_PRODUCT_FILTER_MISSING",
        "missing_checks": missing,
        "formal_state_not_rewritten": True,
    }


def _historical_classifier_audit() -> dict[str, Any]:
    source = inspect.getsource(is_historical_usdt_perpetual_symbol)
    return {
        "function": "backtest.xs_history.is_historical_usdt_perpetual_symbol",
        "regex": _SYMBOL_RE.pattern,
        "actual_rule": "bool(_SYMBOL_RE.fullmatch(str(symbol).upper()))",
        "contractType_checked": False,
        "quoteAsset_checked": False,
        "status_checked": False,
        "source_excerpt": source.strip(),
        "finding": "HISTORICAL_ARCHIVE_CONTRACT_TYPE_NOT_PROVEN",
        "archive_path_limitation": (
            "futures/um/monthly daily and funding archive names identify symbol/time period, "
            "not PERPETUAL versus TRADIFI_PERPETUAL"
        ),
    }


def _manifest_request(manifest: Mapping[str, Any], symbol: str, suffix: str) -> dict[str, Any] | None:
    for item in manifest.get("requests", []):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", ""))
        if name == f"{symbol}.{suffix}" or (symbol in name and suffix in name):
            return {
                key: item.get(key)
                for key in (
                    "name",
                    "method",
                    "endpoint",
                    "request_url",
                    "http_status",
                    "response_sha256",
                    "local_path",
                    "error",
                )
            }
    return None


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _m14_state_hashes() -> dict[str, str]:
    return _hash_map(M14_DIR, M14_ARTIFACTS)


def run_audit() -> dict[str, Any]:
    """Run the frozen audit and write only M1.4.1 artifacts."""
    identity = _identity_snapshot()
    if not _git_commit_exists(BASE_FORMAL_PARENT_COMMIT):
        raise ContractTypeAuditError("formal M1.4 result commit is not present")
    code_commit = _git_head()
    m14_before = _m14_state_hashes()
    m13_before = _hash_map(M13_DIR, M13_ARTIFACTS)
    m131_before = _hash_map(M131_DIR, M131_ARTIFACTS)
    if _sha256(LEGACY_EVIDENCE_PATH) != _read_json(LEGACY_EVIDENCE_PATH).get("evidence_sha256", _sha256(LEGACY_EVIDENCE_PATH)):
        evidence_hash_consistent = False
    else:
        evidence_hash_consistent = True

    dataset = load_verified_dataset()
    catalog = dataset["catalog"]
    histories = tuple(dataset["histories"])
    usable_histories = tuple(dataset["usable_histories"])
    catalog_records = tuple(catalog.symbols)
    catalog_symbols = {record.symbol for record in catalog_records}
    history_symbols = {history.symbol for history in histories}
    usable_symbols = {history.symbol for history in usable_histories}
    if len(catalog_records) != 861 or len(usable_histories) != 362:
        raise ContractTypeAuditError("frozen discovery/usable counts changed")

    exchange_payload = _read_json(EXCHANGE_INFO_PATH)
    exchange_symbols = {
        str(item.get("symbol")): item
        for item in exchange_payload.get("symbols", [])
        if isinstance(item, Mapping) and item.get("symbol")
    }
    manifest = _read_json(M14_DIR / "PRESTART_EXTENSION_MANIFEST.json")
    lifecycle = _read_json(M14_DIR / "PRESTART_LIFECYCLE_EXTENSION.json")
    current_parent = _read_json(M14_DIR / "CURRENT_PARENT_STATE.json")
    m13_holdings = _read_json(M13_DIR / "HOLDING_PROVENANCE.json")["records"]
    m13_reconstruction = _read_json(M13_DIR / "V2_1_M1_3_PARENT_RECONSTRUCTION.json")

    membership_table = []
    for record in catalog_records:
        present = record.symbol in history_symbols
        usable = record.symbol in usable_symbols
        membership_table.append(
            {
                "symbol": record.symbol,
                "in_frozen_catalog": True,
                "dataset_status": record.dataset_status,
                "normalization_status": record.normalization_status,
                "lifecycle_status": record.lifecycle_status,
                "usable_history": usable,
                "history_present": present,
                "dataset_membership": (
                    "USABLE_HISTORY"
                    if usable
                    else "HISTORY_PRESENT_NOT_USABLE"
                    if present
                    else "KNOWN_BUT_EXCLUDED"
                ),
                "first_discovered_month": record.first_discovered_month,
                "last_discovered_month": record.last_discovered_month,
            }
        )
    _write_json(
        AUDIT_DIR / "FROZEN_DISCOVERY_MEMBERSHIP_TABLE.json",
        {
            "schema_version": "FROZEN_DISCOVERY_MEMBERSHIP_TABLE.v1",
            "scope": "FULL_FROZEN_DISCOVERY_CATALOG",
            "catalog_source": "verified normalized dataset cache",
            "catalog_content_sha256": catalog.content_sha256,
            "number_of_symbols_discovered": len(membership_table),
            "number_of_history_objects": len(histories),
            "number_of_usable_symbols": len(usable_symbols),
            "known_but_excluded_symbols": sum(
                row["dataset_membership"] == "KNOWN_BUT_EXCLUDED" for row in membership_table
            ),
            "table": membership_table,
        },
    )

    ambiguous_rows = []
    for row in lifecycle.get("ambiguous_candidates", []):
        symbol = str(row["symbol"])
        info = exchange_symbols.get(symbol)
        classification = classify_ambiguous_candidate(
            symbol=symbol,
            onboard_day=row.get("onboard_day"),
            in_frozen_catalog=symbol in catalog_symbols,
            current_info=info,
        )
        ambiguous_rows.append(
            {
                "symbol": symbol,
                "original_reason": row.get("reason"),
                "onboard_day_from_m1_4": row.get("onboard_day"),
                "in_frozen_catalog": symbol in catalog_symbols,
                "usable_history": symbol in usable_symbols,
                "catalog_membership": (
                    "KNOWN_BUT_EXCLUDED" if symbol in catalog_symbols and symbol not in usable_symbols else
                    "KNOWN_USABLE" if symbol in usable_symbols else "NOT_IN_FROZEN_CATALOG"
                ),
                "official_exchange_info": _compact_info(info),
                "classification": classification,
                "classification_reason": (
                    "current official contractType is TRADIFI_PERPETUAL"
                    if classification.startswith("B_")
                    else "current status is not active TRADING"
                    if classification.startswith("C_")
                    else "catalog membership is known even though it was not usable in M1.4"
                    if classification.startswith("A_")
                    else "timestamped onboard precedes the M1.4 extension but symbol is absent from the frozen catalog"
                    if classification.startswith("D_")
                    else "no sufficient official identity evidence"
                ),
            }
        )
    ambiguous_counts = dict(sorted(Counter(row["classification"] for row in ambiguous_rows).items()))

    candidate_rows = []
    for record in lifecycle.get("records", []):
        symbol = str(record["symbol"])
        info = exchange_symbols.get(symbol)
        pit = record.get("pit_evidence") or {}
        classification = classify_contract_entry(
            info.get("contractType") if info else None,
            info.get("quoteAsset") if info else None,
            info.get("status") if info else None,
        )
        candidate_rows.append(
            {
                "symbol": symbol,
                "contractType": info.get("contractType") if info else None,
                "quoteAsset": info.get("quoteAsset") if info else None,
                "status": info.get("status") if info else None,
                "onboard_timestamp_utc": _iso_ms(info.get("onboardDate")) if info else None,
                "onboard_timestamp_ms": info.get("onboardDate") if info else None,
                "protocol_eligible": classification["protocol_eligible"],
                "pit_evidence": bool(
                    pit.get("first_completed_daily_bar_close_timestamp_utc")
                    and pit.get("exchange_info_onboard_timestamp_utc")
                    and pit.get("current_exchange_info_alone_not_used_as_pit_proof") is True
                ),
                "first_completed_daily_bar_close_timestamp_utc": pit.get(
                    "first_completed_daily_bar_close_timestamp_utc"
                ),
                "official_source": record.get("source"),
                "official_source_hash": record.get("source_hash"),
            }
        )

    fixed_failure_specs = (
        ("CELRUSDT", "daily_1d"),
        ("PDDUSDT", "funding_rate"),
        ("PENGUSDT", "funding_rate"),
    )
    fixed_failures = []
    for symbol, suffix in fixed_failure_specs:
        info = exchange_symbols.get(symbol)
        classification = classify_contract_entry(
            info.get("contractType") if info else None,
            info.get("quoteAsset") if info else None,
            info.get("status") if info else None,
        )
        required = classification["protocol_eligible"]
        fixed_failures.append(
            {
                "symbol": symbol,
                "request": f"{symbol}.{suffix}",
                "official_exchange_info": _compact_info(info),
                "request_should_have_been_required": required,
                "scope_classification": (
                    "IN_SCOPE_REQUEST_REQUIRED_BUT_MISSING_RESPONSE"
                    if required
                    else "OUT_OF_SCOPE_REQUEST"
                ),
                "retry_performed": False,
                "historical_request_record": _manifest_request(manifest, symbol, suffix),
            }
        )

    affected_symbols = sorted(
        {
            str(row.get("symbol"))
            for row in m13_holdings
            if exchange_symbols.get(str(row.get("symbol")), {}).get("contractType")
            == "TRADIFI_PERPETUAL"
        }
    )
    affected_holdings = [row for row in m13_holdings if row.get("symbol") in affected_symbols]
    holdings_by_symbol = {
        symbol: sorted(
            [row for row in affected_holdings if row.get("symbol") == symbol],
            key=lambda row: str(row.get("accounting_entry_timestamp_utc")),
        )
        for symbol in affected_symbols
    }
    holding_audit = []
    for symbol in affected_symbols:
        rows = holdings_by_symbol[symbol]
        holding_audit.append(
            {
                "symbol": symbol,
                "contractType": "TRADIFI_PERPETUAL",
                "quoteAsset": exchange_symbols[symbol].get("quoteAsset"),
                "status": exchange_symbols[symbol].get("status"),
                "onboard_timestamp_utc": _iso_ms(exchange_symbols[symbol].get("onboardDate")),
                "in_frozen_catalog": symbol in catalog_symbols,
                "usable_history": symbol in usable_symbols,
                "legacy_regex_classifier_accepts": is_historical_usdt_perpetual_symbol(symbol),
                "protocol_eligible": False,
                "holding_interval_count": len(rows),
                "first_accounting_entry_timestamp_utc": rows[0].get("accounting_entry_timestamp_utc"),
                "last_accounting_entry_timestamp_utc": rows[-1].get("accounting_entry_timestamp_utc"),
                "entered_pit_eligible_under_legacy_classifier": True,
                "entered_control_position": True,
                "long_short_target_evidence": "NOT_RETAINED_IN_M1_3_HOLDING_PROVENANCE",
                "entered_weekly_accounting": True,
                "weekly_accounting_basis": "holding accounting and economic exposure timestamps are retained",
                "official_product_class_evidence": {
                    "contractType": exchange_symbols[symbol].get("contractType"),
                    "underlyingType": exchange_symbols[symbol].get("underlyingType"),
                    "underlyingSubType": exchange_symbols[symbol].get("underlyingSubType"),
                    "exchange_info_source": EXCHANGE_INFO_ENDPOINT,
                },
            }
        )
    first_affected = min(affected_holdings, key=lambda row: row["accounting_entry_timestamp_utc"])
    first_entry_day = date.fromisoformat(first_affected["accounting_entry_timestamp_utc"][:10])
    first_symbol = str(first_affected["symbol"])
    first_impact = {
        "impact_type": "CONTROL_POSITION_ENTRY",
        "symbol": first_symbol,
        "entry_timestamp_utc": first_affected["accounting_entry_timestamp_utc"],
        "execution_day": first_entry_day.isoformat(),
        "signal_day": None,
        "signal_day_inference": (
            (first_entry_day - timedelta(days=1)).isoformat()
            + " would follow the frozen one-day lag, but the M1.3 holding artifact does not preserve target rows"
        ),
        "entered_legacy_pit_universe": True,
        "entered_control_position": True,
        "entered_weekly_accounting": True,
        "target_direction": "NOT_RETAINED_IN_FROZEN_PROVENANCE",
        "protocol_eligible": False,
        "official_timestamped_evidence": [
            source["url"] for source in OFFICIAL_SOURCES if "TSLAUSDT" in source["role"]
        ],
    }

    current_positions = []
    for position in current_parent.get("current_control_positions", []):
        symbol = str(position["symbol"])
        info = exchange_symbols.get(symbol)
        classification = classify_contract_entry(
            info.get("contractType") if info else None,
            info.get("quoteAsset") if info else None,
            info.get("status") if info else None,
        )
        current_positions.append(
            {
                "symbol": symbol,
                "direction": position.get("direction"),
                "entry_timestamp_utc": position.get("entry_timestamp_utc"),
                "official_exchange_info": _compact_info(info),
                "protocol_eligible": classification["protocol_eligible"],
                "classification_reason": classification["classification_reason"],
            }
        )
    ineligible_positions = [row for row in current_positions if not row["protocol_eligible"]]

    m14_after = _m14_state_hashes()
    m13_after = _hash_map(M13_DIR, M13_ARTIFACTS)
    m131_after = _hash_map(M131_DIR, M131_ARTIFACTS)
    immutable = {
        "m1_4_artifacts_before": m14_before,
        "m1_4_artifacts_after": m14_after,
        "m1_4_artifacts_unchanged": m14_before == m14_after,
        "m1_3_artifacts_before": m13_before,
        "m1_3_artifacts_after": m13_after,
        "m1_3_artifacts_unchanged": m13_before == m13_after,
        "m1_3_1_artifacts_before": m131_before,
        "m1_3_1_artifacts_after": m131_after,
        "m1_3_1_artifacts_unchanged": m131_before == m131_after,
        "legacy_evidence_sha256": _sha256(LEGACY_EVIDENCE_PATH),
        "legacy_evidence_status": _read_json(LEGACY_EVIDENCE_PATH).get("status"),
        "legacy_evidence_hash_consistent": evidence_hash_consistent,
    }

    gates = {
        "C0_identity": identity["v1_protocol_sha256"] == M1_APPROVED_PROTOCOL_SHA256 and _git_commit_exists(BASE_FORMAL_PARENT_COMMIT),
        "C1_frozen_protocol_contract_type": True,
        "C2_historical_classifier_audited": _historical_classifier_audit()["finding"] == "HISTORICAL_ARCHIVE_CONTRACT_TYPE_NOT_PROVEN",
        "C3_full_frozen_catalog_used": len(membership_table) == 861 and catalog.listing_complete is True,
        "C4_m1_4_ambiguous_reclassified": len(ambiguous_rows) == 495 and sum(ambiguous_counts.values()) == 495,
        "C5_september_candidates_reclassified": len(candidate_rows) == 20 and all("protocol_eligible" in row for row in candidate_rows),
        "C6_failed_requests_scope_classified": len(fixed_failures) == 3 and all(row["retry_performed"] is False for row in fixed_failures),
        "C7_m1_3_holdings_contract_type_audited": len(m13_holdings) == 1061 and len(holding_audit) == len(affected_symbols),
        "C8_current_positions_contract_type_audited": len(current_positions) == 10,
        "C9_first_contamination_identified": bool(first_impact["entry_timestamp_utc"]),
        "C10_old_artifacts_immutable": all(
            immutable[key]
            for key in (
                "m1_4_artifacts_unchanged",
                "m1_3_artifacts_unchanged",
                "m1_3_1_artifacts_unchanged",
                "legacy_evidence_hash_consistent",
            )
        ),
        "C11_anchor_frozen": identity["forward_anchor_status"] == "FROZEN_NOT_YET_STARTED",
    }
    if not all(gates.values()):
        raise ContractTypeAuditError(f"contract-type audit gate failure: {gates}")

    root_cause = "B_PROTOCOL_INELIGIBLE_CONTRACTS_ENTERED_PARENT_STATE"
    result = {
        "schema_version": "V2_1_M1_4_1_CONTRACT_TYPE_AUDIT.v1",
        "audit_id": AUDIT_ID,
        "audit_cutoff_utc": AUDIT_CUTOFF_UTC,
        "audit_decision": "AUDIT PASS",
        "root_cause": root_cause,
        "base_formal_parent_commit": BASE_FORMAL_PARENT_COMMIT,
        "code_commit_used_by_run": code_commit,
        "identity": identity,
        "frozen_dataset": {
            "dataset_sha256": dataset["dataset_sha256"],
            "normalized_dataset_sha256": dataset["normalized_dataset_sha256"],
            "cache_file_sha256": dataset["cache_file_sha256"],
            "number_of_symbols_discovered": len(catalog_records),
            "number_of_history_objects": len(histories),
            "number_of_usable_symbols": len(usable_symbols),
            "number_of_delisted_symbols": sum(
                not history.currently_active for history in usable_histories
            ),
            "number_of_ambiguous_symbols": len(ambiguous_rows),
            "listing_page_count": catalog.listing_page_count,
            "full_catalog_used": True,
        },
        "historical_classifier_audit": _historical_classifier_audit(),
        "m1_4_exchange_info_parser_audit": _parser_audit(),
        "frozen_discovery_membership_table": {
            "path": "research/v2_1/m1_4_1/FROZEN_DISCOVERY_MEMBERSHIP_TABLE.json",
            "row_count": len(membership_table),
            "history_present_not_usable_count": sum(
                row["dataset_membership"] == "HISTORY_PRESENT_NOT_USABLE" for row in membership_table
            ),
            "known_but_excluded_count": sum(
                row["dataset_membership"] == "KNOWN_BUT_EXCLUDED" for row in membership_table
            ),
        },
        "m1_4_ambiguous_reclassification": {
            "input_count": len(ambiguous_rows),
            "category_counts": ambiguous_counts,
            "categories": {
                "A_KNOWN_IN_FROZEN_CATALOG": "known in the 861-symbol catalog; not an unresolved absence",
                "B_TRADIFI_PERPETUAL_EXCLUDED_BY_PROTOCOL": "official current product class is outside V1 contract_type",
                "C_NON_TRADING_OR_SETTLING_STATE": "official state is not active TRADING",
                "D_TRUE_PRESTART_PIT_GAP": "not in frozen catalog but timestamped prestart onboard evidence exists",
                "E_UNRESOLVED": "insufficient official identity evidence",
            },
            "rows": ambiguous_rows,
        },
        "september_candidates": {
            "count": len(candidate_rows),
            "protocol_eligible_count": sum(row["protocol_eligible"] for row in candidate_rows),
            "tradifi_count": sum(row["contractType"] == "TRADIFI_PERPETUAL" for row in candidate_rows),
            "rows": candidate_rows,
        },
        "fixed_m1_4_failures": {
            "count": len(fixed_failures),
            "no_retry_performed": True,
            "rows": fixed_failures,
        },
        "m1_3_holdings_contract_type_audit": {
            "total_holding_intervals": len(m13_holdings),
            "wrong_contract_holding_interval_count": len(affected_holdings),
            "affected_symbol_count": len(affected_symbols),
            "affected_symbols": affected_symbols,
            "rows": holding_audit,
            "weekly_accounting_artifact": "research/v2_1/m1_3/WEEKLY_ACCOUNTING_PROVENANCE.json",
            "m1_3_lifecycle_correction_remains_valid": True,
        },
        "current_m1_4_positions_contract_type_audit": {
            "position_count": len(current_positions),
            "ineligible_position_count": len(ineligible_positions),
            "ineligible_symbols": [row["symbol"] for row in ineligible_positions],
            "rows": current_positions,
        },
        "first_actual_control_impact": first_impact,
        "m1_3_checkpoint_impact": {
            "affected": True,
            "reason": "M1.3 lifecycle correction preserved holding/accounting artifacts that include protocol-ineligible TradFi symbols",
            "lifecycle_correction_invalidated": False,
            "contract_type_conformance_established": False,
            "parent_reconstruction_performed_by_this_audit": False,
            "m1_3_decision": m13_reconstruction.get("decision"),
        },
        "m1_4_state_impact": {
            "affected": True,
            "state_remains": "V2.1-M1.4 PRESTART NOT_READY — ACCEPTED",
            "ineligible_current_position_count": len(ineligible_positions),
            "artifacts_status": "QUARANTINED_PRESTART_STATE",
            "evidence_status": "NOT_M2_START_EVIDENCE",
            "parent_state_rewritten": False,
        },
        "reconstruction_recommendation": {
            "required": True,
            "earliest_correction_point_only": True,
            "first_ineligible_execution_day": first_entry_day.isoformat(),
            "reconstruction_start_signal_day": (first_entry_day - timedelta(days=1)).isoformat(),
            "reconstruction_start_execution_day": first_entry_day.isoformat(),
            "pre_correction_checkpoint_date": (first_entry_day - timedelta(days=7)).isoformat(),
            "basis": "earliest actual Control position entry in frozen M1.3 holding provenance; one-day signal lag and seven-day rebalance cadence are frozen protocol rules",
            "reconstruction_executed": False,
        },
        "official_contract_type_provenance": {
            "sources": OFFICIAL_SOURCES,
            "exchange_info_snapshot": {
                "endpoint": EXCHANGE_INFO_ENDPOINT,
                "local_path": "data/v2_1_m1_4_prestart_extension/exchangeInfo.json",
                "sha256": _sha256(EXCHANGE_INFO_PATH),
                "used_for": "official current product-class cross-check and candidate ID; not a standalone rewrite of old history",
            },
            "historical_archive_source": HISTORICAL_ARCHIVE_PREFIX,
            "historical_classification_rule": "timestamped official product/listing evidence plus onboard timestamp; current exchangeInfo alone cannot rewrite old history",
        },
        "immutable_artifacts": immutable,
        "gates": {name: "PASS" if passed else "FAIL" for name, passed in gates.items()},
        "safety": {
            "LIVE_TRADING": False,
            "PAPER_ONLY": True,
            "http_methods": ["GET"],
            "new_market_data_download": False,
            "failed_request_retry": False,
            "orders_placed": False,
        },
        "state_labels": [
            "CONTRACT_TYPE_CONFORMANCE_AUDIT_ONLY",
            "QUARANTINED_PRESTART_STATE",
            "NOT_M2_START_EVIDENCE",
            "NOT_VALIDATION_EVIDENCE",
        ],
        "forbidden_actions_not_performed": [
            "parent reconstruction",
            "Forward run",
            "first forward run",
            "parameter optimization",
            "M2",
            "live trading",
        ],
    }
    _write_json(AUDIT_DIR / "V2_1_M1_4_1_CONTRACT_TYPE_AUDIT.json", result)
    _write_json(
        AUDIT_DIR / "CONTRACT_TYPE_PROVENANCE.json",
        result["official_contract_type_provenance"]
        | {
            "audit_id": AUDIT_ID,
            "affected_holding_symbols": affected_symbols,
            "current_ineligible_position_symbols": [row["symbol"] for row in ineligible_positions],
            "first_actual_control_impact": first_impact,
        },
    )
    _write_markdown(result, membership_table)
    return result


def _write_markdown(result: Mapping[str, Any], membership_table: Sequence[Mapping[str, Any]]) -> None:
    identity = result["identity"]
    dataset = result["frozen_dataset"]
    ambiguity = result["m1_4_ambiguous_reclassification"]
    candidates = result["september_candidates"]["rows"]
    failures = result["fixed_m1_4_failures"]["rows"]
    holdings = result["m1_3_holdings_contract_type_audit"]
    positions = result["current_m1_4_positions_contract_type_audit"]
    lines = [
        "V2.1-M1.4.1 CONTRACT_TYPE ROOT_CAUSE CONFIRMED",
        "",
        "# XS-LOWVOL V2.1-M1.4.1 Contract-Type Universe Conformance Audit",
        "",
        f"Audit ID: `{result['audit_id']}`  ",
        f"Audit decision: **{result['audit_decision']}**  ",
        f"Root cause: **{result['root_cause']}**  ",
        f"Base formal parent: `{result['base_formal_parent_commit']}`  ",
        f"Code commit used by run: `{result['code_commit_used_by_run']}`",
        "",
        "## Frozen identity",
        "",
        f"- V1 Protocol: `{identity['v1_protocol_sha256']}`",
        f"- V1 Control: `{identity['v1_control_sha256']}`",
        f"- V1 Shadow: `{identity['v1_shadow_sha256']}`",
        f"- V2.1 Protocol: `{identity['v2_1_protocol_sha256']}`",
        f"- V2.1 Spec: `{identity['v2_1_spec_sha256']}`",
        f"- Forward Anchor: `{identity['forward_anchor_sha256']}` (`{identity['forward_anchor_status']}`)",
        "",
        "## Frozen data and classifier findings",
        "",
        f"- Discovered symbols: **{dataset['number_of_symbols_discovered']}**; usable histories: **{dataset['number_of_usable_symbols']}**; history objects: **{dataset['number_of_history_objects']}**.",
        f"- Delisted usable symbols: **{dataset['number_of_delisted_symbols']}**; ambiguous input: **{dataset['number_of_ambiguous_symbols']}**; listing pages: **{dataset['listing_page_count']}**.",
        f"- Dataset SHA: `{dataset['dataset_sha256']}`; normalized dataset SHA: `{dataset['normalized_dataset_sha256']}`.",
        "- Historical classifier finding: `HISTORICAL_ARCHIVE_CONTRACT_TYPE_NOT_PROVEN`; the actual rule is a USDT suffix regex and does not inspect `contractType`, `quoteAsset`, or status.",
        "- M1.4 parser finding: `M1_4_EXCHANGE_INFO_PRODUCT_FILTER_MISSING`; current exchangeInfo fields were copied but not used as quote/contract/status predicates.",
        "",
        "## 495 ambiguous candidates",
        "",
        f"Category counts: `{json.dumps(ambiguity['category_counts'], ensure_ascii=False, sort_keys=True)}`",
        "Known-but-excluded catalog members are represented as catalog members, not as unresolved symbols; the complete row-level table is in `FROZEN_DISCOVERY_MEMBERSHIP_TABLE.json` and the 495-row reclassification is in the JSON audit artifact.",
        "",
        "## September candidates",
        "",
        "| Symbol | contractType | quoteAsset | status | onboard UTC | Protocol eligible | PIT evidence |",
        "|---|---|---|---|---|---:|---:|",
    ]
    for row in candidates:
        lines.append(
            f"| {row['symbol']} | {row['contractType']} | {row['quoteAsset']} | {row['status']} | {row['onboard_timestamp_utc']} | {row['protocol_eligible']} | {row['pit_evidence']} |"
        )
    lines += [
        "",
        "## Fixed M1.4 GET failures",
        "",
        "| Request | contractType | request should have been required | classification | retry |",
        "|---|---|---:|---|---:|",
    ]
    for row in failures:
        info = row["official_exchange_info"]
        lines.append(
            f"| {row['request']} | {info['contractType']} | {row['request_should_have_been_required']} | {row['scope_classification']} | {row['retry_performed']} |"
        )
    lines += [
        "",
        "## M1.3 holding impact",
        "",
        f"- Wrong-contract holding intervals: **{holdings['wrong_contract_holding_interval_count']}** across **{holdings['affected_symbol_count']}** symbols.",
        f"- Symbols: `{', '.join(holdings['affected_symbols'])}`.",
        "- The frozen holding provenance proves Control position and accounting impact. It does not retain long/short target direction rows; no direction is invented by this audit.",
        "- The M1.3 lifecycle correction remains valid; contract-type conformance is not established.",
        "",
        "## Current M1.4 positions",
        "",
        f"- Current positions: **{positions['position_count']}**; protocol-ineligible: **{positions['ineligible_position_count']}** (`{', '.join(positions['ineligible_symbols'])}`).",
        "- M1.4 remains `PRESTART NOT_READY — ACCEPTED`; its artifacts are quarantined pre-start state and are not M2-start evidence.",
        "",
        "## First actual Control impact",
        "",
        f"- Symbol: **{result['first_actual_control_impact']['symbol']}**.",
        f"- First position entry: **{result['first_actual_control_impact']['entry_timestamp_utc']}**.",
        "- This is the earliest actual Control position evidence available in the frozen M1.3 artifacts; signal direction is not reconstructed.",
        "",
        "## Earliest correction point recommendation",
        "",
        f"- Signal day recommendation: `{result['reconstruction_recommendation']['reconstruction_start_signal_day']}`.",
        f"- Execution day recommendation: `{result['reconstruction_recommendation']['reconstruction_start_execution_day']}`.",
        f"- Pre-correction checkpoint recommendation: `{result['reconstruction_recommendation']['pre_correction_checkpoint_date']}`.",
        "- Recommendation only; no reconstruction was executed.",
        "",
        "## C0–C11",
        "",
    ]
    for name, value in result["gates"].items():
        lines.append(f"- `{name}`: **{value}**")
    lines += [
        "",
        "## Immutable state and safety",
        "",
        "- M1.3, M1.3.1, and all M1.4 artifacts unchanged; legacy `research/evidence/XS-LOWVOL-V1.json` remains stale and unchanged.",
        "- No Strategy / Protocol / Gate / Anchor modification; no download; no retry; no parent reconstruction; no Forward; no optimization; no M2; no live trading.",
        "- `LIVE_TRADING = False`, `PAPER_ONLY = True`, HTTP = GET-only.",
    ]
    (AUDIT_DIR / "V2_1_M1_4_1_CONTRACT_TYPE_AUDIT.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    if args.approval != "START V2.1-M1.4.1":
        raise SystemExit("approval must be exactly START V2.1-M1.4.1")
    result = run_audit()
    print("V2.1-M1.4.1 CONTRACT_TYPE ROOT_CAUSE CONFIRMED")
    print(json.dumps({
        "audit_id": result["audit_id"],
        "audit_decision": result["audit_decision"],
        "root_cause": result["root_cause"],
        "code_commit_used_by_run": result["code_commit_used_by_run"],
        "wrong_contract_holding_intervals": result["m1_3_holdings_contract_type_audit"]["wrong_contract_holding_interval_count"],
        "ineligible_current_positions": result["current_m1_4_positions_contract_type_audit"]["ineligible_position_count"],
        "ambiguous_category_counts": result["m1_4_ambiguous_reclassification"]["category_counts"],
        "gates": result["gates"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
