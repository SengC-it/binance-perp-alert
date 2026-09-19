# XS-LOWVOL V2.1-M1.4.2.1 Product Provenance

V2.1-M1.4.2.1 PRODUCT_PROVENANCE NOT_READY

This is a provenance-only artifact. It does not reconstruct the parent, recompute latest-13, or create Forward evidence. The original `research/v2_1/m1_4_2/**` artifacts remain immutable and are classified as `INDEPENDENT_ACCEPTANCE_PENDING` / `NOT_M2_START_EVIDENCE`.

## Scope

- Run ID: `XS-LOWVOL-V2.1-M1.4.2.1-PRODUCT-PROVENANCE-1`
- Base commit: `b01086dcc8417f404f369099305a74cf2afaebb1`
- Original excluded TradFi count: `145`
- Historically proven TradFi count: `139`
- Unresolved count: `6`
- Announcement count: `48`
- Multi-symbol announcement count: `31`
- Membership set unchanged: `False`
- Added symbols: `[]`
- Removed symbols: `['AMCUSDT', 'ANETUSDT', 'APLDUSDT', 'CYPHUSDT', 'HUTUSDT', 'PATHUSDT']`
- Effective timestamp differences: `[]`

## Required prohibitions

- Parent reconstruction executed: `False`
- Latest-13 recomputed: `False`
- Reference volatility recomputed: `False`
- Position scale recomputed: `False`
- Current positions recomputed: `False`
- Forward evidence created: `False`
- M2 started: `False`
- HTTP policy: `GET_ONLY`

## H0-H12

- `H0_identity`: `PASS`
- `H1_old_m142_artifacts_immutable`: `PASS`
- `H2_tradfi_registry_scope_complete`: `PASS`
- `H3_symbol_level_historical_sources`: `FAIL`
- `H4_effective_timestamp_provenance`: `FAIL`
- `H5_all_145_classified`: `FAIL`
- `H6_affected_15_classified`: `PASS`
- `H7_tsla_symbol_level_provenance`: `PASS`
- `H8_post_2026_01_08_unknowns_fail_closed`: `PASS`
- `H9_membership_set_comparison`: `FAIL`
- `H10_anchor_still_frozen`: `PASS`
- `H11_no_parent_reconstruction`: `PASS`
- `H12_no_forward_evidence`: `PASS`

Failed gates: `H3_symbol_level_historical_sources, H4_effective_timestamp_provenance, H5_all_145_classified, H9_membership_set_comparison`.

The complete machine-readable record is `V2_1_M1_4_2_1_PRODUCT_PROVENANCE.json`; symbol rows and announcement-to-symbol mappings are in the registry and V2 overlay.
