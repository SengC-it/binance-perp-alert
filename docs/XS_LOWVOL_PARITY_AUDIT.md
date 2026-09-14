# XS-LOWVOL-V1 parity audit

审计范围是 `research/xs_lowvol_v1.yaml`、`backtest/cross_sectional.py`、
`src/directional_engine.py`、`src/weekly_paper.py`、`src/forward_check.py`、
`src/store.py`、`src/engine.py` 和 `config.example.yaml`。本阶段只做 M0
验证基础设施；没有扩展历史样本、没有做参数搜索，也没有用新结果改参数。

## Frozen identity

| Variant | Strategy ID | Spec SHA-256 | Position sizing |
|---|---|---|---|
| Control | `XS-LOWVOL-V1-Control` | `5ab2c21a93a965ce363b02ebca5a4e42f8fc573a89b24ac7ad4e88c345aa8678` | equal notional, no volatility targeting |
| VT80 Shadow | `XS-LOWVOL-V1-VT80-Shadow` | `97e9025928bc234603139f90e58c077dddec16de55a0c9bd06c45d19b4296bdd` | Control signal + target 80%, max scale 3.0 |

`src.xs_lowvol_spec` 对 YAML 做 canonical JSON 后计算 SHA-256。Backtest
结果、PAPER trades/NAV/rebalance/portfolio 和生成式报告都带 strategy ID 与
spec hash；hash 不匹配的 evidence 只显示 `EVIDENCE_STALE`。

## Item-by-item comparison

| Item | Status | Backtest | Scanner / Forward PAPER | Notes |
|---|---|---|---|---|
| Universe definition | MATCH | `MarketSeries` 只接受 USDT、PERPETUAL 且按观测日 active | `exchangeInfo` 的 TRADING + USDT + PERPETUAL；target 进入同一 ledger | 不按市值或请求便利截断 |
| Liquidity filter | MATCH | signal completed day 的 perp quote volume >= 50,000,000 USDT | 先枚举完整 active universe，再用该 completed day 的 quote volume；ticker 仅作无 quote-volume fixture 的 fallback | 当前实现不再先用 ticker top-N 预筛 |
| Top-N restriction | MATCH | 没有 top-N | `xs_scan_top_n` 已从示例配置移除；所有 eligible 都读取 | 请求量增加是有意的 correctness trade-off |
| Lookback | MATCH | 30 个连续完整日收益 | 30 个连续 completed daily closes | 缺口不会被压缩成 30 个非连续观测 |
| `k_long` | MATCH | 5 | 5 | 来自 frozen spec |
| `k_short` | MATCH | 5 | 5 | 来自 frozen spec |
| Rebalance interval | MATCH | 每 7 个日历日尝试一次 | `AlertService` / `record_signal` 以 7 天 gate；ledger 在 execution day 做 target-diff | 不再按天登记新一轮组合 |
| Signal timestamp | MATCH | signal day 的 completed close | K 线 `closeTime`；signal 对外保存 `signal_timestamp` | 只读已完成蜡烛 |
| Execution timestamp | MATCH | signal 后下一个 completed day | `execution_date = signal day + 1 day` | execution price 单独落账 |
| Completed candle | MATCH | 批量归档只含完成 K 线；缺口会使 score 无效 | parser 要求 open/close/closeTime，未来 closeTime 被丢弃 | 未完成日线不进入排序 |
| Execution lag | MATCH | `exec_lag_days=1`，按日历日期 | `execution_lag_days=1` | 不用当前未收盘价格 |
| Entry price | MATCH | execution day 的 perp close | ledger 在 execution day 的 completed close；PAPER trade 完成后更新实际 entry price | signal snapshot 不再冒充实际成交价 |
| Exit / rebalance rule | MATCH | 只对 target 变化的标的结算并收 exit cost；同方向保留 | 同样的 target-diff；同方向不平不开 | 组合末日不虚构一次完整换仓 |
| Taker fee | MATCH | 0.05% / side | 0.05% / side | 每次实际开/关一条变化腿收费 |
| Slippage | MATCH | 0.03% / side | 0.03% / side | 与手续费一起按 notional 计 |
| Funding | MATCH | `funding_sum_between` 使用结算事件；正费率空头收、多头付 | `funding_history` 事件按完成日逐日记账；缺取数 fail-closed | 不把当前 funding snapshot 当结算历史 |
| Position sizing | MATCH | 10 个 slot 等名义金额 | ledger 10 个 slot 等名义金额 | Control 默认每槽 capital / 10 |
| Volatility targeting | MATCH | Control 强制关闭；Shadow 单独模拟 | Control 强制 scale 1；Shadow 独立 strategy identity | 禁止跨 variant 动态择优 |
| Max scale | MATCH | Control 1；Shadow 3.0 | Control 1；Shadow 3.0 | 仅 Shadow 使用目标波动缩放 |
| Delisted symbol handling | MATCH | `listed_from` / `delisted_at`，旧缓存从观测边界推断；观测期外不 active | exchange metadata 只纳入当前有效合约；symbol 从 target 移除 | 缺日线也会触发 no-signal，不会补齐 |
| Missing data | MATCH | `NO_SIGNAL` / `complete=False`，缺价不静默估算 | 缺任一 eligible history、价格或 funding 时 fail-closed | 不将剩余标的悄悄当作新 universe |

## Mismatch / not implemented ledger

以下是已发现的非当前收益计算差异，均已隔离并明确标记：

1. `backtest/report_cross_sectional.md` 是 M0 前生成的历史快照，原数值不含
   当前 completed-candle、daily funding 和 target-diff ledger 口径。文件顶部已
   标为 `LEGACY / EVIDENCE_STALE`，并写入当前 Control/Shadow identity；它不再
   被 Scanner 或 Forward 读取。状态：**MISMATCH（legacy artifact，已隔离）**。
2. 已存在的旧 SQLite `paper_trades` 行可能没有 strategy ID/hash。迁移会保留原行
   以便追溯，不会伪造为 V1；有当前 Control 行时报告按当前 identity 隔离统计，旧行
   会显示为“旧未版本化记录”。状态：**MISMATCH（legacy data，已隔离）**。
3. 旧 baseline evidence 的 long/short Sharpe 与 bull/bear attribution 没有历史日级
   原始字段，因此 artifact 对这些字段明确写 `null` / `not_available_in_baseline_artifact`。
   重新生成的 `build_report()` 会从新的 daily leg/equity 数据输出这些永久指标；这不是
   用旧数字填充新指标。状态：**NOT_IMPLEMENTED（仅旧 artifact 的补算）**。

当前代码路径中没有发现会改变 V1 收益计算的未隔离 MISMATCH。上述 legacy
artifact/data 不作为当前策略结论输入。

## Safety boundary

`src.config.LIVE_TRADING` 固定为 `False`。`python main.py audit` 继续扫描全部
自有 Python 源码，HTTP 只允许 GET；本次改动没有增加任何交易执行接口。

