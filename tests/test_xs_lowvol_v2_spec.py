"""Synthetic V2-M0 Strategy Spec freeze tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

import src.xs_lowvol_spec as v1_spec
from src.xs_lowvol_v2_spec import (
    PARENT_CONTROL_SHA256,
    PARENT_CONTROL_STRATEGY_ID,
    V2_RISK_CONFIG,
    V2_SPEC_SHA256,
    V2_STRATEGY_ID,
    V2SpecError,
    canonical_spec_json,
    load_v2_strategy_spec,
    spec_sha256,
    validate_v2_spec,
    verify_v2_spec_hash,
)


def test_v2_spec_hash_and_sidecar_are_frozen():
    spec = load_v2_strategy_spec()
    assert spec["strategy_id"] == V2_STRATEGY_ID
    assert spec["parent_strategy_id"] == PARENT_CONTROL_STRATEGY_ID
    assert spec["parent_control_sha256"] == PARENT_CONTROL_SHA256
    assert spec_sha256(spec) == V2_SPEC_SHA256
    assert verify_v2_spec_hash() == V2_SPEC_SHA256
    validate_v2_spec()


def test_v2_risk_layer_has_only_the_registered_values():
    assert V2_RISK_CONFIG.target_annualized_vol == 0.15
    assert V2_RISK_CONFIG.vol_lookback_weeks == 13
    assert V2_RISK_CONFIG.vol_estimator == "population_std"
    assert V2_RISK_CONFIG.min_scale == 0.0
    assert V2_RISK_CONFIG.max_scale == 1.0
    assert V2_RISK_CONFIG.leverage_allowed is False
    payload = load_v2_strategy_spec()
    risk = payload["risk_layer"]
    assert risk["input_components"] == ["price_pnl", "funding_pnl", "transaction_cost"]
    assert canonical_spec_json(payload) == canonical_spec_json(deepcopy(payload))


def test_v2_spec_tampering_is_rejected():
    altered = load_v2_strategy_spec()
    altered["risk_layer"]["target_annualized_vol"] = 0.20
    with pytest.raises(V2SpecError, match="target_annualized_vol"):
        validate_v2_spec(altered, require_approved_hash=False, require_current_v1=False)


def test_v2_rejects_a_changed_current_v1_hash(monkeypatch):
    monkeypatch.setattr(v1_spec, "strategy_spec_hash", lambda *args, **kwargs: "0" * 64)
    with pytest.raises(V2SpecError, match="V1 Control spec 内容"):
        validate_v2_spec(require_approved_hash=False)
