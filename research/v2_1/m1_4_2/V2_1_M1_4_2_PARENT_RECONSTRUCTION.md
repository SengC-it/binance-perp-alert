V2.1-M1.4.2 PARENT_STATE READY

# XS-LOWVOL V2.1-M1.4.2 Parent Reconstruction

This artifact is 'PRESTART_STATE_INITIALIZATION_ONLY', 'NOT_FORWARD', and 'NOT_VALIDATION_EVIDENCE'.
It contains no historical aggregate result and does not select a Forward signal.

## Frozen identity

- Code commit used by run: '82fffabada8da6bd41d919136f22eb4a3405bfb3'
- V1 Protocol: '607d262248a4ef0d0f968c1db61f9f09ffd8ee19a247a46571e131caaa8ea03d'
- V1 Control: '5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678'
- V1 Shadow: '97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd'
- V2.1 Spec: 'e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c'
- V2.1 Protocol: '6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d'
- Forward Anchor: 'a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b'

## R0-R20

- 'R0_identity': 'PASS'
- 'R1_contract_type_registry_complete': 'PASS'
- 'R2_pre_contamination_checkpoint': 'PASS'
- 'R3_pre_correction_state_parity': 'PASS'
- 'R4_product_filter': 'PASS'
- 'R5_lifecycle_layer_preserved': 'PASS'
- 'R6_scheduler_continuity': 'PASS'
- 'R7_position_continuity': 'PASS'
- 'R8_no_protocol_ineligible_eligible_symbols': 'PASS'
- 'R9_no_protocol_ineligible_targets': 'PASS'
- 'R10_no_protocol_ineligible_positions': 'PASS'
- 'R11_divergence_explained': 'PASS'
- 'R12_funding_integrity': 'PASS'
- 'R13_required_prestart_gets_complete': 'PASS'
- 'R14_current_parent_state_complete': 'PASS'
- 'R15_current_latest_13_selection': 'PASS'
- 'R16_current_latest_13_complete': 'PASS'
- 'R17_risk_state_valid': 'PASS'
- 'R18_anchor_still_frozen': 'PASS'
- 'R19_no_forward_evidence': 'PASS'
- 'R20_no_full_formal_historical_rerun': 'PASS'

## Corrected state

- Checkpoint: '2026-02-27T23:59:59.999Z'
- Formal correction start: '2026-02-28'
- First corrected divergence: '{'signal_day': '2026-03-05', 'execution_day': '2026-03-06', 'legacy_target_symbols': ['1000PEPEUSDT', 'APTUSDT', 'AVAXUSDT', 'BNBUSDT', 'BTCUSDT', 'DOTUSDT', 'LINKUSDT', 'SUIUSDT', 'TSLAUSDT', 'UNIUSDT'], 'corrected_target_symbols': ['1000PEPEUSDT', '1000SHIBUSDT', 'APTUSDT', 'AVAXUSDT', 'BNBUSDT', 'BTCUSDT', 'DOTUSDT', 'LINKUSDT', 'SUIUSDT', 'UNIUSDT'], 'reason': 'CONTRACT_TYPE_CORRECTION'}'
- Explained divergence count: '24'
- Unexplained divergence count: '0'
- Current positions: '10'
- Latest-13 complete: 'True'

## Safety

- LIVE_TRADING: 'False'
- PAPER_ONLY: 'True'
- HTTP: 'GET-only'
- Anchor: 'FROZEN_NOT_YET_STARTED'
- No Forward evidence, no V2, no M2.
