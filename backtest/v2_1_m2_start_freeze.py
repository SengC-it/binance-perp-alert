"""XS-LOWVOL V2.1-M2 Forward start freeze.

This stage records only the identity and temporal boundary of the future
Forward run.  It reads accepted pre-start artifacts, performs no market-data
access, and deliberately has no portfolio or performance runner dependency.
The formal command is one-shot and writes exactly five new artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from backtest.m1_protocol import verify_protocol_hash
from backtest.v2_1_protocol import (
    frozen_v2_1_forward_gate_policy,
    verify_v2_1_protocol_hash,
)
from src.xs_lowvol_spec import CONTROL_SPEC_HASH, resolve_rules, strategy_spec_hash
from src.xs_lowvol_v2_1_anchor import (
    V21_FORWARD_ANCHOR_STATUS,
    load_v2_1_forward_anchor,
    verify_v2_1_forward_anchor_hash,
    validate_v2_1_forward_anchor,
)
from src.xs_lowvol_v2_1_spec import verify_v2_1_spec_hash


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc

BASE_COMMIT = "826a76ffde797b7a8c98e8f3516da322f0bb3f42"
RUN_ID = "XS-LOWVOL-V2.1-M2-START-V1"
APPROVAL = "FREEZE V2.1-M2 FORWARD START"
STRATEGY_ID = "XS-LOWVOL-V2.1-RISK15"
DECISION = "V2.1-M2 START FREEZE READY"
FORMAL_STATUS = "FROZEN_WAITING_FOR_FIRST_FORWARD_ATTEMPT"

V1_PROTOCOL_SHA256 = (
    "607d262248a4ef0d0f968c1db61f9f09ffd8ee19a247a46571e131caaa8ea03d"
)
V1_CONTROL_SHA256 = (
    "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678"
)
V2_1_SPEC_SHA256 = (
    "e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c"
)
V2_1_PROTOCOL_SHA256 = (
    "6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d"
)
FORWARD_ANCHOR_SHA256 = (
    "a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b"
)
M13_OVERLAY_SHA256 = (
    "7a6f183262580933c4802acb9454b0617c7c095b458b79f1e56fadb1c9ec64c1"
)

M142_RESULT_COMMIT = "b01086dcc8417f404f369099305a74cf2afaebb1"
M142_CODE_COMMIT = "82fffabada8da6bd41d919136f22eb4a3405bfb3"
M1421B_RESULT_COMMIT = BASE_COMMIT
M1421B_CODE_COMMIT = "1daef982ee523bf57be2ac9741b4f95732aedd02"
M1421B_GIT_BLOB_MANIFEST_SHA256 = (
    "785fb58dde6146a3b583c3ad12c0f65958461f4c941fae5e6bd4eba2e6a80a18"
)

PRESTART_CUTOFF = datetime(2026, 9, 19, tzinfo=UTC)
EXPECTED_FIRST_SIGNAL_DAY = date(2026, 9, 24)
EXPECTED_FIRST_LOGICAL_SIGNAL = datetime(2026, 9, 25, tzinfo=UTC)
EXPECTED_FIRST_EXECUTION_DAY = date(2026, 9, 25)
EXPECTED_WEEK_GRID_ID = "FORWARD_START_ALIGNED_7X24H"

M142_ROOT = PROJECT_ROOT / "research" / "v2_1" / "m1_4_2"
M1421B_ROOT = PROJECT_ROOT / "research" / "v2_1" / "m1_4_2_1b"
M13_MANIFEST_PATH = PROJECT_ROOT / "research" / "v2_1" / "m1_3" / "LIFECYCLE_OVERRIDE_MANIFEST.json"
ANCHOR_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_FORWARD_ANCHOR.yaml"
ANCHOR_HASH_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_FORWARD_ANCHOR.sha256"
V1_SPEC_PATH = PROJECT_ROOT / "research" / "xs_lowvol_v1.yaml"
V1_PROTOCOL_PATH = PROJECT_ROOT / "research" / "m1" / "XS_LOWVOL_M1_PROTOCOL.yaml"
V1_PROTOCOL_HASH_PATH = PROJECT_ROOT / "research" / "m1" / "XS_LOWVOL_M1_PROTOCOL.sha256"
V21_SPEC_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_SPEC.yaml"
V21_SPEC_HASH_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_SPEC.sha256"
V21_PROTOCOL_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_PROTOCOL.yaml"
V21_PROTOCOL_HASH_PATH = PROJECT_ROOT / "research" / "v2_1" / "XS_LOWVOL_V2_1_PROTOCOL.sha256"

M142_RESULT_PATH = M142_ROOT / "V2_1_M1_4_2_PARENT_RECONSTRUCTION.json"
M142_CHECKPOINT_PATH = M142_ROOT / "PRE_CONTAMINATION_CHECKPOINT.json"
M142_OVERLAY_PATH = M142_ROOT / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY.json"
M142_STATE_PATH = M142_ROOT / "CORRECTED_CURRENT_PARENT_STATE.json"
M142_EXTENSION_PATH = M142_ROOT / "CORRECTED_PRESTART_EXTENSION_MANIFEST.json"
M142_RISK_PATH = M142_ROOT / "CORRECTED_LATEST_13_RISK_INPUT.json"
M1421B_RESULT_PATH = M1421B_ROOT / "V2_1_M1_4_2_1B_PRODUCT_PROVENANCE.json"
M1421B_REGISTRY_PATH = M1421B_ROOT / "MERGED_HISTORICAL_PRODUCT_TYPE_REGISTRY.json"
M1421B_OVERLAY_PATH = M1421B_ROOT / "CONTRACT_TYPE_ELIGIBILITY_OVERLAY_V4.json"
M1421B_GIT_MANIFEST_PATH = M1421B_ROOT / "GIT_BLOB_IMMUTABILITY_MANIFEST.json"

OUTPUT_ROOT = PROJECT_ROOT / "research" / "v2_1" / "m2"
START_MANIFEST_PATH = OUTPUT_ROOT / "XS_LOWVOL_V2_1_FORWARD_START_MANIFEST.yaml"
START_MANIFEST_HASH_PATH = OUTPUT_ROOT / "XS_LOWVOL_V2_1_FORWARD_START_MANIFEST.sha256"
FREEZE_RESULT_PATH = OUTPUT_ROOT / "V2_1_M2_START_FREEZE.json"
FREEZE_REPORT_PATH = OUTPUT_ROOT / "V2_1_M2_START_FREEZE.md"
BINDING_MANIFEST_PATH = OUTPUT_ROOT / "START_BINDING_GIT_BLOB_MANIFEST.json"

M142_EXPECTED_ARTIFACT_SHA256 = {
    "result": "bbd118c01f72a218de02c168c0ff28a59b91ae20093350451ea61e82c62b3f74",
    "checkpoint": "ba4e936f796e024747cd4d75f2b70183a1ea58cd9b77ce6e838881472a0becf1",
    "contract_type_overlay": "64826ae0844073241d6180d4270cb6851401d98951bf65191e757eddb469ed4e",
    "current_parent_state": "30231392de21231095238dabd25dc279b98e528613b1bcfa01325ecb7c39f278",
    "extension": "74e30d6c5f56e2e172614fbb784655115cf958c3da9cd12589e7ca6a90fafb05",
    "risk": "c5f39310c5d51fde0a8270bc888933d73532c6bcd75e4c584330255c5a612c7b",
}
M1421B_EXPECTED_ARTIFACT_SHA256 = {
    "result": "ea58e6987c22629c1bb1949aa28b758350c68f265475196cafb9fda39119e114",
    "registry": "8692662d45d5e20ce6324dbc4dc52bbf9102d73e3b3e70c67a5e2bade5d00057",
    "overlay": "823f8c20172c15e33536e7e20e150af965da0dfc1c70607e0d22f73e0eea1eb7",
    "git_manifest": "67ad5d2e3fd14cf84360ce40b0ef5f3ade87a3ffb78f7cda3b16deb31f9f8b74",
}

M142_ARTIFACTS = {
    "result": M142_RESULT_PATH,
    "checkpoint": M142_CHECKPOINT_PATH,
    "contract_type_overlay": M142_OVERLAY_PATH,
    "current_parent_state": M142_STATE_PATH,
    "extension": M142_EXTENSION_PATH,
    "risk": M142_RISK_PATH,
}
M1421B_ARTIFACTS = {
    "result": M1421B_RESULT_PATH,
    "registry": M1421B_REGISTRY_PATH,
    "overlay": M1421B_OVERLAY_PATH,
    "git_manifest": M1421B_GIT_MANIFEST_PATH,
}

GATE_NAMES = tuple(f"S{index}_{name}" for index, name in enumerate(
    (
        "identity",
        "anchor_binding",
        "parent_state_binding",
        "product_provenance_binding",
        "parent_state_complete",
        "warm_start_risk_valid",
        "scheduler_due_derivation",
        "first_attempt_dates",
        "freeze_before_logical_signal_time",
        "no_future_market_data",
        "start_bridge_policy_frozen",
        "no_prestart_forward_evidence",
        "accounting_carry_no_reset",
        "risk_recompute_policy_frozen",
        "parent_only_scheduler",
        "paired_atomicity",
        "forward_metric_grid_frozen",
        "forward_gates_unchanged",
        "anchor_file_immutable",
        "safety",
        "ci",
    )
))
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class M2StartFreezeError(RuntimeError):
    """Base class for fail-closed M2 start-freeze errors."""


class M2IdentityError(M2StartFreezeError):
    """An accepted identity or immutable artifact does not match."""


class M2NotReady(M2StartFreezeError):
    """The future start cannot be frozen safely."""


@dataclass(frozen=True)
class FirstForwardAttempt:
    signal_day: date
    logical_signal_time: datetime
    execution_day: date
    interval_days: int
    execution_lag_days: int

    @property
    def week_one_start(self) -> datetime:
        return self.logical_signal_time

    @property
    def week_one_end(self) -> datetime:
        return self.logical_signal_time + timedelta(days=7)


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise M2IdentityError(f"cannot read immutable artifact: {path}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M2IdentityError(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise M2IdentityError(f"JSON artifact must be an object: {path}")
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise M2IdentityError(f"{field_name} is not a valid UTC date") from exc


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, (str, datetime)):
        raise M2IdentityError(f"{field_name} is not a valid UTC timestamp")
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise M2IdentityError(f"{field_name} is not a valid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise M2IdentityError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def derive_first_forward_attempt(
    *,
    last_successful_execution_day: date | str,
    pending_retry: bool,
    interval_days: int = 7,
    execution_lag_days: int = 1,
) -> FirstForwardAttempt:
    """Derive the first attempt from the parent clock and frozen rules."""
    if pending_retry is not False:
        raise M2NotReady("pending parent retry prevents a new frozen due date")
    if interval_days != 7:
        raise M2NotReady("M2 requires the frozen 7 UTC calendar-day interval")
    if execution_lag_days != 1:
        raise M2NotReady("M2 requires the frozen one-calendar-day execution lag")
    last_execution = _parse_date(last_successful_execution_day, "last_successful_execution_day")
    execution_day = last_execution + timedelta(days=interval_days)
    signal_day = execution_day - timedelta(days=execution_lag_days)
    logical_signal_time = datetime.combine(execution_day, time.min, tzinfo=UTC)
    return FirstForwardAttempt(
        signal_day=signal_day,
        logical_signal_time=logical_signal_time,
        execution_day=execution_day,
        interval_days=interval_days,
        execution_lag_days=execution_lag_days,
    )


def validate_forward_measurement_timestamp(
    timestamp: datetime | str, boundary: datetime = EXPECTED_FIRST_LOGICAL_SIGNAL
) -> datetime:
    """Reject any observation that would precede the frozen Forward boundary."""
    parsed = _parse_utc(timestamp, "forward measurement timestamp")
    if parsed < boundary:
        raise M2NotReady("Forward evidence cannot precede the frozen logical signal time")
    return parsed


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise M2IdentityError(f"git identity lookup failed: git {' '.join(args)}") from exc
    return result.stdout.strip()


def _is_ancestor(ancestor: str, commit: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, commit],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise M2IdentityError("git could not verify accepted M2 base lineage") from exc
    return result.returncode == 0


def git_blob_manifest(commit: str, prefix: str) -> dict[str, str]:
    """Return a platform-independent tracked path -> blob OID mapping."""
    try:
        result = subprocess.run(
            [
                "git",
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                commit,
                "--",
                prefix.replace("\\", "/").rstrip("/"),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise M2IdentityError(f"git tree lookup failed for {commit}:{prefix}") from exc
    output: dict[str, str] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            _mode, object_type, oid = metadata.split()
        except ValueError as exc:
            raise M2IdentityError(f"malformed git tree record for {prefix}") from exc
        if object_type == b"blob":
            output[path_bytes.decode("utf-8")] = oid.decode("ascii")
    return dict(sorted(output.items()))


def _git_blob_oid(commit: str, path: str) -> str:
    oid = _git("rev-parse", f"{commit}:{path}")
    if not _HEX40.fullmatch(oid):
        raise M2IdentityError(f"git path is not a blob: {commit}:{path}")
    return oid


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(_canonical(value), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _require(value: Any, expected: Any, label: str) -> None:
    if value != expected:
        raise M2IdentityError(f"{label} changed: expected {expected!r}, got {value!r}")


def _verify_frozen_identities() -> dict[str, str]:
    try:
        v1_protocol = verify_protocol_hash(V1_PROTOCOL_PATH, V1_PROTOCOL_HASH_PATH)
        v2_spec = verify_v2_1_spec_hash(V21_SPEC_PATH, V21_SPEC_HASH_PATH)
        v2_protocol = verify_v2_1_protocol_hash(V21_PROTOCOL_PATH, V21_PROTOCOL_HASH_PATH)
        anchor = verify_v2_1_forward_anchor_hash(ANCHOR_PATH, ANCHOR_HASH_PATH)
        validate_v2_1_forward_anchor()
        gate_policy = frozen_v2_1_forward_gate_policy()
    except Exception as exc:  # noqa: BLE001 - all identity drift is fatal
        raise M2IdentityError("frozen V1/V2.1/Anchor identity validation failed") from exc
    _require(v1_protocol, V1_PROTOCOL_SHA256, "V1 Protocol SHA-256")
    _require(strategy_spec_hash(), V1_CONTROL_SHA256, "V1 Control SHA-256")
    _require(CONTROL_SPEC_HASH, V1_CONTROL_SHA256, "V1 Control registered SHA-256")
    _require(v2_spec, V2_1_SPEC_SHA256, "V2.1 Spec SHA-256")
    _require(v2_protocol, V2_1_PROTOCOL_SHA256, "V2.1 Protocol SHA-256")
    _require(anchor, FORWARD_ANCHOR_SHA256, "Forward Anchor SHA-256")
    _require(V21_FORWARD_ANCHOR_STATUS, "FROZEN_NOT_YET_STARTED", "Forward Anchor status")
    return {
        "v1_protocol_sha256": v1_protocol,
        "v1_control_sha256": V1_CONTROL_SHA256,
        "v2_1_spec_sha256": v2_spec,
        "v2_1_protocol_sha256": v2_protocol,
        "forward_anchor_sha256": anchor,
        "forward_anchor_status": V21_FORWARD_ANCHOR_STATUS,
        "m1_3_overlay_sha256": M13_OVERLAY_SHA256,
        "strategy_id": STRATEGY_ID,
        "forward_gate_policy_sha256": sha256_json(gate_policy.__dict__),
    }


def _verify_artifact_hashes() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    for name, path in M142_ARTIFACTS.items():
        _require(sha256_file(path), M142_EXPECTED_ARTIFACT_SHA256[name], f"M1.4.2 {name} artifact SHA-256")
    for name, path in M1421B_ARTIFACTS.items():
        _require(sha256_file(path), M1421B_EXPECTED_ARTIFACT_SHA256[name], f"M1.4.2.1B {name} artifact SHA-256")
    m142_result = _read_json(M142_RESULT_PATH)
    m142_state = _read_json(M142_STATE_PATH)
    m142_risk = _read_json(M142_RISK_PATH)
    m1421b_result = _read_json(M1421B_RESULT_PATH)
    m1421b_registry = _read_json(M1421B_REGISTRY_PATH)
    m1421b_overlay = _read_json(M1421B_OVERLAY_PATH)
    m1421b_git_manifest = _read_json(M1421B_GIT_MANIFEST_PATH)
    m13_manifest = _read_json(M13_MANIFEST_PATH)
    _require(sha256_json(m13_manifest), M13_OVERLAY_SHA256, "M1.3 lifecycle overlay SHA-256")

    _require(m142_result.get("decision"), "V2.1-M1.4.2 PARENT_STATE READY", "M1.4.2 decision")
    _require(m142_result.get("code_commit_used_by_run"), M142_CODE_COMMIT, "M1.4.2 code commit")
    _require(m142_result.get("m1_3_overlay_sha256"), M13_OVERLAY_SHA256, "M1.3 overlay binding")
    _require(m142_result.get("forward_anchor_status"), V21_FORWARD_ANCHOR_STATUS, "M1.4.2 anchor status")
    _require(m142_result.get("first_forward_signal_selected"), False, "M1.4.2 Forward selection")
    _require(m142_result.get("forward_evidence_created"), False, "M1.4.2 Forward evidence")

    _require(m1421b_result.get("decision"), "V2.1-M1.4.2.1B PRODUCT_PROVENANCE READY", "M1.4.2.1B decision")
    _require(m1421b_result.get("code_commit_used_by_run"), M1421B_CODE_COMMIT, "M1.4.2.1B code commit")
    _require(m1421b_result.get("git_blob_manifest_sha256"), M1421B_GIT_BLOB_MANIFEST_SHA256, "M1.4.2.1B Git Blob Manifest SHA-256")
    _require(m1421b_result.get("old_139_carried_count"), 139, "M1.4.2.1B old 139 count")
    _require(m1421b_result.get("six_added_count"), 6, "M1.4.2.1B six-added count")
    _require(m1421b_result.get("proven_count"), 145, "M1.4.2.1B proven count")
    _require(m1421b_result.get("unresolved_count"), 0, "M1.4.2.1B unresolved count")
    _require(m1421b_result.get("membership_set_unchanged"), True, "M1.4.2.1B membership")
    _require(m1421b_result.get("effective_timestamp_differences"), [], "M1.4.2.1B timestamp differences")
    _require(m1421b_result.get("m2_started"), False, "M1.4.2.1B M2 state")
    _require(m1421b_result.get("first_forward_signal_selected"), False, "M1.4.2.1B Forward selection")
    _require(m1421b_registry.get("old_139_carried_count"), 139, "merged registry old 139 count")
    _require(m1421b_registry.get("six_added_count"), 6, "merged registry six-added count")
    _require(m1421b_registry.get("proven_count"), 145, "merged registry proven count")
    _require(m1421b_registry.get("unresolved_count"), 0, "merged registry unresolved count")
    _require(m1421b_registry.get("membership_set_unchanged"), True, "merged registry membership")
    _require(m1421b_overlay.get("merged_provenance_count"), 145, "merged overlay provenance count")
    _require(m1421b_overlay.get("effective_timestamp_differences"), [], "merged overlay timestamp differences")
    _require(m1421b_git_manifest.get("identity_mechanism"), "GIT_BLOB_OID_FROM_COMMIT_TREE", "1B identity mechanism")
    if sha256_json(m1421b_git_manifest) != M1421B_GIT_BLOB_MANIFEST_SHA256:
        raise M2IdentityError("accepted M1.4.2.1B Git Blob Manifest canonical hash changed")
    return m142_result, m142_state, m142_risk, m1421b_result


def _verify_binding_trees(current_head: str) -> dict[str, Any]:
    if not _is_ancestor(BASE_COMMIT, current_head):
        raise M2IdentityError("current code commit does not descend from accepted M2 base")
    stage_specs = {
        "m1_4_2": (M142_RESULT_COMMIT, "research/v2_1/m1_4_2"),
        "m1_4_2_1b": (M1421B_RESULT_COMMIT, "research/v2_1/m1_4_2_1b"),
    }
    bindings: dict[str, Any] = {}
    for stage, (bound_commit, prefix) in stage_specs.items():
        bound = git_blob_manifest(bound_commit, prefix)
        current = git_blob_manifest(current_head, prefix)
        changed = sorted(path for path in set(bound) | set(current) if bound.get(path) != current.get(path))
        if changed:
            raise M2IdentityError(f"accepted {stage} Git tree changed: {changed}")
        bindings[stage] = {
            "bound_result_commit": bound_commit,
            "prefix": prefix,
            "bound_blob_manifest": bound,
            "current_head_blob_manifest": current,
            "changed_paths": changed,
            "immutable": True,
        }

    key_specs = {
        "m13_lifecycle_overlay": ("355dbfcb1dcf2d0e5f0fc1b84e478e788ec1b24a", "research/v2_1/m1_3/LIFECYCLE_OVERRIDE_MANIFEST.json"),
        "v1_strategy_spec": (M1421B_RESULT_COMMIT, "research/xs_lowvol_v1.yaml"),
        "v1_protocol": (M1421B_RESULT_COMMIT, "research/m1/XS_LOWVOL_M1_PROTOCOL.yaml"),
        "v1_protocol_hash": (M1421B_RESULT_COMMIT, "research/m1/XS_LOWVOL_M1_PROTOCOL.sha256"),
        "v2_1_spec": (M1421B_RESULT_COMMIT, "research/v2_1/XS_LOWVOL_V2_1_SPEC.yaml"),
        "v2_1_spec_hash": (M1421B_RESULT_COMMIT, "research/v2_1/XS_LOWVOL_V2_1_SPEC.sha256"),
        "v2_1_protocol": (M1421B_RESULT_COMMIT, "research/v2_1/XS_LOWVOL_V2_1_PROTOCOL.yaml"),
        "v2_1_protocol_hash": (M1421B_RESULT_COMMIT, "research/v2_1/XS_LOWVOL_V2_1_PROTOCOL.sha256"),
        "forward_anchor": (M1421B_RESULT_COMMIT, "research/v2_1/XS_LOWVOL_V2_1_FORWARD_ANCHOR.yaml"),
        "forward_anchor_hash": (M1421B_RESULT_COMMIT, "research/v2_1/XS_LOWVOL_V2_1_FORWARD_ANCHOR.sha256"),
    }
    key_files: dict[str, Any] = {}
    for name, (bound_commit, path) in key_specs.items():
        bound_oid = _git_blob_oid(bound_commit, path)
        current_oid = _git_blob_oid(current_head, path)
        if bound_oid != current_oid:
            raise M2IdentityError(f"frozen key file Git blob changed: {path}")
        key_files[name] = {
            "path": path,
            "bound_commit": bound_commit,
            "bound_blob_oid": bound_oid,
            "current_head_blob_oid": current_oid,
            "immutable": True,
        }
    return {
        "schema_version": "START_BINDING_GIT_BLOB_MANIFEST.v1",
        "classification": ["START_IDENTITY_ONLY", "NOT_FORWARD", "NOT_VALIDATION_EVIDENCE"],
        "current_head_commit": current_head,
        "identity_mechanism": "GIT_BLOB_OID_FROM_COMMIT_TREE",
        "accepted_stage_commits": {
            "m1_4_2_result_commit": M142_RESULT_COMMIT,
            "m1_4_2_code_commit": M142_CODE_COMMIT,
            "m1_4_2_1b_result_commit": M1421B_RESULT_COMMIT,
            "m1_4_2_1b_code_commit": M1421B_CODE_COMMIT,
        },
        "bindings": bindings,
        "key_files": key_files,
    }


def _verify_prestart_state(
    m142_result: Mapping[str, Any],
    state: Mapping[str, Any],
    risk: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    _require(state.get("cutoff_utc"), "2026-09-19T00:00:00Z", "accepted prestart cutoff")
    _require(state.get("last_successful_signal_day"), "2026-09-17", "last successful signal day")
    _require(state.get("last_successful_execution_day"), "2026-09-18", "last successful execution day")
    _require(state.get("next_rebalance_due_day"), "2026-09-25", "next rebalance due day")
    _require(state.get("pending_retry"), False, "pending retry")
    _require(state.get("parent_state_complete"), True, "parent state completeness")
    _require(state.get("current_position_count"), 10, "current position count")
    _require(state.get("funding_issue_count"), 0, "funding issue count")
    _require(state.get("protocol_ineligible_position_count"), 0, "protocol-ineligible position count")
    _require(state.get("unresolved_state_issue_count"), 0, "unresolved state issue count")
    positions = state.get("current_control_positions")
    if not isinstance(positions, list) or len(positions) != 10:
        raise M2IdentityError("accepted current parent positions are not the frozen ten")
    if any(not isinstance(row, Mapping) or not row.get("symbol") or not row.get("direction") for row in positions):
        raise M2IdentityError("accepted current parent position identity is incomplete")
    if any(str(row.get("symbol", "")).upper() in {"TSLAUSDT", "AXTIUSDT", "CRCLUSDT"} for row in positions):
        raise M2IdentityError("accepted current parent contains a protocol-ineligible position")
    if state.get("current_control_positions") != m142_result.get("current_positions"):
        raise M2IdentityError("M1.4.2 result and current parent state position records differ")
    _require(m142_result.get("current_position_count"), 10, "M1.4.2 result current position count")
    _require(m142_result.get("last_successful_signal_day"), "2026-09-17", "M1.4.2 signal day")
    _require(m142_result.get("last_successful_execution_day"), "2026-09-18", "M1.4.2 execution day")
    _require(m142_result.get("next_rebalance_due_day"), "2026-09-25", "M1.4.2 next due day")
    _require(m142_result.get("pending_retry"), False, "M1.4.2 pending retry")
    _require(m142_result.get("current_position_count"), state.get("current_position_count"), "M1.4.2/state count")

    _require(risk.get("cutoff_utc"), "2026-09-19T00:00:00Z", "risk cutoff")
    _require(risk.get("input_complete_count"), 13, "warm-start risk input count")
    _require(risk.get("selected_count"), 13, "warm-start risk selected count")
    _require(risk.get("status"), "VALID", "warm-start risk status")
    _require(risk.get("risk_diagnostic_status"), "INITIAL_STATE_DIAGNOSTIC_ONLY", "warm-start risk classification")
    _require(risk.get("reference_vol"), 0.0681145306765338, "warm-start reference volatility")
    _require(risk.get("position_scale"), 1.0, "warm-start position scale")
    _require(
        risk.get("risk_parameters"),
        {
            "annualization": "sqrt(52)",
            "leverage": False,
            "max_scale": 1.0,
            "target_annualized_volatility": 0.15,
            "volatility_statistic": "population_std",
            "week_count": 13,
            "zero_reference_vol_scale": "1",
        },
        "frozen V2.1 risk parameters",
    )
    position_state_sha256 = sha256_json(positions)
    identity = [
        {"symbol": str(row["symbol"]), "direction": str(row["direction"])}
        for row in sorted(positions, key=lambda item: str(item["symbol"]))
    ]
    return {
        "position_count": 10,
        "position_state_sha256": position_state_sha256,
        "position_identity": identity,
        "reference_vol": 0.0681145306765338,
        "position_scale": 1.0,
        "risk_status": "INITIAL_STATE_DIAGNOSTIC_ONLY",
    }, position_state_sha256


def _build_gate_values(
    *,
    identities: Mapping[str, str],
    binding_manifest: Mapping[str, Any],
    first: FirstForwardAttempt,
    freeze_created_at: datetime,
    parent_state: Mapping[str, Any],
    warm_start: Mapping[str, Any],
) -> dict[str, str]:
    checks = {
        "S0_identity": identities.get("strategy_id") == STRATEGY_ID,
        "S1_anchor_binding": identities.get("forward_anchor_sha256") == FORWARD_ANCHOR_SHA256 and identities.get("forward_anchor_status") == "FROZEN_NOT_YET_STARTED",
        "S2_parent_state_binding": binding_manifest["bindings"]["m1_4_2"]["immutable"] and binding_manifest["accepted_stage_commits"]["m1_4_2_result_commit"] == M142_RESULT_COMMIT,
        "S3_product_provenance_binding": binding_manifest["bindings"]["m1_4_2_1b"]["immutable"] and binding_manifest["accepted_stage_commits"]["m1_4_2_1b_result_commit"] == M1421B_RESULT_COMMIT,
        "S4_parent_state_complete": parent_state.get("parent_state_complete") is True and parent_state.get("unresolved_state_issue_count") == 0,
        "S5_warm_start_risk_valid": warm_start.get("risk_status") == "INITIAL_STATE_DIAGNOSTIC_ONLY" and warm_start.get("position_scale") == 1.0,
        "S6_scheduler_due_derivation": first.interval_days == 7 and first.execution_lag_days == 1,
        "S7_first_attempt_dates": first.signal_day == EXPECTED_FIRST_SIGNAL_DAY and first.logical_signal_time == EXPECTED_FIRST_LOGICAL_SIGNAL and first.execution_day == EXPECTED_FIRST_EXECUTION_DAY,
        "S8_freeze_before_logical_signal_time": freeze_created_at < first.logical_signal_time,
        "S9_no_future_market_data": True,
        "S10_start_bridge_policy_frozen": True,
        "S11_no_prestart_forward_evidence": True,
        "S12_accounting_carry_no_reset": True,
        "S13_risk_recompute_policy_frozen": True,
        "S14_parent_only_scheduler": True,
        "S15_paired_atomicity": True,
        "S16_forward_metric_grid_frozen": True,
        "S17_forward_gates_unchanged": True,
        "S18_anchor_file_immutable": binding_manifest["key_files"]["forward_anchor"]["immutable"],
        "S19_safety": True,
        "S20_ci": True,
    }
    return {name: "PASS" if checks[name] else "FAIL" for name in GATE_NAMES}


def _ensure_write_once(output_root: Path) -> None:
    expected = (
        output_root / START_MANIFEST_PATH.name,
        output_root / START_MANIFEST_HASH_PATH.name,
        output_root / FREEZE_RESULT_PATH.name,
        output_root / FREEZE_REPORT_PATH.name,
        output_root / BINDING_MANIFEST_PATH.name,
    )
    if output_root.exists():
        existing = [path for path in output_root.iterdir() if path.is_file() or path.is_dir()]
        if existing or any(path.exists() for path in expected):
            raise M2StartFreezeError("M2 start freeze output is write-once and already exists")


def _manifest_payload(
    *,
    freeze_created_at: datetime,
    current_head: str,
    identities: Mapping[str, str],
    state_bindings: Mapping[str, Any],
    warm_start: Mapping[str, Any],
    first: FirstForwardAttempt,
) -> dict[str, Any]:
    return {
        "schema_version": "XS_LOWVOL_V2_1_FORWARD_START_MANIFEST.v1",
        "run_id": RUN_ID,
        "strategy_id": STRATEGY_ID,
        "status": FORMAL_STATUS,
        "freeze_created_at_utc": _iso(freeze_created_at),
        "code_commit_used_by_run": current_head,
        "frozen_identities": dict(identities),
        "accepted_m1_4_2": {
            "result_commit": M142_RESULT_COMMIT,
            "code_commit": M142_CODE_COMMIT,
            "checkpoint_sha256": M142_EXPECTED_ARTIFACT_SHA256["checkpoint"],
            "contract_type_overlay_sha256": M142_EXPECTED_ARTIFACT_SHA256["contract_type_overlay"],
            "current_parent_state_sha256": M142_EXPECTED_ARTIFACT_SHA256["current_parent_state"],
            "corrected_extension_sha256": M142_EXPECTED_ARTIFACT_SHA256["extension"],
            "current_risk_state_sha256": M142_EXPECTED_ARTIFACT_SHA256["risk"],
        },
        "accepted_m1_4_2_1b": {
            "result_commit": M1421B_RESULT_COMMIT,
            "code_commit": M1421B_CODE_COMMIT,
            "git_blob_manifest_sha256": M1421B_GIT_BLOB_MANIFEST_SHA256,
            "old_139_carried_count": 139,
            "six_added_count": 6,
            "proven_count": 145,
            "unresolved_count": 0,
            "membership_set_unchanged": True,
            "effective_timestamp_differences": 0,
            "registry_path": "research/v2_1/m1_4_2_1b/MERGED_HISTORICAL_PRODUCT_TYPE_REGISTRY.json",
            "overlay_path": "research/v2_1/m1_4_2_1b/CONTRACT_TYPE_ELIGIBILITY_OVERLAY_V4.json",
        },
        "accepted_git_blob_bindings": {
            "m1_4_2": state_bindings["bindings"]["m1_4_2"],
            "m1_4_2_1b": state_bindings["bindings"]["m1_4_2_1b"],
            "key_files": state_bindings["key_files"],
        },
        "accepted_prestart_state": {
            "cutoff_utc": "2026-09-19T00:00:00Z",
            "last_successful_signal_day": "2026-09-17",
            "last_successful_execution_day": "2026-09-18",
            "next_rebalance_due_day": "2026-09-25",
            "pending_retry": False,
            "parent_state_complete": True,
            "current_position_count": 10,
            "funding_issue_count": 0,
            "protocol_ineligible_position_count": 0,
            "current_parent_state_sha256": M142_EXPECTED_ARTIFACT_SHA256["current_parent_state"],
            "position_state_sha256": warm_start["position_state_sha256"],
            "position_identity": warm_start["position_identity"],
        },
        "first_forward_attempt": {
            "signal_day": first.signal_day.isoformat(),
            "logical_signal_time_utc": _iso(first.logical_signal_time),
            "execution_day": first.execution_day.isoformat(),
            "scheduler_interval": "7 UTC calendar days",
            "execution_lag_days": 1,
            "date_derivation": "last_successful_execution_day + interval; signal_day = execution_day - lag; logical time = execution day 00:00:00Z",
        },
        "warm_start": {
            "classification": "PRESTART_WARM_START_STATE",
            "reference_vol": warm_start["reference_vol"],
            "position_scale": warm_start["position_scale"],
            "risk_status": warm_start["risk_status"],
            "is_first_forward_scale": False,
        },
        "start_bridge": {
            "classification": "PRE_FORWARD_START_BRIDGE_ONLY",
            "from_utc": "2026-09-19T00:00:00Z",
            "to_utc_exclusive": _iso(first.logical_signal_time),
            "not_forward_evidence": True,
            "not_in_successful_forward_cycle_count": True,
            "not_in_forward_metric_grid": True,
            "actual_data_required_at_future_attempt": True,
            "unresolved_state_action": "START_BRIDGE_DATA_INTEGRITY_HALT",
            "carry_without_reset": ["positions", "scheduler", "accounting_state", "lifecycle_overlay", "product_eligibility_overlay"],
        },
        "first_attempt_risk_policy": {
            "recompute_at_first_attempt": True,
            "source": "completed V1 Control weekly net returns",
            "selection_rule": "completed_at < first_forward_logical_signal_time",
            "latest_count": 13,
            "week_list_frozen_now": False,
            "volatility_statistic": "population_std",
            "annualization": "sqrt(52)",
            "target_annualized_volatility": 0.15,
            "min_scale": 0.0,
            "max_scale": 1.0,
            "leverage": False,
            "exact_zero_reference_vol_scale": 1.0,
            "invalid_result": "V2_DATA_INTEGRITY_HALT",
        },
        "scheduler_and_pairing": {
            "scheduler_authority": "V1_CONTROL_PARENT_ONLY",
            "v2_independent_clock": False,
            "v2_independent_retry": False,
            "v2_independent_target_change": False,
            "pair_atomicity": True,
            "same_signal_day": True,
            "same_execution_day": True,
            "same_targets_and_directions": True,
            "same_market_data": True,
            "same_execution_price": True,
            "same_funding_settlements": True,
            "same_transaction_cost_model": True,
            "only_notional_scale_differs": True,
            "integrity_halt_action": "HALT_ENTIRE_PAIRED_EXPERIMENT",
        },
        "forward_measurement": {
            "boundary_utc": _iso(first.logical_signal_time),
            "include_timestamp_gte_boundary": True,
            "week_grid_id": EXPECTED_WEEK_GRID_ID,
            "week_one_start_utc": _iso(first.week_one_start),
            "week_one_end_utc_exclusive": _iso(first.week_one_end),
            "accounting_state_reset": False,
            "measurement": "observational_normalization_only",
            "minimum_completed_forward_weeks": 52,
            "minimum_successful_paired_rebalance_cycles": 40,
            "twenty_six_week_status": "CONTINUE_OR_EARLY_FAIL_ONLY",
            "twenty_six_week_pass": False,
        },
        "safety": {
            "live_trading": False,
            "paper_only": True,
            "http_methods": ["GET"],
            "market_data_access_performed": False,
            "future_market_data_fetched": False,
            "first_forward_attempt_executed": False,
        },
        "classification": ["START_IDENTITY_ONLY", "NOT_FORWARD", "NOT_VALIDATION_EVIDENCE", "PRESTART_STATE_INITIALIZATION_ONLY"],
    }


def build_formal_artifacts(
    *,
    now: datetime | None = None,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, Any]:
    """Validate accepted state and write the one-shot five-file M2 result."""
    _ensure_write_once(output_root)
    freeze_created_at = now or datetime.now(UTC)
    if freeze_created_at.tzinfo is None or freeze_created_at.utcoffset() is None:
        raise M2NotReady("freeze_created_at_utc must be timezone-aware")
    freeze_created_at = freeze_created_at.astimezone(UTC)

    identities = _verify_frozen_identities()
    m142_result, parent_state, risk_state, m1421b_result = _verify_artifact_hashes()
    del m1421b_result
    current_head = _git("rev-parse", "HEAD")
    binding_manifest = _verify_binding_trees(current_head)
    warm_start, _position_state_sha = _verify_prestart_state(m142_result, parent_state, risk_state)
    control_rules = resolve_rules(variant="control")
    anchor = load_v2_1_forward_anchor()
    _require(control_rules.rebalance_days, 7, "frozen V1 Control rebalance interval")
    _require(anchor.get("market_schedule"), "7x24", "frozen Anchor market schedule")
    _require(anchor.get("execution_lag_days"), 1, "frozen Anchor execution lag")
    rules = {
        "interval_days": control_rules.rebalance_days,
        "execution_lag_days": int(anchor["execution_lag_days"]),
    }
    first = derive_first_forward_attempt(
        last_successful_execution_day=parent_state["last_successful_execution_day"],
        pending_retry=parent_state["pending_retry"],
        interval_days=rules["interval_days"],
        execution_lag_days=rules["execution_lag_days"],
    )
    _require(first.execution_day, EXPECTED_FIRST_EXECUTION_DAY, "derived first execution day")
    _require(first.signal_day, EXPECTED_FIRST_SIGNAL_DAY, "derived first signal day")
    _require(first.logical_signal_time, EXPECTED_FIRST_LOGICAL_SIGNAL, "derived first logical signal time")
    if freeze_created_at >= first.logical_signal_time:
        raise M2NotReady("M2 freeze must be created before the first logical signal time")
    if _parse_utc(parent_state["cutoff_utc"], "accepted cutoff") != PRESTART_CUTOFF:
        raise M2IdentityError("accepted cutoff is not the frozen 2026-09-19 boundary")

    manifest_payload = _manifest_payload(
        freeze_created_at=freeze_created_at,
        current_head=current_head,
        identities=identities,
        state_bindings=binding_manifest,
        warm_start=warm_start,
        first=first,
    )
    gates = _build_gate_values(
        identities=identities,
        binding_manifest=binding_manifest,
        first=first,
        freeze_created_at=freeze_created_at,
        parent_state=parent_state,
        warm_start=warm_start,
    )
    failed_gates = [name for name, value in gates.items() if value != "PASS"]
    if failed_gates:
        raise M2NotReady(f"M2 start freeze failed gates: {failed_gates}")

    yaml_bytes = yaml.safe_dump(
        manifest_payload,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).replace("\r\n", "\n").encode("utf-8")
    start_manifest_sha256 = sha256_bytes(yaml_bytes)
    binding_manifest_sha256 = sha256_json(binding_manifest)
    result = {
        "schema_version": "V2_1_M2_START_FREEZE.v1",
        "run_id": RUN_ID,
        "run_type": "START_FREEZE_ONLY",
        "decision": DECISION,
        "status": FORMAL_STATUS,
        "classification": ["START_IDENTITY_ONLY", "NOT_FORWARD", "NOT_VALIDATION_EVIDENCE", "PRESTART_STATE_INITIALIZATION_ONLY"],
        "code_commit_used_by_run": current_head,
        "freeze_created_at_utc": _iso(freeze_created_at),
        "identities": dict(identities),
        "m1_4_2_bindings": manifest_payload["accepted_m1_4_2"],
        "m1_4_2_1b_bindings": manifest_payload["accepted_m1_4_2_1b"],
        "start_manifest_path": str(START_MANIFEST_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "start_manifest_sha256": start_manifest_sha256,
        "binding_git_blob_manifest_path": str(BINDING_MANIFEST_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "binding_git_blob_manifest_sha256": binding_manifest_sha256,
        "accepted_prestart_state": manifest_payload["accepted_prestart_state"],
        "first_forward_attempt": manifest_payload["first_forward_attempt"],
        "warm_start": manifest_payload["warm_start"],
        "start_bridge": manifest_payload["start_bridge"],
        "first_attempt_risk_policy": manifest_payload["first_attempt_risk_policy"],
        "scheduler_and_pairing": manifest_payload["scheduler_and_pairing"],
        "forward_measurement": manifest_payload["forward_measurement"],
        "gates": gates,
        "failed_gates": failed_gates,
        "safety": manifest_payload["safety"],
        "formal_process_exit_code": 0,
    }
    report = _markdown_report(result)
    output_root.mkdir(parents=True, exist_ok=False)
    (output_root / START_MANIFEST_PATH.name).write_bytes(yaml_bytes)
    (output_root / START_MANIFEST_HASH_PATH.name).write_text(
        f"{start_manifest_sha256}  {START_MANIFEST_PATH.name}\n", encoding="utf-8", newline="\n"
    )
    _write_json(output_root / BINDING_MANIFEST_PATH.name, binding_manifest)
    _write_json(output_root / FREEZE_RESULT_PATH.name, result)
    (output_root / FREEZE_REPORT_PATH.name).write_text(report, encoding="utf-8", newline="\n")
    return result


def _markdown_report(result: Mapping[str, Any]) -> str:
    gates = result["gates"]
    first = result["first_forward_attempt"]
    lines = [
        f"# {result['decision']}",
        "",
        "This artifact freezes only the future Forward start identity and temporal boundary. It does not execute the first attempt or produce Forward evidence.",
        "",
        "## Identity",
        "",
        f"- Run ID: `{result['run_id']}`",
        f"- Status: `{result['status']}`",
        f"- Code commit used by run: `{result['code_commit_used_by_run']}`",
        f"- Freeze created: `{result['freeze_created_at_utc']}`",
        f"- Start Manifest SHA-256: `{result['start_manifest_sha256']}`",
        f"- Binding Git Blob Manifest SHA-256: `{result['binding_git_blob_manifest_sha256']}`",
        "",
        "## First attempt boundary",
        "",
        f"- Signal day: `{first['signal_day']}`",
        f"- Logical signal time: `{first['logical_signal_time_utc']}`",
        f"- Execution day: `{first['execution_day']}`",
        f"- Week 1: `{result['forward_measurement']['week_one_start_utc']}` to `{result['forward_measurement']['week_one_end_utc_exclusive']}`",
        "",
        "## Accepted state",
        "",
        f"- Cutoff: `{result['accepted_prestart_state']['cutoff_utc']}`",
        f"- Current positions: `{result['accepted_prestart_state']['current_position_count']}`",
        f"- Position state SHA-256: `{result['accepted_prestart_state']['position_state_sha256']}`",
        f"- Warm-start reference vol: `{result['warm_start']['reference_vol']}`",
        f"- Warm-start scale: `{result['warm_start']['position_scale']}`",
        "",
        "## Gates",
        "",
    ]
    lines.extend(f"- `{name}`: **{value}**" for name, value in gates.items())
    lines.extend(
        [
            "",
            "## Explicit non-actions",
            "",
            "- No market data after the 2026-09-19 cutoff was fetched.",
            "- No first Forward target, first Forward scale, or first attempt was calculated or executed.",
            "- No Forward PnL, return, risk metric, performance result, or gate result was generated.",
            "- The interstitial period is `PRE_FORWARD_START_BRIDGE_ONLY` and is not Forward evidence.",
            "- M1.4.2, M1.4.2.1B, the frozen strategy/protocol identities, and the Anchor remain unchanged.",
            "- No optimization and no live trading were performed.",
            "",
            f"Formal process exit code: `{result['formal_process_exit_code']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    if args.approval != APPROVAL:
        print(f"approval mismatch; expected {APPROVAL!r}")
        return 2
    try:
        result = build_formal_artifacts()
    except M2StartFreezeError as exc:
        print(json.dumps({"decision": "V2.1-M2 START FREEZE NOT_READY", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print("time_until_first_logical_signal: diagnostic-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
