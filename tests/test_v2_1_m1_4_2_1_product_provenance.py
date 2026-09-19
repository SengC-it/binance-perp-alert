import json
from pathlib import Path

from backtest.m1_4_2_1 import (
    FORWARD_ANCHOR_STATUS,
    OLD_M142_BASE_COMMIT,
    _effective_timestamp,
    load_immutable_scope,
    parse_structured_listing_table,
    parse_symbol_evidence,
)
from backtest.m1_4_2_1b import (
    IMMUTABLE_STAGE_COMMITS,
    git_blob_manifest,
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


def _table_article(rows, title="Binance Futures Will Launch Multiple Perpetual Contracts"):
    table = {
        "node": "element",
        "tag": "table",
        "child": [
            {
                "node": "element",
                "tag": "tbody",
                "child": [
                    {
                        "node": "element",
                        "tag": "tr",
                        "child": [
                            {
                                "node": "element",
                                "tag": "td",
                                "child": [{"node": "text", "text": value}],
                            }
                            for value in row
                        ],
                    }
                    for row in rows
                ],
            }
        ],
    }
    body = {"node": "root", "child": [table]}
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


def _column_fixture():
    return _table_article(
        [
            ["Contract Type", "USDT-Priced", "USDT-Priced"],
            ["USDⓈ-M Perpetual Contract", "PATHUSDT", "AMCUSDT"],
            ["Launch Time", "2026-09-18 09:00", "2026-09-18 09:05"],
            [
                "Underlying Equity/Index",
                "UiPath, Inc. Class A Common Stock",
                "AMC Entertainment Holdings, Inc. Common Stock",
            ],
            ["Settlement Asset", "USDT", "USDT"],
        ]
    )


def _row_fixture():
    return _table_article(
        [
            [
                "Symbol",
                "Launch Time",
                "Contract Type",
                "Underlying Equity/Index",
                "Settlement Asset",
            ],
            [
                "PATHUSDT",
                "2026-09-18 09:00",
                "USDⓈ-M Perpetual Contract",
                "UiPath, Inc. Common Stock",
                "USDT",
            ],
        ]
    )


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


def test_column_oriented_structured_table_binds_each_symbol_to_its_column():
    parsed = parse_structured_listing_table(_column_fixture())
    assert set(parsed) == {"PATHUSDT", "AMCUSDT"}
    assert parsed["PATHUSDT"]["parser_mode"] == "COLUMN_ORIENTED"
    assert parsed["PATHUSDT"]["listing_effective_timestamp"] == "2026-09-18T09:00:00Z"
    assert parsed["AMCUSDT"]["underlying_evidence_cell"].endswith("Common Stock")
    assert parsed["PATHUSDT"]["table_column_evidence_sha256"]


def test_row_oriented_structured_table_is_supported():
    parsed = parse_structured_listing_table(_row_fixture())
    assert set(parsed) == {"PATHUSDT"}
    assert parsed["PATHUSDT"]["parser_mode"] == "ROW_ORIENTED"
    assert parsed["PATHUSDT"]["listing_effective_timestamp"] == "2026-09-18T09:00:00Z"


def test_regression_announcement_parses_all_seven_columns():
    symbols = ["PATHUSDT", "AMCUSDT", "CYPHUSDT", "ANETUSDT", "HUTUSDT", "APLDUSDT", "AGPUUSDT"]
    parsed = parse_structured_listing_table(
        _table_article(
            [
                ["Contract Type"] + ["USDT-Priced"] * 7,
                ["USDⓈ-M Perpetual Contract"] + symbols,
                [
                    "Launch Time",
                    "2026-09-18 09:00",
                    "2026-09-18 09:05",
                    "2026-09-18 09:10",
                    "2026-09-18 09:15",
                    "2026-09-18 09:20",
                    "2026-09-18 09:25",
                    "2026-09-18 09:30",
                ],
                ["Underlying Equity/Index"] + [f"{symbol} Common Stock" for symbol in symbols],
                ["Settlement Asset"] + ["USDT"] * 7,
            ]
        )
    )
    assert set(parsed) == set(symbols)
    assert [
        parsed[symbol]["listing_effective_timestamp"] for symbol in symbols
    ] == [
        "2026-09-18T09:00:00Z",
        "2026-09-18T09:05:00Z",
        "2026-09-18T09:10:00Z",
        "2026-09-18T09:15:00Z",
        "2026-09-18T09:20:00Z",
        "2026-09-18T09:25:00Z",
        "2026-09-18T09:30:00Z",
    ]
    assert all(
        item["evidence_method"] == "STRUCTURED_MULTI_SYMBOL_OFFICIAL_LISTING_TABLE"
        for item in parsed.values()
    )


def test_structured_table_symbol_absent_has_no_evidence():
    assert parse_symbol_evidence(
        _column_fixture(), "TSLAUSDT", "2026-09-18T09:00:00Z"
    ) is None


def test_structured_table_missing_underlying_fails_closed_even_with_generic_text():
    article = _table_article(
        [
            ["Contract Type", "USDT-Priced"],
            ["USDⓈ-M Perpetual Contract", "PATHUSDT"],
            ["Launch Time", "2026-09-18 09:00"],
            ["Underlying Equity/Index", ""],
            ["Settlement Asset", "USDT"],
        ]
    )
    article["data"]["body"] = json.dumps(
        json.loads(article["data"]["body"])
        | {
            "child": [
                json.loads(article["data"]["body"])["child"][0],
                {"node": "text", "text": "Generic Underlying Equity/Index products."},
            ]
        },
        ensure_ascii=False,
    )
    assert parse_symbol_evidence(article, "PATHUSDT", "2026-09-18T09:00:00Z") is None


def test_mismatched_column_lengths_fail_closed():
    article = _table_article(
        [
            ["Contract Type", "USDT-Priced", "USDT-Priced"],
            ["USDⓈ-M Perpetual Contract", "PATHUSDT", "AMCUSDT"],
            ["Launch Time", "2026-09-18 09:00"],
            ["Underlying Equity/Index", "PATH Common Stock", "AMC Common Stock"],
            ["Settlement Asset", "USDT", "USDT"],
        ]
    )
    assert parse_structured_listing_table(article) == {}
    assert parse_symbol_evidence(article, "PATHUSDT", "2026-09-18T09:00:00Z") is None


def test_launch_time_after_onboard_is_rejected_and_exact_onboard_is_accepted():
    article = _column_fixture()
    assert parse_symbol_evidence(article, "PATHUSDT", "2026-09-18T08:59:00Z") is None
    evidence = parse_symbol_evidence(article, "PATHUSDT", "2026-09-18T09:00:00Z")
    assert evidence is not None
    assert evidence["listing_effective_timestamp"] == "2026-09-18T09:00:00Z"


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
    for _, (bound_commit, prefix) in IMMUTABLE_STAGE_COMMITS.items():
        assert git_blob_manifest(bound_commit, prefix) == git_blob_manifest("HEAD", prefix)
    corrective_root = Path("research/v2_1/m1_4_2_1a")
    if corrective_root.exists():
        result = json.loads(
            Path(
                "research/v2_1/m1_4_2_1a/"
                "V2_1_M1_4_2_1A_PRODUCT_PROVENANCE.json"
            ).read_text(encoding="utf-8")
        )
        assert result["old_m1_4_2_artifacts_unchanged"] is True
        assert result["old_m1_4_2_1_artifacts_unchanged"] is True
        assert git_blob_manifest(
            IMMUTABLE_STAGE_COMMITS["m1_4_2_1a"][0],
            IMMUTABLE_STAGE_COMMITS["m1_4_2_1a"][1],
        ) == git_blob_manifest("HEAD", "research/v2_1/m1_4_2_1a")


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


def test_previous_139_provenance_rows_and_145_membership_are_immutable_inputs():
    registry = json.loads(
        Path(
            "research/v2_1/m1_4_2_1/HISTORICAL_PRODUCT_TYPE_REGISTRY.json"
        ).read_text(encoding="utf-8")
    )
    scope = load_immutable_scope()
    assert registry["historically_proven_tradifi_count"] == 139
    assert len(registry["unresolved_historical_product_types"]) == 6
    assert len(scope["excluded_symbols"]) == 145
    assert set(scope["excluded_symbols"]) == set(
        scope["overlay"]["excluded_tradifi_symbols"]
    )
