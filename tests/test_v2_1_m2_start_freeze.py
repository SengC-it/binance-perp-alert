import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backtest.v2_1_m2_start_freeze import (
    BASE_COMMIT,
    EXPECTED_FIRST_EXECUTION_DAY,
    EXPECTED_FIRST_LOGICAL_SIGNAL,
    EXPECTED_FIRST_SIGNAL_DAY,
    GATE_NAMES,
    M2NotReady,
    _verify_artifact_hashes,
    _verify_binding_trees,
    _verify_frozen_identities,
    _verify_prestart_state,
    build_formal_artifacts,
    derive_first_forward_attempt,
    validate_forward_measurement_timestamp,
)


ROOT = Path(__file__).resolve().parents[1]
ANCHOR_PATH = ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_FORWARD_ANCHOR.yaml"


def test_accepted_identities_and_parent_state_are_bound_without_reconstruction():
    identities = _verify_frozen_identities()
    result, state, risk, provenance = _verify_artifact_hashes()
    warm_start, position_state_sha = _verify_prestart_state(result, state, risk)

    assert identities["v1_control_sha256"] == "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678"
    assert identities["forward_anchor_status"] == "FROZEN_NOT_YET_STARTED"
    assert state["cutoff_utc"] == "2026-09-19T00:00:00Z"
    assert state["current_position_count"] == 10
    assert position_state_sha == warm_start["position_state_sha256"]
    assert provenance["proven_count"] == 145


def test_accepted_git_trees_are_immutable_from_base():
    binding = _verify_binding_trees(BASE_COMMIT)
    assert binding["bindings"]["m1_4_2"]["immutable"] is True
    assert binding["bindings"]["m1_4_2_1b"]["immutable"] is True
    assert binding["key_files"]["forward_anchor"]["immutable"] is True


def test_first_attempt_is_mechanically_derived_from_parent_clock():
    first = derive_first_forward_attempt(
        last_successful_execution_day=date(2026, 9, 18),
        pending_retry=False,
    )
    assert first.execution_day == EXPECTED_FIRST_EXECUTION_DAY
    assert first.signal_day == EXPECTED_FIRST_SIGNAL_DAY
    assert first.logical_signal_time == EXPECTED_FIRST_LOGICAL_SIGNAL
    assert first.week_one_end.isoformat() == "2026-10-02T00:00:00+00:00"


def test_pending_retry_and_calendar_adjustment_are_rejected():
    with pytest.raises(M2NotReady):
        derive_first_forward_attempt(
            last_successful_execution_day="2026-09-18",
            pending_retry=True,
        )
    with pytest.raises(M2NotReady):
        derive_first_forward_attempt(
            last_successful_execution_day="2026-09-18",
            pending_retry=False,
            interval_days=5,
        )


def test_pre_boundary_measurement_is_not_forward_evidence():
    with pytest.raises(M2NotReady):
        validate_forward_measurement_timestamp("2026-09-24T23:59:59Z")
    assert validate_forward_measurement_timestamp("2026-09-25T00:00:00Z") == EXPECTED_FIRST_LOGICAL_SIGNAL


def test_formal_output_is_write_once_and_contains_only_five_files(tmp_path):
    output_root = tmp_path / "m2"
    result = build_formal_artifacts(
        now=datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc),
        output_root=output_root,
    )
    assert result["decision"] == "V2.1-M2 START FREEZE READY"
    assert result["status"] == "FROZEN_WAITING_FOR_FIRST_FORWARD_ATTEMPT"
    assert set(path.name for path in output_root.iterdir()) == {
        "XS_LOWVOL_V2_1_FORWARD_START_MANIFEST.yaml",
        "XS_LOWVOL_V2_1_FORWARD_START_MANIFEST.sha256",
        "V2_1_M2_START_FREEZE.json",
        "V2_1_M2_START_FREEZE.md",
        "START_BINDING_GIT_BLOB_MANIFEST.json",
    }
    assert set(result["gates"]) == set(GATE_NAMES)
    assert all(value == "PASS" for value in result["gates"].values())
    with pytest.raises(Exception):
        build_formal_artifacts(
            now=datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc),
            output_root=output_root,
        )

    freeze = json.loads((output_root / "V2_1_M2_START_FREEZE.json").read_text(encoding="utf-8"))
    forbidden = {
        "forward_return",
        "forward_pnl",
        "forward_sharpe",
        "forward_drawdown",
        "forward_profit_factor",
        "bootstrap",
        "successful_forward_cycle_count",
        "52_week_sample_count",
    }
    assert not forbidden.intersection(freeze)
    assert freeze["safety"]["future_market_data_fetched"] is False
    assert freeze["safety"]["first_forward_attempt_executed"] is False
    assert freeze["warm_start"]["is_first_forward_scale"] is False
    assert freeze["first_attempt_risk_policy"]["recompute_at_first_attempt"] is True


def test_formal_freeze_after_boundary_is_not_ready(tmp_path):
    with pytest.raises(M2NotReady):
        build_formal_artifacts(
            now=datetime(2026, 9, 25, tzinfo=timezone.utc),
            output_root=tmp_path / "too-late",
        )


def test_m2_runner_has_no_market_downloader_dependency():
    source = (ROOT / "backtest" / "v2_1_m2_start_freeze.py").read_text(encoding="utf-8")
    assert "urllib" not in source
    assert "requests" not in source
    assert "urlopen" not in source
    assert "m1_b" not in source
    assert "market_data_access_performed\": False" in source


def test_anchor_source_bytes_are_not_changed():
    before = ANCHOR_PATH.read_bytes()
    _verify_frozen_identities()
    assert ANCHOR_PATH.read_bytes() == before
