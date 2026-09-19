V2.1-M1.4.1 CONTRACT_TYPE ROOT_CAUSE CONFIRMED

# XS-LOWVOL V2.1-M1.4.1 Contract-Type Universe Conformance Audit

Audit ID: `XS-LOWVOL-V2.1-M1.4.1-CONTRACT-TYPE-AUDIT-1`  
Audit decision: **AUDIT PASS**  
Root cause: **B_PROTOCOL_INELIGIBLE_CONTRACTS_ENTERED_PARENT_STATE**  
Base formal parent: `4072283a997117d8c24e289d3e90a303384b26db`  
Code commit used by run: `7ed53e623f5f2c9bc0dc972d73e5cbf726c98aab`

## Frozen identity

- V1 Protocol: `607d262248a4ef0d0f968c1db61f9f09ffd8ee19a247a46571e131caaa8ea03d`
- V1 Control: `5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678`
- V1 Shadow: `97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd`
- V2.1 Protocol: `6aa94ad92fa1811774788d11aecc0326a00e256950df61cb7cbe9db8e504fb7d`
- V2.1 Spec: `e5b026c106e46d04fb160ee012789c39ff1b9ef3f318a49da871a6a3cd51959c`
- Forward Anchor: `a4e61eae444e08ef038fc2108b1b29ef39551e90c4d40b6dab5c04df6004f41b` (`FROZEN_NOT_YET_STARTED`)

## Frozen data and classifier findings

- Discovered symbols: **861**; usable histories: **362**; history objects: **860**.
- Delisted usable symbols: **27**; ambiguous input: **495**; listing pages: **927**.
- Dataset SHA: `f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755`; normalized dataset SHA: `6adf9f06cba3a9de377ed59f69f422320b1524f635512271ebee60516896c387`.
- Historical classifier finding: `HISTORICAL_ARCHIVE_CONTRACT_TYPE_NOT_PROVEN`; the actual rule is a USDT suffix regex and does not inspect `contractType`, `quoteAsset`, or status.
- M1.4 parser finding: `M1_4_EXCHANGE_INFO_PRODUCT_FILTER_MISSING`; current exchangeInfo fields were copied but not used as quote/contract/status predicates.

## 495 ambiguous candidates

Category counts: `{"A_KNOWN_IN_FROZEN_CATALOG": 420, "B_TRADIFI_PERPETUAL_EXCLUDED_BY_PROTOCOL": 52, "C_NON_TRADING_OR_SETTLING_STATE": 23, "D_TRUE_PRESTART_PIT_GAP": 0, "E_UNRESOLVED": 0}`
Known-but-excluded catalog members are represented as catalog members, not as unresolved symbols; the complete row-level table is in `FROZEN_DISCOVERY_MEMBERSHIP_TABLE.json` and the 495-row reclassification is in the JSON audit artifact.

## September candidates

| Symbol | contractType | quoteAsset | status | onboard UTC | Protocol eligible | PIT evidence |
|---|---|---|---|---|---:|---:|
| AGPUUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-18T09:30:00.000Z | False | True |
| AMCUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-18T09:05:00.000Z | False | True |
| ANETUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-18T09:15:00.000Z | False | True |
| APLDUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-18T09:25:00.000Z | False | True |
| BYDUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-07T02:00:00.000Z | False | True |
| CYPHUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-18T09:10:00.000Z | False | True |
| DDOGUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-02T10:10:00.000Z | False | True |
| GPROUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-03T13:45:00.000Z | False | True |
| GTLBUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-02T10:30:00.000Z | False | True |
| HK0625USDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-01T02:45:00.000Z | False | True |
| HK0992USDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-07T02:05:00.000Z | False | True |
| HUTUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-18T09:20:00.000Z | False | True |
| MARSCOINUSDT | PERPETUAL | USDT | TRADING | 2026-09-01T09:45:00.000Z | True | True |
| MDBUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-02T10:20:00.000Z | False | True |
| NVDLUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-02T10:00:00.000Z | False | True |
| PATHUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-18T09:00:00.000Z | False | True |
| PONSUSDT | PERPETUAL | USDT | TRADING | 2026-09-06T06:45:00.000Z | True | True |
| TEAMUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-02T10:15:00.000Z | False | True |
| TSLLUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-02T10:05:00.000Z | False | True |
| ZSUSDT | TRADIFI_PERPETUAL | USDT | TRADING | 2026-09-02T10:25:00.000Z | False | True |

## Fixed M1.4 GET failures

| Request | contractType | request should have been required | classification | retry |
|---|---|---:|---|---:|
| CELRUSDT.daily_1d | PERPETUAL | True | IN_SCOPE_REQUEST_REQUIRED_BUT_MISSING_RESPONSE | False |
| PDDUSDT.funding_rate | TRADIFI_PERPETUAL | False | OUT_OF_SCOPE_REQUEST | False |
| PENGUSDT.funding_rate | TRADIFI_PERPETUAL | False | OUT_OF_SCOPE_REQUEST | False |

## M1.3 holding impact

- Wrong-contract holding intervals: **27** across **15** symbols.
- Symbols: `AXTIUSDT, CBRSUSDT, CRCLUSDT, DRAMUSDT, INTCUSDT, LITEUSDT, MSTRUSDT, MUUUSDT, MVLLUSDT, NBISUSDT, SNDKUSDT, SNXXUSDT, SOXLUSDT, SOXSUSDT, TSLAUSDT`.
- The frozen holding provenance proves Control position and accounting impact. It does not retain long/short target direction rows; no direction is invented by this audit.
- The M1.3 lifecycle correction remains valid; contract-type conformance is not established.

## Current M1.4 positions

- Current positions: **10**; protocol-ineligible: **5** (`AMDUSDT, EWYUSDT, SNXXUSDT, SPCXUSDT, TSLAUSDT`).
- M1.4 remains `PRESTART NOT_READY — ACCEPTED`; its artifacts are quarantined pre-start state and are not M2-start evidence.

## First actual Control impact

- Symbol: **TSLAUSDT**.
- First position entry: **2026-03-06T23:59:59.999Z**.
- This is the earliest actual Control position evidence available in the frozen M1.3 artifacts; signal direction is not reconstructed.

## Earliest correction point recommendation

- Signal day recommendation: `2026-03-05`.
- Execution day recommendation: `2026-03-06`.
- Pre-correction checkpoint recommendation: `2026-02-27`.
- Recommendation only; no reconstruction was executed.

## C0–C11

- `C0_identity`: **PASS**
- `C1_frozen_protocol_contract_type`: **PASS**
- `C2_historical_classifier_audited`: **PASS**
- `C3_full_frozen_catalog_used`: **PASS**
- `C4_m1_4_ambiguous_reclassified`: **PASS**
- `C5_september_candidates_reclassified`: **PASS**
- `C6_failed_requests_scope_classified`: **PASS**
- `C7_m1_3_holdings_contract_type_audited`: **PASS**
- `C8_current_positions_contract_type_audited`: **PASS**
- `C9_first_contamination_identified`: **PASS**
- `C10_old_artifacts_immutable`: **PASS**
- `C11_anchor_frozen`: **PASS**

## Immutable state and safety

- M1.3, M1.3.1, and all M1.4 artifacts unchanged; legacy `research/evidence/XS-LOWVOL-V1.json` remains stale and unchanged.
- No Strategy / Protocol / Gate / Anchor modification; no download; no retry; no parent reconstruction; no Forward; no optimization; no M2; no live trading.
- `LIVE_TRADING = False`, `PAPER_ONLY = True`, HTTP = GET-only.
