"""跨交易所资金费扫描（Bybit / OKX）。

为什么单独做一个模块而不是引入 ccxt：
跨所资金费套利只需要"各所的资金费率 + 成交额"这几个公开字段，
Bybit 一次 tickers 调用就能拿到全量线性合约的费率与成交额；
OKX 需要先取成交额榜再逐个查费率，但也可以限制在流动性最好的前 N 个。
用 ccxt 会引入一个重量级依赖，而这里真正需要的能力只有两个公开 GET。

套利方向：**在资金费高的交易所做空，在资金费低的交易所做多。**
净收益 = (费率高的所 − 费率低的所) × 3 × 365，再扣掉两条腿的往返成本。

成本口径与单所内不同：跨所是两条永续腿，进场 2 笔 + 出场 2 笔 = 4 笔成交，
且需要在两个交易所各留保证金，资金效率天然低于单所内套利。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from .config import Config, threshold

log = logging.getLogger(__name__)

FUNDING_INTERVALS_PER_DAY = 3.0
DAYS_PER_YEAR = 365.0


class ExchangeError(Exception):
    """交易所公开接口异常。"""


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class FundingQuote:
    exchange: str
    symbol: str            # 该交易所上的原始合约名
    base: str              # 归一化后的基础资产，用于跨所匹配
    funding_rate: float    # 每 8 小时
    quote_volume_24h: float


@dataclass
class CrossExchangeOpportunity:
    base: str
    high: FundingQuote
    low: FundingQuote
    spread_rate: float          # 每 8 小时的费率差
    gross_annual_pct: float
    net_annual_pct: float
    hold_days: float


# --------------------------------------------------------------------------
# 符号归一化
# --------------------------------------------------------------------------


def normalize_base(symbol: str, exchange: str) -> str | None:
    """把各所合约名归一化成基础资产名。

    币安/Bybit: BTCUSDT -> BTC
    OKX:        BTC-USDT-SWAP -> BTC

    已知限制：OKX 对部分币种使用乘数合约（如 PEPE-USDT-SWAP 对应 100 万枚 PEPE），
    币安则是 1000PEPEUSDT。这类合约归一化后名称不一致，会被直接跳过而不是错误匹配。
    宁可漏配，不可错配。
    """
    if not symbol:
        return None
    if exchange == "okx":
        parts = symbol.split("-")
        if len(parts) < 3 or parts[1] != "USDT" or parts[2] != "SWAP":
            return None
        return parts[0].upper()
    # 币安 / Bybit：仅接受 USDT 本位永续
    if not symbol.upper().endswith("USDT"):
        return None
    return symbol.upper()[:-4]


# --------------------------------------------------------------------------
# 解析（与网络解耦，便于单测）
# --------------------------------------------------------------------------


def parse_bybit_tickers(payload: dict[str, Any]) -> list[FundingQuote]:
    """解析 Bybit v5 linear tickers，一次拿到全部线性合约。"""
    if payload.get("retCode") not in (0, None):
        raise ExchangeError(f"Bybit 返回错误：{payload.get('retMsg')}")
    quotes: list[FundingQuote] = []
    for item in (payload.get("result") or {}).get("list", []) or []:
        symbol = str(item.get("symbol", "")).upper()
        base = normalize_base(symbol, "bybit")
        if base is None:
            continue
        quotes.append(
            FundingQuote(
                exchange="bybit",
                symbol=symbol,
                base=base,
                funding_rate=_f(item.get("fundingRate")),
                quote_volume_24h=_f(item.get("turnover24h")),
            )
        )
    return quotes


def parse_okx_tickers(
    payload: dict[str, Any], max_symbols: int
) -> list[tuple[str, float]]:
    """从 OKX SWAP tickers 中挑出成交额最高的前 N 个 USDT 永续。

    返回 [(instId, 24h 名义成交额)]。OKX 查资金费必须逐个 instId，
    因此先用成交额筛一遍，把请求数控制住。
    """
    rows: list[tuple[str, float]] = []
    for item in payload.get("data", []) or []:
        inst_id = str(item.get("instId", ""))
        base = normalize_base(inst_id, "okx")
        if base is None:
            continue
        # SWAP 的 volCcy24h 以币计价，乘以最新价得到 USDT 名义成交额
        notional = _f(item.get("volCcy24h")) * _f(item.get("last"))
        rows.append((inst_id, notional))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:max_symbols]


def parse_okx_funding(payload: dict[str, Any], volume: float) -> FundingQuote | None:
    """解析单个 OKX 合约的资金费。"""
    rows = payload.get("data", []) or []
    if not rows:
        return None
    item = rows[0]
    inst_id = str(item.get("instId", ""))
    base = normalize_base(inst_id, "okx")
    if base is None:
        return None
    return FundingQuote(
        exchange="okx",
        symbol=inst_id,
        base=base,
        funding_rate=_f(item.get("fundingRate")),
        quote_volume_24h=volume,
    )


def binance_quotes(
    premium_raw: Any, ticker_raw: Any
) -> list[FundingQuote]:
    """把币安的 premiumIndex + ticker24hr 转成统一的 FundingQuote。

    复用同一个公开接口，因此跨所扫描不需要额外的币安私有权限。
    """
    volume_map = {
        str(item.get("symbol", "")).upper(): _f(item.get("quoteVolume"))
        for item in (ticker_raw if isinstance(ticker_raw, list) else [ticker_raw])
    }
    quotes: list[FundingQuote] = []
    items = premium_raw if isinstance(premium_raw, list) else [premium_raw]
    for item in items:
        symbol = str(item.get("symbol", "")).upper()
        base = normalize_base(symbol, "binance")
        if base is None:
            continue
        quotes.append(
            FundingQuote(
                exchange="binance",
                symbol=symbol,
                base=base,
                funding_rate=_f(item.get("lastFundingRate")),
                quote_volume_24h=volume_map.get(symbol, 0.0),
            )
        )
    return quotes


# --------------------------------------------------------------------------
# HTTP 客户端（只读公开接口）
# --------------------------------------------------------------------------


class PublicExchangeClient:
    def __init__(self, timeout: int = 15, session: requests.Session | None = None):
        self.timeout = timeout
        self.session = session or requests.Session()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ExchangeError(f"网络请求失败：{exc}") from exc
        if resp.status_code >= 400:
            raise ExchangeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ExchangeError(f"响应不是合法 JSON：{resp.text[:200]}") from exc

    def bybit_quotes(self, base_url: str) -> list[FundingQuote]:
        payload = self._get(f"{base_url.rstrip('/')}/v5/market/tickers",
                            {"category": "linear"})
        return parse_bybit_tickers(payload)

    def okx_quotes(self, base_url: str, max_symbols: int) -> list[FundingQuote]:
        root = base_url.rstrip("/")
        tickers = self._get(f"{root}/api/v5/market/tickers", {"instType": "SWAP"})
        candidates = parse_okx_tickers(tickers, max_symbols)

        quotes: list[FundingQuote] = []
        failures = 0
        for inst_id, volume in candidates:
            try:
                payload = self._get(
                    f"{root}/api/v5/public/funding-rate", {"instId": inst_id}
                )
            except ExchangeError as exc:
                failures += 1
                log.debug("OKX 资金费查询失败 %s：%s", inst_id, exc)
                continue
            quote = parse_okx_funding(payload, volume)
            if quote is not None:
                quotes.append(quote)
        if failures:
            log.warning("OKX 有 %d 个合约的资金费查询失败", failures)
        return quotes


# --------------------------------------------------------------------------
# 匹配与净收益
# --------------------------------------------------------------------------


def cross_leg_cost_pct(cfg: Config) -> float:
    """跨所套利两条永续腿的往返成本（%）。

    进场 2 笔 + 出场 2 笔 = 4 笔成交，各计一次 taker 费率与一次滑点。
    """
    perp = threshold(cfg, "perp_taker_fee_pct", 0.05)
    slip = threshold(cfg, "slippage_pct", 0.03)
    return 4 * (perp + slip)


def build_cross_opportunities(
    quotes: list[FundingQuote], cfg: Config
) -> list[CrossExchangeOpportunity]:
    """按基础资产分组，找出费率差最大的交易所组合。"""
    hold_days = threshold(cfg, "assumed_hold_days", 30.0)
    min_volume = threshold(cfg, "min_volume_usdt_24h", 50_000_000.0)
    cost = cross_leg_cost_pct(cfg)
    amortized_cost = cost * DAYS_PER_YEAR / hold_days if hold_days > 0 else float("inf")

    by_base: dict[str, list[FundingQuote]] = {}
    for quote in quotes:
        if quote.quote_volume_24h < min_volume:
            continue
        by_base.setdefault(quote.base, []).append(quote)

    out: list[CrossExchangeOpportunity] = []
    for base, group in by_base.items():
        # 至少两个不同交易所才有跨所价差
        exchanges = {q.exchange for q in group}
        if len(exchanges) < 2:
            continue
        high = max(group, key=lambda q: q.funding_rate)
        low = min(group, key=lambda q: q.funding_rate)
        if high.exchange == low.exchange:
            continue

        spread = high.funding_rate - low.funding_rate
        gross = spread * FUNDING_INTERVALS_PER_DAY * DAYS_PER_YEAR * 100.0
        out.append(
            CrossExchangeOpportunity(
                base=base,
                high=high,
                low=low,
                spread_rate=spread,
                gross_annual_pct=gross,
                net_annual_pct=gross - amortized_cost,
                hold_days=hold_days,
            )
        )

    out.sort(key=lambda o: o.net_annual_pct, reverse=True)
    return out


def format_cross_table(
    opportunities: list[CrossExchangeOpportunity], top_n: int = 15
) -> str:
    from .opportunity_engine import display_width, pad

    if not opportunities:
        return "（没有满足流动性要求的跨所价差）"

    columns = [
        ("标的", 14, "left"),
        ("做空(高费率)", 20, "left"),
        ("做多(低费率)", 20, "left"),
        ("费率差/8h", 12, "right"),
        ("毛年化", 11, "right"),
        ("净年化", 11, "right"),
    ]
    header = "".join(pad(name, width, align) for name, width, align in columns)
    lines = [header, "-" * display_width(header)]
    for opp in opportunities[:top_n]:
        cells = [
            pad(opp.base, 14, "left"),
            pad(f"{opp.high.exchange}:{opp.high.symbol}", 20, "left"),
            pad(f"{opp.low.exchange}:{opp.low.symbol}", 20, "left"),
            pad(f"{opp.spread_rate * 100:.4f}%", 12, "right"),
            pad(f"{opp.gross_annual_pct:.1f}%", 11, "right"),
            pad(f"{opp.net_annual_pct:.1f}%", 11, "right"),
        ]
        lines.append("".join(cells))
    return "\n".join(lines)
