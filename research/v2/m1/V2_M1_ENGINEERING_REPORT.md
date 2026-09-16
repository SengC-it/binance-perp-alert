# XS-LOWVOL V2-M1 Engineering Replay

## ENGINEERING FAIL

Classification: `CONTAMINATED_DEVELOPMENT_DATA` / `NOT_PERFORMANCE_EVIDENCE` / `NOT_OOS` / `NOT_FORWARD`.

This artifact records implementation traces and engineering diagnostics only.

## Frozen identity

- Base commit: `bacf6e351832fc9b8f2a1f619eaa04818995973d`
- Code commit: `c7a114d0434b107a9a4487b0bacd7a97add04dc2`
- V1 Control SHA: `5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678`
- V2 Spec SHA: `0337c9d026c5544d31f53d6812e59b985809a46b8329b37ec2f7864aa8ea71bc`
- V2 Protocol SHA: `2460cd4db23b81ec769307c95b1dd74b15301a3995f0326aa194714208487fd4`
- Forward Anchor SHA: `e4659797089bc940f7ade73d3d19cae23b79a8fa0d9b4b5042eb71ec7d05e569`
- Dataset SHA: `f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755`
- Normalized Dataset SHA: `6adf9f06cba3a9de377ed59f69f422320b1524f635512271ebee60516896c387`

## E0–E10

- `E0_identity`: `True`
- `E1_parent_schedule`: `True`
- `E2_risk_formula`: `False`
- `E3_no_lookahead`: `True`
- `E4_transition_accounting`: `True`
- `E5_funding_fail_closed`: `True`
- `E6_retry_and_paired_schedule`: `False`
- `E7_rebalance_clock`: `True`
- `E8_temporal_semantics`: `True`
- `E9_behavior_trace_integrity`: `True`
- `E10_warm_start_readiness`: `READY`

## Engineering diagnostics

```json
{
  "approved_m0_commit": "5b5c81529c92256846a16f98f1227a7af782370a",
  "base_commit": "bacf6e351832fc9b8f2a1f619eaa04818995973d",
  "classification": {
    "data": "CONTAMINATED_DEVELOPMENT_DATA",
    "evidence": "NOT_PERFORMANCE_EVIDENCE",
    "forward": "NOT_FORWARD",
    "sample": "NOT_OOS"
  },
  "code_commit": "c7a114d0434b107a9a4487b0bacd7a97add04dc2",
  "dataset": {
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
    "raw_file_count": 41682
  },
  "early_rebalance_violation_count": 0,
  "eventually_recovered_count": 257,
  "failed_checks": [
    "E2_risk_formula",
    "E6_retry_and_paired_schedule"
  ],
  "final_decision": "ENGINEERING FAIL",
  "first_divergence": {
    "execution_day": "2020-08-14",
    "expected_status": "SUCCESS",
    "index": 195,
    "parent_status": "SUCCESS",
    "reason": "status SUCCESS != V2_RISK_SCALE_INVALID",
    "signal_day": "2020-08-13",
    "v2_status": "V2_RISK_SCALE_INVALID"
  },
  "first_valid_signal_date": "2020-08-13",
  "forward_anchor_status": "FROZEN_NOT_YET_STARTED",
  "frozen_identity": {
    "forward_anchor_sha256": "e4659797089bc940f7ade73d3d19cae23b79a8fa0d9b4b5042eb71ec7d05e569",
    "v1_control_sha256": "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678",
    "v2_protocol_sha256": "2460cd4db23b81ec769307c95b1dd74b15301a3995f0326aa194714208487fd4",
    "v2_spec_sha256": "0337c9d026c5544d31f53d6812e59b985809a46b8329b37ec2f7864aa8ea71bc"
  },
  "funding": {
    "affected_holding_intervals": [
      {
        "entry_timestamp_ms": 1617321599999,
        "exit_timestamp_ms": 1617926399999,
        "symbol": "AKROUSDT"
      },
      {
        "entry_timestamp_ms": 1634860799999,
        "exit_timestamp_ms": 1637884799999,
        "symbol": "KEEPUSDT"
      },
      {
        "entry_timestamp_ms": 1638489599999,
        "exit_timestamp_ms": 1639094399999,
        "symbol": "KEEPUSDT"
      },
      {
        "entry_timestamp_ms": 1644537599999,
        "exit_timestamp_ms": 1644969599999,
        "symbol": "KEEPUSDT"
      },
      {
        "entry_timestamp_ms": 1652399999999,
        "exit_timestamp_ms": 1652486399999,
        "symbol": "ANCUSDT"
      },
      {
        "entry_timestamp_ms": 1653609599999,
        "exit_timestamp_ms": 1653695999999,
        "symbol": "AKROUSDT"
      },
      {
        "entry_timestamp_ms": 1721347199999,
        "exit_timestamp_ms": 1723161599999,
        "symbol": "TONUSDT"
      },
      {
        "entry_timestamp_ms": 1728604799999,
        "exit_timestamp_ms": 1734652799999,
        "symbol": "TONUSDT"
      },
      {
        "entry_timestamp_ms": 1735257599999,
        "exit_timestamp_ms": 1735862399999,
        "symbol": "TONUSDT"
      },
      {
        "entry_timestamp_ms": 1736467199999,
        "exit_timestamp_ms": 1738281599999,
        "symbol": "TONUSDT"
      },
      {
        "entry_timestamp_ms": 1740700799999,
        "exit_timestamp_ms": 1741305599999,
        "symbol": "VINEUSDT"
      },
      {
        "entry_timestamp_ms": 1740095999999,
        "exit_timestamp_ms": 1741910399999,
        "symbol": "TONUSDT"
      },
      {
        "entry_timestamp_ms": 1741910399999,
        "exit_timestamp_ms": 1742515199999,
        "symbol": "VINEUSDT"
      },
      {
        "entry_timestamp_ms": 1743724799999,
        "exit_timestamp_ms": 1744934399999,
        "symbol": "VINEUSDT"
      },
      {
        "entry_timestamp_ms": 1745539199999,
        "exit_timestamp_ms": 1746143999999,
        "symbol": "VINEUSDT"
      },
      {
        "entry_timestamp_ms": 1747353599999,
        "exit_timestamp_ms": 1747958399999,
        "symbol": "VINEUSDT"
      },
      {
        "entry_timestamp_ms": 1747353599999,
        "exit_timestamp_ms": 1749167999999,
        "symbol": "TONUSDT"
      },
      {
        "entry_timestamp_ms": 1752191999999,
        "exit_timestamp_ms": 1788220799999,
        "symbol": "TONUSDT"
      },
      {
        "entry_timestamp_ms": 1754006399999,
        "exit_timestamp_ms": 1788220799999,
        "symbol": "OMNIUSDT"
      },
      {
        "entry_timestamp_ms": 1754006399999,
        "exit_timestamp_ms": 1788220799999,
        "symbol": "VINEUSDT"
      },
      {
        "entry_timestamp_ms": 1754611199999,
        "exit_timestamp_ms": 1788220799999,
        "symbol": "MEMEFIUSDT"
      },
      {
        "entry_timestamp_ms": 1754611199999,
        "exit_timestamp_ms": 1788220799999,
        "symbol": "DMCUSDT"
      }
    ],
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
    "structural_issue_count": 0
  },
  "funding_issue_count": 8,
  "gates": {
    "E0_identity": true,
    "E10_warm_start_readiness": "READY",
    "E1_parent_schedule": true,
    "E2_risk_formula": false,
    "E3_no_lookahead": true,
    "E4_transition_accounting": true,
    "E5_funding_fail_closed": true,
    "E6_retry_and_paired_schedule": false,
    "E7_rebalance_clock": true,
    "E8_temporal_semantics": true,
    "E9_behavior_trace_integrity": true
  },
  "natural_transition_cases": [
    "decrease_resize",
    "direction_flip",
    "increase_resize",
    "symbol_replacement"
  ],
  "no_lookahead_failure_count": 0,
  "no_lookahead_test_count": 251,
  "paired_schedule_divergence_count": 1,
  "parent_schedule_attempt_count": 892,
  "parent_schedule_mismatch_count": 0,
  "parent_success_count": 252,
  "rebalance_count": 252,
  "recovered_retry_count": 0,
  "retry_count": 639,
  "retry_semantic_violation_count": 0,
  "risk_invalid_attempt_count": 1,
  "run_id": "XS-LOWVOL-V2-M1-ENGINEERING-REPLAY",
  "run_type": "CONTAMINATED_ENGINEERING_REPLAY",
  "runtime_safety": {
    "forward_evidence_created": false,
    "http_methods": [
      "GET"
    ],
    "live_trading": false,
    "paper_only": true
  },
  "scale_behavior": {
    "fraction_scale_eq_1": 0.30278884462151395,
    "fraction_scale_lt_1": 0.6972111553784861,
    "invalid_scale_count": 1,
    "reference_vol_max": 0.5712520494572064,
    "reference_vol_mean": 0.2143565922337106,
    "reference_vol_median": 0.18497646639505785,
    "reference_vol_min": 0.01826335637713413,
    "scale_max": 1.0,
    "scale_mean": 0.7647351869516895,
    "scale_median": 0.810913966102272,
    "scale_min": 0.26258111483806024,
    "valid_scale_count": 251
  },
  "schema_version": "V2-M1-ENGINEERING-REPLAY.v1",
  "synthetic_transition_fixture": {
    "cases": {
      "decrease_resize": true,
      "direction_flip": true,
      "increase_resize": true,
      "symbol_replacement": true
    },
    "change_count": 6,
    "valid": true
  },
  "target_mismatch_count": 0,
  "temporal_violation_count": 0,
  "trace_manifest_path": "research/v2/m1/V2_M1_TRACE_MANIFEST.json",
  "transition_violation_count": 0,
  "transition_violations": [],
  "unresolved_risk_invalid_count": 1,
  "v2_strategy_id": "XS-LOWVOL-V2-RISK15",
  "warm_start": {
    "available_observation_count": 13,
    "historical_funding_issue_count": 8,
    "incomplete_observations": [],
    "initial_state_diagnostic": {
      "position_scale": 1.0,
      "reference_vol": 0.07993543207471024
    },
    "provenance": [
      {
        "complete": true,
        "completed_at": "2026-06-07T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-06-07"
      },
      {
        "complete": true,
        "completed_at": "2026-06-14T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-06-14"
      },
      {
        "complete": true,
        "completed_at": "2026-06-21T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-06-21"
      },
      {
        "complete": true,
        "completed_at": "2026-06-28T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-06-28"
      },
      {
        "complete": true,
        "completed_at": "2026-07-05T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-07-05"
      },
      {
        "complete": true,
        "completed_at": "2026-07-12T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-07-12"
      },
      {
        "complete": true,
        "completed_at": "2026-07-19T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-07-19"
      },
      {
        "complete": true,
        "completed_at": "2026-07-26T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-07-26"
      },
      {
        "complete": true,
        "completed_at": "2026-08-02T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-08-02"
      },
      {
        "complete": true,
        "completed_at": "2026-08-09T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-08-09"
      },
      {
        "complete": true,
        "completed_at": "2026-08-16T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-08-16"
      },
      {
        "complete": true,
        "completed_at": "2026-08-23T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-08-23"
      },
      {
        "complete": true,
        "completed_at": "2026-08-30T23:59:59.999999+00:00",
        "funding_coverage_status": "NOT_YET_STARTED_NO_FORWARD_HOLDINGS",
        "week_ending": "2026-08-30"
      }
    ],
    "required_observation_count": 13,
    "status": "READY"
  }
}
```

## Trace manifest

- `research/v2/m1/V2_M1_TRACE_MANIFEST.json`
- Trace rows remain engineering provenance; no Forward evidence was created.
