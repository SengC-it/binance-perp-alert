# XS-LOWVOL V2.1-M1.1 Forward Warm-Start Provenance Audit

## V2.1-M1.1 WARM_START NOT_READY

Classification: `PRESTART_EXTENSION_INITIALIZATION_ONLY` / `NOT_FORWARD_EVIDENCE` / `NOT_OOS_PERFORMANCE`.

No Forward signal, ledger, clock, NAV, or aggregate strategy performance was created.

## Frozen identity and cutoff

- Base commit: `76412b9cfa3ac102ff2b8db8ee5242971bfa1480`
- Code commit: `382dd84c205596bb51e02fd4c116b10801637ecb`
- Audit ID: `XS-LOWVOL-V2.1-M1.1-WARM-START-1`
- Audit cutoff UTC: `2026-09-16T00:00:00+00:00`
- Dataset SHA: `f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755`
- Normalized Dataset SHA: `6adf9f06cba3a9de377ed59f69f422320b1524f635512271ebee60516896c387`
- PRESTART extension manifest SHA: `1affb9256fa21093ce93bb28bb34f59e038b06adb4a57a8d24662ca729a5b26a`

## W0-W8

- `W0_identity`: `PASS`
- `W1_incremental_data_provenance`: `PASS`
- `W2_latest_13_week_selection`: `PASS`
- `W3_price_component_integrity`: `PASS`
- `W4_funding_component_integrity`: `FAIL`
- `W5_cost_component_integrity`: `PASS`
- `W6_weekly_return_completeness`: `FAIL`
- `W7_risk_input_fail_closed`: `PASS`
- `W8_anchor_still_not_started`: `PASS`

## Required weekly provenance

| Week ending | Completed at | Held intervals | Funding | Price | Cost | Complete |
|---|---|---:|---|---|---|---|
| 2026-06-21 | 2026-06-21T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-06-28 | 2026-06-28T23:59:59.999999+00:00 | 10 | False | True | True | False |
| 2026-07-05 | 2026-07-05T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-07-12 | 2026-07-12T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-07-19 | 2026-07-19T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-07-26 | 2026-07-26T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-08-02 | 2026-08-02T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-08-09 | 2026-08-09T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-08-16 | 2026-08-16T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-08-23 | 2026-08-23T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-08-30 | 2026-08-30T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-09-06 | 2026-09-06T23:59:59.999999+00:00 | 10 | True | True | True | True |
| 2026-09-13 | 2026-09-13T23:59:59.999999+00:00 | 10 | True | True | True | True |

## Audit summary

```json
{
  "approved_m0_commit_timestamp_utc": "2026-09-16T06:08:22+00:00",
  "artifacts": {
    "markdown": "research/v2_1/m1_1/V2_1_M1_1_WARM_START_AUDIT.md",
    "prestart_extension_manifest": "research/v2_1/m1_1/PRESTART_EXTENSION_MANIFEST.json",
    "report": "research/v2_1/m1_1/V2_1_M1_1_WARM_START_AUDIT.json"
  },
  "audit_cutoff_utc": "2026-09-16T00:00:00+00:00",
  "audit_id": "XS-LOWVOL-V2.1-M1.1-WARM-START-1",
  "base_commit": "76412b9cfa3ac102ff2b8db8ee5242971bfa1480",
  "blocking": {
    "data_types": [
      "funding"
    ],
    "reasons": [
      "funding_component_incomplete"
    ],
    "symbols": [
      "TONUSDT"
    ],
    "weeks": [
      "2026-06-28"
    ]
  },
  "classification": {
    "data": "PRESTART_EXTENSION_INITIALIZATION_ONLY",
    "evidence": "NOT_FORWARD_EVIDENCE",
    "sample": "NOT_OOS_PERFORMANCE",
    "scope": "WARM_START_PROVENANCE_ONLY",
    "validation": "NOT_HISTORICAL_VALIDATION"
  },
  "code_commit": "382dd84c205596bb51e02fd4c116b10801637ecb",
  "completeness": {
    "required_week_count": 13,
    "weekly_return_complete_count": 12,
    "weekly_return_complete_total": 13
  },
  "dataset": {
    "cache_file_sha256": "767440c16f64d3deb8134371554b3a3579a053301ba4fe04106b5cc7f3a48964",
    "catalog_complete": true,
    "dataset_sha256": "f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755",
    "extension_end": "2026-09-15",
    "extension_start": "2026-09-01",
    "frozen_data_end": "2026-08-31",
    "frozen_usable_symbol_count": 362,
    "listing_page_count": 927,
    "normalized_dataset_sha256": "6adf9f06cba3a9de377ed59f69f422320b1524f635512271ebee60516896c387",
    "number_of_ambiguous_symbols": 52,
    "number_of_delisted_symbols": 31,
    "number_of_symbols_discovered": 861,
    "number_of_usable_symbols": 362,
    "raw_file_count": 41682
  },
  "failed_gates": [
    "W4_funding_component_integrity",
    "W6_weekly_return_completeness"
  ],
  "final_decision": "V2.1-M1.1 WARM_START NOT_READY",
  "forward_anchor_sha256": "a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b",
  "forward_anchor_status": "FROZEN_NOT_YET_STARTED",
  "gates": {
    "W0_identity": "PASS",
    "W1_incremental_data_provenance": "PASS",
    "W2_latest_13_week_selection": "PASS",
    "W3_price_component_integrity": "PASS",
    "W4_funding_component_integrity": "FAIL",
    "W5_cost_component_integrity": "PASS",
    "W6_weekly_return_completeness": "FAIL",
    "W7_risk_input_fail_closed": "PASS",
    "W8_anchor_still_not_started": "PASS"
  },
  "global_funding_diagnostic": {
    "affected_symbols": [
      "AKROUSDT",
      "ANCUSDT",
      "DMCUSDT",
      "KEEPUSDT",
      "MEMEFIUSDT",
      "OMNIUSDT",
      "TONUSDT",
      "VINEUSDT"
    ],
    "first_issue": {
      "code": "FUNDING_COVERAGE_GAP",
      "day": null,
      "detail": "held interval has an unverified funding transition or boundary",
      "symbol": "KEEPUSDT"
    },
    "funding_integrity_issue_count": 8,
    "last_issue": {
      "code": "FUNDING_COVERAGE_GAP",
      "day": null,
      "detail": "held interval has an unverified funding transition or boundary",
      "symbol": "DMCUSDT"
    },
    "provenance_status": "CONTAMINATED_DATA_QUALITY_DIAGNOSTIC",
    "required_window_intersection_issue_count": 1,
    "required_window_intersection_symbols": [
      "TONUSDT"
    ],
    "synthetic_funding_used": false
  },
  "historical_performance_output": false,
  "latest_13_week_endings": [
    "2026-06-21",
    "2026-06-28",
    "2026-07-05",
    "2026-07-12",
    "2026-07-19",
    "2026-07-26",
    "2026-08-02",
    "2026-08-09",
    "2026-08-16",
    "2026-08-23",
    "2026-08-30",
    "2026-09-06",
    "2026-09-13"
  ],
  "legacy_m1_artifact_hashes_after": {
    "E:\\Codex\\Binance-perp-alert\\research\\v2_1\\m1\\V2_1_M1_ENGINEERING_REPORT.json": "924c78e84b0406d4cf8481f5c332a3ad39222b4d6526fa4d95badaab637eda22",
    "E:\\Codex\\Binance-perp-alert\\research\\v2_1\\m1\\V2_1_M1_ENGINEERING_REPORT.md": "1f92b228028497d39cd06e1e2d8584d048c7459e72e93f208a09cc9bb410f21d",
    "E:\\Codex\\Binance-perp-alert\\research\\v2_1\\m1\\V2_1_M1_TRACE_MANIFEST.json": "69e0688fa987144ac4c3d2929fa61ffe5a224ec91faeb23828e3708026a43ec9"
  },
  "legacy_m1_artifact_hashes_before": {
    "E:\\Codex\\Binance-perp-alert\\research\\v2_1\\m1\\V2_1_M1_ENGINEERING_REPORT.json": "924c78e84b0406d4cf8481f5c332a3ad39222b4d6526fa4d95badaab637eda22",
    "E:\\Codex\\Binance-perp-alert\\research\\v2_1\\m1\\V2_1_M1_ENGINEERING_REPORT.md": "1f92b228028497d39cd06e1e2d8584d048c7459e72e93f208a09cc9bb410f21d",
    "E:\\Codex\\Binance-perp-alert\\research\\v2_1\\m1\\V2_1_M1_TRACE_MANIFEST.json": "69e0688fa987144ac4c3d2929fa61ffe5a224ec91faeb23828e3708026a43ec9"
  },
  "legacy_m1_artifacts_unchanged": true,
  "parameter_optimization": false,
  "position_scale": null,
  "prestart_extension_data_range": {
    "cutoff_utc": "2026-09-16T00:00:00+00:00",
    "end_utc": "2026-09-15",
    "start_utc": "2026-09-01"
  },
  "prestart_extension_manifest_sha256": "1affb9256fa21093ce93bb28bb34f59e038b06adb4a57a8d24662ca729a5b26a",
  "reference_vol": null,
  "risk_diagnostic_status": "NOT_COMPUTED_DUE_TO_INCOMPLETE_INPUT",
  "risk_input": {
    "fail_closed_status": "V2_DATA_INTEGRITY_HALT",
    "input_complete_count": 12,
    "input_row_count": 13,
    "reason": "V2_DATA_INTEGRITY_HALT: a required Control weekly return is incomplete",
    "selected_count": 0,
    "status": "V2_DATA_INTEGRITY_HALT"
  },
  "runtime_safety": {
    "first_forward_signal_selected": false,
    "forward_clock_started": false,
    "forward_evidence_created": false,
    "forward_ledger_created": false,
    "forward_nav_created": false,
    "http_methods": [
      "GET"
    ],
    "live_trading": false,
    "paper_only": true
  },
  "schema_version": "V2.1-M1.1-WARM-START-AUDIT.v1",
  "v1_control_sha256": "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678",
  "v2_1_m2_started": false,
  "v2_1_protocol_sha256": "6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d",
  "v2_1_spec_sha256": "e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c",
  "v2_1_strategy_id": "XS-LOWVOL-V2.1-RISK15"
}
```
