"""Frozen identity and temporal anchor for the V2 Forward phase.

This module registers the Forward epoch only.  It intentionally contains no
market-data loader, observation writer, PnL calculation, or performance gate.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from backtest.v2_protocol import (
    V2_PROTOCOL_SHA256,
    verify_v2_protocol_hash,
)
from .xs_lowvol_v2_spec import (
    PARENT_CONTROL_SHA256,
    V2_SPEC_SHA256,
    verify_v2_spec_hash,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FORWARD_ANCHOR_PATH = PROJECT_ROOT / "research" / "v2" / "XS_LOWVOL_V2_FORWARD_ANCHOR.yaml"
FORWARD_ANCHOR_HASH_PATH = (
    PROJECT_ROOT / "research" / "v2" / "XS_LOWVOL_V2_FORWARD_ANCHOR.sha256"
)

FORWARD_ANCHOR_ID = "XS-LOWVOL-V2-FORWARD-ANCHOR-V1"
FORWARD_ANCHOR_STATUS = "FROZEN_NOT_YET_STARTED"
APPROVED_V2_M0_COMMIT = "5b5c81529c92256846a16f98f1227a7af782370a"
APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC = datetime(
    2026, 9, 16, 2, 6, 55, tzinfo=timezone.utc
)
APPROVED_V2_M0_COMMIT_TIMESTAMP_TEXT = "2026-09-16T02:06:55Z"
APPROVED_V1_CONTROL_SHA256 = PARENT_CONTROL_SHA256
APPROVED_V2_SPEC_SHA256 = V2_SPEC_SHA256
APPROVED_V2_PROTOCOL_SHA256 = V2_PROTOCOL_SHA256

# Filled from the canonical YAML payload and sidecar after the anchor file is
# added.  The source pin is checked together with both on-disk representations.
APPROVED_V2_FORWARD_ANCHOR_SHA256 = "e4659797089bc940f7ade73d3d19cae23b79a8fa0d9b4b5042eb71ec7d05e569"
V2_FORWARD_ANCHOR_SHA256 = APPROVED_V2_FORWARD_ANCHOR_SHA256

_ANCHOR_FIELDS = frozenset(
    {
        "anchor_id",
        "status",
        "approved_v2_m0_commit",
        "approved_v2_m0_commit_timestamp_utc",
        "v1_control_sha256",
        "v2_spec_sha256",
        "v2_protocol_sha256",
        "time_basis",
        "market_day_basis",
        "forward_evidence_before_anchor_acceptance",
        "historical_backfill_into_forward",
    }
)


class V2ForwardAnchorError(ValueError):
    """A Forward anchor or its frozen identity is invalid."""

    code = "FORWARD_ANCHOR_INVALID"

    def __init__(self, reason: str):
        super().__init__(f"{self.code}: {reason}")
        self.reason = reason


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
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


def canonical_forward_anchor_json(anchor: Mapping[str, Any]) -> str:
    """Return the key-sorted canonical JSON representation of the anchor."""
    return json.dumps(
        _canonical(anchor), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _load_yaml(path: str | Path = FORWARD_ANCHOR_PATH) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise V2ForwardAnchorError(f"cannot read Forward anchor: {path}") from exc
    if not isinstance(raw, dict):
        raise V2ForwardAnchorError("Forward anchor top level must be a mapping")
    return raw


def load_v2_forward_anchor(path: str | Path = FORWARD_ANCHOR_PATH) -> dict[str, Any]:
    """Load the registered Forward anchor without applying defaults."""
    return _load_yaml(path)


def forward_anchor_sha256(
    anchor_or_path: Mapping[str, Any] | str | Path = FORWARD_ANCHOR_PATH,
) -> str:
    """Hash a parsed Forward anchor or its YAML path."""
    anchor = (
        _load_yaml(anchor_or_path)
        if isinstance(anchor_or_path, (str, Path))
        else anchor_or_path
    )
    if not isinstance(anchor, Mapping):
        raise V2ForwardAnchorError("Forward anchor must be a mapping")
    return hashlib.sha256(
        canonical_forward_anchor_json(anchor).encode("utf-8")
    ).hexdigest()


def read_v2_forward_anchor_hash(
    path: str | Path = FORWARD_ANCHOR_HASH_PATH,
) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip().split()[0]
    except (IndexError, OSError) as exc:
        raise V2ForwardAnchorError(
            f"missing Forward anchor hash sidecar: {path}"
        ) from exc
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise V2ForwardAnchorError("Forward anchor sidecar is not a lowercase SHA-256")
    return token


def verify_v2_forward_anchor_hash(
    path: str | Path = FORWARD_ANCHOR_PATH,
    hash_path: str | Path = FORWARD_ANCHOR_HASH_PATH,
    *,
    approved_hash: str = APPROVED_V2_FORWARD_ANCHOR_SHA256,
) -> str:
    """Require YAML, sidecar, and source-pinned Forward-anchor hashes to agree."""
    actual = forward_anchor_sha256(path)
    recorded = read_v2_forward_anchor_hash(hash_path)
    if actual != recorded or actual != approved_hash:
        raise V2ForwardAnchorError(
            "Forward anchor hash mismatch: "
            f"approved={approved_hash}, recorded={recorded}, actual={actual}"
        )
    return actual


def _require(anchor: Mapping[str, Any], field_name: str, expected: Any) -> None:
    actual = anchor.get(field_name)
    if actual != expected:
        raise V2ForwardAnchorError(
            f"{field_name} must be frozen as {expected!r}, got {actual!r}"
        )


def validate_v2_forward_anchor(
    anchor: Mapping[str, Any] | None = None,
    *,
    require_approved_hash: bool = True,
    require_current_identities: bool = True,
) -> None:
    """Validate the complete independently pinned Forward epoch identity."""
    source = load_v2_forward_anchor() if anchor is None else dict(anchor)
    if set(source) != _ANCHOR_FIELDS:
        raise V2ForwardAnchorError("Forward anchor fields are not exactly frozen")

    actual = forward_anchor_sha256(source)
    if anchor is None:
        recorded = read_v2_forward_anchor_hash()
        if recorded != actual:
            raise V2ForwardAnchorError(
                f"Forward anchor sidecar mismatch: recorded={recorded}, actual={actual}"
            )
    if require_approved_hash and actual != APPROVED_V2_FORWARD_ANCHOR_SHA256:
        raise V2ForwardAnchorError(
            "Forward anchor is not pinned to the approved SHA-256: "
            f"approved={APPROVED_V2_FORWARD_ANCHOR_SHA256}, actual={actual}"
        )

    _require(source, "anchor_id", FORWARD_ANCHOR_ID)
    _require(source, "status", FORWARD_ANCHOR_STATUS)
    _require(source, "approved_v2_m0_commit", APPROVED_V2_M0_COMMIT)
    _require(
        source,
        "approved_v2_m0_commit_timestamp_utc",
        APPROVED_V2_M0_COMMIT_TIMESTAMP_TEXT,
    )
    _require(source, "v1_control_sha256", APPROVED_V1_CONTROL_SHA256)
    _require(source, "v2_spec_sha256", APPROVED_V2_SPEC_SHA256)
    _require(source, "v2_protocol_sha256", APPROVED_V2_PROTOCOL_SHA256)
    _require(source, "time_basis", "UTC")
    _require(source, "market_day_basis", "UTC_CALENDAR_DAY")
    _require(source, "forward_evidence_before_anchor_acceptance", "FORBIDDEN")
    _require(source, "historical_backfill_into_forward", "FORBIDDEN")

    if require_current_identities:
        if verify_v2_spec_hash() != APPROVED_V2_SPEC_SHA256:
            raise V2ForwardAnchorError("current V2 spec identity is not approved")
        if verify_v2_protocol_hash() != APPROVED_V2_PROTOCOL_SHA256:
            raise V2ForwardAnchorError("current V2 protocol identity is not approved")


def v2_forward_anchor_identity() -> dict[str, str]:
    """Return the validated immutable identity tuple used by ForwardEpoch."""
    validate_v2_forward_anchor()
    return {
        "anchor_id": FORWARD_ANCHOR_ID,
        "anchor_sha256": verify_v2_forward_anchor_hash(),
        "approved_v2_m0_commit": APPROVED_V2_M0_COMMIT,
        "v1_control_sha256": APPROVED_V1_CONTROL_SHA256,
        "v2_spec_sha256": APPROVED_V2_SPEC_SHA256,
        "v2_protocol_sha256": APPROVED_V2_PROTOCOL_SHA256,
    }


__all__ = [
    "APPROVED_V1_CONTROL_SHA256",
    "APPROVED_V2_FORWARD_ANCHOR_SHA256",
    "APPROVED_V2_M0_COMMIT",
    "APPROVED_V2_M0_COMMIT_TIMESTAMP_UTC",
    "APPROVED_V2_M0_COMMIT_TIMESTAMP_TEXT",
    "APPROVED_V2_PROTOCOL_SHA256",
    "APPROVED_V2_SPEC_SHA256",
    "FORWARD_ANCHOR_HASH_PATH",
    "FORWARD_ANCHOR_ID",
    "FORWARD_ANCHOR_PATH",
    "FORWARD_ANCHOR_STATUS",
    "V2_FORWARD_ANCHOR_SHA256",
    "V2ForwardAnchorError",
    "canonical_forward_anchor_json",
    "forward_anchor_sha256",
    "load_v2_forward_anchor",
    "read_v2_forward_anchor_hash",
    "v2_forward_anchor_identity",
    "validate_v2_forward_anchor",
    "verify_v2_forward_anchor_hash",
]
