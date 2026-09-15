"""M1-A pre-registration protocol and immutable gate configuration.

The protocol is deliberately separate from the V1 strategy spec.  It fixes the
historical windows, data rules, stress scenarios and decision gates before any
formal M1-B data download or backtest is allowed.  The sidecar hash is the
SHA-256 of the canonical JSON representation of the parsed YAML document.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.xs_lowvol_spec import (
    CONTROL_SPEC_HASH,
    CONTROL_STRATEGY_ID,
    SHADOW_SPEC_HASH,
    SHADOW_STRATEGY_ID,
    strategy_spec_hash,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
M1_PROTOCOL_PATH = PROJECT_ROOT / "research" / "m1" / "XS_LOWVOL_M1_PROTOCOL.yaml"
M1_PROTOCOL_HASH_PATH = PROJECT_ROOT / "research" / "m1" / "XS_LOWVOL_M1_PROTOCOL.sha256"
M1_PROTOCOL_ID = "XS-LOWVOL-M1-PIT-V1"
M1_BASE_COMMIT = "961d2e60ee34d032bed54466d309724d916b731f"
M1_DATA_START = date(2020, 1, 1)
M1_DATA_END = date(2026, 8, 31)
M1_EXTERNAL_START = date(2020, 1, 1)
M1_EXTERNAL_END = date(2025, 8, 31)
M1_DISCOVERY_START = date(2025, 9, 1)
M1_DISCOVERY_END = date(2026, 8, 31)
M1_COST_RATES = {
    "COST_1X": (0.05, 0.03),
    "COST_2X": (0.10, 0.06),
    "COST_3X": (0.15, 0.09),
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ProtocolError(ValueError):
    """The pre-registered M1 protocol is missing or inconsistent."""


@dataclass(frozen=True)
class InclusiveWindow:
    """An inclusive UTC calendar-date window."""

    name: str
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ProtocolError(f"{self.name} window end precedes start")

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end


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


def canonical_protocol_json(protocol: Mapping[str, Any]) -> str:
    """Return canonical, key-sorted JSON for protocol hashing."""
    return json.dumps(
        _canonical(protocol), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def load_protocol(path: str | Path = M1_PROTOCOL_PATH) -> dict[str, Any]:
    """Load the YAML protocol without applying any result-dependent defaults."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProtocolError(f"无法读取 M1 protocol: {path}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("M1 protocol 顶层必须是 mapping")
    return raw


def protocol_sha256(protocol_or_path: Mapping[str, Any] | str | Path = M1_PROTOCOL_PATH) -> str:
    """Hash a parsed protocol or a protocol YAML path."""
    protocol = (
        load_protocol(protocol_or_path)
        if isinstance(protocol_or_path, (str, Path))
        else protocol_or_path
    )
    return hashlib.sha256(canonical_protocol_json(protocol).encode("utf-8")).hexdigest()


def read_protocol_hash(path: str | Path = M1_PROTOCOL_HASH_PATH) -> str:
    """Read a hash-only sidecar; accept ``hash`` or ``hash filename`` format."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProtocolError(f"缺少 M1 protocol hash sidecar: {path}") from exc
    token = text.split()[0] if text else ""
    if not _HEX64.fullmatch(token):
        raise ProtocolError("M1 protocol hash sidecar 不是 64 位小写 SHA-256")
    return token


def verify_protocol_hash(
    protocol_path: str | Path = M1_PROTOCOL_PATH,
    hash_path: str | Path = M1_PROTOCOL_HASH_PATH,
) -> str:
    """Verify the YAML and sidecar hash and return the verified digest."""
    actual = protocol_sha256(protocol_path)
    recorded = read_protocol_hash(hash_path)
    if actual != recorded:
        raise ProtocolError(
            f"M1 protocol hash mismatch: recorded={recorded}, actual={actual}"
        )
    return actual


def _date_field(mapping: Mapping[str, Any], key: str, context: str) -> date:
    value = mapping.get(key)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{context}.{key} 不是合法日期") from exc


def protocol_windows(protocol: Mapping[str, Any]) -> tuple[InclusiveWindow, InclusiveWindow]:
    external_raw = protocol.get("primary_external_validation")
    discovery_raw = protocol.get("discovery_contaminated_reference")
    if not isinstance(external_raw, Mapping) or not isinstance(discovery_raw, Mapping):
        raise ProtocolError("缺少 external/discovery window")
    external = InclusiveWindow(
        str(external_raw.get("name", "EXTERNAL_VALIDATION")),
        _date_field(external_raw, "start", "primary_external_validation"),
        _date_field(external_raw, "end", "primary_external_validation"),
    )
    discovery = InclusiveWindow(
        str(discovery_raw.get("name", "DISCOVERY_REFERENCE")),
        _date_field(discovery_raw, "start", "discovery_contaminated_reference"),
        _date_field(discovery_raw, "end", "discovery_contaminated_reference"),
    )
    if discovery.start <= external.end:
        raise ProtocolError("EXTERNAL_VALIDATION 与 DISCOVERY_REFERENCE 重叠")
    if (discovery.start - external.end).days != 1:
        raise ProtocolError("external/discovery window 必须无空洞且相邻")
    return external, discovery


def cost_rate(protocol: Mapping[str, Any], scenario: str) -> float:
    """Return the frozen per-side actual-turnover cost as a decimal."""
    scenarios = protocol.get("cost_scenarios")
    raw = scenarios.get(scenario) if isinstance(scenarios, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ProtocolError(f"缺少预注册 cost scenario: {scenario}")
    try:
        taker = float(raw["taker_pct_per_side"])
        slippage = float(raw["slippage_pct_per_side"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ProtocolError(f"cost scenario {scenario} 参数非法") from exc
    return (taker + slippage) / 100.0


def validate_protocol(
    protocol: Mapping[str, Any] | None = None,
    *,
    require_current_specs: bool = True,
) -> None:
    """Validate all M1-A invariants without downloading or running a backtest."""
    protocol = protocol or load_protocol()
    if protocol.get("protocol_id") != M1_PROTOCOL_ID:
        raise ProtocolError("protocol_id 不是 XS-LOWVOL-M1-PIT-V1")
    if protocol.get("base_commit") != M1_BASE_COMMIT:
        raise ProtocolError("base_commit 不匹配 M0 accepted commit")

    control = protocol.get("control")
    shadow = protocol.get("shadow")
    if not isinstance(control, Mapping) or not isinstance(shadow, Mapping):
        raise ProtocolError("缺少 control/shadow identity")
    if control.get("strategy_id") != CONTROL_STRATEGY_ID:
        raise ProtocolError("Control strategy_id 不匹配")
    if control.get("spec_hash") != CONTROL_SPEC_HASH:
        raise ProtocolError("Control spec hash 不匹配")
    if shadow.get("strategy_id") != SHADOW_STRATEGY_ID:
        raise ProtocolError("Shadow strategy_id 不匹配")
    if shadow.get("spec_hash") != SHADOW_SPEC_HASH:
        raise ProtocolError("Shadow spec hash 不匹配")
    if require_current_specs:
        if strategy_spec_hash() != CONTROL_SPEC_HASH:
            raise ProtocolError("当前 Control spec hash 已变化")
        if strategy_spec_hash(variant="shadow") != SHADOW_SPEC_HASH:
            raise ProtocolError("当前 Shadow spec hash 已变化")

    data_window = protocol.get("data_window")
    if not isinstance(data_window, Mapping):
        raise ProtocolError("缺少 data_window")
    if protocol.get("data_start") != M1_DATA_START or protocol.get("data_end") != M1_DATA_END:
        raise ProtocolError("顶层 data_start/data_end 不匹配")
    if _date_field(data_window, "start", "data_window") != M1_DATA_START:
        raise ProtocolError("data_window.start 不匹配")
    if _date_field(data_window, "end", "data_window") != M1_DATA_END:
        raise ProtocolError("data_window.end 不匹配")
    external, discovery = protocol_windows(protocol)
    if (external.start, external.end) != (M1_EXTERNAL_START, M1_EXTERNAL_END):
        raise ProtocolError("EXTERNAL_VALIDATION window 不匹配")
    if (discovery.start, discovery.end) != (M1_DISCOVERY_START, M1_DISCOVERY_END):
        raise ProtocolError("DISCOVERY_REFERENCE window 不匹配")
    if discovery_raw := protocol.get("discovery_contaminated_reference"):
        if discovery_raw.get("included_in_primary_gate") is not False:
            raise ProtocolError("DISCOVERY_REFERENCE 不得进入 primary gate")

    universe = protocol.get("point_in_time_universe")
    if not isinstance(universe, Mapping):
        raise ProtocolError("缺少 point_in_time_universe")
    liquidity = universe.get("liquidity")
    history = universe.get("history")
    selection = universe.get("selection")
    if not isinstance(liquidity, Mapping) or liquidity.get("minimum_quote_volume_usdt") != 50_000_000:
        raise ProtocolError("PIT liquidity threshold 不匹配")
    if not isinstance(history, Mapping) or history.get("lookback_returns") != 30:
        raise ProtocolError("PIT lookback 不匹配")
    if not isinstance(selection, Mapping):
        raise ProtocolError("缺少 PIT selection")
    if selection.get("k_long") != 5 or selection.get("k_short") != 5:
        raise ProtocolError("PIT k_long/k_short 不匹配")

    scenarios = protocol.get("cost_scenarios")
    if not isinstance(scenarios, Mapping) or set(scenarios) != set(M1_COST_RATES):
        raise ProtocolError("cost_scenarios 必须完整包含 COST_1X/2X/3X")
    for scenario, (taker, slippage) in M1_COST_RATES.items():
        raw = scenarios[scenario]
        if not isinstance(raw, Mapping):
            raise ProtocolError(f"cost scenario {scenario} 非 mapping")
        if raw.get("taker_pct_per_side") != taker or raw.get("slippage_pct_per_side") != slippage:
            raise ProtocolError(f"cost scenario {scenario} 不匹配预注册值")

    gates = protocol.get("hard_gates")
    required_gates = {f"G{index}_{name}" for index, name in enumerate(
        (
            "data_integrity",
            "external_return",
            "cost_2x",
            "weekly_sharpe",
            "drawdown",
            "profit_factor",
            "block_bootstrap",
            "best_5pct_removed",
            "leave_one_out",
            "symbol_concentration",
            "multi_year_breadth",
            "single_year_catastrophe",
            "bull_survival",
        )
    )}
    if not isinstance(gates, Mapping) or set(gates) != required_gates:
        raise ProtocolError("M1 hard_gates 不完整或包含未预注册 gate")

    m1_controls = protocol.get("m1_a_controls")
    if not isinstance(m1_controls, Mapping):
        raise ProtocolError("缺少 M1-A controls")
    for key, value in m1_controls.items():
        if value != "forbidden":
            raise ProtocolError(f"M1-A control {key} 必须为 forbidden")


def verified_protocol_identity(
    protocol_path: str | Path = M1_PROTOCOL_PATH,
    hash_path: str | Path = M1_PROTOCOL_HASH_PATH,
) -> dict[str, str]:
    """Return the verified protocol and frozen strategy identities."""
    protocol = load_protocol(protocol_path)
    validate_protocol(protocol)
    protocol_hash = verify_protocol_hash(protocol_path, hash_path)
    return {
        "protocol_id": str(protocol["protocol_id"]),
        "protocol_hash": protocol_hash,
        "control_strategy_id": CONTROL_STRATEGY_ID,
        "control_spec_hash": CONTROL_SPEC_HASH,
        "shadow_strategy_id": SHADOW_STRATEGY_ID,
        "shadow_spec_hash": SHADOW_SPEC_HASH,
    }
