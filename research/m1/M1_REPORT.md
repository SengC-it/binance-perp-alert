# M1 FAIL

## Executive Decision

Primary decision strategy: `XS-LOWVOL-V1-Control`.
Shadow is secondary and cannot rescue a Control failure.

## Failed Gates

- `G0_data_integrity`: FAIL
- `G3_weekly_sharpe`: FAIL
- `G4_drawdown`: FAIL
- `G6_block_bootstrap`: FAIL
- `G7_best_5pct_removed`: FAIL
- `G11_single_year_catastrophe`: FAIL
- `G12_bull_survival`: FAIL

## Frozen Hashes

- Protocol SHA-256: `607d262248a4ef0d0f968c1db61f9f09ffd8ee19a247a46571e131caaa8ea03d`
- Dataset SHA-256: `f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755`
- Control SHA-256: `5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678`
- Shadow SHA-256: `97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd`

## G0–G12

- `G0_data_integrity`: FAIL
- `G1_external_return`: PASS
- `G2_cost_2x`: PASS
- `G3_weekly_sharpe`: FAIL
- `G4_drawdown`: FAIL
- `G5_profit_factor`: PASS
- `G6_block_bootstrap`: FAIL
- `G7_best_5pct_removed`: FAIL
- `G8_leave_one_out`: PASS
- `G9_symbol_concentration`: PASS
- `G10_multi_year_breadth`: PASS
- `G11_single_year_catastrophe`: FAIL
- `G12_bull_survival`: FAIL

## Dataset and Data Quality

- Symbols discovered / usable / delisted / ambiguous: 861 / 362 / 31 / 52
- Listing pages: 927; raw archives: 41682
- Daily bars: 636990; funding events: 2629011
- First/last available date: 2020-01-01 / 2026-08-31
- First valid signal date: 2020-08-14; rebalances: 302
- Funding held-interval coverage: FAIL

## External Validation (2020-01-01 to 2025-08-31)

| Run | Return | CAGR | Weekly Sharpe | Profit Factor | Max Drawdown |
|---|---:|---:|---:|---:|---:|
| Control COST_1X | 55.5948% | 8.1112% | 0.4116 | 1.2040 | 41.0958% |
| Control COST_2X | 42.0908% | 6.3934% | 0.3660 | 1.1786 | 44.0459% |
| Control COST_3X | 28.5868% | 4.5355% | 0.3210 | 1.1543 | 47.4284% |

## 2020–2021 Bull Death Test

- Return: 6.4358%.
- Max Drawdown: 35.3611%
- Long PnL: 0.9034; Short PnL: -0.8390

## Funding and Cost Attribution

- External price PnL: 1.0143
- External funding PnL: -0.3233
- External cost PnL: -0.1350
- External long / short PnL: 1.2668 / -0.7108

## Bootstrap and Robustness

- 4-week block bootstrap: 10000 rounds, seed 20260915
- CI95 lower: -0.0027; P(mean > 0): 0.8453
- Best 5% weeks removed compound return: -56.7293%
- LOO positive / total: 362 / 362; worst removed symbol: WLDUSDT
- Top-2 positive PnL concentration: 12.5653%

## Calendar Years and Regimes

- Positive full calendar years: [2020, 2022, 2024]
- 2020–2024 worst full-year return: -23.9588%
- Regime attribution: `{"BEAR": {"days": 329, "pnl": 0.18106967984967207, "return_sum_pct": 17.198454675287966}, "BULL": {"days": 871, "pnl": 0.30065478197755763, "return_sum_pct": 29.64020857944837}, "SIDEWAYS": {"days": 870, "pnl": 0.07422348519537621, "return_sum_pct": 31.39495247851216}}`

## Discovery Reference (2025-09-01 to 2026-08-31)

Control COST_1X discovery return: 75.5680%. This window is descriptive only and is excluded from all primary gates.

## Safety Controls

- Runtime Evidence remains `EVIDENCE_STALE`; no evidence publication was performed.
- `LIVE_TRADING = False`; HTTP access in this run is GET-only.
- No M1-B result is used to optimize V1 and no M2 work is started.

## Machine-readable result

```json
{
  "attribution": {
    "bull_2020_2021": {
      "cost_pnl": -0.030400000000000073,
      "funding_pnl": 0.040779688999999904,
      "long_pnl": 0.9033628444891029,
      "net_pnl": 0.06435837258255417,
      "pnl_by_symbol": {
        "1000SHIBUSDT": -0.06311878248969657,
        "1INCHUSDT": -0.015406013095680494,
        "AAVEUSDT": -0.08306263827566583,
        "ADAUSDT": -0.028905390220017153,
        "AKROUSDT": 0.015424990623092768,
        "ALGOUSDT": 0.052236552631745835,
        "ALPHAUSDT": 0.042211527628933904,
        "ATOMUSDT": -0.005239840972659961,
        "AVAXUSDT": -0.03864469071399228,
        "BAKEUSDT": 0.020490643199762584,
        "BANDUSDT": -0.016731767554052708,
        "BATUSDT": -0.03765416701365294,
        "BCHUSDT": 0.08006752616701185,
        "BELUSDT": -0.005257253746269243,
        "BLZUSDT": 0.09187200996809156,
        "BNBUSDT": 0.18068434177401693,
        "BTCUSDT": 0.13819304264034157,
        "BTTUSDT": 0.01760861480281613,
        "BZRXUSDT": 0.03446368152499572,
        "CELRUSDT": -0.05729805502204526,
        "CHRUSDT": -0.07777443399696864,
        "CHZUSDT": -0.1998817268265582,
        "COMPUSDT": 0.005470056915226203,
        "COTIUSDT": -0.06261011322329992,
        "CRVUSDT": -0.15840964835304835,
        "DASHUSDT": 0.05531129547756374,
        "DEFIUSDT": 0.011640899314003006,
        "DENTUSDT": 6.710601781104945e-05,
        "DODOUSDT": -0.03542201423758562,
        "DOGEUSDT": -0.16313147403174683,
        "DOTUSDT": 0.009055332157007965,
        "EGLDUSDT": 0.03360302482816169,
        "EOSUSDT": 0.06175261896554615,
        "ETCUSDT": 0.20957693378991415,
        "ETHUSDT": 0.1502631615635162,
        "GALAUSDT": -0.0483178051536662,
        "HNTUSDT": -0.055311455949241056,
        "IOSTUSDT": -0.07630389512854598,
        "IOTXUSDT": -0.057714051899323444,
        "KEEPUSDT": 0.0455550122845524,
        "LENDUSDT": -0.00829989251853036,
        "LINKUSDT": 0.02730099405248531,
        "MATICUSDT": -0.15026164024054342,
        "MTLUSDT": 0.05310970333409788,
        "NEOUSDT": 0.014047752660394925,
        "NKNUSDT": 0.018022302131317697,
        "QTUMUSDT": 0.07676158531783962,
        "RENUSDT": -0.012526216568108206,
        "RUNEUSDT": -0.04809515134999534,
        "SFPUSDT": 0.04684240519240049,
        "SNXUSDT": -0.023349864264412504,
        "SXPUSDT": 0.06891073994611695,
        "THETAUSDT": 0.04696891176690905,
        "UNIUSDT": -0.062131180090378484,
        "XMRUSDT": 0.047704768842560606
      },
      "price_pnl": 0.0539786835825538,
      "short_pnl": -0.8390044719065861
    },
    "external": {
      "cost_pnl": -0.13503999999999586,
      "funding_pnl": -0.32332623400000726,
      "long_pnl": 1.2667514973729088,
      "net_pnl": 0.5559479470226061,
      "pnl_by_symbol": {
        "1000FLOKIUSDT": 0.09748712804028074,
        "1000LUNCUSDT": -0.039135953522302376,
        "1000PEPEUSDT": -0.12885430426997346,
        "1000SHIBUSDT": 0.19881612128996462,
        "1000XUSDT": 0.03642548179290354,
        "1INCHUSDT": -0.015406013095680494,
        "AAVEUSDT": -0.03589503529038726,
        "ACHUSDT": 0.09331680396800708,
        "ADAUSDT": 0.050276219843480116,
        "AGIXUSDT": -0.0037275873037433634,
        "AGLDUSDT": 0.013480449641580296,
        "AI16ZUSDT": 0.14090526541966242,
        "AIUSDT": 0.006395966950662024,
        "AKROUSDT": 0.015424990623092768,
        "ALGOUSDT": 0.10046644190166161,
        "ALPACAUSDT": -0.5324427125923845,
        "ALPHAUSDT": 0.1086157977728337,
        "AMBUSDT": 0.05875107309817554,
        "ANTUSDT": -0.003145531841424587,
        "APEUSDT": 0.10566554501631623,
        "APTUSDT": -0.07369747725835524,
        "ARBUSDT": -0.04977959260675576,
        "ARKMUSDT": 0.010975909001255025,
        "ARPAUSDT": 0.06648786116229422,
        "ARUSDT": -0.06821067228839617,
        "ATAUSDT": 0.030707988670970385,
        "ATOMUSDT": -0.016280027756380808,
        "AUDIOUSDT": -0.01545278421595402,
        "AVAXUSDT": -0.06490824720156467,
        "BAKEUSDT": 0.14302839140278995,
        "BANDUSDT": 0.014079662631138893,
        "BATUSDT": -0.041973289431357634,
        "BCHUSDT": 0.08129309251406618,
        "BELUSDT": -0.014567038508696981,
        "BIDUSDT": -0.04620308508709932,
        "BLZUSDT": 0.021636572168442423,
        "BNBUSDT": 0.30883395103499955,
        "BONDUSDT": 0.009564013394593262,
        "BSWUSDT": -0.013786994104353386,
        "BTCUSDT": 0.19838769530347877,
        "BTTUSDT": 0.016346270938010842,
        "BZRXUSDT": 0.03446368152499572,
        "C98USDT": 0.028914057145810582,
        "CELOUSDT": 0.006914952385070969,
        "CELRUSDT": 0.03234466460957166,
        "CFXUSDT": -0.03210759234483616,
        "CHRUSDT": -0.050481218431303335,
        "CHZUSDT": -0.15530673904092196,
        "CKBUSDT": -0.04181918250632044,
        "COMPUSDT": 0.005533258189918202,
        "COTIUSDT": -0.10627394925749005,
        "CRVUSDT": -0.05698929915103386,
        "CTSIUSDT": 0.036618765471700565,
        "DARUSDT": 0.009936090177671305,
        "DASHUSDT": 0.05531129547756374,
        "DEFIUSDT": 0.011640899314003006,
        "DEGOUSDT": 0.02288877142273332,
        "DENTUSDT": 6.710601781104945e-05,
        "DODOUSDT": -0.03542201423758562,
        "DOGEUSDT": -0.1808132049094542,
        "DOTUSDT": -0.00929194681045006,
        "DYDXUSDT": -0.10139866177872724,
        "EDUUSDT": 0.04729293235149564,
        "EGLDUSDT": -0.0076671217996364584,
        "ENSUSDT": 0.11074713064122926,
        "EOSUSDT": 0.19659260315611485,
        "ETCUSDT": 0.25482943233141264,
        "ETHUSDT": 0.1347407882044356,
        "FETUSDT": -0.03387764449827253,
        "FISUSDT": 0.03395493217238651,
        "FOOTBALLUSDT": -0.005212820600553071,
        "FRONTUSDT": -0.027255178533424074,
        "FUNUSDT": -0.14219662560252394,
        "FXSUSDT": 0.00699876482597297,
        "GALAUSDT": -0.006538052666291592,
        "GALUSDT": 0.022243268348911636,
        "HFTUSDT": 0.02801661313630852,
        "HIFIUSDT": 0.07459328561202419,
        "HIGHUSDT": 0.09063112640244168,
        "HIPPOUSDT": -0.0003373089135014877,
        "HNTUSDT": -0.07987176196013912,
        "HOOKUSDT": 0.02849142007644352,
        "INJUSDT": -0.05281062136831199,
        "IOSTUSDT": -0.061912992714150905,
        "IOTXUSDT": -0.07837724696291334,
        "JASMYUSDT": 0.058374590561102856,
        "JOEUSDT": 0.0032363327526259917,
        "KEEPUSDT": 0.0520676637649428,
        "KLAYUSDT": 0.04126633264630765,
        "LDOUSDT": -0.0855113909070934,
        "LENDUSDT": -0.00829989251853036,
        "LEVERUSDT": -0.0032889249889780335,
        "LINKUSDT": -0.008694252156639973,
        "LOKAUSDT": -0.03720152065252561,
        "LOOMUSDT": 0.010622187076959225,
        "LQTYUSDT": 0.05755807979293032,
        "MATICUSDT": -0.25117986313245516,
        "MAVUSDT": -0.01682523153619867,
        "MEMEFIUSDT": 0.03791294793156742,
        "MINAUSDT": -0.022664275421117208,
        "MTLUSDT": 0.03671399816247665,
        "MYROUSDT": 0.01190619996829982,
        "NEIROETHUSDT": -0.01893479559454147,
        "NEOUSDT": 0.054318854972373244,
        "NKNUSDT": -0.02931510003149123,
        "NTRNUSDT": 0.017501002146897995,
        "OMNIUSDT": 0.02284175829952253,
        "OMUSDT": 0.05453643707285342,
        "OPUSDT": -0.029049837118776123,
        "OXTUSDT": 0.008156128653963632,
        "PENDLEUSDT": 0.0041507070394729995,
        "PERPUSDT": 0.012930929803244983,
        "PHBUSDT": 0.027067981474195785,
        "PONKEUSDT": 0.023841988837116353,
        "QNTUSDT": -0.004847221038552146,
        "QTUMUSDT": 0.07676158531783962,
        "RDNTUSDT": -0.0032838225351849728,
        "REEFUSDT": -0.14808001216410407,
        "RENUSDT": -0.040373787303664734,
        "RNDRUSDT": -0.05114676391253196,
        "RUNEUSDT": -0.12278347288905718,
        "SEIUSDT": -0.06650677068513312,
        "SFPUSDT": 0.059884009280345436,
        "SNXUSDT": 0.030077128904467877,
        "SPELLUSDT": -0.05923503726468237,
        "SRMUSDT": -0.07117531470712957,
        "SSVUSDT": -0.014114301783644352,
        "STXUSDT": -0.04733180435796732,
        "SUIUSDT": -0.07747053842715138,
        "SXPUSDT": 0.1084312369522108,
        "THETAUSDT": 0.03154518986065556,
        "TOKENUSDT": 0.0345072935447031,
        "TOMOUSDT": -0.11729025974367853,
        "TONUSDT": -0.001129466122062581,
        "TROYUSDT": 0.03652898810685814,
        "TRUUSDT": 0.029258797023679572,
        "UNIUSDT": -0.04021737701089104,
        "UXLINKUSDT": 0.060928332697195095,
        "VIDTUSDT": -0.016477812541072005,
        "VINEUSDT": -0.05898156356097841,
        "VOXELUSDT": -0.044389683178857775,
        "WLDUSDT": 0.15595493633576704,
        "WOOUSDT": 0.017620513725336524,
        "XMRUSDT": 0.0596909181711524,
        "XVGUSDT": 0.016528672710603126,
        "XVSUSDT": -0.004711990412890849,
        "ZKJUSDT": 0.032031305018462804
      },
      "price_pnl": 1.0143141810226095,
      "short_pnl": -0.7108035503504545
    },
    "regime_external": {
      "BEAR": {
        "days": 329,
        "pnl": 0.18106967984967207,
        "return_sum_pct": 17.198454675287966
      },
      "BULL": {
        "days": 871,
        "pnl": 0.30065478197755763,
        "return_sum_pct": 29.64020857944837
      },
      "SIDEWAYS": {
        "days": 870,
        "pnl": 0.07422348519537621,
        "return_sum_pct": 31.39495247851216
      }
    }
  },
  "best_5pct": {
    "compound_return": -0.5672931258258573,
    "compound_return_pct": -56.729312582585735,
    "removed_count": 15,
    "removed_indices": [
      36,
      57,
      64,
      65,
      67,
      99,
      117,
      120,
      135,
      200,
      216,
      230,
      238,
      249,
      261
    ]
  },
  "bootstrap": {
    "primary_4_week": {
      "block_length_weeks": 4,
      "confidence": 0.95,
      "mean_weekly_return_ci95": [
        -0.0026685854352511006,
        0.007519357447163786
      ],
      "mean_weekly_return_ci95_lower": -0.0026685854352511006,
      "probability_mean_return_gt_zero": 0.8453,
      "rounds": 10000,
      "seed": 20260915,
      "weekly_sharpe_ci95": [
        -0.3656746102841212,
        1.2909376099143497
      ]
    },
    "sensitivity": {
      "13": {
        "block_length_weeks": 13,
        "confidence": 0.95,
        "mean_weekly_return_ci95": [
          -0.0015405860177633885,
          0.006189942970555886
        ],
        "mean_weekly_return_ci95_lower": -0.0015405860177633885,
        "probability_mean_return_gt_zero": 0.884,
        "rounds": 10000,
        "seed": 20260915,
        "weekly_sharpe_ci95": [
          -0.21179293693829526,
          1.0898967250951472
        ]
      },
      "8": {
        "block_length_weeks": 8,
        "confidence": 0.95,
        "mean_weekly_return_ci95": [
          -0.0017370297656541644,
          0.006385197912429431
        ],
        "mean_weekly_return_ci95_lower": -0.0017370297656541644,
        "probability_mean_return_gt_zero": 0.8791,
        "rounds": 10000,
        "seed": 20260915,
        "weekly_sharpe_ci95": [
          -0.24459017528471122,
          1.128254148653227
        ]
      }
    }
  },
  "bull_summary": {
    "cagr_pct": 3.167745569172631,
    "daily_observations": 731,
    "end": "2021-12-31",
    "max_drawdown_pct": 35.361147304302065,
    "pnl": 0.06435837258255384,
    "profit_factor_weekly": 1.1445739087729854,
    "start": "2020-01-01",
    "total_return_pct": 6.4358372582553836,
    "weekly_observations": 103,
    "weekly_sharpe": 0.263691866908941
  },
  "ci_status": "PENDING_PUSH",
  "control": {
    "COST_1X": {
      "complete": false,
      "cost_pnl": -0.1681599999999945,
      "discovery": {
        "cagr_pct": 75.83972451782242,
        "daily_observations": 365,
        "end": "2026-08-31",
        "max_drawdown_pct": 17.029951619998627,
        "pnl": 1.175799244162018,
        "profit_factor_weekly": 2.7249995220388135,
        "start": "2025-09-01",
        "total_return_pct": 75.56803210621383,
        "weekly_observations": 52,
        "weekly_sharpe": 2.614932742574203
      },
      "external": {
        "cagr_pct": 8.111169326073897,
        "daily_observations": 2070,
        "end": "2025-08-31",
        "max_drawdown_pct": 41.0957693282282,
        "pnl": 0.5559479470226061,
        "profit_factor_weekly": 1.2040066773830838,
        "start": "2020-01-01",
        "total_return_pct": 55.59479470226061,
        "weekly_observations": 295,
        "weekly_sharpe": 0.41163650354648046
      },
      "funding_pnl": -0.42459311800002486,
      "issues": [
        "DATA_INVALID: missing execution prices ANCUSDT"
      ],
      "long_pnl": 1.104810342332422,
      "price_pnl": 2.324500309184626,
      "rebalance_count": 302,
      "short_pnl": 0.6269368488519977,
      "spec_hash": "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678",
      "strategy_id": "XS-LOWVOL-V1-Control",
      "trade_count": 1056,
      "turnover_notional": 210.19999999999234
    },
    "COST_2X": {
      "complete": false,
      "cost_pnl": -0.336319999999989,
      "discovery": {
        "cagr_pct": 80.71167925965106,
        "daily_observations": 365,
        "end": "2026-08-31",
        "max_drawdown_pct": 18.449857720647156,
        "pnl": 1.1426792441620184,
        "profit_factor_weekly": 2.675489004268907,
        "start": "2025-09-01",
        "total_return_pct": 80.41894948623572,
        "weekly_observations": 52,
        "weekly_sharpe": 2.554913259733369
      },
      "external": {
        "cagr_pct": 6.393409497834246,
        "daily_observations": 2070,
        "end": "2025-08-31",
        "max_drawdown_pct": 44.04594354525746,
        "pnl": 0.4209079470226058,
        "profit_factor_weekly": 1.1785850106032167,
        "start": "2020-01-01",
        "total_return_pct": 42.09079470226058,
        "weekly_observations": 295,
        "weekly_sharpe": 0.36601527475044543
      },
      "funding_pnl": -0.42459311800002486,
      "issues": [
        "DATA_INVALID: missing execution prices ANCUSDT"
      ],
      "long_pnl": 1.046170342332486,
      "price_pnl": 2.324500309184626,
      "rebalance_count": 302,
      "short_pnl": 0.5174168488519142,
      "spec_hash": "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678",
      "strategy_id": "XS-LOWVOL-V1-Control",
      "trade_count": 1056,
      "turnover_notional": 210.19999999999234
    },
    "COST_3X": {
      "complete": false,
      "cost_pnl": -0.5044800000000147,
      "discovery": {
        "cagr_pct": 86.60740571441903,
        "daily_observations": 365,
        "end": "2026-08-31",
        "max_drawdown_pct": 20.0963726327954,
        "pnl": 1.109559244162019,
        "profit_factor_weekly": 2.6294764810886653,
        "start": "2025-09-01",
        "total_return_pct": 86.2887395810103,
        "weekly_observations": 52,
        "weekly_sharpe": 2.4963512626079254
      },
      "external": {
        "cagr_pct": 4.535485037496967,
        "daily_observations": 2070,
        "end": "2025-08-31",
        "max_drawdown_pct": 47.42843223161831,
        "pnl": 0.2858679470226049,
        "profit_factor_weekly": 1.1543449936272698,
        "start": "2020-01-01",
        "total_return_pct": 28.586794702260487,
        "weekly_observations": 295,
        "weekly_sharpe": 0.3209826491334138
      },
      "funding_pnl": -0.42459311800002486,
      "issues": [
        "DATA_INVALID: missing execution prices ANCUSDT"
      ],
      "long_pnl": 0.9875303423325273,
      "price_pnl": 2.324500309184626,
      "rebalance_count": 302,
      "short_pnl": 0.4078968488518988,
      "spec_hash": "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678",
      "strategy_id": "XS-LOWVOL-V1-Control",
      "trade_count": 1056,
      "turnover_notional": 210.19999999999234
    }
  },
  "control_sha256": "5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678",
  "control_strategy_id": "XS-LOWVOL-V1-Control",
  "data": {
    "archive_download_errors": [],
    "catalog_complete": true,
    "daily_bar_count": 636990,
    "first_available_date": "2020-01-01",
    "funding_event_count": 2629011,
    "last_available_date": "2026-08-31",
    "listing_page_count": 927,
    "number_of_ambiguous_symbols": 52,
    "number_of_delisted_symbols": 31,
    "number_of_symbols_discovered": 861,
    "number_of_usable_symbols": 362,
    "raw_file_count": 41682
  },
  "dataset_freeze_path": "backtest/m1_cache/normalized/XS_LOWVOL_M1_DATASET.pkl",
  "dataset_manifest_path": "research/m1/M1_DATASET_MANIFEST.json",
  "dataset_sha256": "f6b7f03897366ad66d9f1af10201091dbf3f406728ad22121a313ecd0321f755",
  "decision": "M1 FAIL",
  "failed_gates": [
    "G0_data_integrity",
    "G3_weekly_sharpe",
    "G4_drawdown",
    "G6_block_bootstrap",
    "G7_best_5pct_removed",
    "G11_single_year_catastrophe",
    "G12_bull_survival"
  ],
  "first_valid_signal_date": "2020-08-14",
  "formal_run": true,
  "gates": {
    "G0_data_integrity": false,
    "G10_multi_year_breadth": true,
    "G11_single_year_catastrophe": false,
    "G12_bull_survival": false,
    "G1_external_return": true,
    "G2_cost_2x": true,
    "G3_weekly_sharpe": false,
    "G4_drawdown": false,
    "G5_profit_factor": true,
    "G6_block_bootstrap": false,
    "G7_best_5pct_removed": false,
    "G8_leave_one_out": true,
    "G9_symbol_concentration": true
  },
  "http_methods": [
    "GET"
  ],
  "leave_one_out": {
    "median_return_pct": 55.59479470226061,
    "min_return_pct": 34.0777151797879,
    "negative_runs": 0,
    "positive_runs": 362,
    "runs": [
      {
        "removed_symbol": "1000BTTCUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "1000FLOKIUSDT",
        "total_return_pct": 50.8824524615554
      },
      {
        "removed_symbol": "1000LUNCUSDT",
        "total_return_pct": 53.7992236196355
      },
      {
        "removed_symbol": "1000PEPEUSDT",
        "total_return_pct": 53.84187051312963
      },
      {
        "removed_symbol": "1000SHIBUSDT",
        "total_return_pct": 49.834921599717184
      },
      {
        "removed_symbol": "1000WHYUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "1000XUSDT",
        "total_return_pct": 52.55838841958784
      },
      {
        "removed_symbol": "1INCHUSDT",
        "total_return_pct": 40.29954511159208
      },
      {
        "removed_symbol": "2ZUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "42USDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "A2ZUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "AAOIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "AAVEUSDT",
        "total_return_pct": 61.563385906269644
      },
      {
        "removed_symbol": "ACHUSDT",
        "total_return_pct": 49.948917480707024
      },
      {
        "removed_symbol": "ADAUSDT",
        "total_return_pct": 47.58030466613068
      },
      {
        "removed_symbol": "ADBEUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "AGIXUSDT",
        "total_return_pct": 50.0440236408273
      },
      {
        "removed_symbol": "AGLDUSDT",
        "total_return_pct": 53.222285132245894
      },
      {
        "removed_symbol": "AI16ZUSDT",
        "total_return_pct": 46.90107098010958
      },
      {
        "removed_symbol": "AIUSDT",
        "total_return_pct": 47.587970959093575
      },
      {
        "removed_symbol": "AKROUSDT",
        "total_return_pct": 53.082017145201
      },
      {
        "removed_symbol": "ALABUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ALGOUSDT",
        "total_return_pct": 44.111956417871625
      },
      {
        "removed_symbol": "ALLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ALPACAUSDT",
        "total_return_pct": 110.95793683923421
      },
      {
        "removed_symbol": "ALPHAUSDT",
        "total_return_pct": 42.53322175461807
      },
      {
        "removed_symbol": "AMBUSDT",
        "total_return_pct": 51.550376290443765
      },
      {
        "removed_symbol": "AMDUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "AMZNUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ANCUSDT",
        "total_return_pct": 57.42580739071239
      },
      {
        "removed_symbol": "ANTHROPICUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ANTUSDT",
        "total_return_pct": 51.17095090087091
      },
      {
        "removed_symbol": "APEUSDT",
        "total_return_pct": 45.00270990217592
      },
      {
        "removed_symbol": "APPUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "APTUSDT",
        "total_return_pct": 57.73708126259041
      },
      {
        "removed_symbol": "ARBUSDT",
        "total_return_pct": 63.7414596360095
      },
      {
        "removed_symbol": "ARKMUSDT",
        "total_return_pct": 51.883985044277004
      },
      {
        "removed_symbol": "ARMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ARPAUSDT",
        "total_return_pct": 54.686094195708264
      },
      {
        "removed_symbol": "ARUSDT",
        "total_return_pct": 58.14187357911902
      },
      {
        "removed_symbol": "ARXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ASMLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ASTRUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ASTSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ATAUSDT",
        "total_return_pct": 58.283227544489336
      },
      {
        "removed_symbol": "ATOMUSDT",
        "total_return_pct": 61.29779412312759
      },
      {
        "removed_symbol": "AUDIOUSDT",
        "total_return_pct": 61.454300598215504
      },
      {
        "removed_symbol": "AVAXUSDT",
        "total_return_pct": 53.407388986247064
      },
      {
        "removed_symbol": "AXTIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "B3USDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BADGERUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BAKEUSDT",
        "total_return_pct": 55.744343759330974
      },
      {
        "removed_symbol": "BALUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BANDUSDT",
        "total_return_pct": 51.64960848197464
      },
      {
        "removed_symbol": "BATUSDT",
        "total_return_pct": 65.30789350971263
      },
      {
        "removed_symbol": "BBXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BCHUSDT",
        "total_return_pct": 55.78248488373059
      },
      {
        "removed_symbol": "BDXNUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BELUSDT",
        "total_return_pct": 47.05149785358529
      },
      {
        "removed_symbol": "BEUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BICOUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BIDUSDT",
        "total_return_pct": 59.56744308520059
      },
      {
        "removed_symbol": "BITOUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BLUEBIRDUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BLZUSDT",
        "total_return_pct": 49.59110039508785
      },
      {
        "removed_symbol": "BMNRUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BNBUSDT",
        "total_return_pct": 48.70158165928382
      },
      {
        "removed_symbol": "BNCUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BOBUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BONDUSDT",
        "total_return_pct": 53.35390534380793
      },
      {
        "removed_symbol": "BOTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BRKBUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BSPUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BSWUSDT",
        "total_return_pct": 55.71379489664254
      },
      {
        "removed_symbol": "BTCDOMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BTCUSDT",
        "total_return_pct": 57.10144730550844
      },
      {
        "removed_symbol": "BTSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "BTTUSDT",
        "total_return_pct": 53.551992700427476
      },
      {
        "removed_symbol": "BZRXUSDT",
        "total_return_pct": 51.74032888079005
      },
      {
        "removed_symbol": "C98USDT",
        "total_return_pct": 50.20368274740823
      },
      {
        "removed_symbol": "CAPUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CBRSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CCUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CELOUSDT",
        "total_return_pct": 54.461917748970045
      },
      {
        "removed_symbol": "CELRUSDT",
        "total_return_pct": 69.81004498762529
      },
      {
        "removed_symbol": "CFXUSDT",
        "total_return_pct": 58.347707944456296
      },
      {
        "removed_symbol": "CHESSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CHRUSDT",
        "total_return_pct": 65.52415038586345
      },
      {
        "removed_symbol": "CHZUSDT",
        "total_return_pct": 73.72325005695654
      },
      {
        "removed_symbol": "CIENUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CKBUSDT",
        "total_return_pct": 48.44100440691346
      },
      {
        "removed_symbol": "COCOSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "COHRUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "COINUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "COMBOUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "COMMONUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "COMPUSDT",
        "total_return_pct": 59.15937435128047
      },
      {
        "removed_symbol": "COSTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "COTIUSDT",
        "total_return_pct": 48.476494798812574
      },
      {
        "removed_symbol": "CRCLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CRDOUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CRVUSDT",
        "total_return_pct": 44.786566963455286
      },
      {
        "removed_symbol": "CRWVUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CSOPSAMSUNG2LUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CSOPSKHYNIX2LUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CTSIUSDT",
        "total_return_pct": 52.12316202805643
      },
      {
        "removed_symbol": "CUDISUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "CXMTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DAMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DARUSDT",
        "total_return_pct": 53.263902425845714
      },
      {
        "removed_symbol": "DASHUSDT",
        "total_return_pct": 55.49918634511717
      },
      {
        "removed_symbol": "DATAIPUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DEFIUSDT",
        "total_return_pct": 54.411133354500606
      },
      {
        "removed_symbol": "DEGENUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DEGOUSDT",
        "total_return_pct": 54.557309850148684
      },
      {
        "removed_symbol": "DENTUSDT",
        "total_return_pct": 40.180826868690914
      },
      {
        "removed_symbol": "DFUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DGBUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DJTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DKNGUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DMCUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DODOUSDT",
        "total_return_pct": 60.43459442708867
      },
      {
        "removed_symbol": "DOGEUSDT",
        "total_return_pct": 64.17018754709387
      },
      {
        "removed_symbol": "DOSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DOTECOUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DOTUSDT",
        "total_return_pct": 38.12946287979955
      },
      {
        "removed_symbol": "DRAMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "DYDXUSDT",
        "total_return_pct": 54.17306595665021
      },
      {
        "removed_symbol": "EDUUSDT",
        "total_return_pct": 49.9917374160805
      },
      {
        "removed_symbol": "EGLDUSDT",
        "total_return_pct": 52.28603670591818
      },
      {
        "removed_symbol": "ENSUSDT",
        "total_return_pct": 38.525306592049446
      },
      {
        "removed_symbol": "EOSUSDT",
        "total_return_pct": 63.4723390404571
      },
      {
        "removed_symbol": "EPTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ETCUSDT",
        "total_return_pct": 61.111123548091896
      },
      {
        "removed_symbol": "ETHUSDT",
        "total_return_pct": 61.57574138972058
      },
      {
        "removed_symbol": "EWJUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "EWTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "EWYUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "EWZUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "FETUSDT",
        "total_return_pct": 56.31122807431619
      },
      {
        "removed_symbol": "FIOUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "FISUSDT",
        "total_return_pct": 55.25510115009518
      },
      {
        "removed_symbol": "FLEXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "FLNCUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "FLUIDUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "FOLKSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "FOOTBALLUSDT",
        "total_return_pct": 54.971897705487734
      },
      {
        "removed_symbol": "FORTHUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "FRONTUSDT",
        "total_return_pct": 48.934689986147674
      },
      {
        "removed_symbol": "FUNUSDT",
        "total_return_pct": 69.52356161228114
      },
      {
        "removed_symbol": "FWDIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "FXSUSDT",
        "total_return_pct": 53.04303850089438
      },
      {
        "removed_symbol": "GALAUSDT",
        "total_return_pct": 59.048422833705146
      },
      {
        "removed_symbol": "GALUSDT",
        "total_return_pct": 55.43773587670566
      },
      {
        "removed_symbol": "GDXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "GEVUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "GHSTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "GIGADEVUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "GIGGLEUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "GLMRUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "GMEUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "GMXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "GRAMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "GRVTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "HANMIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "HFTUSDT",
        "total_return_pct": 51.39488532059211
      },
      {
        "removed_symbol": "HIFIUSDT",
        "total_return_pct": 48.785015149823074
      },
      {
        "removed_symbol": "HIGHUSDT",
        "total_return_pct": 41.28795524341853
      },
      {
        "removed_symbol": "HIMSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "HIPPOUSDT",
        "total_return_pct": 55.146796855471614
      },
      {
        "removed_symbol": "HK0700USDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "HK1810USDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "HNTUSDT",
        "total_return_pct": 57.77975942561184
      },
      {
        "removed_symbol": "HOODUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "HOOKUSDT",
        "total_return_pct": 51.71356379300407
      },
      {
        "removed_symbol": "IDEXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "INJUSDT",
        "total_return_pct": 58.33534345919078
      },
      {
        "removed_symbol": "INTCUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "INTWUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "IONQUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "IOSTUSDT",
        "total_return_pct": 52.7968789828265
      },
      {
        "removed_symbol": "IOTXUSDT",
        "total_return_pct": 68.76757606929762
      },
      {
        "removed_symbol": "IRENUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "IRUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "IRYSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "IWMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "JASMYUSDT",
        "total_return_pct": 40.64589164065309
      },
      {
        "removed_symbol": "JCTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "JOEUSDT",
        "total_return_pct": 55.21377097673541
      },
      {
        "removed_symbol": "KDAUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "KEEPUSDT",
        "total_return_pct": 58.20658172798539
      },
      {
        "removed_symbol": "KEYUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "KLAYUSDT",
        "total_return_pct": 46.1041125972435
      },
      {
        "removed_symbol": "KODEX200USDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "KOUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "KSTRUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "KUAISHOUUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "LDOUSDT",
        "total_return_pct": 64.45811735586256
      },
      {
        "removed_symbol": "LENDUSDT",
        "total_return_pct": 57.17828879488094
      },
      {
        "removed_symbol": "LEVERUSDT",
        "total_return_pct": 58.05812613221586
      },
      {
        "removed_symbol": "LGELECTRONICSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "LINKUSDT",
        "total_return_pct": 63.38973188910266
      },
      {
        "removed_symbol": "LITEUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "LOKAUSDT",
        "total_return_pct": 59.37221449297301
      },
      {
        "removed_symbol": "LOOMUSDT",
        "total_return_pct": 58.543697080565174
      },
      {
        "removed_symbol": "LQTYUSDT",
        "total_return_pct": 43.993061788966536
      },
      {
        "removed_symbol": "LRCXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "LYTEUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MARAUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MATICUSDT",
        "total_return_pct": 84.08473368917397
      },
      {
        "removed_symbol": "MAVUSDT",
        "total_return_pct": 53.6760470270945
      },
      {
        "removed_symbol": "MBLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MBOXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MDTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MEITUANUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MEMEFIUSDT",
        "total_return_pct": 53.46001401357221
      },
      {
        "removed_symbol": "MILKUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MINAUSDT",
        "total_return_pct": 51.53931817917816
      },
      {
        "removed_symbol": "MINIMAXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MLNUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MRKUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MRNAUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MSTRUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MTLUSDT",
        "total_return_pct": 64.8478480308028
      },
      {
        "removed_symbol": "MUUUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MVLLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "MYROUSDT",
        "total_return_pct": 49.24092603864305
      },
      {
        "removed_symbol": "NAVERUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "NBISUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "NEIROETHUSDT",
        "total_return_pct": 54.27187398976672
      },
      {
        "removed_symbol": "NEOUSDT",
        "total_return_pct": 53.076554997753966
      },
      {
        "removed_symbol": "NETUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "NFLXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "NKNUSDT",
        "total_return_pct": 59.52458991470672
      },
      {
        "removed_symbol": "NOKUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "NOWUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "NTRNUSDT",
        "total_return_pct": 52.2893573205367
      },
      {
        "removed_symbol": "NULSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "NUUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "OBOLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "OLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "OMNIUSDT",
        "total_return_pct": 53.3859564505377
      },
      {
        "removed_symbol": "OMUSDT",
        "total_return_pct": 61.64930343224315
      },
      {
        "removed_symbol": "ONDSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ONUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "OPENAIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "OPUSDT",
        "total_return_pct": 53.88080794814858
      },
      {
        "removed_symbol": "ORBSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "OUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "OXTUSDT",
        "total_return_pct": 55.19309422564
      },
      {
        "removed_symbol": "PANWUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "PAYPUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "PDDUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "PENDLEUSDT",
        "total_return_pct": 43.85348781457621
      },
      {
        "removed_symbol": "PENGUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "PERPUSDT",
        "total_return_pct": 54.39704886794197
      },
      {
        "removed_symbol": "PHBUSDT",
        "total_return_pct": 56.54853132915627
      },
      {
        "removed_symbol": "PLTRUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "PONKEUSDT",
        "total_return_pct": 55.09326002468262
      },
      {
        "removed_symbol": "POPMARTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "PORT3USDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "PUFFERUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "PYPLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "QNTUSDT",
        "total_return_pct": 55.016523465100754
      },
      {
        "removed_symbol": "QNTXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "QTUMUSDT",
        "total_return_pct": 54.07099430348487
      },
      {
        "removed_symbol": "QUICKUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "RADUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "RAMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "RAYUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "RDDTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "RDNTUSDT",
        "total_return_pct": 53.825998124445064
      },
      {
        "removed_symbol": "REEFUSDT",
        "total_return_pct": 63.87170072303685
      },
      {
        "removed_symbol": "REIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "RENUSDT",
        "total_return_pct": 49.595653232230205
      },
      {
        "removed_symbol": "RIVNUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "RKLBUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "RLSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "RNDRUSDT",
        "total_return_pct": 60.4857326282267
      },
      {
        "removed_symbol": "RUNEUSDT",
        "total_return_pct": 69.44879722710282
      },
      {
        "removed_symbol": "RVVUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SAMSUNGEMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SCUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SEIUSDT",
        "total_return_pct": 56.29830117827193
      },
      {
        "removed_symbol": "SFPUSDT",
        "total_return_pct": 56.75917681706344
      },
      {
        "removed_symbol": "SHAZUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SHOPUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SKATEUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SKDDUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SKHYUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SKUUUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SLERFUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SMCIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SMHUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SNDKUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SNOWUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SNTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SNXUSDT",
        "total_return_pct": 62.6511593825823
      },
      {
        "removed_symbol": "SNXXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SOFIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SONYUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SOXLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SOXSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SPCXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SPELLUSDT",
        "total_return_pct": 56.934205025333284
      },
      {
        "removed_symbol": "SQQQUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SRMUSDT",
        "total_return_pct": 62.100198869487855
      },
      {
        "removed_symbol": "SSVUSDT",
        "total_return_pct": 56.709544492512705
      },
      {
        "removed_symbol": "STBLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "STPTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "STRAXUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "STXUSDT",
        "total_return_pct": 56.658550735352485
      },
      {
        "removed_symbol": "SUIUSDT",
        "total_return_pct": 46.85746017204697
      },
      {
        "removed_symbol": "SWELLUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "SXPUSDT",
        "total_return_pct": 51.989245991684996
      },
      {
        "removed_symbol": "SYSUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "TANSSIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "TBTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "TEMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "TENCENTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "TERUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "THETAUSDT",
        "total_return_pct": 55.103345779689164
      },
      {
        "removed_symbol": "TMFUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "TOKENUSDT",
        "total_return_pct": 44.524511909465446
      },
      {
        "removed_symbol": "TOMOUSDT",
        "total_return_pct": 79.14655691057865
      },
      {
        "removed_symbol": "TONUSDT",
        "total_return_pct": 67.0301261859751
      },
      {
        "removed_symbol": "TQQQUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "TROYUSDT",
        "total_return_pct": 55.457215947368056
      },
      {
        "removed_symbol": "TRUUSDT",
        "total_return_pct": 53.958573119576144
      },
      {
        "removed_symbol": "TSLAUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "TTWOUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "TZAUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "UAIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "UBERUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "UNITREEUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "UNIUSDT",
        "total_return_pct": 58.584457554706695
      },
      {
        "removed_symbol": "URNMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "USARUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "USDCUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "UVXYUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "UXLINKUSDT",
        "total_return_pct": 46.0945315780843
      },
      {
        "removed_symbol": "VFYUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "VIDTUSDT",
        "total_return_pct": 55.40750381347341
      },
      {
        "removed_symbol": "VINEUSDT",
        "total_return_pct": 61.794862067441535
      },
      {
        "removed_symbol": "VOXELUSDT",
        "total_return_pct": 57.99283831587581
      },
      {
        "removed_symbol": "VRTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "VSTUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "WLDUSDT",
        "total_return_pct": 34.0777151797879
      },
      {
        "removed_symbol": "WOOUSDT",
        "total_return_pct": 53.16176239296861
      },
      {
        "removed_symbol": "XANUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "XBIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "XCNUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "XMRUSDT",
        "total_return_pct": 52.358396361786276
      },
      {
        "removed_symbol": "XVGUSDT",
        "total_return_pct": 58.25201090820009
      },
      {
        "removed_symbol": "XVSUSDT",
        "total_return_pct": 54.5221338361205
      },
      {
        "removed_symbol": "YALAUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ZHIPUUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ZHONGJIUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ZKJUSDT",
        "total_return_pct": 51.74400407464441
      },
      {
        "removed_symbol": "ZMUSDT",
        "total_return_pct": 55.59479470226061
      },
      {
        "removed_symbol": "ZRCUSDT",
        "total_return_pct": 55.59479470226061
      }
    ],
    "total_runs": 362,
    "worst_removed_symbol": "WLDUSDT"
  },
  "live_trading": false,
  "missing_gate_metrics": [],
  "no_signal_day_count": 41,
  "normalized_dataset_sha256": "6adf9f06cba3a9de377ed59f69f422320b1524f635512271ebee60516896c387",
  "primary_decision_strategy": "XS-LOWVOL-V1-Control",
  "protocol_id": "XS-LOWVOL-M1-PIT-V1",
  "protocol_sha256": "607d262248a4ef0d0f968c1db61f9f09ffd8ee19a247a46571e131caaa8ea03d",
  "quality": {
    "all_normalized": {
      "affected_symbols": {
        "0GUSDT": 1,
        "1000000BOBUSDT": 1,
        "1000000MOGUSDT": 1,
        "1000BONKUSDT": 1,
        "1000CATUSDT": 1,
        "1000CHEEMSUSDT": 1,
        "1000RATSUSDT": 1,
        "1000SATSUSDT": 1,
        "1000XECUSDT": 1,
        "1MBABYDOGEUSDT": 1,
        "4USDT": 1,
        "AAPLUSDT": 5,
        "ACEUSDT": 2,
        "ACTUSDT": 1,
        "ACUUSDT": 1,
        "ACXUSDT": 1,
        "AERGOUSDT": 1,
        "AEROUSDT": 1,
        "AEVOUSDT": 1,
        "AGTUSDT": 1,
        "AIAUSDT": 3,
        "AIGENSYNUSDT": 1,
        "AINUSDT": 1,
        "AIOTUSDT": 1,
        "AIOUSDT": 1,
        "AIXBTUSDT": 1,
        "AKEUSDT": 1,
        "AKTUSDT": 1,
        "ALCHUSDT": 1,
        "ALICEUSDT": 1,
        "ALLOUSDT": 1,
        "ALPINEUSDT": 1,
        "ALTUSDT": 1,
        "AMATUSDT": 2,
        "ANIMEUSDT": 2,
        "ANKRUSDT": 2,
        "API3USDT": 2,
        "APRUSDT": 1,
        "ARCUSDT": 1,
        "ARIAUSDT": 2,
        "ARKUSDT": 2,
        "ASRUSDT": 1,
        "ASTERUSDT": 1,
        "ATHUSDT": 1,
        "ATUSDT": 1,
        "AUCTIONUSDT": 2,
        "AUSDT": 1,
        "AVAAIUSDT": 1,
        "AVAUSDT": 1,
        "AVGOUSDT": 3,
        "AVNTUSDT": 1,
        "AWEUSDT": 1,
        "AXLUSDT": 1,
        "AXSUSDT": 1,
        "AZTECUSDT": 2,
        "B2USDT": 1,
        "BABAUSDT": 3,
        "BABYUSDT": 1,
        "BANANAS31USDT": 1,
        "BANANAUSDT": 1,
        "BANKUSDT": 2,
        "BANUSDT": 1,
        "BARDUSDT": 1,
        "BASEDUSDT": 1,
        "BASUSDT": 1,
        "BBUSDT": 1,
        "BEAMXUSDT": 1,
        "BEATUSDT": 1,
        "BERAUSDT": 1,
        "BIGTIMEUSDT": 1,
        "BILLUSDT": 1,
        "BIOUSDT": 2,
        "BIRBUSDT": 1,
        "BLESSUSDT": 1,
        "BLUAIUSDT": 1,
        "BLURUSDT": 2,
        "BMTUSDT": 1,
        "BNTUSDT": 1,
        "BNXUSDT": 27,
        "BOMEUSDT": 1,
        "BRETTUSDT": 1,
        "BREVUSDT": 1,
        "BROCCOLI714USDT": 1,
        "BROCCOLIF3BUSDT": 1,
        "BRUSDT": 1,
        "BSBUSDT": 1,
        "BSVUSDT": 1,
        "BTCSTUSDT": 1,
        "BTRUSDT": 1,
        "BTWUSDT": 1,
        "BULLAUSDT": 3,
        "BUSDT": 1,
        "BXUSDT": 2,
        "BZUSDT": 1,
        "CAKEUSDT": 1,
        "CARVUSDT": 1,
        "CATIUSDT": 1,
        "CATUSDT": 10,
        "CETUSUSDT": 1,
        "CFGUSDT": 1,
        "CGPTUSDT": 1,
        "CHILLGUYUSDT": 1,
        "CHIPUSDT": 1,
        "CLANKERUSDT": 1,
        "CLOUSDT": 1,
        "CLUSDT": 1,
        "COAIUSDT": 2,
        "COLLECTUSDT": 1,
        "COOKIEUSDT": 1,
        "COPPERUSDT": 1,
        "COSUSDT": 2,
        "COWUSDT": 1,
        "CRMUSDT": 3,
        "CROSSUSDT": 1,
        "CRWDUSDT": 1,
        "CSCOUSDT": 2,
        "CTKUSDT": 1,
        "CTRUSDT": 1,
        "CUSDT": 2,
        "CVCUSDT": 2,
        "CVXUSDT": 2,
        "CYBERUSDT": 1,
        "CYSUSDT": 1,
        "DEEPUSDT": 1,
        "DELLUSDT": 2,
        "DEXEUSDT": 2,
        "DIAUSDT": 1,
        "DISUSDT": 2,
        "DODOXUSDT": 1,
        "DOGSUSDT": 1,
        "DOLOUSDT": 1,
        "DOODUSDT": 2,
        "DRIFTUSDT": 1,
        "DUSKUSDT": 1,
        "DYMUSDT": 1,
        "EBAYUSDT": 2,
        "EDENUSDT": 1,
        "EDGEUSDT": 1,
        "EIGENUSDT": 1,
        "ELSAUSDT": 2,
        "ENAUSDT": 1,
        "ENJUSDT": 3,
        "ENSOUSDT": 1,
        "EPICUSDT": 2,
        "ERAUSDT": 1,
        "ESPORTSUSDT": 1,
        "ESPUSDT": 2,
        "ETHFIUSDT": 1,
        "ETHWUSDT": 1,
        "EULUSDT": 1,
        "EVAAUSDT": 1,
        "FARTCOINUSDT": 1,
        "FFUSDT": 1,
        "FHEUSDT": 1,
        "FIDAUSDT": 2,
        "FIGHTUSDT": 1,
        "FILUSDT": 6,
        "FLMUSDT": 7,
        "FLOCKUSDT": 1,
        "FLOWUSDT": 8,
        "FLUXUSDT": 1,
        "FOGOUSDT": 1,
        "FORMUSDT": 1,
        "FRAXUSDT": 1,
        "FTMUSDT": 6,
        "FTTUSDT": 2,
        "FUSDT": 1,
        "GASUSDT": 1,
        "GENIUSUSDT": 1,
        "GLMUSDT": 1,
        "GLWUSDT": 3,
        "GMTUSDT": 4,
        "GOATUSDT": 1,
        "GOOGLUSDT": 3,
        "GPSUSDT": 2,
        "GRASSUSDT": 1,
        "GRIFFAINUSDT": 1,
        "GRTUSDT": 6,
        "GSUSDT": 1,
        "GTCUSDT": 6,
        "GUAUSDT": 2,
        "GUNUSDT": 1,
        "GUSDT": 2,
        "GWEIUSDT": 1,
        "HAEDALUSDT": 1,
        "HANAUSDT": 1,
        "HBARUSDT": 6,
        "HDUSDT": 3,
        "HEIUSDT": 2,
        "HEMIUSDT": 1,
        "HIVEUSDT": 2,
        "HMSTRUSDT": 1,
        "HOLOUSDT": 1,
        "HOMEUSDT": 1,
        "HOTUSDT": 6,
        "HPEUSDT": 3,
        "HUMAUSDT": 1,
        "HUSDT": 2,
        "HYPERUSDT": 1,
        "HYPEUSDT": 1,
        "HYUNDAIUSDT": 2,
        "IBMUSDT": 2,
        "ICNTUSDT": 1,
        "ICPUSDT": 27,
        "ICXUSDT": 1,
        "IDOLUSDT": 1,
        "IDUSDT": 1,
        "ILVUSDT": 1,
        "IMXUSDT": 7,
        "INITUSDT": 2,
        "INUSDT": 2,
        "INXUSDT": 1,
        "IOTAUSDT": 6,
        "IOUSDT": 1,
        "IPUSDT": 2,
        "JELLYJELLYUSDT": 2,
        "JPMUSDT": 2,
        "JSTUSDT": 1,
        "JTOUSDT": 1,
        "JUPUSDT": 1,
        "KAIAUSDT": 1,
        "KAITOUSDT": 1,
        "KASUSDT": 1,
        "KATUSDT": 1,
        "KAVAUSDT": 2,
        "KERNELUSDT": 1,
        "KGENUSDT": 1,
        "KITEUSDT": 1,
        "KLACUSDT": 2,
        "KMNOUSDT": 1,
        "KNCUSDT": 6,
        "KOMAUSDT": 1,
        "KORUUSDT": 1,
        "KSMUSDT": 6,
        "LABUSDT": 2,
        "LAUSDT": 2,
        "LAYERUSDT": 2,
        "LIGHTUSDT": 1,
        "LINAUSDT": 6,
        "LINEAUSDT": 1,
        "LISTAUSDT": 1,
        "LITUSDT": 8,
        "LLYUSDT": 2,
        "LPTUSDT": 1,
        "LRCUSDT": 6,
        "LSKUSDT": 1,
        "LTCUSDT": 6,
        "LUMIAUSDT": 1,
        "LUNA2USDT": 1,
        "LUNAUSDT": 1577,
        "LYNUSDT": 2,
        "MAGICUSDT": 1,
        "MAGMAUSDT": 1,
        "MANAUSDT": 6,
        "MANTAUSDT": 1,
        "MANTRAUSDT": 1,
        "MASKUSDT": 7,
        "MAVIAUSDT": 1,
        "MEGAUSDT": 1,
        "MELANIAUSDT": 1,
        "MEMEUSDT": 1,
        "MERLUSDT": 2,
        "METAUSDT": 3,
        "METISUSDT": 1,
        "METUSDT": 1,
        "MEUSDT": 3,
        "MEWUSDT": 1,
        "MIRAUSDT": 1,
        "MITOUSDT": 1,
        "MKRUSDT": 6,
        "MMTUSDT": 1,
        "MOCAUSDT": 1,
        "MONUSDT": 1,
        "MOODENGUSDT": 1,
        "MORPHOUSDT": 1,
        "MOVEUSDT": 1,
        "MOVRUSDT": 1,
        "MRVLUSDT": 2,
        "MSFTUSDT": 5,
        "MUBARAKUSDT": 1,
        "MUSDT": 1,
        "MUUSDT": 2,
        "MYXUSDT": 1,
        "NAORISUSDT": 1,
        "NATGASUSDT": 1,
        "NEARUSDT": 6,
        "NEIROUSDT": 1,
        "NEWTUSDT": 1,
        "NFPUSDT": 1,
        "NIGHTUSDT": 1,
        "NILUSDT": 1,
        "NMRUSDT": 1,
        "NOMUSDT": 2,
        "NOTUSDT": 1,
        "NVDAUSDT": 3,
        "NVOUSDT": 2,
        "NXPCUSDT": 1,
        "OCEANUSDT": 6,
        "OGNUSDT": 7,
        "OGUSDT": 1,
        "OMGUSDT": 7,
        "ONDOUSDT": 1,
        "ONEUSDT": 6,
        "ONGUSDT": 2,
        "ONTUSDT": 3,
        "OPENUSDT": 1,
        "OPGUSDT": 1,
        "OPNUSDT": 1,
        "ORCAUSDT": 1,
        "ORCLUSDT": 2,
        "ORDERUSDT": 1,
        "ORDIUSDT": 1,
        "PARTIUSDT": 2,
        "PAXGUSDT": 1,
        "PENGUUSDT": 1,
        "PEOPLEUSDT": 6,
        "PHAROSUSDT": 1,
        "PHAUSDT": 1,
        "PIEVERSEUSDT": 1,
        "PIPPINUSDT": 4,
        "PIXELUSDT": 1,
        "PLAYUSDT": 2,
        "PLUMEUSDT": 2,
        "PNUTUSDT": 1,
        "POLUSDT": 1,
        "POLYXUSDT": 1,
        "POPCATUSDT": 1,
        "PORTALUSDT": 1,
        "POWERUSDT": 1,
        "POWRUSDT": 1,
        "PRLUSDT": 2,
        "PROMPTUSDT": 2,
        "PROMUSDT": 2,
        "PROVEUSDT": 1,
        "PTBUSDT": 2,
        "PUMPBTCUSDT": 1,
        "PUMPUSDT": 2,
        "PUNDIXUSDT": 1,
        "PYTHUSDT": 1,
        "QCOMUSDT": 3,
        "QQQUSDT": 3,
        "QUSDT": 1,
        "RAREUSDT": 1,
        "RAVEUSDT": 1,
        "RAYSOLUSDT": 1,
        "RECALLUSDT": 1,
        "REDUSDT": 1,
        "RENDERUSDT": 1,
        "RESOLVUSDT": 1,
        "REUSDT": 1,
        "REZUSDT": 1,
        "RIFUSDT": 1,
        "RIVERUSDT": 2,
        "RLCUSDT": 6,
        "ROBOUSDT": 1,
        "RONINUSDT": 1,
        "ROSEUSDT": 6,
        "RPLUSDT": 2,
        "RSRUSDT": 6,
        "RVNUSDT": 7,
        "SAFEUSDT": 1,
        "SAGAUSDT": 1,
        "SAHARAUSDT": 2,
        "SAMSUNGUSDT": 2,
        "SANDUSDT": 6,
        "SANTOSUSDT": 1,
        "SAPIENUSDT": 1,
        "SCRTUSDT": 1,
        "SCRUSDT": 1,
        "SENTUSDT": 2,
        "SHELLUSDT": 1,
        "SIGNUSDT": 2,
        "SIRENUSDT": 2,
        "SKHYNIXUSDT": 2,
        "SKLUSDT": 7,
        "SKRUSDT": 2,
        "SKYAIUSDT": 1,
        "SKYUSDT": 1,
        "SLPUSDT": 2,
        "SLXUSDT": 2,
        "SOLUSDT": 6,
        "SOLVUSDT": 1,
        "SOMIUSDT": 1,
        "SONICUSDT": 1,
        "SOONUSDT": 2,
        "SOPHUSDT": 1,
        "SPACEUSDT": 1,
        "SPKUSDT": 1,
        "SPORTFUNUSDT": 1,
        "SPXUSDT": 1,
        "SPYUSDT": 3,
        "SQDUSDT": 2,
        "STABLEUSDT": 2,
        "STARUSDT": 1,
        "STEEMUSDT": 2,
        "STGUSDT": 2,
        "STMXUSDT": 1,
        "STORJUSDT": 7,
        "STOUSDT": 3,
        "STRCUSDT": 9,
        "STRKUSDT": 1,
        "STXXUSDT": 2,
        "SUNUSDT": 1,
        "SUPERUSDT": 1,
        "SUSDT": 1,
        "SUSHIUSDT": 6,
        "SWARMSUSDT": 1,
        "SXTUSDT": 1,
        "SYNUSDT": 1,
        "SYRUPUSDT": 1,
        "TACUSDT": 1,
        "TAGUSDT": 1,
        "TAIKOUSDT": 2,
        "TAKEUSDT": 1,
        "TAOUSDT": 1,
        "TAUSDT": 1,
        "THEUSDT": 1,
        "TIAUSDT": 1,
        "TLMUSDT": 31,
        "TNSRUSDT": 1,
        "TOSHIUSDT": 1,
        "TOWNSUSDT": 1,
        "TRADOORUSDT": 1,
        "TRBUSDT": 7,
        "TREEUSDT": 2,
        "TRIAUSDT": 1,
        "TRUMPUSDT": 1,
        "TRUSTUSDT": 2,
        "TRUTHUSDT": 1,
        "TRXUSDT": 6,
        "TSMUSDT": 3,
        "TSTUSDT": 1,
        "TURBOUSDT": 1,
        "TURTLEUSDT": 2,
        "TUSDT": 2,
        "TUTUSDT": 1,
        "TWTUSDT": 1,
        "TXNUSDT": 2,
        "UBUSDT": 1,
        "UMAUSDT": 1,
        "UNFIUSDT": 6,
        "USELESSUSDT": 1,
        "USTCUSDT": 1,
        "USUALUSDT": 1,
        "USUSDT": 1,
        "VANAUSDT": 1,
        "VANRYUSDT": 1,
        "VELODROMEUSDT": 1,
        "VELVETUSDT": 1,
        "VETUSDT": 6,
        "VICUSDT": 1,
        "VIRTUALUSDT": 1,
        "VTHOUSDT": 1,
        "VUSDT": 2,
        "VVVUSDT": 1,
        "WALUSDT": 1,
        "WAVESUSDT": 6,
        "WAXPUSDT": 1,
        "WCTUSDT": 1,
        "WDCUSDT": 3,
        "WENUSDT": 1,
        "WETUSDT": 1,
        "WIFUSDT": 1,
        "WLFIUSDT": 1,
        "WMTUSDT": 2,
        "WUSDT": 1,
        "XAGUSDT": 1,
        "XAIUSDT": 1,
        "XAUTUSDT": 1,
        "XAUUSDT": 1,
        "XEMUSDT": 6,
        "XLEUSDT": 3,
        "XLMUSDT": 6,
        "XNYUSDT": 1,
        "XPDUSDT": 1,
        "XPINUSDT": 1,
        "XPLUSDT": 1,
        "XPTUSDT": 1,
        "XRPUSDT": 6,
        "XTZUSDT": 1,
        "YBUSDT": 1,
        "YFIIUSDT": 1608,
        "YFIUSDT": 6,
        "YGGUSDT": 3,
        "ZAMAUSDT": 1,
        "ZBTUSDT": 1,
        "ZECUSDT": 6,
        "ZENUSDT": 6,
        "ZEREBROUSDT": 1,
        "ZESTUSDT": 1,
        "ZETAUSDT": 1,
        "ZILUSDT": 7,
        "ZKCUSDT": 1,
        "ZKPUSDT": 4,
        "ZKUSDT": 1,
        "ZORAUSDT": 1,
        "ZROUSDT": 1,
        "ZRXUSDT": 1
      },
      "checked_symbols": 860,
      "duplicate_symbols": [],
      "issue_codes": [
        "FUNDING_COVERAGE_GAP",
        "MISSING_INTERNAL_DAILY_BAR",
        "UNEXPLAINED_SYMBOL_GAP"
      ],
      "issue_count": 4174,
      "issue_counts": {
        "FUNDING_COVERAGE_GAP": 626,
        "MISSING_INTERNAL_DAILY_BAR": 3496,
        "UNEXPLAINED_SYMBOL_GAP": 52
      },
      "passed": false,
      "sample_issues": [
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 2h",
          "symbol": "0GUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "1000000BOBUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "1000000MOGUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "1000BONKUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "1000CATUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "1000CHEEMSUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "1000RATSUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "1000SATSUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 3h",
          "symbol": "1000XECUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "1MBABYDOGEUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "4USDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 6h",
          "symbol": "AAPLUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8.33333e-07h",
          "symbol": "AAPLUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 3h",
          "symbol": "AAPLUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 4h",
          "symbol": "AAPLUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 0.0002775h",
          "symbol": "AAPLUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ACEUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 2h",
          "symbol": "ACEUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ACTUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ACUUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ACXUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AERGOUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AEROUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AEVOUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AGTUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 3h",
          "symbol": "AIAUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 955h",
          "symbol": "AIAUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AIAUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AIGENSYNUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AINUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AIOTUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AIOUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AIXBTUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AKEUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "AKTUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ALCHUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ALICEUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ALLOUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ALPINEUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ALTUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 4h",
          "symbol": "AMATUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 0.000278056h",
          "symbol": "AMATUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 3h",
          "symbol": "ANIMEUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ANIMEUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 2h",
          "symbol": "ANKRUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ANKRUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 3h",
          "symbol": "API3USDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "API3USDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "APRUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "funding spacing 8h",
          "symbol": "ARCUSDT"
        }
      ]
    },
    "funding_coverage": {
      "hold_issue_count": 9,
      "hold_issue_counts": {
        "FUNDING_COVERAGE_GAP": 9
      },
      "holds_passed": false,
      "passed": false,
      "sample_issues": [
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "held interval has an unverified funding transition or boundary",
          "symbol": "LENDUSDT"
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
          "symbol": "LOKAUSDT"
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
          "symbol": "LEVERUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "held interval has an unverified funding transition or boundary",
          "symbol": "BSWUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "held interval has an unverified funding transition or boundary",
          "symbol": "AI16ZUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "held interval has an unverified funding transition or boundary",
          "symbol": "OMUSDT"
        },
        {
          "code": "FUNDING_COVERAGE_GAP",
          "day": null,
          "detail": "held interval has an unverified funding transition or boundary",
          "symbol": "MLNUSDT"
        }
      ],
      "structural_issue_count": 0,
      "structural_issue_counts": {},
      "structural_passed": true
    },
    "usable_dataset": {
      "affected_symbols": {},
      "checked_symbols": 362,
      "duplicate_symbols": [],
      "issue_codes": [],
      "issue_count": 0,
      "issue_counts": {},
      "passed": true,
      "sample_issues": []
    }
  },
  "rebalance_count": 302,
  "runtime_evidence_status": "EVIDENCE_STALE",
  "scheduled_signal_count": 344,
  "shadow": {
    "complete": false,
    "cost_pnl": -0.13979520722793623,
    "discovery": {
      "cagr_pct": 53.87752904712466,
      "daily_observations": 365,
      "end": "2026-08-31",
      "max_drawdown_pct": 11.542522644319575,
      "pnl": 0.819164472324668,
      "profit_factor_weekly": 2.4936335816734214,
      "start": "2025-09-01",
      "total_return_pct": 53.69593986873949,
      "weekly_observations": 52,
      "weekly_sharpe": 2.615762346193202
    },
    "external": {
      "cagr_pct": 7.7356682640161045,
      "daily_observations": 2070,
      "end": "2025-08-31",
      "max_drawdown_pct": 27.812507913153752,
      "pnl": 0.5255612888555965,
      "profit_factor_weekly": 1.2011904505423505,
      "start": "2020-01-01",
      "total_return_pct": 52.55612888555965,
      "weekly_observations": 295,
      "weekly_sharpe": 0.44003674071266063
    },
    "funding_pnl": -0.3473105710531472,
    "issues": [
      "DATA_INVALID: missing execution prices ANCUSDT"
    ],
    "price_pnl": 1.8318315394613394,
    "rebalance_count": 302,
    "spec_hash": "97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd",
    "strategy_id": "XS-LOWVOL-V1-VT80-Shadow",
    "trade_count": 1056,
    "turnover_notional": 174.744009034921
  },
  "shadow_is_secondary": true,
  "shadow_sha256": "97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd",
  "shadow_strategy_id": "XS-LOWVOL-V1-VT80-Shadow",
  "symbol_concentration": {
    "ranked": [
      [
        "BNBUSDT",
        0.30883395103499955
      ],
      [
        "ETCUSDT",
        0.25482943233141264
      ],
      [
        "1000SHIBUSDT",
        0.19881612128996462
      ],
      [
        "BTCUSDT",
        0.19838769530347877
      ],
      [
        "EOSUSDT",
        0.19659260315611485
      ],
      [
        "WLDUSDT",
        0.15595493633576704
      ],
      [
        "BAKEUSDT",
        0.14302839140278995
      ],
      [
        "AI16ZUSDT",
        0.14090526541966242
      ],
      [
        "ETHUSDT",
        0.1347407882044356
      ],
      [
        "ENSUSDT",
        0.11074713064122926
      ],
      [
        "ALPHAUSDT",
        0.1086157977728337
      ],
      [
        "SXPUSDT",
        0.1084312369522108
      ],
      [
        "APEUSDT",
        0.10566554501631623
      ],
      [
        "ALGOUSDT",
        0.10046644190166161
      ],
      [
        "1000FLOKIUSDT",
        0.09748712804028074
      ],
      [
        "ACHUSDT",
        0.09331680396800708
      ],
      [
        "HIGHUSDT",
        0.09063112640244168
      ],
      [
        "BCHUSDT",
        0.08129309251406618
      ],
      [
        "QTUMUSDT",
        0.07676158531783962
      ],
      [
        "HIFIUSDT",
        0.07459328561202419
      ],
      [
        "ARPAUSDT",
        0.06648786116229422
      ],
      [
        "UXLINKUSDT",
        0.060928332697195095
      ],
      [
        "SFPUSDT",
        0.059884009280345436
      ],
      [
        "XMRUSDT",
        0.0596909181711524
      ],
      [
        "AMBUSDT",
        0.05875107309817554
      ],
      [
        "JASMYUSDT",
        0.058374590561102856
      ],
      [
        "LQTYUSDT",
        0.05755807979293032
      ],
      [
        "DASHUSDT",
        0.05531129547756374
      ],
      [
        "OMUSDT",
        0.05453643707285342
      ],
      [
        "NEOUSDT",
        0.054318854972373244
      ],
      [
        "KEEPUSDT",
        0.0520676637649428
      ],
      [
        "ADAUSDT",
        0.050276219843480116
      ],
      [
        "EDUUSDT",
        0.04729293235149564
      ],
      [
        "KLAYUSDT",
        0.04126633264630765
      ],
      [
        "MEMEFIUSDT",
        0.03791294793156742
      ],
      [
        "MTLUSDT",
        0.03671399816247665
      ],
      [
        "CTSIUSDT",
        0.036618765471700565
      ],
      [
        "TROYUSDT",
        0.03652898810685814
      ],
      [
        "1000XUSDT",
        0.03642548179290354
      ],
      [
        "TOKENUSDT",
        0.0345072935447031
      ],
      [
        "BZRXUSDT",
        0.03446368152499572
      ],
      [
        "FISUSDT",
        0.03395493217238651
      ],
      [
        "CELRUSDT",
        0.03234466460957166
      ],
      [
        "ZKJUSDT",
        0.032031305018462804
      ],
      [
        "THETAUSDT",
        0.03154518986065556
      ],
      [
        "ATAUSDT",
        0.030707988670970385
      ],
      [
        "SNXUSDT",
        0.030077128904467877
      ],
      [
        "TRUUSDT",
        0.029258797023679572
      ],
      [
        "C98USDT",
        0.028914057145810582
      ],
      [
        "HOOKUSDT",
        0.02849142007644352
      ],
      [
        "HFTUSDT",
        0.02801661313630852
      ],
      [
        "PHBUSDT",
        0.027067981474195785
      ],
      [
        "PONKEUSDT",
        0.023841988837116353
      ],
      [
        "DEGOUSDT",
        0.02288877142273332
      ],
      [
        "OMNIUSDT",
        0.02284175829952253
      ],
      [
        "GALUSDT",
        0.022243268348911636
      ],
      [
        "BLZUSDT",
        0.021636572168442423
      ],
      [
        "WOOUSDT",
        0.017620513725336524
      ],
      [
        "NTRNUSDT",
        0.017501002146897995
      ],
      [
        "XVGUSDT",
        0.016528672710603126
      ],
      [
        "BTTUSDT",
        0.016346270938010842
      ],
      [
        "AKROUSDT",
        0.015424990623092768
      ],
      [
        "BANDUSDT",
        0.014079662631138893
      ],
      [
        "AGLDUSDT",
        0.013480449641580296
      ],
      [
        "PERPUSDT",
        0.012930929803244983
      ],
      [
        "MYROUSDT",
        0.01190619996829982
      ],
      [
        "DEFIUSDT",
        0.011640899314003006
      ],
      [
        "ARKMUSDT",
        0.010975909001255025
      ],
      [
        "LOOMUSDT",
        0.010622187076959225
      ],
      [
        "DARUSDT",
        0.009936090177671305
      ],
      [
        "BONDUSDT",
        0.009564013394593262
      ],
      [
        "OXTUSDT",
        0.008156128653963632
      ],
      [
        "FXSUSDT",
        0.00699876482597297
      ],
      [
        "CELOUSDT",
        0.006914952385070969
      ],
      [
        "AIUSDT",
        0.006395966950662024
      ],
      [
        "COMPUSDT",
        0.005533258189918202
      ],
      [
        "PENDLEUSDT",
        0.0041507070394729995
      ],
      [
        "JOEUSDT",
        0.0032363327526259917
      ],
      [
        "DENTUSDT",
        6.710601781104945e-05
      ],
      [
        "1000LUNCUSDT",
        0.0
      ],
      [
        "1000PEPEUSDT",
        0.0
      ],
      [
        "1INCHUSDT",
        0.0
      ],
      [
        "AAVEUSDT",
        0.0
      ],
      [
        "AGIXUSDT",
        0.0
      ],
      [
        "ALPACAUSDT",
        0.0
      ],
      [
        "ANTUSDT",
        0.0
      ],
      [
        "APTUSDT",
        0.0
      ],
      [
        "ARBUSDT",
        0.0
      ],
      [
        "ARUSDT",
        0.0
      ],
      [
        "ATOMUSDT",
        0.0
      ],
      [
        "AUDIOUSDT",
        0.0
      ],
      [
        "AVAXUSDT",
        0.0
      ],
      [
        "BATUSDT",
        0.0
      ],
      [
        "BELUSDT",
        0.0
      ],
      [
        "BIDUSDT",
        0.0
      ],
      [
        "BSWUSDT",
        0.0
      ],
      [
        "CFXUSDT",
        0.0
      ],
      [
        "CHRUSDT",
        0.0
      ],
      [
        "CHZUSDT",
        0.0
      ],
      [
        "CKBUSDT",
        0.0
      ],
      [
        "COTIUSDT",
        0.0
      ],
      [
        "CRVUSDT",
        0.0
      ],
      [
        "DODOUSDT",
        0.0
      ],
      [
        "DOGEUSDT",
        0.0
      ],
      [
        "DOTUSDT",
        0.0
      ],
      [
        "DYDXUSDT",
        0.0
      ],
      [
        "EGLDUSDT",
        0.0
      ],
      [
        "FETUSDT",
        0.0
      ],
      [
        "FOOTBALLUSDT",
        0.0
      ],
      [
        "FRONTUSDT",
        0.0
      ],
      [
        "FUNUSDT",
        0.0
      ],
      [
        "GALAUSDT",
        0.0
      ],
      [
        "HIPPOUSDT",
        0.0
      ],
      [
        "HNTUSDT",
        0.0
      ],
      [
        "INJUSDT",
        0.0
      ],
      [
        "IOSTUSDT",
        0.0
      ],
      [
        "IOTXUSDT",
        0.0
      ],
      [
        "LDOUSDT",
        0.0
      ],
      [
        "LENDUSDT",
        0.0
      ],
      [
        "LEVERUSDT",
        0.0
      ],
      [
        "LINKUSDT",
        0.0
      ],
      [
        "LOKAUSDT",
        0.0
      ],
      [
        "MATICUSDT",
        0.0
      ],
      [
        "MAVUSDT",
        0.0
      ],
      [
        "MINAUSDT",
        0.0
      ],
      [
        "NEIROETHUSDT",
        0.0
      ],
      [
        "NKNUSDT",
        0.0
      ],
      [
        "OPUSDT",
        0.0
      ],
      [
        "QNTUSDT",
        0.0
      ],
      [
        "RDNTUSDT",
        0.0
      ],
      [
        "REEFUSDT",
        0.0
      ],
      [
        "RENUSDT",
        0.0
      ],
      [
        "RNDRUSDT",
        0.0
      ],
      [
        "RUNEUSDT",
        0.0
      ],
      [
        "SEIUSDT",
        0.0
      ],
      [
        "SPELLUSDT",
        0.0
      ],
      [
        "SRMUSDT",
        0.0
      ],
      [
        "SSVUSDT",
        0.0
      ],
      [
        "STXUSDT",
        0.0
      ],
      [
        "SUIUSDT",
        0.0
      ],
      [
        "TOMOUSDT",
        0.0
      ],
      [
        "TONUSDT",
        0.0
      ],
      [
        "UNIUSDT",
        0.0
      ],
      [
        "VIDTUSDT",
        0.0
      ],
      [
        "VINEUSDT",
        0.0
      ],
      [
        "VOXELUSDT",
        0.0
      ],
      [
        "XVSUSDT",
        0.0
      ]
    ],
    "top2_positive_pnl_share_pct": 12.565258840722882,
    "top_1_profit_symbol": "BNBUSDT",
    "top_1_share_of_positive_pnl_pct": 6.884567364269103,
    "top_5_share_of_positive_pnl_pct": 25.802247321838284
  },
  "window_split": {
    "DISCOVERY_REFERENCE": 52,
    "EXTERNAL_VALIDATION": 295
  },
  "yearly_breakdown": {
    "positive_full_calendar_years": 3,
    "positive_years": [
      2020,
      2022,
      2024
    ],
    "worst_full_calendar_year_return_pct": -23.958823778846295,
    "years": {
      "2020": {
        "end": "2020-12-31",
        "full_calendar_year": true,
        "max_drawdown_pct": 7.699514382952355,
        "start": "2020-01-01",
        "total_return_pct": 39.97131889267944
      },
      "2021": {
        "end": "2021-12-31",
        "full_calendar_year": true,
        "max_drawdown_pct": 35.361147304302186,
        "start": "2021-01-01",
        "total_return_pct": -23.958823778846295
      },
      "2022": {
        "end": "2022-12-31",
        "full_calendar_year": true,
        "max_drawdown_pct": 21.379660274099376,
        "start": "2022-01-01",
        "total_return_pct": 32.236478473839725
      },
      "2023": {
        "end": "2023-12-31",
        "full_calendar_year": true,
        "max_drawdown_pct": 21.89719285133028,
        "start": "2023-01-01",
        "total_return_pct": -13.172073725911137
      },
      "2024": {
        "end": "2024-12-31",
        "full_calendar_year": true,
        "max_drawdown_pct": 20.51970442281348,
        "start": "2024-01-01",
        "total_return_pct": 28.555210274133792
      }
    }
  }
}
```
