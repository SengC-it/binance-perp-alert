"""M1-A preflight and M1-B runner boundary.

The formal historical run is intentionally not implemented in this phase.  A
future M1-B invocation must receive the separately approved instruction and a
frozen normalized dataset before it can execute any result-producing code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .m1_protocol import (
    M1_PROTOCOL_HASH_PATH,
    M1_PROTOCOL_PATH,
    load_protocol,
    validate_protocol,
    verified_protocol_identity,
)


class M1BApprovalRequired(RuntimeError):
    """Formal M1-B execution requires an explicit later approval."""


class M1RunnerNotReady(RuntimeError):
    """The M1-A runner skeleton has no formal result-producing implementation."""


@dataclass(frozen=True)
class M1Preflight:
    protocol_id: str
    protocol_hash: str
    control_strategy_id: str
    control_spec_hash: str
    shadow_strategy_id: str
    shadow_spec_hash: str
    data_window: tuple[str, str]
    external_window: tuple[str, str]
    discovery_window: tuple[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_id": self.protocol_id,
            "protocol_hash": self.protocol_hash,
            "control_strategy_id": self.control_strategy_id,
            "control_spec_hash": self.control_spec_hash,
            "shadow_strategy_id": self.shadow_strategy_id,
            "shadow_spec_hash": self.shadow_spec_hash,
            "data_window": self.data_window,
            "external_window": self.external_window,
            "discovery_window": self.discovery_window,
            "formal_run": False,
        }


def preflight(
    protocol_path: str | Path = M1_PROTOCOL_PATH,
    hash_path: str | Path = M1_PROTOCOL_HASH_PATH,
) -> M1Preflight:
    """Verify protocol and strategy identity without reading a dataset."""
    protocol = load_protocol(protocol_path)
    validate_protocol(protocol)
    identity = verified_protocol_identity(protocol_path, hash_path)
    data_window = protocol["data_window"]
    external = protocol["primary_external_validation"]
    discovery = protocol["discovery_contaminated_reference"]
    return M1Preflight(
        protocol_id=identity["protocol_id"],
        protocol_hash=identity["protocol_hash"],
        control_strategy_id=identity["control_strategy_id"],
        control_spec_hash=identity["control_spec_hash"],
        shadow_strategy_id=identity["shadow_strategy_id"],
        shadow_spec_hash=identity["shadow_spec_hash"],
        data_window=(str(data_window["start"]), str(data_window["end"])),
        external_window=(str(external["start"]), str(external["end"])),
        discovery_window=(str(discovery["start"]), str(discovery["end"])),
    )


def run_m1(
    *,
    protocol_path: str | Path = M1_PROTOCOL_PATH,
    hash_path: str | Path = M1_PROTOCOL_HASH_PATH,
    dataset_manifest: Mapping[str, Any] | None = None,
    approval: str | None = None,
) -> M1Preflight:
    """Runner skeleton: M1-A always stops before a formal historical run."""
    result = preflight(protocol_path, hash_path)
    if approval != "START M1-B":
        raise M1BApprovalRequired(
            "M1-A only: formal M1-B historical download/backtest requires explicit 'START M1-B'"
        )
    if dataset_manifest is None:
        raise M1RunnerNotReady("M1-B dataset manifest 尚未提供")
    raise M1RunnerNotReady(
        "M1-A runner skeleton 已完成；正式 M1-B execution 尚未在本阶段实现"
    )
