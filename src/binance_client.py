"""币安 U 本位合约只读 REST 客户端。

只使用读取类接口。本模块不包含任何下单、撤单、划转能力，
以便配合"只读 API Key + IP 白名单"的安全要求。

签名：query string + HMAC-SHA256(secret)。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import requests


class BinanceError(Exception):
    """币安接口返回错误或网络异常。"""


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
        self, symbol: str, start_ms: int, limit: int = 200
    ) -> list[dict[str, Any]]:
        """历史资金费结算记录，用于前向验证时回填真实资金费收支。"""
        data = self._request(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": start_ms, "limit": limit},
        )
        return data if isinstance(data, list) else []

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
