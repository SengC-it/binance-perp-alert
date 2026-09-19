"""XS-LOWVOL V2.1-M1.4.2.1a historical product provenance correction.

This runner revalidates the immutable 145-symbol provenance scope only. It
does not import or execute any parent reconstruction, portfolio simulation, or
risk initialization code.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from backtest.m1_4_2_1 import (
    FORWARD_ANCHOR_SHA256,
    FORWARD_ANCHOR_STATUS,
    M13_OVERLAY_SHA256,
    PUBLIC_ANNOUNCEMENT,
    ROOT,
    V1_CONTROL_SHA256,
    V1_PROTOCOL_SHA256,
    V1_SHADOW_SHA256,
    V2_1_PROTOCOL_SHA256,
    V2_1_SPEC_SHA256,
    _current_missing_usable_symbols,
    _fetch_json,
    _git_head,
    _git_status,
    _old_artifact_manifest,
    _parse_iso,
    _sha256_bytes,
    _sha256_file,
    _sha256_json,
    load_immutable_scope,
    parse_structured_listing_table,
    parse_symbol_evidence,
)


BASE_COMMIT = "738c874d578c160c72f84feb92d5fadfb6b9b1b2"
RUN_ID = "XS-LOWVOL-V2.1-M1.4.2.1A-PARSER-CORRECTIVE-1"
APPROVAL = "START V2.1-M1.4.2.1A"

OLD_ROOT = ROOT / "research" / "v2_1" / "m1_4_2"
OLD_STAGE_ROOT = ROOT / "research" / "v2_1" / "m1_4_2_1"
OLD_STAGE_REGISTRY_PATH = OLD_STAGE_ROOT / "HISTORICAL_PRODUCT_TYPE_REGISTRY.json"
NEW_ROOT = ROOT / "research" / "v2_1" / "m1_4_2_1a"
REGISTRY_PATH = NEW_ROOT / "HISTORICAL_PRODUCT_TYPE_REGISTRY_CORRECTED.json"
OVERLAY_PATH = NEW_ROOT / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY_V3.json"
RESULT_PATH = NEW_ROOT / "V2_1_M1_4_2_1A_PRODUCT_PROVENANCE.json"
REPORT_PATH = NEW_ROOT / "V2_1_M1_4_2_1A_PRODUCT_PROVENANCE.md"
CI_GUARD_TEST_PATH = ROOT / "tests" / "test_v2_1_m1_4_2_1_product_provenance.py"

REGRESSION_SOURCE_ID = "3c2b63c9c3e44d40b8cec99961166fe8"
REGRESSION_SYMBOLS = (
    "PATHUSDT",
    "AMCUSDT",
    "CYPHUSDT",
    "ANETUSDT",
    "HUTUSDT",
    "APLDUSDT",
    "AGPUUSDT",
)
REGRESSION_TIMESTAMPS = {
    "PATHUSDT": "2026-09-18T09:00:00Z",
    "AMCUSDT": "2026-09-18T09:05:00Z",
    "CYPHUSDT": "2026-09-18T09:10:00Z",
    "ANETUSDT": "2026-09-18T09:15:00Z",
    "HUTUSDT": "2026-09-18T09:20:00Z",
    "APLDUSDT": "2026-09-18T09:25:00Z",
    "AGPUUSDT": "2026-09-18T09:30:00Z",
}
PREVIOUSLY_UNRESOLVED = {
    "AMCUSDT",
    "ANETUSDT",
    "APLDUSDT",
    "CYPHUSDT",
    "HUTUSDT",
    "PATHUSDT",
}


class ProvenanceCorrectionError(RuntimeError):
    """Raised when a frozen provenance precondition is not satisfied."""


def _old_stage_manifest() -> dict[str, str]:
    if not OLD_STAGE_ROOT.is_dir():
        raise ProvenanceCorrectionError(f"missing immutable M1.4.2.1 scope: {OLD_STAGE_ROOT}")
    return {
        str(path.relative_to(OLD_STAGE_ROOT)).replace("\\", "/"): _sha256_file(path)
        for path in sorted(OLD_STAGE_ROOT.rglob("*"))
        if path.is_file()
    }


def _load_old_provenance() -> dict[str, Any]:
    payload = json.loads(OLD_STAGE_REGISTRY_PATH.read_text(encoding="utf-8"))
    if payload.get("original_excluded_tradifi_count") != 145:
        raise ProvenanceCorrectionError("old M1.4.2.1 registry is not the 145-symbol scope")
    if payload.get("historically_proven_tradifi_count") != 139:
        raise ProvenanceCorrectionError("old M1.4.2.1 registry is not the accepted 139/145 result")
    if len(payload.get("unresolved_historical_product_types", [])) != 6:
        raise ProvenanceCorrectionError("old M1.4.2.1 unresolved scope is not exactly six symbols")
    sources = payload.get("announcement_sources", [])
    if len(sources) != 48:
        raise ProvenanceCorrectionError("old M1.4.2.1 source scope is not the accepted 48 announcements")
    return payload


def _prepare_source(source: dict[str, Any]) -> dict[str, Any]:
    source_id = str(source["source_id"])
    expected_url = PUBLIC_ANNOUNCEMENT.format(code=source_id)
    if source.get("official_source_url") != expected_url:
        raise ProvenanceCorrectionError(f"official URL/code mismatch for {source_id}")
    payload, raw = _fetch_json(str(source["detail_api_url"]))
    data = dict(payload.get("data") or {})
    actual_code = str(data.get("code") or payload.get("code") or source_id)
    if actual_code != source_id:
        raise ProvenanceCorrectionError(f"official detail code mismatch for {source_id}")
    data["code"] = source_id
    if not data.get("title"):
        data["title"] = source.get("source_title") or ""
    payload["data"] = data
    structured = parse_structured_listing_table(payload)
    body = data.get("body")
    body_bytes = (
        body.encode("utf-8")
        if isinstance(body, str)
        else json.dumps(body, sort_keys=True).encode("utf-8")
    )
    prepared = dict(source)
    prepared.update(
        {
            "detail_response_sha256": _sha256_bytes(raw),
            "current_source_content_sha256": _sha256_bytes(body_bytes),
            "source_content_sha256_unchanged": (
                _sha256_bytes(body_bytes) == source.get("source_content_sha256")
            ),
            "structured_symbols": sorted(structured),
            "_detail": payload,
        }
    )
    return prepared


def _fetch_accepted_sources(old_provenance: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _prepare_source(source)
        for source in sorted(
            old_provenance["announcement_sources"], key=lambda item: item["source_id"]
        )
    ]


def _choose_evidence(
    symbol: str,
    old_row: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any] | None:
    onboard = old_row.get("effective_from_timestamp")
    if not onboard:
        return None
    valid: list[dict[str, Any]] = []
    for source in sources:
        evidence = parse_symbol_evidence(source["_detail"], symbol, onboard)
        if evidence is None:
            continue
        publication = (
            evidence.get("source_publication_timestamp")
            or source.get("source_publication_timestamp")
        )
        effective = _parse_iso(evidence["listing_effective_timestamp"])
        if effective > _parse_iso(onboard):
            continue
        if publication and _parse_iso(publication) > effective:
            continue
        evidence["source_publication_timestamp"] = publication
        evidence["source_id"] = source["source_id"]
        evidence["source_content_sha256_unchanged"] = source[
            "source_content_sha256_unchanged"
        ]
        valid.append(evidence)
    if not valid:
        return None
    valid.sort(
        key=lambda row: (
            0
            if row.get("evidence_method")
            == "STRUCTURED_MULTI_SYMBOL_OFFICIAL_LISTING_TABLE"
            else 1,
            row.get("source_publication_timestamp") or "",
            row.get("official_source_url") or "",
        )
    )
    return valid[0]


def _row_from_evidence(
    symbol: str,
    old_row: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    base = {
        "symbol": symbol,
        "onboard_timestamp": old_row.get("effective_from_timestamp"),
        "historical_contract_type": "TRADIFI_PERPETUAL",
        "historical_quote_asset": "USDT",
        "historical_product_category": "TRADFI",
    }
    if evidence is None:
        base.update(
            {
                "official_source_url": None,
                "source_title": None,
                "source_publication_timestamp": None,
                "listing_effective_timestamp": None,
                "source_content_sha256": None,
                "evidence_method": None,
                "evidence_status": "HISTORICAL_PRODUCT_TYPE_UNRESOLVED",
                "evidence_found": False,
                "source_content_sha256_unchanged": False,
            }
        )
        return base
    base.update(
        {
            "official_source_url": evidence["official_source_url"],
            "source_title": evidence["source_title"],
            "source_publication_timestamp": evidence.get(
                "source_publication_timestamp"
            ),
            "listing_effective_timestamp": evidence[
                "listing_effective_timestamp"
            ],
            "source_content_sha256": evidence["source_content_sha256"],
            "evidence_method": evidence["evidence_method"],
            "evidence_status": "PROVEN_HISTORICAL_PRODUCT_TYPE",
            "evidence_found": True,
            "timestamp_parse_method": evidence.get("timestamp_parse_method"),
            "source_id": evidence.get("source_id"),
            "source_content_sha256_unchanged": evidence[
                "source_content_sha256_unchanged"
            ],
        }
    )
    for key in (
        "parser_mode",
        "table_index",
        "table_column_index",
        "contract_type_cell",
        "launch_time_cell",
        "underlying_evidence_cell",
        "settlement_asset_cell",
        "table_column_evidence_sha256",
        "table_evidence_hash",
    ):
        if key in evidence:
            base[key] = evidence[key]
    return base


def _timestamp_differences(
    old_rows: dict[str, dict[str, Any]],
    new_rows: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    differences: list[dict[str, str]] = []
    for symbol in sorted(old_rows):
        expected = old_rows[symbol].get("effective_from_timestamp")
        actual = new_rows.get(symbol, {}).get("listing_effective_timestamp")
        if not expected or not actual:
            differences.append(
                {"symbol": symbol, "expected": expected or "", "actual": actual or ""}
            )
            continue
        if _parse_iso(expected) != _parse_iso(actual):
            differences.append(
                {"symbol": symbol, "expected": expected, "actual": actual}
            )
    return differences


def _previously_proven_changed_count(
    old_registry: dict[str, Any],
    new_rows: dict[str, dict[str, Any]],
) -> int:
    changed = 0
    for old in old_registry.get("rows", []):
        if old.get("evidence_status") != "PROVEN_HISTORICAL_PRODUCT_TYPE":
            continue
        new = new_rows.get(old["symbol"])
        if not new:
            changed += 1
            continue
        for key in (
            "evidence_status",
            "historical_contract_type",
            "historical_quote_asset",
            "historical_product_category",
        ):
            if new.get(key) != old.get(key):
                changed += 1
                break
        else:
            if old.get("listing_effective_timestamp") and (
                _parse_iso(old["listing_effective_timestamp"])
                != _parse_iso(
                    new.get("listing_effective_timestamp", "1970-01-01T00:00:00Z")
                )
            ):
                changed += 1
    return changed


def _phase_safe_ci_guard() -> bool:
    source = CI_GUARD_TEST_PATH.read_text(encoding="utf-8")
    return (
        "old_m1_4_2_artifact_manifest_sha256" in source
        and 'assert not (Path("research/v2_1/m1_4_2_1")).exists()' not in source
    )


def _build_artifacts(
    scope: dict[str, Any],
    old_provenance: dict[str, Any],
    sources: list[dict[str, Any]],
    old_m1421_manifest_before: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    old_rows = {row["symbol"]: row for row in scope["overlay"]["rows"]}
    registry_rows = [
        _row_from_evidence(
            symbol,
            old_rows[symbol],
            _choose_evidence(symbol, old_rows[symbol], sources),
        )
        for symbol in scope["excluded_symbols"]
    ]
    new_rows = {row["symbol"]: row for row in registry_rows}
    unresolved = sorted(
        row["symbol"] for row in registry_rows if not row["evidence_found"]
    )
    proven = sorted(
        row["symbol"] for row in registry_rows if row["evidence_found"]
    )
    current_missing = _current_missing_usable_symbols(scope)
    post_start_unknowns = [
        item["symbol"]
        for item in current_missing
        if item["classification"] == "HISTORICAL_PRODUCT_TYPE_UNRESOLVED"
    ]
    source_records: list[dict[str, Any]] = []
    for source in sources:
        source_records.append(
            {
                key: value
                for key, value in source.items()
                if not key.startswith("_")
            }
        )
    structured_sources = [
        source for source in sources if source.get("structured_symbols")
    ]
    regression_source = next(
        source for source in sources if source["source_id"] == REGRESSION_SOURCE_ID
    )
    regression_parsed = parse_structured_listing_table(regression_source["_detail"])
    regression_result = {
        symbol: {
            "evidence_found": symbol in regression_parsed,
            "listing_effective_timestamp": (
                regression_parsed[symbol]["listing_effective_timestamp"]
                if symbol in regression_parsed
                else None
            ),
            "evidence_method": (
                regression_parsed[symbol]["evidence_method"]
                if symbol in regression_parsed
                else None
            ),
            "table_evidence_hash": (
                regression_parsed[symbol]["table_evidence_hash"]
                if symbol in regression_parsed
                else None
            ),
        }
        for symbol in REGRESSION_SYMBOLS
    }
    membership_unchanged = set(proven) == set(scope["excluded_symbols"])
    effective_differences = _timestamp_differences(old_rows, new_rows)
    previously_proven_changed = _previously_proven_changed_count(
        old_provenance, new_rows
    )
    regression_record = {
        "article_code": REGRESSION_SOURCE_ID,
        "official_source_url": PUBLIC_ANNOUNCEMENT.format(
            code=REGRESSION_SOURCE_ID
        ),
        "covered_symbols": list(REGRESSION_SYMBOLS),
        "parsed": regression_result,
    }
    registry = {
        "schema_version": "HISTORICAL_PRODUCT_TYPE_REGISTRY_CORRECTED.v1",
        "run_id": RUN_ID,
        "scope": "M1.4.2 EXCLUDED TRADFI SYMBOLS ONLY; PARSER CORRECTIVE; NO PARENT RECONSTRUCTION",
        "classification": [
            "PRODUCT_PROVENANCE_ONLY",
            "NOT_FORWARD",
            "NOT_M2_START_EVIDENCE",
        ],
        "historical_product_registry_complete": not unresolved
        and not post_start_unknowns,
        "current_exchangeinfo_not_used_as_historical_proof": True,
        "original_excluded_tradifi_count": len(scope["excluded_symbols"]),
        "historically_proven_tradifi_count": len(proven),
        "unresolved_historical_product_types": unresolved + post_start_unknowns,
        "previously_proven_changed_count": previously_proven_changed,
        "current_exchangeinfo_missing_usable_symbols": current_missing,
        "rows": registry_rows,
        "announcement_sources": source_records,
        "announcement_to_covered_symbols": {
            source["source_id"]: source.get("structured_symbols", [])
            for source in sources
        },
        "structured_table_announcement_count": len(structured_sources),
        "regression_announcement": regression_record,
    }
    overlay = {
        "schema_version": "CONTRACT_TYPE_ELIGIBILITY_OVERLAY_V3.v1",
        "run_id": RUN_ID,
        "classification": [
            "PRODUCT_PROVENANCE_ONLY",
            "NOT_FORWARD",
            "NOT_M2_START_EVIDENCE",
        ],
        "source_semantics": "STRUCTURED_OR_SYMBOL_LEVEL_TIMESTAMPED_OFFICIAL_HISTORICAL_PRODUCT_EVIDENCE_ONLY",
        "generic_category_boundary_not_used_as_symbol_proof": True,
        "current_exchangeinfo_not_used_as_historical_proof": True,
        "membership_set_unchanged": membership_unchanged,
        "original_excluded_tradifi_symbols": list(scope["excluded_symbols"]),
        "proven_excluded_tradifi_symbols": proven,
        "symbols_added": sorted(set(proven) - set(scope["excluded_symbols"])),
        "symbols_removed": sorted(set(scope["excluded_symbols"]) - set(proven)),
        "effective_timestamp_differences": effective_differences,
        "previously_proven_changed_count": previously_proven_changed,
        "old_overlay_sha256": _sha256_file(
            OLD_ROOT / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY.json"
        ),
        "old_m1_4_2_1_registry_sha256": _sha256_file(
            OLD_STAGE_REGISTRY_PATH
        ),
        "rows": registry_rows,
        "announcement_sources": source_records,
        "regression_announcement": regression_record,
    }
    result = {
        "schema_version": "V2_1_M1_4_2_1A_PRODUCT_PROVENANCE.v1",
        "run_id": RUN_ID,
        "run_type": "HISTORICAL_PRODUCT_PROVENANCE_PARSER_CORRECTIVE",
        "base_commit": BASE_COMMIT,
        "code_commit_used_by_run": _git_head(),
        "decision": "V2.1-M1.4.2.1A PRODUCT_PROVENANCE NOT_READY",
        "audit_id": RUN_ID,
        "classification": [
            "PRODUCT_PROVENANCE_ONLY",
            "NOT_FORWARD",
            "NOT_M2_START_EVIDENCE",
        ],
        "original_excluded_count": len(scope["excluded_symbols"]),
        "proven_count": len(proven),
        "unresolved_count": len(unresolved) + len(post_start_unknowns),
        "official_announcement_count": len(sources),
        "structured_table_announcement_count": len(structured_sources),
        "regression_announcement": regression_record,
        "previously_proven_changed_count": previously_proven_changed,
        "membership_unchanged": membership_unchanged,
        "added_symbols": overlay["symbols_added"],
        "removed_symbols": overlay["symbols_removed"],
        "effective_timestamp_differences": effective_differences,
        "tsla_provenance": new_rows.get("TSLAUSDT"),
        "post_jan_08_unknown_count": len(post_start_unknowns),
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
        "old_m1_4_2_1_artifacts_unchanged": False,
        "parent_reconstruction_executed": False,
        "latest_13_recomputed": False,
        "reference_vol_recomputed": False,
        "position_scale_recomputed": False,
        "current_positions_recomputed": False,
        "forward_evidence_created": False,
        "first_forward_signal_selected": False,
        "optimization": False,
        "m2_started": False,
        "strategy_changed": False,
        "protocol_changed": False,
        "live_trading": False,
        "paper_only": True,
        "http_method_policy": "GET_ONLY",
        "old_m1_4_2_artifact_manifest_sha256": _sha256_json(
            scope["old_artifact_manifest_before"]
        ),
        "old_m1_4_2_1_artifact_manifest_sha256": _sha256_json(
            old_m1421_manifest_before
        ),
        "gates": {},
        "failed_gates": [],
    }
    old_after = _old_artifact_manifest()
    old_m1421_after = _old_stage_manifest()
    gates = {
        "J0_identity": (
            result["base_commit"] == BASE_COMMIT
            and result["identity"]["forward_anchor_status"] == FORWARD_ANCHOR_STATUS
            and bool(result["code_commit_used_by_run"])
        ),
        "J1_old_m142_immutable": (
            old_after == scope["old_artifact_manifest_before"]
        ),
        "J2_old_m1421_immutable": old_m1421_after == old_m1421_manifest_before,
        "J3_structured_table_parser": (
            set(regression_parsed) >= set(REGRESSION_SYMBOLS)
            and all(
                regression_result[symbol]["evidence_method"]
                == "STRUCTURED_MULTI_SYMBOL_OFFICIAL_LISTING_TABLE"
                and regression_result[symbol]["listing_effective_timestamp"]
                == REGRESSION_TIMESTAMPS[symbol]
                for symbol in REGRESSION_SYMBOLS
            )
        ),
        "J4_six_unresolved_resolved": not (
            PREVIOUSLY_UNRESOLVED & set(unresolved)
        ),
        "J5_all_145_historical_provenance": (
            len(proven) == 145 and not unresolved and not post_start_unknowns
        ),
        "J6_membership_set_exact_match": membership_unchanged
        and not overlay["symbols_added"]
        and not overlay["symbols_removed"],
        "J7_effective_timestamps_exact": not effective_differences,
        "J8_previous_139_stable": previously_proven_changed == 0,
        "J9_tsla_still_proven": (
            new_rows.get("TSLAUSDT", {}).get("evidence_status")
            == "PROVEN_HISTORICAL_PRODUCT_TYPE"
        ),
        "J10_post_jan08_unknowns_zero": not post_start_unknowns,
        "J11_ci_phase_safe": _phase_safe_ci_guard(),
        "J12_anchor_frozen": (
            result["identity"]["forward_anchor_status"] == FORWARD_ANCHOR_STATUS
        ),
        "J13_no_parent_reconstruction": (
            result["parent_reconstruction_executed"] is False
            and result["latest_13_recomputed"] is False
        ),
        "J14_no_forward_evidence": (
            result["forward_evidence_created"] is False
            and result["first_forward_signal_selected"] is False
        ),
    }
    result["old_m1_4_2_artifacts_unchanged"] = gates["J1_old_m142_immutable"]
    result["old_m1_4_2_1_artifacts_unchanged"] = gates["J2_old_m1421_immutable"]
    result["gates"] = {
        key: "PASS" if value else "FAIL" for key, value in gates.items()
    }
    result["failed_gates"] = [key for key, value in gates.items() if not value]
    result["decision"] = (
        "V2.1-M1.4.2.1A PRODUCT_PROVENANCE READY"
        if not result["failed_gates"]
        else "V2.1-M1.4.2.1A PRODUCT_PROVENANCE NOT_READY"
    )
    return registry, overlay, result


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return _sha256_file(path)


def _render_report(
    result: dict[str, Any], registry: dict[str, Any]
) -> str:
    gates = "\n".join(
        f"- {name}: {value}" for name, value in result["gates"].items()
    )
    return f"""# XS-LOWVOL V2.1-M1.4.2.1a Product Provenance

{result["decision"]}

This is a parser-corrective provenance-only artifact. It does not reconstruct
the parent, recompute latest-13, recompute risk state, or create Forward
evidence. The original M1.4.2 and M1.4.2.1 artifacts remain immutable.

## Scope

- Run ID: {result["run_id"]}
- Base commit: {result["base_commit"]}
- Original excluded count: {result["original_excluded_count"]}
- Proven count: {result["proven_count"]}
- Unresolved count: {result["unresolved_count"]}
- Official announcement count: {result["official_announcement_count"]}
- Structured-table announcement count: {result["structured_table_announcement_count"]}
- Previously proven changed count: {result["previously_proven_changed_count"]}
- Membership unchanged: {result["membership_unchanged"]}
- Added symbols: {result["added_symbols"]}
- Removed symbols: {result["removed_symbols"]}
- Effective timestamp differences: {result["effective_timestamp_differences"]}

## Regression announcement

- Article code: {REGRESSION_SOURCE_ID}
- Covered symbols: {list(REGRESSION_SYMBOLS)}
- Parsed timestamps: {registry["regression_announcement"]["parsed"]}

## J0-J14

{gates}

Failed gates: {", ".join(result["failed_gates"]) or "none"}.

## Prohibitions

- Parent reconstruction: {result["parent_reconstruction_executed"]}
- Latest-13 recompute: {result["latest_13_recomputed"]}
- Reference-vol recompute: {result["reference_vol_recomputed"]}
- Position-scale recompute: {result["position_scale_recomputed"]}
- Current-position recompute: {result["current_positions_recomputed"]}
- Forward evidence: {result["forward_evidence_created"]}
- Anchor status: {FORWARD_ANCHOR_STATUS}
- HTTP policy: {result["http_method_policy"]}
"""


def run_formal(approval: str) -> dict[str, Any]:
    if approval != APPROVAL:
        raise ProvenanceCorrectionError(f"exact approval required: {APPROVAL}")
    if _git_status():
        raise ProvenanceCorrectionError("formal run requires a clean working tree")
    head = _git_head()
    if head == BASE_COMMIT or subprocess.call(
        ["git", "merge-base", "--is-ancestor", BASE_COMMIT, head], cwd=ROOT
    ) != 0:
        raise ProvenanceCorrectionError("formal run must descend from 738c874...")
    if NEW_ROOT.exists():
        raise ProvenanceCorrectionError(
            "M1.4.2.1a result scope already exists; formal run cannot be repeated"
        )
    scope = load_immutable_scope()
    old_provenance = _load_old_provenance()
    old_m1421_manifest_before = _old_stage_manifest()
    scope["old_artifact_manifest_before"] = _old_artifact_manifest()
    sources = _fetch_accepted_sources(old_provenance)
    registry, overlay, result = _build_artifacts(
        scope, old_provenance, sources, old_m1421_manifest_before
    )
    NEW_ROOT.mkdir(parents=True)
    result["registry_sha256"] = _write_json(REGISTRY_PATH, registry)
    result["overlay_v3_sha256"] = _write_json(OVERLAY_PATH, overlay)
    result["old_m1_4_2_overlay_sha256"] = _sha256_file(
        OLD_ROOT / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY.json"
    )
    result["old_m1_4_2_1_registry_sha256"] = _sha256_file(OLD_STAGE_REGISTRY_PATH)
    report = _render_report(result, registry)
    REPORT_PATH.write_text(report, encoding="utf-8")
    result["report_sha256"] = _sha256_file(REPORT_PATH)
    result["formal_process_exit_code"] = 0
    _write_json(RESULT_PATH, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_formal(args.approval)
    except Exception as exc:
        print("V2.1-M1.4.2.1A PRODUCT_PROVENANCE NOT_READY")
        print(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "run_error": str(exc),
                    "parent_reconstruction_executed": False,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 1
    print(result["decision"])
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
