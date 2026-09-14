"""冻结的 XS-LOWVOL-V1 规则与可复现的 spec hash。

规则只在 ``research/xs_lowvol_v1.yaml`` 中声明一次。运行时代码从这里读取
解析后的值，避免回测、扫描和 paper ledger 各自维护一套容易漂移的默认值。
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

SPEC_PATH = Path(__file__).resolve().parent.parent / "research" / "xs_lowvol_v1.yaml"

BASE_STRATEGY_ID = "XS-LOWVOL-V1"
CONTROL_STRATEGY_ID = "XS-LOWVOL-V1-Control"
SHADOW_STRATEGY_ID = "XS-LOWVOL-V1-VT80-Shadow"
CONTROL_VARIANT = "control"
SHADOW_VARIANT = "vt80-shadow"
StrategyVariant = Literal["control", "shadow", "vt80-shadow"]


@dataclass(frozen=True)
class XsRuleSet:
    """Resolved rules used by all three execution paths."""

    strategy_id: str
    variant: str
    spec_hash: str
    lookback_days: int
    k_long: int
    k_short: int
    rebalance_days: int
    min_quote_volume_usdt: float
    min_symbols: int
    execution_lag_days: int
    taker_fee_pct: float
    slippage_pct: float
    target_vol_pct: float
    max_scale: float
    volatility_targeting: bool

    @property
    def total_slots(self) -> int:
        return self.k_long + self.k_short

    @property
    def cost_rate(self) -> float:
        """One portfolio-side cost rate as a decimal."""
        return (self.taker_fee_pct + self.slippage_pct) / 100.0


def _load_yaml(path: str | Path = SPEC_PATH) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("XS-LOWVOL spec 顶层必须是 mapping")
    return raw


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def canonical_spec_json(spec: dict[str, Any]) -> str:
    """返回与 YAML 空白和键顺序无关的 canonical JSON。"""
    return json.dumps(
        _canonical(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def spec_sha256(spec: dict[str, Any]) -> str:
    """对已解析 spec 计算 SHA-256。"""
    return hashlib.sha256(canonical_spec_json(spec).encode("utf-8")).hexdigest()


def _resolved_spec(path: str | Path = SPEC_PATH, variant: StrategyVariant = "control") -> dict[str, Any]:
    raw = _load_yaml(path)
    if variant in ("shadow", "vt80-shadow"):
        shadow = raw.get("shadow")
        if not isinstance(shadow, dict):
            raise ValueError("XS-LOWVOL spec 缺少 shadow 定义")
        base = deepcopy(raw)
        base.pop("shadow", None)
        override = deepcopy(shadow)
        override.pop("inherits", None)
        merged = _deep_merge(base, override)
        merged["strategy_id"] = SHADOW_STRATEGY_ID
        merged["status"] = "shadow"
        return merged
    raw.pop("shadow", None)
    return raw


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def load_strategy_spec(
    path: str | Path = SPEC_PATH, variant: StrategyVariant = "control"
) -> dict[str, Any]:
    """读取一个冻结版本；返回副本，调用方不能修改缓存。"""
    return deepcopy(_resolved_spec(path, variant))


def strategy_spec_hash(
    path: str | Path = SPEC_PATH, variant: StrategyVariant = "control"
) -> str:
    return spec_sha256(_resolved_spec(path, variant))


def effective_strategy_id(variant: StrategyVariant = "control") -> str:
    return SHADOW_STRATEGY_ID if variant in ("shadow", "vt80-shadow") else CONTROL_STRATEGY_ID


def resolve_rules(
    path: str | Path = SPEC_PATH, variant: StrategyVariant = "control"
) -> XsRuleSet:
    spec = _resolved_spec(path, variant)
    universe = spec["universe"]
    signal = spec["signal"]
    ranking = spec["ranking"]
    rebalance = spec["rebalance"]
    execution = spec["execution"]
    fees = spec["fees"]
    slippage = spec["slippage"]
    sizing = spec["position_sizing"]
    volatility_targeting = sizing.get("volatility_targeting") == "enabled"
    return XsRuleSet(
        strategy_id=effective_strategy_id(variant),
        variant=SHADOW_VARIANT if volatility_targeting else CONTROL_VARIANT,
        spec_hash=spec_sha256(spec),
        lookback_days=int(signal["realized_volatility"]["lookback_days"]),
        k_long=int(ranking["k_long"]),
        k_short=int(ranking["k_short"]),
        rebalance_days=int(rebalance["interval_days"]),
        min_quote_volume_usdt=float(universe["min_quote_volume_24h_usdt"]),
        min_symbols=int(universe["min_symbols_for_signal"]),
        execution_lag_days=int(execution["execution_lag_days"]),
        taker_fee_pct=float(fees["taker_pct_per_side"]),
        slippage_pct=float(slippage["pct_per_side"]),
        target_vol_pct=float(sizing.get("target_vol_pct", 0.0)),
        max_scale=float(sizing.get("max_scale", 1.0)),
        volatility_targeting=volatility_targeting,
    )


def validate_frozen_spec(path: str | Path = SPEC_PATH) -> None:
    """校验 M0 不可变的研究参数。"""
    control = resolve_rules(path, "control")
    shadow = resolve_rules(path, "shadow")
    expected = {
        "lookback_days": 30,
        "k_long": 5,
        "k_short": 5,
        "rebalance_days": 7,
        "min_quote_volume_usdt": 50_000_000.0,
        "execution_lag_days": 1,
        "taker_fee_pct": 0.05,
        "slippage_pct": 0.03,
    }
    for name, value in expected.items():
        if getattr(control, name) != value:
            raise ValueError(f"冻结 spec 参数 {name} 不应为 {getattr(control, name)!r}")
    if control.volatility_targeting or control.target_vol_pct != 0 or control.max_scale != 1:
        raise ValueError("Control 必须关闭 volatility targeting")
    if not shadow.volatility_targeting or shadow.target_vol_pct != 80 or shadow.max_scale != 3:
        raise ValueError("VT80 Shadow 只能启用 80% target_vol 与 3.0 max_scale")


CONTROL_RULES = resolve_rules(variant="control")
SHADOW_RULES = resolve_rules(variant="shadow")
CONTROL_SPEC_HASH = CONTROL_RULES.spec_hash
SHADOW_SPEC_HASH = SHADOW_RULES.spec_hash
