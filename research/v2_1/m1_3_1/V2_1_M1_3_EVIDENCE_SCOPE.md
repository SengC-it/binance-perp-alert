# XS-LOWVOL V2.1-M1.3.1 Evidence Scope Notice

Status: `CORRECTIVE_READY`

M1.3 lifecycle reconstruction: `TECHNICALLY_ACCEPTED`

M1.3 current warm-start readiness: `NOT_ESTABLISHED`

This notice is an evidence-scope correction only. It does not rerun the
historical reconstruction, recompute historical accounting, download data, or
modify any M1.3 artifact.

## Readiness boundary

- M1.3 `data_end`: `2026-08-31`
- M1.3 latest completed 13-week row ends: `2026-08-30`
- Current prestart latest-13 requirement: post-`2026-08-31` state
- Current prestart latest-13 completeness: `NOT_ESTABLISHED`
- Historical-cache P8 meaning: `FROZEN_CACHE_LAST_13_COMPLETE`
- Current prestart P8 meaning: `CURRENT_PRESTART_LATEST_13_COMPLETE_NOT_ESTABLISHED`
- Latest-13 weekly series scope: `RISK_STATE_INITIALIZATION_ONLY`

Therefore, M1.3 being technically accepted does not establish current
warm-start readiness and does not authorize Forward, M1.4, M2, optimization,
or live trading.

## Quarantined diagnostics

The following values remain preserved in the immutable M1.3 result artifact,
but are uniformly classified as:

- `QUARANTINED_CONTAMINATED_DIAGNOSTIC`
- `NOT_VALIDATION_EVIDENCE`
- `NOT_FORWARD_EVIDENCE`
- `NOT_PARAMETER_SELECTION_INPUT`
- `NOT_GATE_INPUT`

The preserved diagnostic keys are:

- `price_component_total`
- `funding_component_total`
- `transaction_cost_component_total`
- `long_component_total`
- `short_component_total`
- weekly `combined_component`

They must not be used to change the 15% target, the 13-week lookback, any
Gate, Forward-start decision, or V2.1 profitability comparison.

## Frozen lifecycle conclusions

- Overlay SHA-256: `7a6f183262580933c4802acb9454b0617c7c095b458b79f1e56fadb1c9ec64c1`
- Overlay records: `22`
- Classification rows: `22`
- Pre-correction schedule mismatch: `0`
- Post-correction unexplained divergence: `0`
- Old lifecycle-induced `MARK_FAILURE`: `383`
- Corrected lifecycle-induced `MARK_FAILURE`: `0`
- Ghost positions after confirmed delist: `0`
- Corrected frozen-replay funding coverage issues: `0`

## Frozen identity and safety

- M1.3 result commit: `355dbfcb1dcf2d0e5f0fc1b84e478e788ec1b24a`
- M1.3 code commit: `aebdda0169cddcdf013d24917515d4acfd1d5e74`
- Forward Anchor: `a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b`
- Anchor status: `FROZEN_NOT_YET_STARTED`
- `LIVE_TRADING=False`
- `PAPER_ONLY=True`
- HTTP policy: `GET_ONLY`
- No new data, reference volatility, position scale, or Forward evidence

The eight M1.3 artifacts remain immutable and are listed in the companion
JSON notice. The original M1.3 result and code commits remain unchanged.
