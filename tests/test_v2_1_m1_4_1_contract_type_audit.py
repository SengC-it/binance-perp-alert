from __future__ import annotations

from backtest.v2_1_m1_4_1_contract_type_audit import (
    classify_ambiguous_candidate,
    classify_contract_entry,
    is_protocol_eligible_contract,
)


def test_perpetual_usdt_trading_is_eligible() -> None:
    assert is_protocol_eligible_contract("PERPETUAL", "USDT", "TRADING")
    assert classify_contract_entry("PERPETUAL", "USDT", "TRADING")["protocol_eligible"]


def test_tradifi_perpetual_is_rejected() -> None:
    result = classify_contract_entry("TRADIFI_PERPETUAL", "USDT", "TRADING")
    assert not result["protocol_eligible"]
    assert result["classification_reason"] == "TRADIFI_PERPETUAL_EXCLUDED_BY_PROTOCOL"


def test_quarterly_contracts_are_rejected() -> None:
    assert not is_protocol_eligible_contract("CURRENT_QUARTER", "USDT", "TRADING")
    assert not is_protocol_eligible_contract("NEXT_QUARTER", "USDT", "TRADING")


def test_non_usdt_quote_is_rejected() -> None:
    assert not is_protocol_eligible_contract("PERPETUAL", "USDC", "TRADING")


def test_settling_and_pending_are_not_active_candidates() -> None:
    assert not is_protocol_eligible_contract("PERPETUAL", "USDT", "SETTLING")
    assert not is_protocol_eligible_contract("PERPETUAL", "USDT", "PENDING_TRADING")


def test_known_but_excluded_is_not_ambiguous() -> None:
    assert classify_ambiguous_candidate(
        symbol="KNOWNUSDT",
        onboard_day="2025-01-01",
        in_frozen_catalog=True,
        current_info={"contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
    ) == "A_KNOWN_IN_FROZEN_CATALOG"


def test_not_in_usable_history_is_not_automatically_unknown() -> None:
    assert classify_ambiguous_candidate(
        symbol="KNOWNBUTEXCLUDEDUSDT",
        onboard_day="2025-01-01",
        in_frozen_catalog=True,
        current_info={"contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
    ) != "E_UNRESOLVED"


def test_tradifi_precedence_is_explicit() -> None:
    assert classify_ambiguous_candidate(
        symbol="EQUITYUSDT",
        onboard_day="2026-08-01",
        in_frozen_catalog=True,
        current_info={"contractType": "TRADIFI_PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
    ) == "B_TRADIFI_PERPETUAL_EXCLUDED_BY_PROTOCOL"


def test_nontrading_state_is_classified_before_catalog_membership() -> None:
    assert classify_ambiguous_candidate(
        symbol="SETTLINGUSDT",
        onboard_day="2026-08-01",
        in_frozen_catalog=True,
        current_info={"contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "SETTLING"},
    ) == "C_NON_TRADING_OR_SETTLING_STATE"


def test_current_exchange_info_does_not_prove_old_history() -> None:
    assert classify_ambiguous_candidate(
        symbol="GAPUSDT",
        onboard_day="2026-08-01",
        in_frozen_catalog=False,
        current_info={"contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
    ) == "D_TRUE_PRESTART_PIT_GAP"
