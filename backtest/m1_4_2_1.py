"""XS-LOWVOL V2.1-M1.4.2.1 historical product provenance hardening.

This module is deliberately separate from the M1.4.2 reconstruction runner.
It reads the accepted M1.4.2 exclusion set, retrieves only Binance's public
announcement catalog/detail APIs, and produces a symbol-level provenance
registry.  It never imports or executes parent reconstruction code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
OLD_ROOT = ROOT / "research" / "v2_1" / "m1_4_2"
OLD_OVERLAY_PATH = OLD_ROOT / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY.json"
OLD_RESULT_PATH = OLD_ROOT / "V2_1_M1_4_2_PARENT_RECONSTRUCTION.json"
MEMBERSHIP_PATH = (
    ROOT / "research" / "v2_1" / "m1_4_1" / "FROZEN_DISCOVERY_MEMBERSHIP_TABLE.json"
)
NEW_ROOT = ROOT / "research" / "v2_1" / "m1_4_2_1"
REGISTRY_PATH = NEW_ROOT / "HISTORICAL_PRODUCT_TYPE_REGISTRY.json"
OVERLAY_V2_PATH = NEW_ROOT / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY_V2.json"
RESULT_PATH = NEW_ROOT / "V2_1_M1_4_2_1_PRODUCT_PROVENANCE.json"
REPORT_PATH = NEW_ROOT / "V2_1_M1_4_2_1_PRODUCT_PROVENANCE.md"

BASE_COMMIT = "b01086dcc8417f404f369099305a74cf2afaebb1"
OLD_M142_BASE_COMMIT = "d4a05746a4a8dc42ab16d120bcdb9465493a04e0"
RUN_ID = "XS-LOWVOL-V2.1-M1.4.2.1-PRODUCT-PROVENANCE-1"
APPROVAL = "START V2.1-M1.4.2.1"

V1_PROTOCOL_SHA256 = "607d262248a4ef0d0f968c1db61f9f09ffd8ee19a247a46571e131caaa8ea03d"
V1_CONTROL_SHA256 = "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678"
V1_SHADOW_SHA256 = "97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd"
V2_1_SPEC_SHA256 = "e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c"
V2_1_PROTOCOL_SHA256 = "6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d"
FORWARD_ANCHOR_SHA256 = "a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b"
FORWARD_ANCHOR_STATUS = "FROZEN_NOT_YET_STARTED"
M13_OVERLAY_SHA256 = "7a6f183262580933c4802acb9454b0617c7c095b458b79f1e56fadb1c9ec64c1"

CATALOG_ENDPOINT = "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query"
DETAIL_ENDPOINT = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
PUBLIC_ANNOUNCEMENT = "https://www.binance.com/en/support/announcement/detail/{code}"
CATALOG_ID = 48
PAGE_SIZE = 20
START_DATE = "2026-01-08T00:00:00Z"
CUTOFF_DATE = "2026-09-19T00:00:00Z"

KNOWN_SOURCE_URLS = (
    "https://www.binance.com/en/support/announcement/detail/40c76b4deaa247f09774e5d1ee747cb8",
    "https://www.binance.com/en/support/announcement/detail/0a613aed15cc4cf78898594d7c767661",
    "https://www.binance.com/en/support/announcement/detail/32ac927d1cbe4aa3b527eca1c401a98f",
    "https://www.binance.com/en/support/announcement/detail/89a035c3ee0e4b7782bf0089323d8e78",
)

_TIMESTAMP_RE = re.compile(
    r"(?P<date>20\d{2}-\d{2}-\d{2})[ T]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"\s*(?:\(UTC\)|UTC)?",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"(?P<date>20\d{2}-\d{2}-\d{2})(?![ T]\d{2}:\d{2})")
_SYMBOL_TOKEN_TEMPLATE = r"(?<![A-Z0-9]){symbol}(?![A-Z0-9])"
_PRODUCT_MARKERS = (
    "tradfi perpetual",
    "equity perpetual",
    "underlying equity",
    "underlying index",
    "common stock",
    "stock perpetual",
    "etf",
    "equity/index",
    "pre-ipo",
)


class ProvenanceError(RuntimeError):
    """Raised when the immutable/provenance contract cannot be satisfied."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _iso_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timestamp_to_iso(match: re.Match[str]) -> str:
    second = int(match.group("second") or "0")
    dt = datetime(
        int(match.group("date")[:4]),
        int(match.group("date")[5:7]),
        int(match.group("date")[8:10]),
        int(match.group("hour")),
        int(match.group("minute")),
        second,
        tzinfo=timezone.utc,
    )
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _old_artifact_manifest() -> dict[str, str]:
    if not OLD_ROOT.is_dir():
        raise ProvenanceError(f"missing immutable M1.4.2 artifact directory: {OLD_ROOT}")
    return {
        str(path.relative_to(OLD_ROOT)).replace("\\", "/"): _sha256_file(path)
        for path in sorted(OLD_ROOT.rglob("*"))
        if path.is_file()
    }


def _git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _git_status() -> str:
    return subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True).strip()


def load_immutable_scope() -> dict[str, Any]:
    overlay = json.loads(OLD_OVERLAY_PATH.read_text(encoding="utf-8"))
    old_result = json.loads(OLD_RESULT_PATH.read_text(encoding="utf-8"))
    membership = json.loads(MEMBERSHIP_PATH.read_text(encoding="utf-8"))
    excluded = tuple(sorted(overlay.get("excluded_tradifi_symbols", [])))
    if len(excluded) != 145:
        raise ProvenanceError(f"expected 145 old excluded symbols, found {len(excluded)}")
    if old_result.get("decision") != "V2.1-M1.4.2 PARENT_STATE READY":
        raise ProvenanceError("old M1.4.2 result is not the accepted READY artifact")
    if old_result.get("base_commit") != OLD_M142_BASE_COMMIT:
        raise ProvenanceError("old M1.4.2 artifact base identity is not intact")
    table = membership.get("table", [])
    if len(table) != 861 or membership.get("number_of_usable_symbols") != 362:
        raise ProvenanceError("frozen 861/362 membership authority is not intact")
    return {
        "overlay": overlay,
        "old_result": old_result,
        "membership": membership,
        "excluded_symbols": excluded,
        "old_overlay_sha256": _sha256_file(OLD_OVERLAY_PATH),
        "old_result_sha256": _sha256_file(OLD_RESULT_PATH),
    }


def _text_from_body(body: Any) -> str:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return body
    pieces: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("node") == "text" and isinstance(value.get("text"), str):
                pieces.append(value["text"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(body)
    return re.sub(r"\s+", " ", " ".join(pieces)).replace("\u00a0", " ").strip()


def _product_class_is_explicit(text: str, context: str) -> bool:
    # Only the symbol-local context is evidence.  A generic category statement
    # elsewhere on a page cannot classify an arbitrary symbol.
    haystack = context.lower()
    return any(marker in haystack for marker in _PRODUCT_MARKERS)


def _effective_timestamp(text: str, symbol: str, onboard_timestamp: str) -> tuple[str | None, str]:
    """Find a launch timestamp near a symbol and classify its timestamp quality."""
    token = re.compile(_SYMBOL_TOKEN_TEMPLATE.format(symbol=re.escape(symbol)))
    occurrences = list(token.finditer(text))
    if not occurrences:
        return None, "NO_SYMBOL_OCCURRENCE"
    onboard = _parse_iso(onboard_timestamp)
    candidates: list[tuple[int, str, str]] = []
    for occurrence in occurrences:
        start = max(0, occurrence.start() - 900)
        end = min(len(text), occurrence.end() + 900)
        context = text[start:end]
        offset = start
        for match in _TIMESTAMP_RE.finditer(context):
            iso = _timestamp_to_iso(match)
            distance = abs((offset + match.start()) - occurrence.start())
            # A timestamp immediately before the symbol is the normal bullet form.
            direction_penalty = 0 if offset + match.end() <= occurrence.start() else 15
            candidates.append((distance + direction_penalty, iso, "EXPLICIT_TIMESTAMP"))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        valid = [item for item in candidates if _parse_iso(item[1]) <= onboard]
        selected = (valid or candidates)[0]
        return selected[1], selected[2]
    # Date-only records are accepted only when the date is not after onboarding.
    for occurrence in occurrences:
        start = max(0, occurrence.start() - 900)
        end = min(len(text), occurrence.end() + 900)
        for match in _DATE_RE.finditer(text[start:end]):
            candidate = datetime.fromisoformat(match.group("date")).replace(tzinfo=timezone.utc)
            if candidate <= onboard:
                return candidate.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "DATE_ONLY"
    return None, "NO_EFFECTIVE_TIMESTAMP"


def parse_symbol_evidence(
    article: dict[str, Any], symbol: str, onboard_timestamp: str
) -> dict[str, Any] | None:
    """Parse one symbol from one official detail response.

    This helper intentionally requires the symbol to occur in the official
    announcement body and requires explicit equity/TradFi product language.
    Current exchangeInfo and the generic category boundary are never inputs.
    """
    data = article.get("data") or {}
    body = data.get("body")
    text = _text_from_body(body)
    token = re.compile(_SYMBOL_TOKEN_TEMPLATE.format(symbol=re.escape(symbol)))
    match = token.search(text)
    if not match:
        return None
    context = text[max(0, match.start() - 500) : min(len(text), match.end() + 500)]
    title = str(data.get("title") or article.get("title") or "")
    if not _product_class_is_explicit(text, f"{title} {context}"):
        return None
    effective, method = _effective_timestamp(text, symbol, onboard_timestamp)
    if effective is None:
        return None
    detail_code = str(data.get("code") or article.get("code") or "")
    publication = article.get("releaseDate") or data.get("releaseDate")
    if publication is None:
        publication_iso = None
    else:
        publication_iso = _iso_timestamp(int(publication))
    raw_body = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, sort_keys=True).encode("utf-8")
    lower_title = title.lower()
    method_name = (
        "SYMBOL_SPECIFIC_OFFICIAL_LISTING_ANNOUNCEMENT"
        if "multiple" not in lower_title
        else "MULTI_SYMBOL_OFFICIAL_TRADFI_LISTING_ANNOUNCEMENT"
    )
    return {
        "symbol": symbol,
        "official_source_url": PUBLIC_ANNOUNCEMENT.format(code=detail_code),
        "detail_api_url": f"{DETAIL_ENDPOINT}?{urlencode({'articleCode': detail_code})}",
        "source_title": title,
        "source_publication_timestamp": publication_iso,
        "listing_effective_timestamp": effective,
        "source_content_sha256": _sha256_bytes(raw_body),
        "evidence_method": method_name,
        "timestamp_parse_method": method,
        "evidence_status": "PROVEN_HISTORICAL_PRODUCT_TYPE",
        "explicit_product_class": True,
        "body_excerpt_sha256": _sha256_bytes(context.encode("utf-8")),
    }


def _fetch_json(url: str, attempts: int = 4) -> tuple[dict[str, Any], bytes]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "XS-LOWVOL-M1.4.2.1-provenance/1.0",
                },
                method="GET",
            )
            with urlopen(request, timeout=40) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("code") != "000000":
                raise ProvenanceError(f"Binance public API returned {payload.get('code')}: {url}")
            return payload, raw
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ProvenanceError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.75 * (attempt + 1))
    raise ProvenanceError(f"official announcement GET failed after {attempts} attempts: {url}: {last_error}")


def _release_in_scope(release_date: int) -> bool:
    value = datetime.fromtimestamp(release_date / 1000, tz=timezone.utc)
    return _parse_iso(START_DATE) <= value < _parse_iso(CUTOFF_DATE)


def _is_launch_candidate(title: str) -> bool:
    lower = title.lower()
    return "futures" in lower and "launch" in lower


def crawl_official_sources(excluded_symbols: Iterable[str]) -> dict[str, Any]:
    """Fetch the official listing catalog and only relevant detail pages."""
    excluded = set(excluded_symbols)
    candidates: dict[str, dict[str, Any]] = {}
    catalog_pages: list[dict[str, Any]] = []
    page = 1
    total = None
    while total is None or (page - 1) * PAGE_SIZE < total:
        query = urlencode({"type": 1, "catalogId": CATALOG_ID, "pageNo": page, "pageSize": PAGE_SIZE})
        payload, raw = _fetch_json(f"{CATALOG_ENDPOINT}?{query}")
        catalog = (payload.get("data") or {}).get("catalogs", [{}])[0]
        articles = catalog.get("articles", [])
        total = int(catalog.get("total") or 0)
        catalog_pages.append(
            {
                "page": page,
                "article_count": len(articles),
                "response_sha256": _sha256_bytes(raw),
                "oldest_release_timestamp": _iso_timestamp(int(articles[-1]["releaseDate"])) if articles else None,
                "newest_release_timestamp": _iso_timestamp(int(articles[0]["releaseDate"])) if articles else None,
            }
        )
        for article in articles:
            title = str(article.get("title") or "")
            release_date = int(article.get("releaseDate") or 0)
            if _release_in_scope(release_date) and _is_launch_candidate(title):
                candidates[str(article.get("code"))] = dict(article)
        if not articles or page * PAGE_SIZE >= total:
            break
        page += 1

    for source_url in KNOWN_SOURCE_URLS:
        code = source_url.rstrip("/").split("/")[-1]
        if code not in candidates:
            candidates[code] = {"code": code, "title": "KNOWN_OFFICIAL_SOURCE", "releaseDate": None}

    by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in sorted(excluded)}
    source_records: dict[str, dict[str, Any]] = {}
    for index, article in enumerate(candidates.values(), start=1):
        code = str(article["code"])
        detail_query = urlencode({"articleCode": code})
        detail_url = f"{DETAIL_ENDPOINT}?{detail_query}"
        detail_payload, detail_raw = _fetch_json(detail_url)
        detail_data = detail_payload.get("data") or {}
        if article.get("releaseDate") is None and detail_data.get("releaseDate") is not None:
            article["releaseDate"] = detail_data["releaseDate"]
        detail_data["code"] = code
        detail_payload["data"] = detail_data
        covered: list[str] = []
        for symbol in sorted(excluded):
            evidence = parse_symbol_evidence(detail_payload, symbol, "9999-12-31T00:00:00.000Z")
            # Effective timestamp is re-parsed against the real onboard value below.
            if evidence:
                covered.append(symbol)
                by_symbol[symbol].append({"article": article, "detail": detail_payload, "raw": detail_raw})
        if covered:
            body = detail_data.get("body")
            body_bytes = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, sort_keys=True).encode("utf-8")
            source_records[code] = {
                "source_id": code,
                "official_source_url": PUBLIC_ANNOUNCEMENT.format(code=code),
                "detail_api_url": detail_url,
                "source_title": str(detail_data.get("title") or article.get("title") or ""),
                "source_publication_timestamp": _iso_timestamp(int(article["releaseDate"])) if article.get("releaseDate") else None,
                "source_content_sha256": _sha256_bytes(body_bytes),
                "covered_symbols": covered,
                "detail_response_sha256": _sha256_bytes(detail_raw),
            }
        # Keep the crawler visibly bounded and polite without changing semantics.
        if index < len(candidates):
            time.sleep(0.05)
    return {
        "catalog_id": CATALOG_ID,
        "catalog_endpoint": CATALOG_ENDPOINT,
        "catalog_total": total,
        "catalog_pages": catalog_pages,
        "candidate_count": len(candidates),
        "candidate_source_ids": sorted(candidates),
        "source_records": sorted(source_records.values(), key=lambda item: item["source_id"]),
        "by_symbol": by_symbol,
    }


def _choose_symbol_source(
    symbol: str, old_row: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    onboard = old_row.get("effective_from_timestamp")
    if not onboard:
        return None
    valid: list[dict[str, Any]] = []
    for item in candidates:
        evidence = parse_symbol_evidence(item["detail"], symbol, onboard)
        if evidence is None:
            continue
        evidence["onboard_timestamp"] = onboard
        effective = _parse_iso(evidence["listing_effective_timestamp"])
        publication = evidence.get("source_publication_timestamp")
        if effective > _parse_iso(onboard):
            continue
        if publication and _parse_iso(publication) > effective:
            continue
        valid.append(evidence)
    if not valid:
        return None
    valid.sort(key=lambda row: (row.get("source_publication_timestamp") or "", row["official_source_url"]))
    return valid[0]


def _current_missing_usable_symbols(scope: dict[str, Any]) -> list[dict[str, Any]]:
    exchange_path = ROOT / "data" / "v2_1_m1_4_prestart_extension" / "exchangeInfo.json"
    exchange = json.loads(exchange_path.read_text(encoding="utf-8"))
    current = {str(row.get("symbol")): row for row in exchange.get("symbols", [])}
    rows = []
    for item in scope["membership"]["table"]:
        if not item.get("usable_history") or item["symbol"] in current:
            continue
        first_month = str(item.get("first_discovered_month") or "")
        rows.append(
            {
                "symbol": item["symbol"],
                "first_frozen_catalog_month": first_month,
                "current_exchange_info_present": False,
                "classification": (
                    "PRE_TRADFI_CATEGORY_EXISTENCE_CONFIRMED"
                    if first_month < "2026-01"
                    else "HISTORICAL_PRODUCT_TYPE_UNRESOLVED"
                ),
                "historical_source_required": first_month >= "2026-01",
            }
        )
    return sorted(rows, key=lambda item: item["symbol"])


def build_provenance(scope: dict[str, Any], crawl: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    old_rows = {row["symbol"]: row for row in scope["overlay"]["rows"]}
    excluded = list(scope["excluded_symbols"])
    registry_rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    source_by_code: dict[str, dict[str, Any]] = {
        item["source_id"]: item for item in crawl["source_records"]
    }
    for symbol in excluded:
        source = _choose_symbol_source(symbol, old_rows[symbol], crawl["by_symbol"].get(symbol, []))
        if source is None:
            unresolved.append(symbol)
            registry_rows.append(
                {
                    "symbol": symbol,
                    "onboard_timestamp": old_rows[symbol].get("effective_from_timestamp"),
                    "historical_contract_type": "TRADIFI_PERPETUAL",
                    "historical_quote_asset": "USDT",
                    "historical_product_category": "TRADFI",
                    "official_source_url": None,
                    "source_title": None,
                    "source_publication_timestamp": None,
                    "listing_effective_timestamp": None,
                    "source_content_sha256": None,
                    "evidence_method": None,
                    "evidence_status": "HISTORICAL_PRODUCT_TYPE_UNRESOLVED",
                    "evidence_found": False,
                }
            )
            continue
        source_id = source["official_source_url"].rstrip("/").split("/")[-1]
        source["source_id"] = source_id
        registry_rows.append(
            {
                "symbol": symbol,
                "onboard_timestamp": source["onboard_timestamp"],
                "historical_contract_type": "TRADIFI_PERPETUAL",
                "historical_quote_asset": "USDT",
                "historical_product_category": "TRADFI",
                "official_source_url": source["official_source_url"],
                "source_title": source["source_title"],
                "source_publication_timestamp": source["source_publication_timestamp"],
                "listing_effective_timestamp": source["listing_effective_timestamp"],
                "source_content_sha256": source["source_content_sha256"],
                "evidence_method": source["evidence_method"],
                "evidence_status": source["evidence_status"],
                "evidence_found": True,
                "timestamp_parse_method": source["timestamp_parse_method"],
            }
        )

    current_missing = _current_missing_usable_symbols(scope)
    post_start_unknowns = [
        item["symbol"]
        for item in current_missing
        if item["classification"] == "HISTORICAL_PRODUCT_TYPE_UNRESOLVED"
    ]
    registry_complete = len(unresolved) == 0 and len(post_start_unknowns) == 0
    registry = {
        "schema_version": "HISTORICAL_PRODUCT_TYPE_REGISTRY.v1",
        "run_id": RUN_ID,
        "scope": "M1.4.2 EXCLUDED TRADFI SYMBOLS ONLY; NO PARENT RECONSTRUCTION",
        "classification": ["PRODUCT_PROVENANCE_ONLY", "NOT_FORWARD", "NOT_M2_START_EVIDENCE"],
        "historical_product_registry_complete": registry_complete,
        "current_exchangeinfo_not_used_as_historical_proof": True,
        "original_excluded_tradifi_count": len(excluded),
        "historically_proven_tradifi_count": sum(1 for row in registry_rows if row["evidence_found"]),
        "unresolved_historical_product_types": unresolved + post_start_unknowns,
        "current_exchangeinfo_missing_usable_symbols": current_missing,
        "rows": registry_rows,
        "announcement_sources": sorted(source_by_code.values(), key=lambda item: item["source_id"]),
        "announcement_to_covered_symbols": {
            item["source_id"]: item["covered_symbols"] for item in sorted(source_by_code.values(), key=lambda item: item["source_id"])
        },
    }

    old_set = set(excluded)
    new_set = {row["symbol"] for row in registry_rows if row["evidence_found"]}
    overlay = {
        "schema_version": "CONTRACT_TYPE_ELIGIBILITY_OVERLAY_V2.v1",
        "run_id": RUN_ID,
        "classification": ["PRODUCT_PROVENANCE_ONLY", "NOT_FORWARD", "NOT_M2_START_EVIDENCE"],
        "source_semantics": "SYMBOL_LEVEL_TIMESTAMPED_OFFICIAL_HISTORICAL_PRODUCT_EVIDENCE_ONLY",
        "generic_category_boundary_not_used_as_symbol_proof": True,
        "current_exchangeinfo_not_used_as_historical_proof": True,
        "membership_set_unchanged": old_set == new_set == set(excluded),
        "original_excluded_tradifi_symbols": excluded,
        "proven_excluded_tradifi_symbols": sorted(new_set),
        "symbols_added": sorted(new_set - old_set),
        "symbols_removed": sorted(old_set - new_set),
        "effective_timestamp_differences": [],
        "old_overlay_sha256": scope["old_overlay_sha256"],
        "rows": registry_rows,
        "announcement_sources": registry["announcement_sources"],
    }
    result = {
        "schema_version": "V2_1_M1_4_2_1_PRODUCT_PROVENANCE.v1",
        "run_id": RUN_ID,
        "run_type": "HISTORICAL_CONTRACT_TYPE_PROVENANCE_HARDENING",
        "base_commit": BASE_COMMIT,
        "code_commit_used_by_run": _git_head(),
        "decision": "V2.1-M1.4.2.1 PRODUCT_PROVENANCE READY" if registry_complete and overlay["membership_set_unchanged"] else "V2.1-M1.4.2.1 PRODUCT_PROVENANCE NOT_READY",
        "audit_id": RUN_ID,
        "classification": ["PRODUCT_PROVENANCE_ONLY", "NOT_FORWARD", "NOT_M2_START_EVIDENCE"],
        "original_excluded_tradifi_count": len(excluded),
        "historically_proven_tradifi_count": registry["historically_proven_tradifi_count"],
        "unresolved_count": len(registry["unresolved_historical_product_types"]),
        "announcement_count": len(registry["announcement_sources"]),
        "multi_symbol_announcement_count": sum(
            1 for item in registry["announcement_sources"] if len(item["covered_symbols"]) > 1
        ),
        "affected_15_completeness": all(
            row["evidence_found"]
            for row in registry_rows
            if row["symbol"]
            in {
                "AXTIUSDT", "CBRSUSDT", "CRCLUSDT", "DRAMUSDT", "INTCUSDT", "LITEUSDT",
                "MSTRUSDT", "MUUUSDT", "MVLLUSDT", "NBISUSDT", "SNDKUSDT", "SNXXUSDT",
                "SOXLUSDT", "SOXSUSDT", "TSLAUSDT",
            }
        ),
        "tsla_evidence": next((row for row in registry_rows if row["symbol"] == "TSLAUSDT"), None),
        "post_jan_08_unknown_count": len(post_start_unknowns),
        "membership_set_unchanged": overlay["membership_set_unchanged"],
        "symbols_added": overlay["symbols_added"],
        "symbols_removed": overlay["symbols_removed"],
        "effective_timestamp_differences": overlay["effective_timestamp_differences"],
        "identity": {
            "v1_protocol_sha256": V1_PROTOCOL_SHA256,
            "v1_control_sha256": V1_CONTROL_SHA256,
            "v1_shadow_sha256": V1_SHADOW_SHA256,
            "v2_1_spec_sha256": V2_1_SPEC_SHA256,
            "v2_1_protocol_sha256": V2_1_PROTOCOL_SHA256,
            "forward_anchor_sha256": FORWARD_ANCHOR_SHA256,
            "forward_anchor_status": FORWARD_ANCHOR_STATUS,
            "m1_3_overlay_sha256": M13_OVERLAY_SHA256,
        },
        "old_m1_4_2_artifacts_unchanged": False,
        "parent_reconstruction_executed": False,
        "parent_reconstruction_window": None,
        "latest_13_recomputed": False,
        "reference_vol_recomputed": False,
        "position_scale_recomputed": False,
        "current_positions_recomputed": False,
        "forward_evidence_created": False,
        "first_forward_signal_selected": False,
        "m2_started": False,
        "strategy_changed": False,
        "protocol_changed": False,
        "parameter_optimization": False,
        "live_trading": False,
        "paper_only": True,
        "http_method_policy": "GET_ONLY",
        "catalog_id": CATALOG_ID,
        "catalog_page_count": len(crawl["catalog_pages"]),
        "catalog_total": crawl["catalog_total"],
        "gates": {},
        "failed_gates": [],
    }
    return registry, overlay, result


def _set_gates(result: dict[str, Any], scope: dict[str, Any], registry: dict[str, Any], overlay: dict[str, Any]) -> None:
    rows = registry["rows"]
    tsla = result.get("tsla_evidence") or {}
    old_after = _old_artifact_manifest()
    affected = {
        "AXTIUSDT", "CBRSUSDT", "CRCLUSDT", "DRAMUSDT", "INTCUSDT", "LITEUSDT",
        "MSTRUSDT", "MUUUSDT", "MVLLUSDT", "NBISUSDT", "SNDKUSDT", "SNXXUSDT",
        "SOXLUSDT", "SOXSUSDT", "TSLAUSDT",
    }
    gates = {
        "H0_identity": result["base_commit"] == BASE_COMMIT and result["identity"]["v1_protocol_sha256"] == V1_PROTOCOL_SHA256,
        "H1_old_m142_artifacts_immutable": old_after == scope["old_artifact_manifest_before"],
        "H2_tradfi_registry_scope_complete": len(rows) == 145 and len(registry["announcement_sources"]) >= 1,
        "H3_symbol_level_historical_sources": all(row["evidence_found"] for row in rows),
        "H4_effective_timestamp_provenance": all(
            row["evidence_found"]
            and row.get("listing_effective_timestamp")
            and _parse_iso(row["listing_effective_timestamp"]) <= _parse_iso(row["onboard_timestamp"])
            for row in rows
        ),
        "H5_all_145_classified": registry["historically_proven_tradifi_count"] == 145 and registry["unresolved_historical_product_types"] == [],
        "H6_affected_15_classified": result["affected_15_completeness"],
        "H7_tsla_symbol_level_provenance": (
            tsla.get("evidence_found") is True
            and "40c76b4deaa247f09774e5d1ee747cb8" in str(tsla.get("official_source_url"))
            and tsla.get("listing_effective_timestamp") == "2026-01-28T14:30:00.000Z"
        ),
        "H8_post_2026_01_08_unknowns_fail_closed": result["post_jan_08_unknown_count"] == 0,
        "H9_membership_set_comparison": overlay["membership_set_unchanged"] is True,
        "H10_anchor_still_frozen": result["identity"]["forward_anchor_status"] == FORWARD_ANCHOR_STATUS and result["identity"]["forward_anchor_sha256"] == FORWARD_ANCHOR_SHA256,
        "H11_no_parent_reconstruction": result["parent_reconstruction_executed"] is False and result["parent_reconstruction_window"] is None,
        "H12_no_forward_evidence": result["forward_evidence_created"] is False and result["first_forward_signal_selected"] is False,
    }
    result["old_m1_4_2_artifacts_unchanged"] = gates["H1_old_m142_artifacts_immutable"]
    result["gates"] = {key: "PASS" if value else "FAIL" for key, value in gates.items()}
    result["failed_gates"] = [key for key, value in gates.items() if not value]
    result["decision"] = (
        "V2.1-M1.4.2.1 PRODUCT_PROVENANCE READY"
        if not result["failed_gates"]
        else "V2.1-M1.4.2.1 PRODUCT_PROVENANCE NOT_READY"
    )


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _sha256_file(path)


def _render_report(result: dict[str, Any], registry: dict[str, Any], overlay: dict[str, Any]) -> str:
    gates = "\n".join(f"- `{name}`: `{value}`" for name, value in result["gates"].items())
    failed = ", ".join(result["failed_gates"]) or "none"
    return f"""# XS-LOWVOL V2.1-M1.4.2.1 Product Provenance

{result['decision']}

This is a provenance-only artifact. It does not reconstruct the parent, recompute latest-13, or create Forward evidence. The original `research/v2_1/m1_4_2/**` artifacts remain immutable and are classified as `INDEPENDENT_ACCEPTANCE_PENDING` / `NOT_M2_START_EVIDENCE`.

## Scope

- Run ID: `{result['run_id']}`
- Base commit: `{result['base_commit']}`
- Original excluded TradFi count: `{result['original_excluded_tradifi_count']}`
- Historically proven TradFi count: `{result['historically_proven_tradifi_count']}`
- Unresolved count: `{result['unresolved_count']}`
- Announcement count: `{result['announcement_count']}`
- Multi-symbol announcement count: `{result['multi_symbol_announcement_count']}`
- Membership set unchanged: `{result['membership_set_unchanged']}`
- Added symbols: `{result['symbols_added']}`
- Removed symbols: `{result['symbols_removed']}`
- Effective timestamp differences: `{result['effective_timestamp_differences']}`

## Required prohibitions

- Parent reconstruction executed: `{result['parent_reconstruction_executed']}`
- Latest-13 recomputed: `{result['latest_13_recomputed']}`
- Reference volatility recomputed: `{result['reference_vol_recomputed']}`
- Position scale recomputed: `{result['position_scale_recomputed']}`
- Current positions recomputed: `{result['current_positions_recomputed']}`
- Forward evidence created: `{result['forward_evidence_created']}`
- M2 started: `{result['m2_started']}`
- HTTP policy: `{result['http_method_policy']}`

## H0-H12

{gates}

Failed gates: `{failed}`.

The complete machine-readable record is `V2_1_M1_4_2_1_PRODUCT_PROVENANCE.json`; symbol rows and announcement-to-symbol mappings are in the registry and V2 overlay.
"""


def run_formal(approval: str) -> dict[str, Any]:
    if approval != APPROVAL:
        raise ProvenanceError(f"exact approval required: {APPROVAL}")
    if _git_status():
        raise ProvenanceError("formal run requires a clean working tree")
    head = _git_head()
    if head == BASE_COMMIT or subprocess.call(
        ["git", "merge-base", "--is-ancestor", BASE_COMMIT, head], cwd=ROOT
    ) != 0:
        raise ProvenanceError("formal run must be based on b01086d...")
    if NEW_ROOT.exists():
        raise ProvenanceError("M1.4.2.1 result scope already exists; corrected formal run cannot be repeated")
    scope = load_immutable_scope()
    scope["old_artifact_manifest_before"] = _old_artifact_manifest()
    crawl = crawl_official_sources(scope["excluded_symbols"])
    registry, overlay, result = build_provenance(scope, crawl)
    _set_gates(result, scope, registry, overlay)
    result["old_m1_4_2_artifacts_unchanged"] = result["gates"]["H1_old_m142_artifacts_immutable"] == "PASS"
    NEW_ROOT.mkdir(parents=True)
    registry_sha = _write_json(REGISTRY_PATH, registry)
    overlay_sha = _write_json(OVERLAY_V2_PATH, overlay)
    result["registry_sha256"] = registry_sha
    result["overlay_v2_sha256"] = overlay_sha
    result["old_m1_4_2_artifact_manifest_sha256"] = _sha256_json(scope["old_artifact_manifest_before"])
    result["identity"]["old_m1_4_2_overlay_sha256"] = scope["old_overlay_sha256"]
    result["identity"]["old_m1_4_2_result_sha256"] = scope["old_result_sha256"]
    report = _render_report(result, registry, overlay)
    REPORT_PATH.write_text(report, encoding="utf-8")
    result["report_sha256"] = _sha256_file(REPORT_PATH)
    _write_json(RESULT_PATH, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_formal(args.approval)
    except Exception as exc:  # formal command must report a deterministic stop condition
        print("V2.1-M1.4.2.1 PRODUCT_PROVENANCE NOT_READY")
        print(json.dumps({"run_id": RUN_ID, "run_error": str(exc), "parent_reconstruction_executed": False}, indent=2))
        return 1
    print(result["decision"])
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["decision"].endswith("READY") else 1


if __name__ == "__main__":
    sys.exit(main())
