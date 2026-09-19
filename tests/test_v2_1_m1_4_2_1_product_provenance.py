import json
from pathlib import Path

from backtest.m1_4_2_1 import (
    FORWARD_ANCHOR_STATUS,
    OLD_M142_BASE_COMMIT,
    _effective_timestamp,
    _old_artifact_manifest,
    load_immutable_scope,
    parse_symbol_evidence,
)


def _article(text: str, title: str = "Binance Futures Will Launch a TradFi Perpetual"):
    body = {"node": "root", "child": [{"node": "text", "text": text}]}
    return {
        "code": "fixture-code",
        "title": title,
        "releaseDate": 1769426940000,
        "data": {
            "code": "fixture-code",
            "title": title,
            "body": json.dumps(body, ensure_ascii=False),
        },
    }


def test_scope_uses_full_frozen_catalog_and_145_exclusions():
    scope = load_immutable_scope()
    assert len(scope["membership"]["table"]) == 861
    assert scope["membership"]["number_of_usable_symbols"] == 362
    assert len(scope["excluded_symbols"]) == 145


def test_symbol_specific_equity_perpetual_announcement_is_accepted():
    evidence = parse_symbol_evidence(
        _article(
            "2026-01-28 14:30 (UTC): TSLAUSDT Equity Perpetual Contract. "
            "Underlying Equity/Index: Tesla, Inc. Settlement Asset: USDT.",
            "Binance Futures Will Launch USDⓈ-Margined TSLAUSDT Equity Perpetual Contract",
        ),
        "TSLAUSDT",
        "2026-01-28T14:30:00.000Z",
    )
    assert evidence is not None
    assert evidence["listing_effective_timestamp"] == "2026-01-28T14:30:00.000Z"
    assert evidence["evidence_status"] == "PROVEN_HISTORICAL_PRODUCT_TYPE"


def test_generic_category_statement_does_not_classify_an_arbitrary_symbol():
    article = _article(
        "TradFi perpetual contracts are USDT-settled. The first contract is XAUUSDT.",
        "Binance Futures Launches TradFi Perpetual Contracts",
    )
    assert parse_symbol_evidence(article, "TSLAUSDT", "2026-01-28T14:30:00.000Z") is None


def test_current_snapshot_or_onboard_date_alone_is_not_evidence():
    article = _article("TSLAUSDT is available on Binance Futures.", "Current exchange information")
    assert parse_symbol_evidence(article, "TSLAUSDT", "2026-01-28T14:30:00.000Z") is None


def test_multi_symbol_announcement_covers_only_explicit_symbols():
    article = _article(
        "2026-07-16 08:00 (UTC): MUUUSDT and SOXSUSDT TradFi Perpetual Contracts. "
        "Underlying Equity/Index: listed funds.",
        "Binance Futures Will Launch Multiple USDⓈ-Margined TradFi Perpetual Contracts",
    )
    assert parse_symbol_evidence(article, "MUUUSDT", "2026-07-16T08:00:00.000Z") is not None
    assert parse_symbol_evidence(article, "SOXSUSDT", "2026-07-16T08:00:00.000Z") is not None
    assert parse_symbol_evidence(article, "TSLAUSDT", "2026-07-16T08:00:00.000Z") is None


def test_effective_timestamp_after_onboard_is_rejected_by_selection():
    effective, method = _effective_timestamp(
        "2026-02-01 00:00 (UTC): TSLAUSDT Equity Perpetual", "TSLAUSDT", "2026-01-28T14:30:00.000Z"
    )
    assert effective == "2026-02-01T00:00:00.000Z"
    assert method == "EXPLICIT_TIMESTAMP"


def test_old_m142_artifact_manifest_is_readable_and_not_reconstructed():
    scope = load_immutable_scope()
    assert scope["old_result"]["base_commit"] == OLD_M142_BASE_COMMIT
    assert scope["old_result"]["forward_anchor_status"] == FORWARD_ANCHOR_STATUS
    manifest = _old_artifact_manifest()
    assert "V2_1_M1_4_2_PARENT_RECONSTRUCTION.json" in manifest
    assert not (Path("research/v2_1/m1_4_2_1")).exists()


def test_tsla_is_in_the_affected_fifteen_set():
    scope = load_immutable_scope()
    affected = {
        "AXTIUSDT", "CBRSUSDT", "CRCLUSDT", "DRAMUSDT", "INTCUSDT", "LITEUSDT",
        "MSTRUSDT", "MUUUSDT", "MVLLUSDT", "NBISUSDT", "SNDKUSDT", "SNXXUSDT",
        "SOXLUSDT", "SOXSUSDT", "TSLAUSDT",
    }
    assert affected <= set(scope["excluded_symbols"])


def test_no_parent_reconstruction_symbols_are_imported():
    source = Path("backtest/m1_4_2_1.py").read_text(encoding="utf-8")
    assert "simulate_frozen_portfolio" not in source
    assert "run_incremental" not in source
    assert '"parent_reconstruction_executed": False' in source


def test_anchor_is_frozen_not_yet_started():
    scope = load_immutable_scope()
    assert scope["old_result"]["forward_anchor_status"] == "FROZEN_NOT_YET_STARTED"
