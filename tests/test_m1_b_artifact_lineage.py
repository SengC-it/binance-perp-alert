"""M1-B.1A.1 corrected-run artifact immutability tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backtest.m1_b import (
    CORRECTED_FROM_COMMIT,
    CORRECTED_M1_OUTPUT_DIR,
    CORRECTED_M1_RUN_ID,
    CORRECTIVE_VALIDATOR_COMMIT,
    M1_DECISION_PATH,
    M1_ROOT_DIR,
    M1_REPORT_PATH,
    ArtifactImmutabilityError,
    _write_legacy_m1_b_artifacts,
    legacy_m1_artifact_hashes,
    write_corrected_m1_b_artifacts,
)
from backtest.m1_protocol import M1_APPROVED_PROTOCOL_SHA256
from src.xs_lowvol_spec import CONTROL_SPEC_HASH, SHADOW_SPEC_HASH


def _synthetic_result(*, g0: bool = False) -> dict[str, object]:
    return {
        "decision": "M1 PASS",
        "gates": {
            "G0_data_integrity": g0,
            "G1_external_return": True,
        },
        "failed_gates": [],
        "dataset_sha256": "a" * 64,
        "normalized_dataset_sha256": "b" * 64,
        "protocol_sha256": M1_APPROVED_PROTOCOL_SHA256,
        "control_sha256": CONTROL_SPEC_HASH,
        "shadow_sha256": SHADOW_SPEC_HASH,
    }


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_corrected_output_path_is_distinct_and_fixed():
    assert CORRECTED_M1_OUTPUT_DIR != M1_ROOT_DIR
    assert CORRECTED_M1_OUTPUT_DIR.name == CORRECTED_M1_RUN_ID
    assert M1_DECISION_PATH.parent == M1_ROOT_DIR
    assert "CORRECTED-2" not in CORRECTED_M1_RUN_ID


def test_writer_preserves_legacy_artifacts_and_records_lineage(tmp_path: Path):
    before = legacy_m1_artifact_hashes()
    output_dir = tmp_path / CORRECTED_M1_RUN_ID
    paths = write_corrected_m1_b_artifacts(
        _synthetic_result(),
        output_dir=output_dir,
        generated_at="2026-09-15T08:00:00+00:00",
        code_commit="c" * 40,
    )

    after = legacy_m1_artifact_hashes()
    assert before == after
    assert _file_hash(M1_DECISION_PATH) == before["research/m1/M1_DECISION.json"]
    assert _file_hash(M1_REPORT_PATH) == before["research/m1/M1_REPORT.md"]
    metadata = json.loads(paths["run_metadata"].read_text(encoding="utf-8"))
    decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
    required = {
        "run_id": CORRECTED_M1_RUN_ID,
        "run_type": "CORRECTED_RERUN",
        "corrected_from_commit": CORRECTED_FROM_COMMIT,
        "supersedes_run": "XS-LOWVOL-M1-B-c4bb2b90",
        "correction_reason": "FIXED_DATE_REBALANCE_SCHEDULER_VIOLATED_FROZEN_M0_RETRY_SEMANTICS",
    }
    assert all(decision[key] == value for key, value in required.items())
    assert all(metadata[key] == value for key, value in required.items())
    assert metadata["status"] == "FINALIZED"
    assert metadata["corrective_validator_commit"] == CORRECTIVE_VALIDATOR_COMMIT
    assert metadata["legacy_run"]["preserved"] is True


def test_g0_false_forces_fail_and_diagnostic_status(tmp_path: Path):
    output_dir = tmp_path / CORRECTED_M1_RUN_ID
    paths = write_corrected_m1_b_artifacts(
        _synthetic_result(g0=False),
        output_dir=output_dir,
        generated_at="2026-09-15T08:00:00+00:00",
        code_commit="d" * 40,
    )
    decision = json.loads(paths["decision"].read_text(encoding="utf-8"))
    assert decision["decision"] == "M1 FAIL"
    assert decision["gates"]["G0_data_integrity"] is False
    assert decision["performance_metrics_status"] == "DIAGNOSTIC_ONLY_DUE_TO_G0"
    assert decision["data_integrity_status"] == "FAIL_CONFIRMED_MISSING_FUNDING_SETTLEMENTS"
    assert decision["performance_metrics_complete"] is False


def test_g0_true_is_rejected_in_current_corrected_run(tmp_path: Path):
    with pytest.raises(ArtifactImmutabilityError, match="G0_data_integrity=false"):
        write_corrected_m1_b_artifacts(
            _synthetic_result(g0=True),
            output_dir=tmp_path / CORRECTED_M1_RUN_ID,
            code_commit="e" * 40,
        )


def test_finalized_corrected_directory_fails_closed(tmp_path: Path):
    output_dir = tmp_path / CORRECTED_M1_RUN_ID
    first = write_corrected_m1_b_artifacts(
        _synthetic_result(),
        output_dir=output_dir,
        generated_at="2026-09-15T08:00:00+00:00",
        code_commit="f" * 40,
    )
    before = {key: _file_hash(path) for key, path in first.items() if path.is_file()}
    with pytest.raises(ArtifactImmutabilityError, match="not empty|finalized|overwrite"):
        write_corrected_m1_b_artifacts(
            _synthetic_result(),
            output_dir=output_dir,
            generated_at="2026-09-15T08:00:01+00:00",
            code_commit="0" * 40,
        )
    after = {key: _file_hash(path) for key, path in first.items() if path.is_file()}
    assert before == after


def test_corrected_writer_rejects_legacy_root_and_alternate_run_id(tmp_path: Path):
    with pytest.raises(ArtifactImmutabilityError, match="legacy"):
        write_corrected_m1_b_artifacts(
            _synthetic_result(),
            output_dir=M1_ROOT_DIR,
            code_commit="1" * 40,
        )
    with pytest.raises(ArtifactImmutabilityError, match="fixed|alternate"):
        write_corrected_m1_b_artifacts(
            _synthetic_result(),
            output_dir=tmp_path / "XS-LOWVOL-M1-B.1B-CORRECTED-2",
            run_id="XS-LOWVOL-M1-B.1B-CORRECTED-2",
            code_commit="2" * 40,
        )


def test_legacy_writer_cannot_overwrite_c4bb_root():
    before = legacy_m1_artifact_hashes()
    with pytest.raises(ArtifactImmutabilityError, match="immutable|overwrite"):
        _write_legacy_m1_b_artifacts(_synthetic_result())
    assert legacy_m1_artifact_hashes() == before

