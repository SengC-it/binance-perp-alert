from datetime import date
from pathlib import Path

import pytest

from backtest import m1_4_2 as corrected
from backtest.v2_1_m1_engineering import M1_DATASET_FREEZE_PATH, load_verified_dataset
from backtest.xs_history import DailyBar, FundingEvent, LifecycleRecord, SymbolHistory


def _history(symbol: str = "AAAUSDT") -> SymbolHistory:
    return SymbolHistory(
        symbol=symbol,
        daily_bars=(
            DailyBar(
                symbol=symbol,
                day=date(2026, 2, 27),
                open=1.0,
                high=1.1,
                low=0.9,
                close=1.0,
                quote_volume=1_000_000.0,
                open_time_ms=1772150400000,
                close_time_ms=1772236799999,
            ),
        ),
        funding_events=(
            FundingEvent(
                symbol=symbol,
                funding_time_ms=1772179200000,
                funding_rate=0.0,
                funding_interval_hours=8.0,
            ),
        ),
        lifecycle=LifecycleRecord(
            symbol=symbol,
            first_available_day=date(2026, 2, 27),
            last_available_day=date(2026, 2, 27),
            listed_from=date(2026, 1, 1),
            delisted_at=None,
            currently_active=True,
            lifecycle_source="unit-test",
            lifecycle_confidence="test",
        ),
    )


def test_perpetual_usdt_is_accepted() -> None:
    assert corrected.protocol_contract_eligible("PERPETUAL", "USDT", "TRADING")


@pytest.mark.parametrize(
    "contract_type,quote_asset",
    [
        ("TRADIFI_PERPETUAL", "USDT"),
        ("CURRENT_QUARTER", "USDT"),
        ("NEXT_QUARTER", "USDT"),
        ("PERPETUAL", "USDC"),
    ],
)
def test_protocol_ineligible_products_are_rejected(
    contract_type: str, quote_asset: str
) -> None:
    assert not corrected.protocol_contract_eligible(contract_type, quote_asset, "TRADING")


def test_lifecycle_is_a_separate_signal_day_predicate() -> None:
    history = _history()
    assert corrected.protocol_contract_eligible_on(
        contract_type="PERPETUAL",
        quote_asset="USDT",
        status="TRADING",
        history=history,
        signal_day=date(2026, 2, 27),
    )
    assert not corrected.protocol_contract_eligible_on(
        contract_type="PERPETUAL",
        quote_asset="USDT",
        status="TRADING",
        history=history,
        signal_day=date(2025, 12, 31),
    )


def test_formal_correction_start_is_after_checkpoint() -> None:
    assert corrected.CHECKPOINT_DAY == date(2026, 2, 27)
    assert corrected.FORMAL_CORRECTION_START == date(2026, 2, 28)
    with pytest.raises(corrected.M142NotReady):
        corrected._run_incremental_parent(
            state=corrected.ParentRunState(
                positions={},
                scheduler={},
                equity_by_day={},
                attempts=[],
                daily_rows=[],
                holding_intervals=[],
                funding_rows=[],
                unresolved=[],
                funding_issue_count=0,
                rebalance_count=0,
            ),
            histories=(),
            registry=corrected.ContractRegistry(
                rows=(),
                excluded_tradifi_symbols=(),
                unresolved_symbols=(),
                eligible_symbols=(),
                full_catalog_symbol_count=0,
                known_but_excluded_count=0,
                overlay_payload={},
            ),
            start=date(2026, 2, 27),
            end=date(2026, 2, 27),
            label="TEST",
            enforce_product_layer=True,
        )


def test_state_schema_rejects_historical_aggregate_fields() -> None:
    payload = {"classification": list(corrected.LABELS), "return": 0.1}
    with pytest.raises(Exception):
        corrected.assert_prestart_state_schema(payload)


def test_registry_uses_full_catalog_and_preserves_known_excluded() -> None:
    if not Path(M1_DATASET_FREEZE_PATH).is_file():
        pytest.skip("verified frozen cache unavailable")
    registry = corrected.build_contract_type_registry(load_verified_dataset())
    assert registry.full_catalog_symbol_count == 861
    assert registry.known_but_excluded_count >= 499
    assert registry.overlay_payload["registry_complete"] is True
    assert not registry.unresolved_symbols
    assert len(registry.excluded_tradifi_symbols) == 145
    rows = {str(row["symbol"]): row for row in registry.rows}
    assert rows["TSLAUSDT"]["classification"] == "TRADIFI_PERPETUAL_EXCLUDED_BY_PROTOCOL"
    assert rows["TSLAUSDT"]["historical_evidence_status"].startswith("TIMESTAMPED_")


def test_corrected_registry_does_not_treat_settling_lifecycle_as_historical_product_change() -> None:
    if not Path(M1_DATASET_FREEZE_PATH).is_file():
        pytest.skip("verified frozen cache unavailable")
    dataset = load_verified_dataset()
    registry = corrected.build_contract_type_registry(dataset)
    assert corrected._registry_is_eligible(registry, "ALPHAUSDT")
    assert not corrected._registry_is_eligible(registry, "TSLAUSDT")


def test_frozen_anchor_and_safety_identity() -> None:
    assert corrected.V21_FORWARD_ANCHOR_STATUS == "FROZEN_NOT_YET_STARTED"
    assert corrected.APPROVAL == "START V2.1-M1.4.2"
    assert corrected.OUTPUT_DIR.name == "m1_4_2"

