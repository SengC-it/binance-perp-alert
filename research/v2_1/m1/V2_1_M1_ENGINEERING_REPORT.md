# XS-LOWVOL V2.1-M1 Contaminated Engineering Replay

## V2.1-M1 ENGINEERING PASS

Classification: `CONTAMINATED_DEVELOPMENT_DATA` / `NOT_PERFORMANCE_EVIDENCE` / `NOT_OOS` / `NOT_FORWARD`.

This report contains implementation traces and data-quality diagnostics only.

## Frozen identity

- Base commit: `abf7338ff4af25e5fd0a12c1eade8a12a02667db`
- Code commit: `7f6f48221bf30611865126ff41d935d5a5eb5cc1`
- Run ID: `XS-LOWVOL-V2.1-M1-ENGINEERING-1`
- V1 Control SHA: `5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678`
- V2.1 Spec SHA: `e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c`
- V2.1 Protocol SHA: `6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d`
- Forward Anchor SHA: `a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b`
- Dataset SHA: `f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755`
- Normalized Dataset SHA: `6adf9f06cba3a9de377ed59f69f422320b1524f635512271ebee60516896c387`

## E0-E10

- `E0_identity`: `PASS`
- `E1_parent_parity`: `PASS`
- `E2_risk_formula`: `PASS`
- `E3_zero_vol_regression`: `PASS`
- `E4_paired_schedule`: `PASS`
- `E5_terminal_data_halt`: `PASS`
- `E6_no_lookahead`: `PASS`
- `E7_transition_accounting`: `PASS`
- `E8_temporal_semantics`: `PASS`
- `E9_funding_provenance`: `PASS`
- `E10_warm_start_readiness`: `READY`

## Diagnostic summary

```json
{
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
  "approved_m0_commit": "31fa706bf86d2177fcc251453c37d09abe8afed2",
  "artifacts": {
    "markdown": "research/v2_1/m1/V2_1_M1_ENGINEERING_REPORT.md",
    "report": "research/v2_1/m1/V2_1_M1_ENGINEERING_REPORT.json",
    "trace_manifest": "research/v2_1/m1/V2_1_M1_TRACE_MANIFEST.json"
  },
  "base_commit": "abf7338ff4af25e5fd0a12c1eade8a12a02667db",
  "classification": {
    "data": "CONTAMINATED_DEVELOPMENT_DATA",
    "evidence": "NOT_PERFORMANCE_EVIDENCE",
    "forward": "NOT_FORWARD",
    "sample": "NOT_OOS",
    "scope": "ENGINEERING_ONLY"
  },
  "code_commit": "7f6f48221bf30611865126ff41d935d5a5eb5cc1",
  "data_integrity_halt_count": 0,
  "dataset": {
    "cache_file_sha256": "767440c16f64d3deb8134371554b3a3579a053301ba4fe04106b5cc7f3a48964",
    "catalog_complete": true,
    "daily_bar_count": 636990,
    "dataset_sha256": "f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755",
    "first_available_date": "2020-01-01",
    "funding_event_count": 2629011,
    "last_available_date": "2026-08-31",
    "listing_page_count": 927,
    "normalized_dataset_sha256": "6adf9f06cba3a9de377ed59f69f422320b1524f635512271ebee60516896c387",
    "number_of_ambiguous_symbols": 52,
    "number_of_delisted_symbols": 31,
    "number_of_symbols_discovered": 861,
    "number_of_usable_symbols": 362,
    "protocol_hash": "607d262248a4ef0d0f968c1db61f9f09ffd8ee19a247a46571e131caaa8ea03d",
    "raw_file_count": 41682
  },
  "failed_gates": [],
  "final_decision": "V2.1-M1 ENGINEERING PASS",
  "first_divergence": null,
  "first_issue": {
    "code": "FUNDING_COVERAGE_GAP",
    "day": null,
    "detail": "held interval has an unverified funding transition or boundary",
    "symbol": "AKROUSDT"
  },
  "first_valid_signal_date": "2020-08-13",
  "formula_failure_count": 0,
  "formula_failures": [],
  "forward_anchor_status": "FROZEN_NOT_YET_STARTED",
  "fraction_scale_eq_1": 0.3055555555555556,
  "fraction_scale_lt_1": 0.6944444444444444,
  "frozen_identity": {
    "dataset_protocol_sha256": "607d262248a4ef0d0f968c1db61f9f09ffd8ee19a247a46571e131caaa8ea03d",
    "forward_anchor_sha256": "a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b",
    "v1_control_sha256": "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678",
    "v2_1_protocol_sha256": "6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d",
    "v2_1_spec_sha256": "e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c"
  },
  "funding": {
    "affected_holding_interval_count": 22,
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
    "fail_closed": true,
    "first_issue": {
      "code": "FUNDING_COVERAGE_GAP",
      "day": null,
      "detail": "held interval has an unverified funding transition or boundary",
      "symbol": "AKROUSDT"
    },
    "funding_integrity_issue_count": 8,
    "issues": [
      {
        "code": "FUNDING_COVERAGE_GAP",
        "day": null,
        "detail": "held interval has an unverified funding transition or boundary",
        "symbol": "AKROUSDT"
      },
      {
        "code": "FUNDING_COVERAGE_GAP",
        "day": null,
        "detail": "held interval has an unverified funding transition or boundary",
        "symbol": "ANCUSDT"
      },
      {
        "code": "FUNDING_COVERAGE_GAP",
        "day": null,
        "detail": "held interval has an unverified funding transition or boundary",
        "symbol": "DMCUSDT"
      },
      {
        "code": "FUNDING_COVERAGE_GAP",
        "day": null,
        "detail": "held interval has an unverified funding transition or boundary",
        "symbol": "KEEPUSDT"
      },
      {
        "code": "FUNDING_COVERAGE_GAP",
        "day": null,
        "detail": "held interval has an unverified funding transition or boundary",
        "symbol": "MEMEFIUSDT"
      },
      {
        "code": "FUNDING_COVERAGE_GAP",
        "day": null,
        "detail": "held interval has an unverified funding transition or boundary",
        "symbol": "OMNIUSDT"
      },
      {
        "code": "FUNDING_COVERAGE_GAP",
        "day": null,
        "detail": "held interval has an unverified funding transition or boundary",
        "symbol": "TONUSDT"
      },
      {
        "code": "FUNDING_COVERAGE_GAP",
        "day": null,
        "detail": "held interval has an unverified funding transition or boundary",
        "symbol": "VINEUSDT"
      }
    ],
    "last_issue": {
      "code": "FUNDING_COVERAGE_GAP",
      "day": null,
      "detail": "held interval has an unverified funding transition or boundary",
      "symbol": "VINEUSDT"
    },
    "no_synthetic_funding": true,
    "provenance_status": "CONTAMINATED_DATA_QUALITY_DIAGNOSTIC",
    "structural_issue_count": 0
  },
  "funding_issue_count": 8,
  "gates": {
    "E0_identity": "PASS",
    "E10_warm_start_readiness": "READY",
    "E1_parent_parity": "PASS",
    "E2_risk_formula": "PASS",
    "E3_zero_vol_regression": "PASS",
    "E4_paired_schedule": "PASS",
    "E5_terminal_data_halt": "PASS",
    "E6_no_lookahead": "PASS",
    "E7_transition_accounting": "PASS",
    "E8_temporal_semantics": "PASS",
    "E9_funding_provenance": "PASS"
  },
  "historical_regression_2020_08_14": {
    "old_v2_zero_vol_result": "V2_RISK_SCALE_INVALID",
    "same_execution_day": true,
    "same_signal_day": true,
    "same_targets": true,
    "status": "PASS",
    "v2_1_position_scale": 1.0,
    "v2_1_reference_vol": 0.0,
    "v2_1_result": "SUCCESS"
  },
  "last_issue": {
    "code": "FUNDING_COVERAGE_GAP",
    "day": null,
    "detail": "held interval has an unverified funding transition or boundary",
    "symbol": "VINEUSDT"
  },
  "lookahead_mutation_count": 1512,
  "lookahead_test_event_count": 252,
  "lookahead_violation_count": 0,
  "natural_transition_cases": [
    "close",
    "direction_flip",
    "open",
    "same_side_decrease_resize",
    "same_side_increase_resize",
    "symbol_replacement"
  ],
  "paired_schedule_divergence_count": 0,
  "parent_retry": {
    "parent_retry_attempt_count": 639,
    "parent_retry_boundary_violation_count": 0,
    "v21_independent_retry_count": 0
  },
  "parent_schedule_attempt_count": 892,
  "parent_schedule_mismatch_count": 0,
  "parent_status_mismatch_count": 0,
  "parent_success_count": 252,
  "rebalance_count": 252,
  "run_id": "XS-LOWVOL-V2.1-M1-ENGINEERING-1",
  "run_type": "CONTAMINATED_ENGINEERING_REPLAY",
  "runtime_safety": {
    "first_forward_signal_selected": false,
    "forward_clock_started": false,
    "forward_evidence_created": false,
    "http_methods": [
      "GET"
    ],
    "live_trading": false,
    "paper_only": true
  },
  "scale_behavior": {
    "data_integrity_halt_count": 0,
    "fraction_scale_eq_1": 0.3055555555555556,
    "fraction_scale_lt_1": 0.6944444444444444,
    "positive_reference_vol_count": 251,
    "scale_max": 1.0,
    "scale_mean": 0.765668777479659,
    "scale_median": 0.8127465644854031,
    "scale_min": 0.26258111483806024,
    "valid_scale_count": 252,
    "zero_reference_vol_count": 1
  },
  "scale_max": 1.0,
  "scale_mean": 0.765668777479659,
  "scale_median": 0.8127465644854031,
  "scale_min": 0.26258111483806024,
  "schema_version": "V2.1-M1-ENGINEERING-REPLAY.v1",
  "skipped_tests": [],
  "successful_rebalance_mismatch_count": 0,
  "synthetic_transition_fixture": {
    "cases": [
      "close",
      "direction_flip",
      "open",
      "same_side_decrease_resize",
      "same_side_increase_resize",
      "symbol_replacement"
    ],
    "change_count": 6,
    "valid": true,
    "violations": []
  },
  "target_mismatch_count": 0,
  "temporal_violation_count": 0,
  "terminal_data_halt": {
    "all_required_inputs_halted": true,
    "cases": {
      "insufficient_13_rows": {
        "first_status": "V2_DATA_INTEGRITY_HALT",
        "halted": true,
        "positions_unchanged": true,
        "second_status_after_recovery_input": "V2_DATA_INTEGRITY_HALT"
      },
      "missing_completed_at": {
        "first_status": "V2_DATA_INTEGRITY_HALT",
        "halted": true,
        "positions_unchanged": true,
        "second_status_after_recovery_input": "V2_DATA_INTEGRITY_HALT"
      },
      "naive_completed_at": {
        "first_status": "V2_DATA_INTEGRITY_HALT",
        "halted": true,
        "positions_unchanged": true,
        "second_status_after_recovery_input": "V2_DATA_INTEGRITY_HALT"
      },
      "required_incomplete": {
        "first_status": "V2_DATA_INTEGRITY_HALT",
        "halted": true,
        "positions_unchanged": true,
        "second_status_after_recovery_input": "V2_DATA_INTEGRITY_HALT"
      },
      "required_inf": {
        "first_status": "V2_DATA_INTEGRITY_HALT",
        "halted": true,
        "positions_unchanged": true,
        "second_status_after_recovery_input": "V2_DATA_INTEGRITY_HALT"
      },
      "required_nan": {
        "first_status": "V2_DATA_INTEGRITY_HALT",
        "halted": true,
        "positions_unchanged": true,
        "second_status_after_recovery_input": "V2_DATA_INTEGRITY_HALT"
      }
    },
    "status": "PASS",
    "terminal_halt_cannot_recover": true,
    "v2_specific_retry_created": false
  },
  "transition_violation_count": 0,
  "transition_violations": [],
  "v2_1_strategy_id": "XS-LOWVOL-V2.1-RISK15",
  "valid_scale_count": 252,
  "warm_start": {
    "available_observation_count": 13,
    "evidence_status": "NOT_PERFORMANCE_EVIDENCE",
    "historical_funding_issue_count": 8,
    "initial_state": {
      "position_scale": 1.0,
      "reference_vol": 0.07993543207471024
    },
    "initial_state_diagnostic_status": "INITIAL_STATE_DIAGNOSTIC_ONLY",
    "provenance": [
      {
        "complete": true,
        "completed_at": "2026-06-07T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-06-07"
      },
      {
        "complete": true,
        "completed_at": "2026-06-14T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-06-14"
      },
      {
        "complete": true,
        "completed_at": "2026-06-21T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-06-21"
      },
      {
        "complete": true,
        "completed_at": "2026-06-28T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-06-28"
      },
      {
        "complete": true,
        "completed_at": "2026-07-05T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-07-05"
      },
      {
        "complete": true,
        "completed_at": "2026-07-12T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-07-12"
      },
      {
        "complete": true,
        "completed_at": "2026-07-19T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-07-19"
      },
      {
        "complete": true,
        "completed_at": "2026-07-26T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-07-26"
      },
      {
        "complete": true,
        "completed_at": "2026-08-02T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-08-02"
      },
      {
        "complete": true,
        "completed_at": "2026-08-09T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-08-09"
      },
      {
        "complete": true,
        "completed_at": "2026-08-16T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-08-16"
      },
      {
        "complete": true,
        "completed_at": "2026-08-23T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-08-23"
      },
      {
        "complete": true,
        "completed_at": "2026-08-30T23:59:59.999999+00:00",
        "funding_coverage_status": "HISTORICAL_CONTROL_ACCOUNTING_AUDITED_NOT_FORWARD_HOLDING",
        "week_ending": "2026-08-30"
      }
    ],
    "required_observation_count": 13,
    "status": "READY"
  },
  "zero_reference_vol_count": 1
}
```

Trace rows are engineering provenance. No Forward evidence was created.
