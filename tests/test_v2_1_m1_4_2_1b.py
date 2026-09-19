import copy
import json
from pathlib import Path

import pytest

from backtest.m1_4_2_1b import (
    EXPECTED_SIX,
    IMMUTABLE_STAGE_COMMITS,
    ProvenanceMergeError,
    compare_blob_manifests,
    compare_effective_timestamps,
    git_blob_manifest,
    merge_registries,
    sha256_json,
)


ROOT = Path(__file__).resolve().parents[1]


def _json(relative_path):
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


@pytest.fixture
def inputs():
    return (
        _json("research/v2_1/m1_4_2_1/HISTORICAL_PRODUCT_TYPE_REGISTRY.json"),
        _json("research/v2_1/m1_4_2_1a/HISTORICAL_PRODUCT_TYPE_REGISTRY_CORRECTED.json"),
        _json("research/v2_1/m1_4_2/CONTRACT_TYPE_ELIGIBILITY_OVERLAY.json"),
        _json("research/v2_1/m1_4_2_1a/V2_1_M1_4_2_1A_PRODUCT_PROVENANCE.json"),
    )


def test_merge_carries_exact_old_139_and_adds_only_six(inputs):
    old, corrected, overlay, corrective = inputs
    merged, diagnostics = merge_registries(old, corrected, overlay, corrective)
    old_proven = {
        row["symbol"]: row
        for row in old["rows"]
        if row["evidence_status"] == "PROVEN_HISTORICAL_PRODUCT_TYPE"
    }
    merged_by_symbol = {row["symbol"]: row for row in merged["rows"]}
    assert diagnostics["old_139_carried_count"] == 139
    assert diagnostics["six_added_count"] == 6
    assert diagnostics["part_b_symbols"] == sorted(EXPECTED_SIX)
    assert len(merged["rows"]) == 145
    assert set(merged_by_symbol) == set(overlay["excluded_tradifi_symbols"])
    for symbol, row in old_proven.items():
        assert merged_by_symbol[symbol] == row
    assert diagnostics["old_139_canonical_changed_count"] == 0


def test_corrected_registry_rows_outside_six_are_quarantined(inputs):
    old, corrected, overlay, corrective = inputs
    merged, diagnostics = merge_registries(old, corrected, overlay, corrective)
    assert set(diagnostics["part_b_symbols"]) == set(EXPECTED_SIX)
    assert not (set(row["symbol"] for row in merged["rows"]) - set(overlay["excluded_tradifi_symbols"]))


def test_duplicate_symbol_fails_closed(inputs):
    old, corrected, overlay, corrective = inputs
    duplicate = copy.deepcopy(old)
    duplicate["rows"].append(copy.deepcopy(duplicate["rows"][0]))
    with pytest.raises(ProvenanceMergeError):
        merge_registries(duplicate, corrected, overlay, corrective)


def test_missing_one_of_six_fails_closed(inputs):
    old, corrected, overlay, corrective = inputs
    missing = copy.deepcopy(corrected)
    missing["rows"] = [row for row in missing["rows"] if row["symbol"] != "PATHUSDT"]
    with pytest.raises(ProvenanceMergeError):
        merge_registries(old, missing, overlay, corrective)


@pytest.mark.parametrize("field,value", [("source_id", "wrong"), ("evidence_method", "wrong")])
def test_six_structured_source_and_method_are_hard_requirements(inputs, field, value):
    old, corrected, overlay, corrective = inputs
    invalid = copy.deepcopy(corrected)
    next(row for row in invalid["rows"] if row["symbol"] == "PATHUSDT")[field] = value
    with pytest.raises(ProvenanceMergeError):
        merge_registries(old, invalid, overlay, corrective)


def test_six_timestamp_mismatch_fails_closed(inputs):
    old, corrected, overlay, corrective = inputs
    invalid = copy.deepcopy(corrected)
    next(row for row in invalid["rows"] if row["symbol"] == "PATHUSDT")[
        "listing_effective_timestamp"
    ] = "2026-09-18T09:01:00Z"
    with pytest.raises(ProvenanceMergeError):
        merge_registries(old, invalid, overlay, corrective)


def test_timestamp_comparison_uses_utc_instants_and_only_145_rows(inputs):
    old, corrected, overlay, corrective = inputs
    merged, _ = merge_registries(old, corrected, overlay, corrective)
    equivalent = copy.deepcopy(overlay)
    path_row = next(row for row in equivalent["rows"] if row["symbol"] == "PATHUSDT")
    path_row["effective_from_timestamp"] = "2026-09-18T09:00:00.000Z"
    assert len(overlay["rows"]) == 328
    assert len(overlay["excluded_tradifi_symbols"]) == 145
    assert compare_effective_timestamps(
        equivalent, merged["rows"], overlay["excluded_tradifi_symbols"]
    ) == []


def test_1a_regression_quarantine_arithmetic_is_preserved(inputs):
    _, _, _, corrective = inputs
    assert corrective["previously_proven_changed_count"] == 41
    assert len(corrective["effective_timestamp_differences"]) == 224
    assert corrective["unresolved_count"] == 41
    assert 183 + 41 == 224


def test_git_blob_identity_is_platform_independent_and_changed_tree_fails():
    for _, (commit, prefix) in IMMUTABLE_STAGE_COMMITS.items():
        bound = git_blob_manifest(commit, prefix)
        current = git_blob_manifest("HEAD", prefix)
        assert bound == current
        assert sha256_json(bound) == sha256_json(current)
        altered = dict(current)
        first_path = next(iter(altered))
        altered[first_path] = "0" * 40
        assert compare_blob_manifests(bound, altered) == [first_path]


def test_crlf_checkout_bytes_do_not_define_git_identity(tmp_path):
    source = ROOT / "backtest/m1_4_2_1b.py"
    text = source.read_text(encoding="utf-8")
    lf = tmp_path / "lf.txt"
    crlf = tmp_path / "crlf.txt"
    lf.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    crlf.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    assert lf.read_bytes() != crlf.read_bytes()
    assert git_blob_manifest("HEAD", "research/v2_1/m1_4_2_1a") == git_blob_manifest(
        IMMUTABLE_STAGE_COMMITS["m1_4_2_1a"][0], IMMUTABLE_STAGE_COMMITS["m1_4_2_1a"][1]
    )


def test_no_parent_or_forward_work_is_imported():
    source = (ROOT / "backtest/m1_4_2_1b.py").read_text(encoding="utf-8")
    assert "simulate_frozen_portfolio" not in source
    assert "run_incremental" not in source
    assert "urlopen" not in source
    assert "START M2" not in source
