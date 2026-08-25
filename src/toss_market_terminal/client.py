from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Credentials
from .models import Candle, MarketSnapshot, Orderbook, Price, StockInfo, Trade

API_BASE_URL = "https://openapi.tossinvest.com"
READ_ONLY_PATHS = frozenset(
    {
        "/api/v1/stocks",
        "/api/v1/prices",
        "/api/v1/orderbook",
        "/api/v1/trades",
        "/api/v1/candles",
    }
)


@dataclass(frozen=True, slots=True)
class TossApiError(RuntimeError):
    status_code: int
    code: str

    def __str__(self) -> str:
        return f"Toss Open API 요청 실패 (HTTP {self.status_code}, code={self.code})"


class TossMarketClient:
    """Strictly read-only client for public Toss market-data endpoints."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credentials = credentials
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=API_BASE_URL,
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
        )
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> TossMarketClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def access_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at - 60:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires_at - 60:
                return self._access_token
            response = await self._http.post(
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._credentials.client_id,
                    "client_secret": self._credentials.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code != 200:
                raise self._sanitized_error(response)
            try:
                body = response.json()
                token = body["access_token"]
                expires_in = int(body["expires_in"])
            except (KeyError, TypeError, ValueError) as exc:
                raise TossApiError(response.status_code, "invalid-token-response") from exc
            if not isinstance(token, str) or not token:
                raise TossApiError(response.status_code, "invalid-token-response")
            self._access_token = token
            self._token_expires_at = time.monotonic() + max(expires_in, 1)
            return token

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if path not in READ_ONLY_PATHS:
            raise ValueError(f"허용되지 않은 API 경로: {path}")
        token = await self.access_token()
        response = await self._http.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 200:
            raise self._sanitized_error(response)
        try:
            body = response.json()
            return body["result"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TossApiError(response.status_code, "invalid-market-data-response") from exc

    @staticmethod
    def _sanitized_error(response: httpx.Response) -> TossApiError:
        code = "http-error"
        try:
            body = response.json()
            if isinstance(body, dict):
                if isinstance(body.get("error"), str):
                    code = body["error"]
                elif isinstance(body.get("error"), dict):
                    raw_code = body["error"].get("code")
                    if isinstance(raw_code, str):
                        code = raw_code
        except ValueError:
            pass
        safe_code = "".join(ch for ch in code if ch.isalnum() or ch in "-_")[:80]
        return TossApiError(response.status_code, safe_code or "http-error")

    async def stock(self, symbol: str) -> StockInfo:
        result = await self._get("/api/v1/stocks", {"symbols": symbol})
        if not isinstance(result, list) or not result:
            raise TossApiError(404, "stock-not-found")
        return StockInfo.from_api(result[0])

    async def price(self, symbol: str) -> Price:
        result = await self._get("/api/v1/prices", {"symbols": symbol})
        if not isinstance(result, list) or not result:
            raise TossApiError(404, "price-not-found")
        return Price.from_api(result[0])

    async def orderbook(self, symbol: str) -> Orderbook:
        result = await self._get("/api/v1/orderbook", {"symbol": symbol})
        if not isinstance(result, dict):
            raise TossApiError(200, "invalid-orderbook-response")
        return Orderbook.from_api(result)

    async def trades(self, symbol: str, count: int = 30) -> tuple[Trade, ...]:
        if not 1 <= count <= 50:
            raise ValueError("체결 조회 건수는 1~50이어야 합니다.")
        result = await self._get("/api/v1/trades", {"symbol": symbol, "count": count})
        if not isinstance(result, list):
            raise TossApiError(200, "invalid-trades-response")
        return tuple(Trade.from_api(item) for item in result)

    async def candles(
        self, symbol: str, *, interval: str = "1m", count: int = 40
    ) -> tuple[Candle, ...]:
        if interval not in {"1m", "1d"}:
            raise ValueError("캔들 간격은 1m 또는 1d만 지원합니다.")
        if not 1 <= count <= 200:
            raise ValueError("캔들 조회 건수는 1~200이어야 합니다.")
        result = await self._get(
            "/api/v1/candles",
            {"symbol": symbol, "interval": interval, "count": count, "adjusted": "true"},
        )
        if not isinstance(result, dict) or not isinstance(result.get("candles"), list):
            raise TossApiError(200, "invalid-candles-response")
        return tuple(Candle.from_api(item) for item in result["candles"])

    async def snapshot(self, symbol: str) -> MarketSnapshot:
        stock, price, orderbook, trades, candles = await asyncio.gather(
            self.stock(symbol),
            self.price(symbol),
            self.orderbook(symbol),
            self.trades(symbol),
            self.candles(symbol),
        )
        return MarketSnapshot(
            stock=stock,
            price=price,
            orderbook=orderbook,
            trades=trades,
            candles=candles,
        )
