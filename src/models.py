"""领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARN = "WARN"
    INFO = "INFO"


SEVERITY_RANK = {Severity.INFO: 0, Severity.WARN: 1, Severity.CRITICAL: 2}


@dataclass
class PositionRisk:
    """单个持仓的风险指标。金额单位 USDT，比例均为小数。"""

    symbol: str
    side: str                      # LONG / SHORT
    position_amt: float
    entry_price: float
    mark_price: float
    notional: float
    unrealized_pnl: float
    leverage: float
    isolated_margin: float
    maint_margin: float
    margin_ratio: float            # 维持保证金 / 保证金余额，1.0 即强平
    liq_price: float
    liq_distance_pct: float        # 强平距离 / 标记价
    margin_ratio_estimated: bool = False  # 维持保证金为估算值（接口未返回时）
    planned_stop_pct: float | None = None
    r_value_usdt: float = 0.0      # 止损触发时的名义亏损 = 1R
    funding_cost_usdt: float = 0.0  # 累计资金费估算（正数代表成本）
    funding_rate: float = 0.0      # 当前资金费率（每 8 小时）

    @property
    def liq_to_stop_ratio(self) -> float | None:
        """强平距离 / 止损距离。低于 3 倍即为危险。"""
        if not self.planned_stop_pct:
            return None
        return self.liq_distance_pct / self.planned_stop_pct

    @property
    def pnl_r(self) -> float | None:
        if self.r_value_usdt <= 0:
            return None
        return self.unrealized_pnl / self.r_value_usdt

    @property
    def funding_cost_r(self) -> float | None:
        if self.r_value_usdt <= 0:
            return None
        return self.funding_cost_usdt / self.r_value_usdt


@dataclass
class FundingOpportunity:
    """一个永续合约的资金费 / 基差机会。

    所有百分比字段的单位都是"百分点"，例如 12.5 表示 12.5%。
    """

    symbol: str
    funding_rate: float            # 当前资金费率（每 8 小时，小数）
    mark_price: float
    index_price: float
    basis_pct: float               # (标记价 - 指数价) / 指数价，单位 %
    gross_annual_pct: float        # 未扣成本的年化资金费收益，单位 %
    net_annual_pct: float          # 扣除往返手续费与滑点后的年化，单位 %
    quote_volume_24h: float        # 24h 名义成交额（USDT）
    hold_days: float               # 计算净收益时假设的持有天数
    next_funding_time: datetime | None = None

    @property
    def is_short_perp_direction(self) -> bool:
        """正资金费时，做空永续 + 做多现货才能收资金费。"""
        return self.funding_rate > 0


@dataclass
class AccountSnapshot:
    """一次轮询得到的账户全景。"""

    equity: float
    available_balance: float
    positions: list[PositionRisk] = field(default_factory=list)
    daily_loss_pct: float = 0.0    # 相对当日起始权益的亏损比例（正数代表亏损）
    drawdown_pct: float = 0.0      # 相对历史峰值权益的回撤比例
    fetched_at: datetime | None = None

    @property
    def portfolio_heat_pct(self) -> float:
        """全部持仓止损同时触发时的风险占权益比例（%）。"""
        if self.equity <= 0:
            return 0.0
        total_risk = sum(p.r_value_usdt for p in self.positions if p.r_value_usdt > 0)
        return total_risk / self.equity * 100.0


@dataclass
class Alert:
    """一条待投递或已投递的告警。"""

    rule_id: str
    dedup_key: str
    severity: Severity
    title: str
    body: str
    symbol: str = "-"
    created_at: datetime | None = None
    alert_id: int | None = None

    def to_email(self) -> tuple[str, str]:
        """返回 (主题, 正文)。"""
        return f"[{self.severity.value}] {self.title}", self.body
