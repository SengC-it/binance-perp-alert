"""币安 U 本位合约只读 REST 客户端。

只使用读取类接口。本模块不包含任何下单、撤单、划转能力，
以便配合"只读 API Key + IP 白名单"的安全要求。

签名：query string + HMAC-SHA256(secret)。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Mapping
from urllib.parse import urlencode

import requests


class BinanceError(Exception):
    """币安接口返回错误或网络异常。"""


FUNDING_API_MAX_LIMIT = 1000
DEFAULT_FUNDING_MAX_PAGES = 1000


def _funding_time(event: Mapping[str, Any]) -> int:
    try:
        return int(event["fundingTime"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise BinanceError("funding history 缺少合法 fundingTime") from exc


def validate_funding_coverage(
    events: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    *,
    expected_interval_hours: float | None = None,
) -> list[dict[str, Any]]:
    """校验 funding 时间窗、去重并按 fundingTime 排序。

    Binance 响应本身通过 ``startTime``/``endTime`` 和分页边界确认覆盖范围；
    如果调用方还知道结算间隔，则额外检查中间是否存在 coverage gap。
    """
    if end_ms < start_ms:
        raise BinanceError("funding history endTime 早于 startTime")
    by_time: dict[int, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            raise BinanceError("funding history 响应包含非法事件")
        timestamp = _funding_time(event)
        if timestamp < start_ms or timestamp > end_ms:
            raise BinanceError("funding history 返回 requested 时间窗之外的事件")
        previous = by_time.get(timestamp)
        if previous is not None and previous != event:
            raise BinanceError("同一 fundingTime 返回了冲突事件")
        by_time[timestamp] = event
    ordered = [by_time[timestamp] for timestamp in sorted(by_time)]
    if not ordered and end_ms > start_ms:
        raise BinanceError("funding history 为空，requested coverage 无法确认")
    if len(ordered) > 2 and expected_interval_hours is None:
        intervals = [
            current - previous
            for previous, current in zip(
                (_funding_time(event) for event in ordered),
                (_funding_time(event) for event in ordered[1:]),
            )
        ]
        if any(interval <= 0 for interval in intervals) or len(set(intervals)) != 1:
            raise BinanceError(
                "funding history settlement interval 不一致，coverage 无法确认"
            )
    if expected_interval_hours is not None and len(ordered) > 1:
        interval_ms = int(float(expected_interval_hours) * 3_600_000)
        if interval_ms <= 0:
            raise BinanceError("expected funding interval 必须为正数")
        timestamps = [_funding_time(event) for event in ordered]
        gaps = [
            (previous, current)
            for previous, current in zip(timestamps, timestamps[1:])
            if current - previous != interval_ms
        ]
        if gaps:
            previous, current = gaps[0]
            raise BinanceError(
                f"funding history coverage gap: {previous} -> {current}"
            )
    return ordered


class BinanceFuturesClient:
    def __init__(
        self,
        base_url: str = "https://fapi.binance.com",
        api_key: str = "",
        api_secret: str = "",
        recv_window: int = 5000,
        timeout: int = 15,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.recv_window = recv_window
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})

    # ---------- 底层 ----------

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        signature = hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    def _request(
        self, path: str, params: dict[str, Any] | None = None, signed: bool = False
    ) -> Any:
        params = dict(params or {})
        url = f"{self.base_url}{path}"
        try:
            if signed:
                params["timestamp"] = int(time.time() * 1000)
                params["recvWindow"] = self.recv_window
                url = f"{url}?{self._sign(params)}"
                resp = self.session.get(url, timeout=self.timeout)
            else:
                resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise BinanceError(f"网络请求失败：{exc}") from exc

        if resp.status_code >= 400:
            raise BinanceError(f"HTTP {resp.status_code} {path}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BinanceError(f"响应不是合法 JSON：{resp.text[:200]}") from exc

    # ---------- 公开接口 ----------

    def ping(self) -> bool:
        self._request("/fapi/v1/ping")
        return True

    def premium_index(self, symbol: str | None = None) -> Any:
        """标记价、指数价与当前资金费率。不传 symbol 返回全部合约。

        返回字段含 markPrice / indexPrice / lastFundingRate / nextFundingTime，
        足以同时支撑资金费与基差两类计算，无需再调现货接口。
        """
        params = {"symbol": symbol} if symbol else {}
        return self._request("/fapi/v1/premiumIndex", params)

    def ticker_24hr(self) -> list[dict[str, Any]]:
        """24 小时行情，用于按成交额过滤流动性不足的合约。"""
        data = self._request("/fapi/v1/ticker/24hr")
        return data if isinstance(data, list) else [data]

    def exchange_info(self) -> dict[str, Any]:
        """当前合约 metadata，用于确认 active USDT perpetual universe。"""
        data = self._request("/fapi/v1/exchangeInfo")
        return data if isinstance(data, dict) else {}

    def klines(
        self, symbol: str, interval: str = "1d", limit: int = 60
    ) -> list[list[Any]]:
        """K 线，用于计算已实现波动率（截面低波动策略的信号来源）。

        返回的每行形如
        [openTime, open, high, low, close, volume, closeTime, quoteVolume, ...]
        """
        data = self._request(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        return data if isinstance(data, list) else []

    def funding_history(
        self,
        symbol: str,
        start_ms: int,
        limit: int = 200,
        *,
        end_ms: int | None = None,
        max_pages: int = DEFAULT_FUNDING_MAX_PAGES,
        expected_interval_hours: float | None = None,
    ) -> list[dict[str, Any]]:
        """分页读取完整 funding history，覆盖 ``[startTime, endTime]``。

        ``limit`` 保留为第三个位置参数以兼容旧的只读 fixture；真实请求始终
        带明确的 ``startTime`` 和 ``endTime``。每次继续使用上一页最大
        ``fundingTime + 1``，并在页数、边界、重复和 coverage gap 异常时失败。
        """
        try:
            start_ms = int(start_ms)
            end_ms = int(time.time() * 1000) if end_ms is None else int(end_ms)
            page_limit = max(1, min(int(limit), FUNDING_API_MAX_LIMIT))
            max_pages = int(max_pages)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BinanceError("funding history 分页参数非法") from exc
        if end_ms < start_ms:
            raise BinanceError("funding history endTime 早于 startTime")
        if max_pages <= 0:
            raise BinanceError("funding history max_pages 必须为正数")

        cursor = start_ms
        collected: dict[int, dict[str, Any]] = {}
        for _ in range(max_pages):
            data = self._request(
                "/fapi/v1/fundingRate",
                {
                    "symbol": symbol,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": page_limit,
                },
            )
            if not isinstance(data, list):
                raise BinanceError("funding history 响应不是数组")
            if not data:
                break

            page_times: list[int] = []
            for item in data:
                if not isinstance(item, dict):
                    raise BinanceError("funding history 响应包含非法事件")
                timestamp = _funding_time(item)
                if timestamp < start_ms or timestamp > end_ms:
                    raise BinanceError("funding history 返回 requested 时间窗之外的事件")
                if timestamp < cursor and timestamp not in collected:
                    raise BinanceError("funding history 分页出现未预期的旧事件")
                previous = collected.get(timestamp)
                if previous is not None and previous != item:
                    raise BinanceError("同一 fundingTime 返回了冲突事件")
                collected[timestamp] = item
                page_times.append(timestamp)

            page_max = max(page_times)
            next_cursor = page_max + 1
            if next_cursor <= cursor:
                raise BinanceError("funding history 分页没有前进，coverage 无法确认")
            if page_max >= end_ms or len(data) < page_limit:
                break
            cursor = next_cursor
        else:
            raise BinanceError("funding history 超过最大分页数，coverage 无法确认")

        return validate_funding_coverage(
            list(collected.values()),
            start_ms,
            end_ms,
            expected_interval_hours=expected_interval_hours,
        )

    # ---------- 私有只读接口 ----------

    def account(self) -> dict[str, Any]:
        return self._request("/fapi/v2/account", signed=True)

    def position_risk_v2(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """v2 持仓信息：含 leverage / marginType / isolatedWallet，但无 maintMargin。"""
        params = {"symbol": symbol} if symbol else {}
        data = self._request("/fapi/v2/positionRisk", params, signed=True)
        return data if isinstance(data, list) else [data]

    def position_risk_v3(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """v3 持仓信息：含 maintMargin / initialMargin，但无 leverage。"""
        params = {"symbol": symbol} if symbol else {}
        data = self._request("/fapi/v3/positionRisk", params, signed=True)
        return data if isinstance(data, list) else [data]
