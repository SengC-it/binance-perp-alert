"""V2.1-M0.2 tests for the independent Forward identity and time anchor."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.xs_lowvol_v2_1_anchor import (
    APPROVED_V1_CONTROL_SHA256,
    APPROVED_V2_1_FORWARD_ANCHOR_SHA256,
    APPROVED_V2_1_M0_COMMIT,
    APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC,
    APPROVED_V2_1_PROTOCOL_SHA256,
    APPROVED_V2_1_SPEC_SHA256,
    V21_FORWARD_ANCHOR_ID,
    V21_FORWARD_ANCHOR_STATUS,
    V21ForwardAnchorError,
    V21ForwardEpoch,
    V2_1_FORWARD_ANCHOR_HASH_PATH,
    V2_1_FORWARD_ANCHOR_PATH,
    load_v2_1_forward_anchor,
    read_v2_1_forward_anchor_hash,
    validate_forward_logical_signal_time,
    validate_forward_temporal_inputs,
    validate_v2_1_forward_anchor,
    v2_1_forward_anchor_sha256,
    verify_v2_1_forward_anchor_hash,
)
from src.xs_lowvol_v2_anchor import load_v2_forward_anchor


UTC = timezone.utc
SIGNAL_DAY = date(2026, 9, 16)
EXECUTION_DAY = date(2026, 9, 17)
EXECUTION_MIDNIGHT = datetime(2026, 9, 17, tzinfo=UTC)


def _invalid_anchor(**changes: object) -> dict[str, object]:
    altered = deepcopy(load_v2_1_forward_anchor())
    altered.update(changes)
    return altered


def test_v2_1_forward_anchor_is_canonically_pinned_and_not_started():
    anchor = load_v2_1_forward_anchor()
    validate_v2_1_forward_anchor()
    assert anchor["anchor_id"] == V21_FORWARD_ANCHOR_ID
    assert anchor["status"] == V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
    assert v2_1_forward_anchor_sha256(anchor) == APPROVED_V2_1_FORWARD_ANCHOR_SHA256
    assert read_v2_1_forward_anchor_hash() == APPROVED_V2_1_FORWARD_ANCHOR_SHA256
    assert verify_v2_1_forward_anchor_hash() == APPROVED_V2_1_FORWARD_ANCHOR_SHA256
    assert anchor["approved_v2_1_m0_commit"] == APPROVED_V2_1_M0_COMMIT
    assert anchor["v1_control_sha256"] == APPROVED_V1_CONTROL_SHA256
    assert anchor["v2_1_spec_sha256"] == APPROVED_V2_1_SPEC_SHA256
    assert anchor["v2_1_protocol_sha256"] == APPROVED_V2_1_PROTOCOL_SHA256


def test_v2_1_anchor_binds_the_complete_identity_and_does_not_choose_first_day():
    anchor = load_v2_1_forward_anchor()
    assert anchor["strategy_id"] == "XS-LOWVOL-V2.1-RISK15"
    assert anchor["execution_lag_days"] == 1
    assert anchor["execution_day_rule"] == "signal_day_plus_1_utc_calendar_day"
    assert anchor["logical_signal_time_rule"] == "execution_day_00:00:00Z"
    assert anchor["forward_start_rule"]["first_signal_day"] == "NOT_SELECTED_IN_M0_2"
    assert "first_eligible_signal_day" not in anchor
    assert anchor["forward_start_rule"]["once_selected_cannot_be_moved_later"] is True


@pytest.mark.parametrize(
    "field_name",
    [
        "v1_control_sha256",
        "v2_1_spec_sha256",
        "v2_1_protocol_sha256",
    ],
)
def test_anchor_rejects_wrong_frozen_hashes(field_name: str):
    with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        validate_v2_1_forward_anchor(
            _invalid_anchor(**{field_name: "0" * 64}),
            require_approved_hash=False,
            require_immutable_lineage=False,
            require_current_identities=False,
        )


def test_anchor_rejects_another_well_formed_commit():
    with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        validate_v2_1_forward_anchor(
            _invalid_anchor(approved_v2_1_m0_commit="0" * 40),
            require_approved_hash=False,
            require_immutable_lineage=False,
            require_current_identities=False,
        )


def test_anchor_tampering_is_rejected_fail_closed():
    with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        validate_v2_1_forward_anchor(
            _invalid_anchor(status="STARTED"),
            require_approved_hash=False,
            require_immutable_lineage=False,
            require_current_identities=False,
        )


@pytest.mark.parametrize(
    "status",
    ["RUNNING", "STARTED", "ACTIVE", "COLLECTING", "FORWARD_DAY_1"],
)
def test_anchor_forbids_started_statuses(status: str):
    with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        validate_v2_1_forward_anchor(
            _invalid_anchor(status=status),
            require_approved_hash=False,
            require_immutable_lineage=False,
            require_current_identities=False,
        )


def test_old_v2_anchor_cannot_be_accepted_as_v2_1_identity():
    with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        validate_v2_1_forward_anchor(
            load_v2_forward_anchor(),
            require_approved_hash=False,
            require_immutable_lineage=False,
            require_current_identities=False,
        )


def test_same_day_execution_is_rejected():
    with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        validate_forward_temporal_inputs(
            signal_day=SIGNAL_DAY,
            execution_day=SIGNAL_DAY,
            logical_signal_time=datetime(2026, 9, 16, tzinfo=UTC),
        )


def test_two_day_execution_is_rejected():
    with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        validate_forward_temporal_inputs(
            signal_day=SIGNAL_DAY,
            execution_day=SIGNAL_DAY + timedelta(days=2),
            logical_signal_time=datetime(2026, 9, 18, tzinfo=UTC),
        )


def test_one_day_execution_and_execution_midnight_pass():
    assert validate_forward_temporal_inputs(
        signal_day=SIGNAL_DAY,
        execution_day=EXECUTION_DAY,
        logical_signal_time=EXECUTION_MIDNIGHT,
    ) == EXECUTION_MIDNIGHT


@pytest.mark.parametrize(
    "logical_signal_time",
    [
        datetime(2026, 9, 16, 12, tzinfo=UTC),
        datetime(2026, 9, 17, 12, tzinfo=UTC),
    ],
)
def test_non_execution_day_midnight_is_rejected(logical_signal_time: datetime):
    with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        validate_forward_temporal_inputs(
            signal_day=SIGNAL_DAY,
            execution_day=EXECUTION_DAY,
            logical_signal_time=logical_signal_time,
        )


def test_naive_logical_signal_time_is_rejected():
    with pytest.raises(V21ForwardAnchorError, match="timezone-aware"):
        validate_forward_temporal_inputs(
            signal_day=SIGNAL_DAY,
            execution_day=EXECUTION_DAY,
            logical_signal_time=datetime(2026, 9, 17),
        )


@pytest.mark.parametrize(
    "logical_signal_time",
    [
        APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC,
        APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC - timedelta(seconds=1),
    ],
)
def test_logical_signal_time_at_or_before_freeze_is_ineligible(
    logical_signal_time: datetime,
):
    with pytest.raises(V21ForwardAnchorError, match="strictly after"):
        validate_forward_logical_signal_time(logical_signal_time)


def test_logical_signal_time_after_freeze_is_temporally_eligible():
    later = APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC + timedelta(seconds=1)
    assert validate_forward_logical_signal_time(later) == later


def test_v21_forward_epoch_rejects_identity_drift():
    for field_name in (
        "approved_m0_commit",
        "v1_control_sha256",
        "v2_1_spec_sha256",
        "v2_1_protocol_sha256",
        "forward_anchor_sha256",
    ):
        with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
            V21ForwardEpoch(**{field_name: "0" * (40 if field_name == "approved_m0_commit" else 64)})

    with pytest.raises(V21ForwardAnchorError, match="FORWARD_ANCHOR_INVALID"):
        V21ForwardEpoch(
            approved_m0_commit_timestamp_utc=APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC
            + timedelta(seconds=1)
        )


def test_v21_forward_epoch_validates_the_frozen_identity_and_boundary():
    epoch = V21ForwardEpoch()
    with pytest.raises(V21ForwardAnchorError, match="strictly after"):
        epoch.validate_logical_signal_time(APPROVED_V2_1_M0_COMMIT_TIMESTAMP_UTC)
    assert epoch.validate_logical_signal_time(EXECUTION_MIDNIGHT) == EXECUTION_MIDNIGHT


def test_anchor_safety_and_paired_halt_semantics_are_frozen():
    anchor = load_v2_1_forward_anchor()
    assert anchor["time_basis"] == "UTC"
    assert anchor["market_day_basis"] == "UTC_CALENDAR_DAY"
    assert anchor["market_schedule"] == "7x24"
    assert anchor["historical_backfill_into_forward"] == "FORBIDDEN"
    assert anchor["forward_evidence_before_start"] == "FORBIDDEN"
    assert anchor["paired_experiment"]["scheduler_authority"] == "V1_CONTROL_PARENT_ONLY"
    assert anchor["paired_experiment"]["risk_data_integrity_halt"] == "V2_DATA_INTEGRITY_HALT"
    assert anchor["paired_experiment"]["halt_semantics"] == "HALT_ENTIRE_PAIRED_EXPERIMENT"
    assert anchor["paired_experiment"]["collect_after_halt"] == "FORBIDDEN"
    assert anchor["safety"] == {"live_trading": False, "paper_only": True, "http_methods": ["GET"]}


def test_frozen_v21_inputs_and_old_v2_anchor_are_byte_unchanged():
    expected = {
        Path("research/v2_1/XS_LOWVOL_V2_1_SPEC.yaml"): "f3be11a96b224d3782512c848ed61e92c0b5d741bf81486eb4f77c3f5a75c2dd",
        Path("research/v2_1/XS_LOWVOL_V2_1_SPEC.sha256"): "cd136b755945233e80adb5fa50b30944cb9ea4a838a62cab55103e196b390819",
        Path("research/v2_1/XS_LOWVOL_V2_1_PROTOCOL.yaml"): "92702942f1005d9df6bf5f58e78b6f06267492fe183acf98459c9010fbfbbd37",
        Path("research/v2_1/XS_LOWVOL_V2_1_PROTOCOL.sha256"): "5790f0c2e30f42369d4a0f42428228716902bbfc6893dbd7bc05290da38d04c9",
        Path("research/v2/XS_LOWVOL_V2_FORWARD_ANCHOR.yaml"): "732e802c5402ba85ae87bc724734086a8c7bf7fc35dfd48960852745347fe58b",
        Path("research/v2/XS_LOWVOL_V2_FORWARD_ANCHOR.sha256"): "d516d62b9c81f930e177f6a855d9e9784b8ca0d65e82a28866e82c54c0abfe58",
    }
    for path, expected_hash in expected.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash

    entries = {path.name for path in Path("research/v2_1").iterdir() if path.is_file()}
    assert entries == {
        "XS_LOWVOL_V2_1_SPEC.yaml",
        "XS_LOWVOL_V2_1_SPEC.sha256",
        "XS_LOWVOL_V2_1_PROTOCOL.yaml",
        "XS_LOWVOL_V2_1_PROTOCOL.sha256",
        V2_1_FORWARD_ANCHOR_PATH.name,
        V2_1_FORWARD_ANCHOR_HASH_PATH.name,
    }
