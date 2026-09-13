"""资金费与基差机会扫描。

资金来源是币安公开的 premiumIndex 接口，它同时返回标记价、指数价与当前资金费率，
因此计算资金费收益与基差不需要额外调用现货接口。

关键点：**永远看扣费后的净收益，不要看毛收益。**
资金费套利是「做空永续 + 做多现货」的 delta 中性组合，往返要付四次手续费：
现货买入、现货卖出、永续开仓、永续平仓。把这笔成本摊到持有期上，
很多看起来年化 20% 的机会实际只剩个位数。
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import Config, threshold
from .models import FundingOpportunity

log = logging.getLogger(__name__)

FUNDING_INTERVALS_PER_DAY = 3.0   # 每 8 小时一次
DAYS_PER_YEAR = 365.0


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def round_trip_cost_pct(cfg: Config) -> float:
    """delta 中性组合的往返成本（单位 %）。

    现货买入 + 现货卖出 + 永续开仓 + 永续平仓，各计一次费率，
    再叠加两侧滑点。默认值按币安 VIP0 计算：
    2 x (0.10% 现货 taker + 0.05% 永续 taker) + 2 x 0.03% 滑点 = 0.36%
    """
    spot = threshold(cfg, "spot_taker_fee_pct", 0.10)
    perp = threshold(cfg, "perp_taker_fee_pct", 0.05)
    slip = threshold(cfg, "slippage_pct", 0.03)
    return 2 * (spot + perp) + 2 * slip


def build_opportunity(
    premium: dict[str, Any],
    quote_volume_24h: float,
    cfg: Config,
    hold_days: float,
) -> FundingOpportunity | None:
    """由单条 premiumIndex 记录构造机会对象。数据不完整时返回 None。"""
    symbol = str(premium.get("symbol", "")).upper()
    if not symbol:
        return None

    mark_price = _f(premium.get("markPrice"))
    index_price = _f(premium.get("indexPrice"))
    if mark_price <= 0 or index_price <= 0:
        return None

    funding_rate = _f(premium.get("lastFundingRate"))
    basis_pct = (mark_price - index_price) / index_price * 100.0
    gross_annual_pct = funding_rate * FUNDING_INTERVALS_PER_DAY * DAYS_PER_YEAR * 100.0

    # 成本摊到持有期：持有越短，同样的往返成本对年化的侵蚀越大
    if hold_days > 0:
        amortized_cost_pct = round_trip_cost_pct(cfg) * DAYS_PER_YEAR / hold_days
    else:
        amortized_cost_pct = float("inf")
    net_annual_pct = gross_annual_pct - amortized_cost_pct

    next_ts = premium.get("nextFundingTime")
    next_funding = None
    if next_ts:
        try:
            next_funding = datetime.fromtimestamp(int(next_ts) / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            next_funding = None

    return FundingOpportunity(
        symbol=symbol,
        funding_rate=funding_rate,
        mark_price=mark_price,
        index_price=index_price,
        basis_pct=basis_pct,
        gross_annual_pct=gross_annual_pct,
        net_annual_pct=net_annual_pct,
        quote_volume_24h=quote_volume_24h,
        hold_days=hold_days,
        next_funding_time=next_funding,
    )


def scan_opportunities(
    premium_raw: Iterable[dict[str, Any]],
    ticker_raw: Iterable[dict[str, Any]],
    cfg: Config,
) -> list[FundingOpportunity]:
    """扫描全部合约，按净年化收益降序返回通过流动性过滤的机会。"""
    hold_days = threshold(cfg, "assumed_hold_days", 30.0)
    min_volume = threshold(cfg, "min_volume_usdt_24h", 50_000_000.0)

    volume_map = {
        str(item.get("symbol", "")).upper(): _f(item.get("quoteVolume"))
        for item in ticker_raw
    }

    out: list[FundingOpportunity] = []
    for item in premium_raw:
        symbol = str(item.get("symbol", "")).upper()
        # 只看 USDT 本位永续，排除交割合约
        if not symbol.endswith("USDT"):
            continue
        volume = volume_map.get(symbol, 0.0)
        if volume < min_volume:
            continue
        opp = build_opportunity(item, volume, cfg, hold_days)
        if opp is not None:
            out.append(opp)

    out.sort(key=lambda o: o.net_annual_pct, reverse=True)
    return out


def display_width(text: str) -> int:
    """按终端显示宽度计算字符串长度。

    币安确实存在中文名的合约（如 龙虾USDT、我踏马来了USDT），
    中日韩字符占两列，直接用 len() 会导致表格错位。
    """
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格。"""
    filler = " " * max(0, width - display_width(text))
    return filler + text if align == "right" else text + filler


def format_opportunity_table(
    opportunities: list[FundingOpportunity], top_n: int = 15
) -> str:
    """渲染机会榜，供 scan 命令与摘要使用。"""
    if not opportunities:
        return "（没有通过流动性过滤的机会）"

    columns = [
        ("合约", 20, "left"),
        ("资金费/8h", 12, "right"),
        ("毛年化", 11, "right"),
        ("净年化", 11, "right"),
        ("基差", 10, "right"),
        ("24h成交额(亿)", 15, "right"),
    ]
    header = "".join(pad(name, width, align) for name, width, align in columns)
    lines = [header, "-" * display_width(header)]
    for opp in opportunities[:top_n]:
        cells = [
            pad(opp.symbol, 20, "left"),
            pad(f"{opp.funding_rate * 100:.4f}%", 12, "right"),
            pad(f"{opp.gross_annual_pct:.1f}%", 11, "right"),
            pad(f"{opp.net_annual_pct:.1f}%", 11, "right"),
            pad(f"{opp.basis_pct:+.3f}%", 10, "right"),
            pad(f"{opp.quote_volume_24h / 1e8:.2f}", 15, "right"),
        ]
        lines.append("".join(cells))
    return "\n".join(lines)
