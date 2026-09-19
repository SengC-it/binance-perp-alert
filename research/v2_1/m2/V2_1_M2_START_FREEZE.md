# V2.1-M2 START FREEZE READY

This artifact freezes only the future Forward start identity and temporal boundary. It does not execute the first attempt or produce Forward evidence.

## Identity

- Run ID: `XS-LOWVOL-V2.1-M2-START-V1`
- Status: `FROZEN_WAITING_FOR_FIRST_FORWARD_ATTEMPT`
- Code commit used by run: `4f23aecdafd876fc25ea25e2a29474cab0c9d13c`
- Freeze created: `2026-09-19T17:17:01.994963Z`
- Start Manifest SHA-256: `868e2b289fc3ddbdf6f56b4574235a8d21a278e4220caa81fd05325362394f2f`
- Binding Git Blob Manifest SHA-256: `cc07252069a879f23856b84ccc224fe234916b31a2bc71fb38034297fc18a100`

## First attempt boundary

- Signal day: `2026-09-24`
- Logical signal time: `2026-09-25T00:00:00.000000Z`
- Execution day: `2026-09-25`
- Week 1: `2026-09-25T00:00:00.000000Z` to `2026-10-02T00:00:00.000000Z`

## Accepted state

- Cutoff: `2026-09-19T00:00:00Z`
- Current positions: `10`
- Position state SHA-256: `c70a81ab4c67b3eba3b47bc3fd9fc06b17fe22e6a6045524356c3a4575a3f26c`
- Warm-start reference vol: `0.0681145306765338`
- Warm-start scale: `1.0`

## Gates

- `S0_identity`: **PASS**
- `S1_anchor_binding`: **PASS**
- `S2_parent_state_binding`: **PASS**
- `S3_product_provenance_binding`: **PASS**
- `S4_parent_state_complete`: **PASS**
- `S5_warm_start_risk_valid`: **PASS**
- `S6_scheduler_due_derivation`: **PASS**
- `S7_first_attempt_dates`: **PASS**
- `S8_freeze_before_logical_signal_time`: **PASS**
- `S9_no_future_market_data`: **PASS**
- `S10_start_bridge_policy_frozen`: **PASS**
- `S11_no_prestart_forward_evidence`: **PASS**
- `S12_accounting_carry_no_reset`: **PASS**
- `S13_risk_recompute_policy_frozen`: **PASS**
- `S14_parent_only_scheduler`: **PASS**
- `S15_paired_atomicity`: **PASS**
- `S16_forward_metric_grid_frozen`: **PASS**
- `S17_forward_gates_unchanged`: **PASS**
- `S18_anchor_file_immutable`: **PASS**
- `S19_safety`: **PASS**
- `S20_ci`: **PASS**

## Explicit non-actions

- No market data after the 2026-09-19 cutoff was fetched.
- No first Forward target, first Forward scale, or first attempt was calculated or executed.
- No Forward PnL, return, risk metric, performance result, or gate result was generated.
- The interstitial period is `PRE_FORWARD_START_BRIDGE_ONLY` and is not Forward evidence.
- M1.4.2, M1.4.2.1B, the frozen strategy/protocol identities, and the Anchor remain unchanged.
- No optimization and no live trading were performed.

Formal process exit code: `0`
