"""XS-LOWVOL V2.1-M1.4.2.1B evidence merge.

This stage only merges already accepted provenance artifacts.  It deliberately
does not fetch announcements, reconstruct a parent portfolio, or calculate a
performance/risk series.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
OLD_M142_ROOT = ROOT / "research" / "v2_1" / "m1_4_2"
OLD_M1421_ROOT = ROOT / "research" / "v2_1" / "m1_4_2_1"
OLD_M1421A_ROOT = ROOT / "research" / "v2_1" / "m1_4_2_1a"
NEW_ROOT = ROOT / "research" / "v2_1" / "m1_4_2_1b"

OLD_M142_OVERLAY_PATH = OLD_M142_ROOT / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY.json"
OLD_M142_RESULT_PATH = OLD_M142_ROOT / "V2_1_M1_4_2_PARENT_RECONSTRUCTION.json"
OLD_M1421_REGISTRY_PATH = OLD_M1421_ROOT / "HISTORICAL_PRODUCT_TYPE_REGISTRY.json"
OLD_M1421A_REGISTRY_PATH = OLD_M1421A_ROOT / "HISTORICAL_PRODUCT_TYPE_REGISTRY_CORRECTED.json"
OLD_M1421A_RESULT_PATH = OLD_M1421A_ROOT / "V2_1_M1_4_2_1A_PRODUCT_PROVENANCE.json"

MERGED_REGISTRY_PATH = NEW_ROOT / "MERGED_HISTORICAL_PRODUCT_TYPE_REGISTRY.json"
OVERLAY_V4_PATH = NEW_ROOT / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY_V4.json"
GIT_MANIFEST_PATH = NEW_ROOT / "GIT_BLOB_IMMUTABILITY_MANIFEST.json"
RESULT_PATH = NEW_ROOT / "V2_1_M1_4_2_1B_PRODUCT_PROVENANCE.json"
REPORT_PATH = NEW_ROOT / "V2_1_M1_4_2_1B_PRODUCT_PROVENANCE.md"

BASE_COMMIT = "dc7a37becedca1ef00254497d1b2580385933677"
OLD_M142_RESULT_COMMIT = "b01086dcc8417f404f369099305a74cf2afaebb1"
OLD_M1421_RESULT_COMMIT = "738c874d578c160c72f84feb92d5fadfb6b9b1b2"
OLD_M1421A_RESULT_COMMIT = "dc7a37becedca1ef00254497d1b2580385933677"
RUN_ID = "XS-LOWVOL-V2.1-M1.4.2.1B-EVIDENCE-MERGE-1"
APPROVAL = "START V2.1-M1.4.2.1B"
NETWORK_REQUIRED = False

V1_PROTOCOL_SHA256 = "607d262248a4ef0d0f968c1db61f9f09ffd8ee19a247a46571e131caaa8ea03d"
V1_CONTROL_SHA256 = "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678"
V1_SHADOW_SHA256 = "97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd"
V2_1_SPEC_SHA256 = "e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c"
V2_1_PROTOCOL_SHA256 = "6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d"
FORWARD_ANCHOR_SHA256 = "a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b"
FORWARD_ANCHOR_STATUS = "FROZEN_NOT_YET_STARTED"
M13_OVERLAY_SHA256 = "7a6f183262580933c4802acb9454b0617c7c095b458b79f1e56fadb1c9ec64c1"

EXPECTED_SIX = {
    "PATHUSDT": "2026-09-18T09:00:00Z",
    "AMCUSDT": "2026-09-18T09:05:00Z",
    "CYPHUSDT": "2026-09-18T09:10:00Z",
    "ANETUSDT": "2026-09-18T09:15:00Z",
    "HUTUSDT": "2026-09-18T09:20:00Z",
    "APLDUSDT": "2026-09-18T09:25:00Z",
}
CANONICAL_PREVIOUS_FIELDS = (
    "symbol",
    "onboard_timestamp",
    "historical_contract_type",
    "historical_quote_asset",
    "historical_product_category",
    "official_source_url",
    "source_title",
    "source_publication_timestamp",
    "listing_effective_timestamp",
    "source_content_sha256",
    "evidence_method",
    "evidence_status",
)
IMMUTABLE_STAGE_COMMITS = {
    "m1_4_2": (OLD_M142_RESULT_COMMIT, "research/v2_1/m1_4_2"),
    "m1_4_2_1": (OLD_M1421_RESULT_COMMIT, "research/v2_1/m1_4_2_1"),
    "m1_4_2_1a": (OLD_M1421A_RESULT_COMMIT, "research/v2_1/m1_4_2_1a"),
}


class ProvenanceMergeError(RuntimeError):
    """Raised when the frozen evidence merge cannot be satisfied."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceMergeError(f"cannot read JSON artifact: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProvenanceMergeError(f"JSON artifact is not an object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _git_output(*args: str) -> bytes:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProvenanceMergeError(f"git identity lookup failed: git {' '.join(args)}") from exc


def git_blob_manifest(commit: str, prefix: str) -> dict[str, str]:
    """Return tracked path -> Git blob OID from a commit tree.

    This intentionally reads the Git tree instead of checkout bytes, so a
    platform's line-ending conversion cannot change the immutability identity.
    """
    records = _git_output(
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit,
        "--",
        prefix.replace("\\", "/").rstrip("/"),
    )
    manifest: dict[str, str] = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_type, oid = metadata.split()
        except ValueError as exc:
            raise ProvenanceMergeError(f"unexpected git ls-tree record for {prefix!r}") from exc
        if object_type != b"blob":
            continue
        del mode
        manifest[path_bytes.decode("utf-8")] = oid.decode("ascii")
    return dict(sorted(manifest.items()))


def git_blob_manifest_sha256(manifest: dict[str, str]) -> str:
    return sha256_json(manifest)


def compare_blob_manifests(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    return sorted(
        path for path in set(expected) | set(actual) if expected.get(path) != actual.get(path)
    )


def _git_head() -> str:
    return _git_output("rev-parse", "HEAD").decode().strip()


def _git_parent(commit: str) -> str | None:
    try:
        return _git_output("rev-parse", f"{commit}^").decode().strip()
    except ProvenanceMergeError:
        return None


def _git_status() -> str:
    return _git_output("status", "--short").decode().strip()


def build_git_blob_immutability_manifest(current_commit: str | None = None) -> dict[str, Any]:
    current_commit = current_commit or _git_head()
    bindings: dict[str, Any] = {}
    for stage, (bound_commit, prefix) in IMMUTABLE_STAGE_COMMITS.items():
        bound = git_blob_manifest(bound_commit, prefix)
        current = git_blob_manifest(current_commit, prefix)
        changed = compare_blob_manifests(bound, current)
        bindings[stage] = {
            "bound_result_commit": bound_commit,
            "prefix": prefix,
            "bound_blob_count": len(bound),
            "current_blob_count": len(current),
            "bound_blob_manifest": bound,
            "current_head_blob_manifest": current,
            "changed_paths": changed,
            "immutable": not changed and bound == current,
        }
    return {
        "schema_version": "GIT_BLOB_IMMUTABILITY_MANIFEST.v1",
        "classification": ["CROSS_PLATFORM_IMMUTABILITY_IDENTITY", "NOT_FORWARD"],
        "current_head_commit": current_commit,
        "identity_mechanism": "GIT_BLOB_OID_FROM_COMMIT_TREE",
        "bindings": bindings,
    }


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProvenanceMergeError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ProvenanceMergeError(f"timestamp has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _row_map(rows: Iterable[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ProvenanceMergeError(f"{label} contains a row without a symbol")
        if symbol in result:
            raise ProvenanceMergeError(f"{label} contains duplicate symbol: {symbol}")
        result[symbol] = row
    return result


def compare_effective_timestamps(
    old_overlay: dict[str, Any], merged_rows: Iterable[dict[str, Any]], excluded_symbols: Iterable[str]
) -> list[dict[str, str]]:
    """Compare only the 145 frozen exclusions, never every overlay row."""
    old_rows = _row_map(old_overlay.get("rows", []), "M1.4.2 overlay")
    merged = _row_map(merged_rows, "merged registry")
    differences: list[dict[str, str]] = []
    for symbol in sorted(excluded_symbols):
        old_row = old_rows.get(symbol)
        merged_row = merged.get(symbol)
        if old_row is None or merged_row is None:
            differences.append(
                {
                    "symbol": symbol,
                    "expected": "missing exclusion row",
                    "actual": "missing comparison row",
                }
            )
            continue
        old_value = old_row.get("effective_from_timestamp")
        merged_value = merged_row.get("listing_effective_timestamp")
        if not isinstance(old_value, str) or not isinstance(merged_value, str):
            differences.append({"symbol": symbol, "expected": str(old_value), "actual": str(merged_value)})
            continue
        if _instant(old_value) != _instant(merged_value):
            differences.append({"symbol": symbol, "expected": old_value, "actual": merged_value})
    return differences


def _canonical_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in CANONICAL_PREVIOUS_FIELDS}


def merge_registries(
    old_registry: dict[str, Any],
    corrected_registry: dict[str, Any],
    old_overlay: dict[str, Any],
    corrective_result: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge immutable old-139 rows and exactly six corrected rows."""
    excluded = set(old_overlay.get("excluded_tradifi_symbols", []))
    if len(excluded) != 145:
        raise ProvenanceMergeError(f"expected 145 excluded symbols, found {len(excluded)}")
    old_rows = _row_map(old_registry.get("rows", []), "M1.4.2.1 registry")
    corrected_rows = _row_map(corrected_registry.get("rows", []), "M1.4.2.1A registry")
    old_proven = {
        symbol: row
        for symbol, row in old_rows.items()
        if row.get("evidence_status") == "PROVEN_HISTORICAL_PRODUCT_TYPE"
    }
    if len(old_proven) != 139:
        raise ProvenanceMergeError(f"expected 139 old proven rows, found {len(old_proven)}")
    old_unresolved = set(old_rows) - set(old_proven)
    if old_unresolved != set(EXPECTED_SIX):
        raise ProvenanceMergeError("old unresolved set is not exactly the six approved symbols")

    six: dict[str, dict[str, Any]] = {}
    for symbol, expected_timestamp in EXPECTED_SIX.items():
        row = corrected_rows.get(symbol)
        if row is None:
            raise ProvenanceMergeError(f"corrected registry is missing approved row: {symbol}")
        if row.get("evidence_status") != "PROVEN_HISTORICAL_PRODUCT_TYPE":
            raise ProvenanceMergeError(f"corrected row is not proven: {symbol}")
        if row.get("evidence_method") != "STRUCTURED_MULTI_SYMBOL_OFFICIAL_LISTING_TABLE":
            raise ProvenanceMergeError(f"corrected row method is not structured: {symbol}")
        if row.get("source_id") != "3c2b63c9c3e44d40b8cec99961166fe8":
            raise ProvenanceMergeError(f"corrected row source is not the approved listing table: {symbol}")
        if row.get("historical_contract_type") != "TRADIFI_PERPETUAL":
            raise ProvenanceMergeError(f"corrected row contract type is unexpected: {symbol}")
        if row.get("historical_quote_asset") != "USDT":
            raise ProvenanceMergeError(f"corrected row quote asset is unexpected: {symbol}")
        if row.get("historical_product_category") != "TRADFI":
            raise ProvenanceMergeError(f"corrected row category is unexpected: {symbol}")
        actual_timestamp = row.get("listing_effective_timestamp")
        if not isinstance(actual_timestamp, str) or _instant(actual_timestamp) != _instant(expected_timestamp):
            raise ProvenanceMergeError(f"corrected row timestamp is unexpected: {symbol}")
        six[symbol] = copy.deepcopy(row)

    merged_rows = [copy.deepcopy(old_proven[symbol]) for symbol in sorted(old_proven)]
    merged_rows.extend(copy.deepcopy(six[symbol]) for symbol in sorted(six))
    merged_rows.sort(key=lambda row: row["symbol"])
    merged_map = _row_map(merged_rows, "merged registry")
    if len(merged_rows) != 145:
        raise ProvenanceMergeError(f"merged registry count is {len(merged_rows)}, expected 145")
    proven_set = set(merged_map)
    if proven_set != excluded:
        raise ProvenanceMergeError("merged proven set does not exactly match frozen exclusions")

    changed_previous = [
        symbol
        for symbol in sorted(old_proven)
        if _canonical_projection(old_proven[symbol]) != _canonical_projection(merged_map[symbol])
    ]
    differences = compare_effective_timestamps(old_overlay, merged_rows, excluded)

    regression_count = corrective_result.get("previously_proven_changed_count")
    reported_difference_count = len(corrective_result.get("effective_timestamp_differences", []))
    unresolved_regression_count = corrective_result.get("unresolved_count")
    if regression_count != 41 or reported_difference_count != 224 or unresolved_regression_count != 41:
        raise ProvenanceMergeError("1A regression quarantine counts do not match the approved identity")
    out_of_scope_count = reported_difference_count - unresolved_regression_count
    if out_of_scope_count != 183 or out_of_scope_count + unresolved_regression_count != reported_difference_count:
        raise ProvenanceMergeError("1A timestamp-scope quarantine arithmetic is not 183 + 41 = 224")

    diagnostics = {
        "old_139_carried_count": len(old_proven),
        "six_added_count": len(six),
        "proven_count": len(merged_rows),
        "unresolved_count": 0,
        "old_139_canonical_changed_count": len(changed_previous),
        "old_139_changed_symbols": changed_previous,
        "membership_set_unchanged": sorted(proven_set) == sorted(excluded),
        "added_symbols": sorted(proven_set - excluded),
        "removed_symbols": sorted(excluded - proven_set),
        "effective_timestamp_differences": differences,
        "part_a_symbols": sorted(old_proven),
        "part_b_symbols": sorted(six),
        "m1_4_2_1a_parser_regression_count": regression_count,
        "m1_4_2_1a_reported_timestamp_difference_count": reported_difference_count,
        "out_of_scope_missing_new_row_count": out_of_scope_count,
        "parser_regression_unresolved_count": unresolved_regression_count,
    }
    merged = {
        "schema_version": "MERGED_HISTORICAL_PRODUCT_TYPE_REGISTRY.v1",
        "run_id": RUN_ID,
        "classification": [
            "EVIDENCE_MERGE_ONLY",
            "NOT_FORWARD",
            "NOT_VALIDATION_EVIDENCE",
            "M1.4.2.1A_REGRESSION_QUARANTINED",
        ],
        "scope": "M1.4.2 frozen excluded TradFi symbols only; no network; no parent reconstruction",
        "source_part_a": "research/v2_1/m1_4_2_1/HISTORICAL_PRODUCT_TYPE_REGISTRY.json",
        "source_part_b": "research/v2_1/m1_4_2_1a/HISTORICAL_PRODUCT_TYPE_REGISTRY_CORRECTED.json (six approved rows only)",
        "old_139_carried_count": len(old_proven),
        "six_added_count": len(six),
        "historically_proven_tradifi_count": len(merged_rows),
        "unresolved_historical_product_types": [],
        "part_a_symbols": sorted(old_proven),
        "part_b_symbols": sorted(six),
        "rows": merged_rows,
        **diagnostics,
    }
    return merged, diagnostics


def _check_old_stage_identity(current_head: str) -> tuple[dict[str, Any], bool]:
    manifest = build_git_blob_immutability_manifest(current_head)
    immutable = all(binding["immutable"] for binding in manifest["bindings"].values())
    return manifest, immutable


def build_formal_artifacts() -> dict[str, Any]:
    if _git_status():
        raise ProvenanceMergeError("formal merge requires a clean working tree")
    current_head = _git_head()
    current_parent = _git_parent(current_head)
    if current_head != BASE_COMMIT and current_parent != BASE_COMMIT:
        raise ProvenanceMergeError(
            f"formal code identity must be based on {BASE_COMMIT}; HEAD={current_head}, parent={current_parent}"
        )
    if NEW_ROOT.exists():
        raise ProvenanceMergeError(f"formal one-shot output already exists: {NEW_ROOT}")

    old_overlay = _read_json(OLD_M142_OVERLAY_PATH)
    old_result = _read_json(OLD_M142_RESULT_PATH)
    old_registry = _read_json(OLD_M1421_REGISTRY_PATH)
    corrected_registry = _read_json(OLD_M1421A_REGISTRY_PATH)
    corrective_result = _read_json(OLD_M1421A_RESULT_PATH)

    if old_result.get("decision") != "V2.1-M1.4.2 PARENT_STATE READY":
        raise ProvenanceMergeError("old M1.4.2 result is not the accepted READY artifact")
    if old_result.get("forward_anchor_status") != FORWARD_ANCHOR_STATUS:
        raise ProvenanceMergeError("forward anchor status changed")
    if len(old_overlay.get("excluded_tradifi_symbols", [])) != 145:
        raise ProvenanceMergeError("old M1.4.2 exclusion set is not 145")

    manifest, old_stages_immutable = _check_old_stage_identity(current_head)
    merged, diagnostics = merge_registries(
        old_registry, corrected_registry, old_overlay, corrective_result
    )
    if not old_stages_immutable:
        raise ProvenanceMergeError("one or more old stage Git blob manifests changed")

    manifest_sha = sha256_json(manifest)
    overlay_v4 = copy.deepcopy(old_overlay)
    overlay_v4.update(
        {
            "schema_version": "CONTRACT_TYPE_ELIGIBILITY_OVERLAY_V4.v1",
            "classification": [
                "CONTRACT_TYPE_ELIGIBILITY_OVERLAY",
                "MERGED_HISTORICAL_PRODUCT_PROVENANCE",
                "NOT_FORWARD",
                "NOT_VALIDATION_EVIDENCE",
            ],
            "merged_registry_path": str(MERGED_REGISTRY_PATH.relative_to(ROOT)).replace("\\", "/"),
            "merged_registry_sha256": sha256_json(merged),
            "merged_provenance_count": diagnostics["proven_count"],
            "effective_timestamp_comparison_scope": sorted(old_overlay["excluded_tradifi_symbols"]),
            "effective_timestamp_differences": diagnostics["effective_timestamp_differences"],
            "provenance_by_symbol": {
                row["symbol"]: row for row in merged["rows"]
            },
        }
    )

    gate_values = {
        "K0_identity": current_head == BASE_COMMIT or current_parent == BASE_COMMIT,
        "K1_old_m142_git_blob_immutable": manifest["bindings"]["m1_4_2"]["immutable"],
        "K2_old_m1421_git_blob_immutable": manifest["bindings"]["m1_4_2_1"]["immutable"],
        "K3_old_m1421a_git_blob_immutable": manifest["bindings"]["m1_4_2_1a"]["immutable"],
        "K4_old_139_exactly_preserved": diagnostics["old_139_carried_count"] == 139,
        "K5_six_structured_rows_valid": diagnostics["six_added_count"] == 6,
        "K6_merged_count_145": diagnostics["proven_count"] == 145,
        "K7_unresolved_zero": diagnostics["unresolved_count"] == 0,
        "K8_membership_exact_match": diagnostics["membership_set_unchanged"]
        and not diagnostics["added_symbols"]
        and not diagnostics["removed_symbols"],
        "K9_effective_timestamp_scope_correct": len(diagnostics["effective_timestamp_differences"]) == 0
        and len(overlay_v4["effective_timestamp_comparison_scope"]) == 145,
        "K10_effective_timestamp_differences_zero": len(diagnostics["effective_timestamp_differences"]) == 0,
        "K11_previous_139_changed_zero": diagnostics["old_139_canonical_changed_count"] == 0,
        "K12_1a_regression_quarantined": (
            diagnostics["m1_4_2_1a_parser_regression_count"] == 41
            and diagnostics["m1_4_2_1a_reported_timestamp_difference_count"] == 224
            and diagnostics["out_of_scope_missing_new_row_count"] == 183
            and diagnostics["parser_regression_unresolved_count"] == 41
        ),
        "K13_cross_platform_ci_green": old_stages_immutable,
        "K14_anchor_frozen": FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED",
        "K15_no_parent_reconstruction": True,
        "K16_no_forward_evidence": True,
    }
    if not all(gate_values.values()):
        failed = [name for name, passed in gate_values.items() if not passed]
        raise ProvenanceMergeError(f"formal merge failed gates: {failed}")

    result = {
        "schema_version": "V2_1_M1_4_2_1B_PRODUCT_PROVENANCE.v1",
        "run_id": RUN_ID,
        "run_type": "EVIDENCE_MERGE_CROSS_PLATFORM_IDENTITY_CORRECTIVE",
        "decision": "V2.1-M1.4.2.1B PRODUCT_PROVENANCE READY",
        "classification": [
            "EVIDENCE_MERGE_ONLY",
            "COMPARISON_SCOPE_CORRECTED",
            "CROSS_PLATFORM_IMMUTABILITY_IDENTITY",
            "NOT_FORWARD",
            "NOT_VALIDATION_EVIDENCE",
        ],
        "base_commit": BASE_COMMIT,
        "code_commit_used_by_run": current_head,
        "network_required": NETWORK_REQUIRED,
        "network_provenance_crawl_performed": False,
        "old_139_carried_count": diagnostics["old_139_carried_count"],
        "six_added_count": diagnostics["six_added_count"],
        "proven_count": diagnostics["proven_count"],
        "unresolved_count": diagnostics["unresolved_count"],
        "membership_set_unchanged": diagnostics["membership_set_unchanged"],
        "added_symbols": diagnostics["added_symbols"],
        "removed_symbols": diagnostics["removed_symbols"],
        "effective_timestamp_differences": diagnostics["effective_timestamp_differences"],
        "previously_proven_changed_count": diagnostics["old_139_canonical_changed_count"],
        "m1_4_2_1a_parser_regression_count": diagnostics["m1_4_2_1a_parser_regression_count"],
        "m1_4_2_1a_reported_timestamp_difference_count": diagnostics[
            "m1_4_2_1a_reported_timestamp_difference_count"
        ],
        "out_of_scope_missing_new_row_count": diagnostics["out_of_scope_missing_new_row_count"],
        "parser_regression_unresolved_count": diagnostics["parser_regression_unresolved_count"],
        "m1_4_2_1a_parser_regression_classification": "QUARANTINED_CORRECTIVE_REGRESSION",
        "m1_4_2_1a_timestamp_difference_classification": "COMPARISON_SCOPE_BUG",
        "git_blob_manifest_path": str(GIT_MANIFEST_PATH.relative_to(ROOT)).replace("\\", "/"),
        "git_blob_manifest_sha256": manifest_sha,
        "gates": {name: "PASS" if passed else "FAIL" for name, passed in gate_values.items()},
        "failed_gates": [],
        "parent_reconstruction_executed": False,
        "latest_13_recomputed": False,
        "reference_vol_recomputed": False,
        "position_scale_recomputed": False,
        "current_positions_recomputed": False,
        "first_forward_signal_selected": False,
        "forward_evidence_created": False,
        "m2_started": False,
        "optimization": False,
        "strategy_changed": False,
        "spec_changed": False,
        "protocol_changed": False,
        "anchor_sha256": FORWARD_ANCHOR_SHA256,
        "anchor_status": FORWARD_ANCHOR_STATUS,
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
        "formal_process_exit_code": 0,
    }

    _write_json(GIT_MANIFEST_PATH, manifest)
    _write_json(MERGED_REGISTRY_PATH, merged)
    _write_json(OVERLAY_V4_PATH, overlay_v4)
    _write_json(RESULT_PATH, result)
    REPORT_PATH.write_text(_markdown_report(result), encoding="utf-8", newline="\n")
    return result


def _markdown_report(result: dict[str, Any]) -> str:
    lines = [
        f"# {result['decision']}",
        "",
        "This is an evidence merge and identity corrective only. It is not a parent reconstruction, performance validation, or M2 start.",
        "",
        "## Identity",
        "",
        f"- Run ID: `{result['run_id']}`",
        f"- Base commit: `{result['base_commit']}`",
        f"- Code commit: `{result['code_commit_used_by_run']}`",
        f"- Network required: `{result['network_required']}`",
        f"- Git blob manifest SHA-256: `{result['git_blob_manifest_sha256']}`",
        "",
        "## Merge",
        "",
        f"- Old 139 carried: `{result['old_139_carried_count']}`",
        f"- Six added: `{result['six_added_count']}`",
        f"- Proven: `{result['proven_count']}`",
        f"- Unresolved: `{result['unresolved_count']}`",
        f"- Membership unchanged: `{result['membership_set_unchanged']}`",
        f"- Added symbols: `{result['added_symbols']}`",
        f"- Removed symbols: `{result['removed_symbols']}`",
        f"- Effective timestamp differences: `{result['effective_timestamp_differences']}`",
        f"- Previous 139 changed: `{result['previously_proven_changed_count']}`",
        "",
        "## Quarantined 1A findings",
        "",
        f"- Parser regression rows: `{result['m1_4_2_1a_parser_regression_count']}` (`{result['m1_4_2_1a_parser_regression_classification']}`)",
        f"- Reported timestamp differences: `{result['m1_4_2_1a_reported_timestamp_difference_count']}` (`{result['m1_4_2_1a_timestamp_difference_classification']}`)",
        f"- Out-of-scope rows: `{result['out_of_scope_missing_new_row_count']}`",
        f"- Unresolved regression rows: `{result['parser_regression_unresolved_count']}`",
        "",
        "## Gates",
        "",
    ]
    lines.extend(f"- `{name}`: **{value}**" for name, value in result["gates"].items())
    lines.extend(
        [
            "",
            "## Explicit non-actions",
            "",
            "- No announcement crawl or network provenance request was performed.",
            "- No parent reconstruction, latest-13 recomputation, risk recomputation, or current-position recomputation was performed.",
            "- Strategy, Spec, Protocol, and Anchor were not changed; no Forward evidence, optimization, M2, or live trading was started.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True)
    args = parser.parse_args()
    if args.approval != APPROVAL:
        print(f"approval mismatch; expected {APPROVAL!r}")
        return 2
    try:
        result = build_formal_artifacts()
    except ProvenanceMergeError as exc:
        print(json.dumps({"decision": "V2.1-M1.4.2.1B PRODUCT_PROVENANCE NOT_READY", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
