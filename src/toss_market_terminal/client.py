from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx

from .config import Credentials
from .models import (
    MAX_BATCH_SYMBOLS,
    Account,
    AccountContext,
    BuyingPower,
    Candle,
    ClosedOrdersPage,
    ExchangeRate,
    HoldingsOverview,
    MarketSnapshot,
    OpenOrdersPage,
    Orderbook,
    PortfolioSnapshot,
    Price,
    StockInfo,
    Trade,
    as_account_seq,
    infer_account_currency,
    normalize_symbol,
)

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
# Phase-2: reference FX is public GET data but deliberately isolated from the
# unchanged market snapshot allowlist above.
EXCHANGE_RATE_READ_ONLY_PATHS = frozenset({"/api/v1/exchange-rate"})
# v0.6: read-only account-context GET endpoints. Account-scoped paths require
# the X-Tossinvest-Account header with a positive integer account_seq.
ACCOUNT_READ_ONLY_PATHS = frozenset(
    {
        "/api/v1/accounts",
        "/api/v1/holdings",
        "/api/v1/buying-power",
    }
)
# v0.6 restriction: only these currencies are accepted for buying-power lookups.
ACCOUNT_CURRENCY_CODES = frozenset({"KRW", "USD"})
# v0.8c: a separate, single-path allowlist for the read-only open-orders GET.
# Kept apart from ACCOUNT_READ_ONLY_PATHS deliberately: order data is more
# sensitive than accounts/holdings/buying-power and gets its own narrow gate.
OPEN_ORDERS_READ_ONLY_PATHS = frozenset({"/api/v1/orders"})


class TossApiError(RuntimeError):
    """Sanitized API failure: only an HTTP status and a safe short code.

    Deliberately a plain exception subclass rather than a frozen/slotted
    dataclass: slotted dataclasses leave ``__traceback__`` unwritable, which
    turns any escaping traceback attachment into a TypeError. Plain
    ``RuntimeError`` subclassing preserves the sanitized message behavior
    without that defect.
    """

    status_code: int
    code: str

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(status_code, code)
        self.status_code = status_code
        self.code = code

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
        self._token_refresh_margin = 60.0
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> TossMarketClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def access_token(self) -> str:
        if (
            self._access_token
            and time.monotonic() < self._token_expires_at - self._token_refresh_margin
        ):
            return self._access_token
        async with self._token_lock:
            if (
                self._access_token
                and time.monotonic() < self._token_expires_at - self._token_refresh_margin
            ):
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
            self._token_refresh_margin = min(60.0, max(1.0, expires_in * 0.1))
            self._token_expires_at = time.monotonic() + max(expires_in, 1)
            return token

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        if method != "GET":
            # Structural guarantee: this client can never issue mutations.
            raise ValueError(f"허용되지 않은 HTTP 메서드: {method}")
        token = await self.access_token()
        response = await self._http.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        return response

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if path not in READ_ONLY_PATHS:
            raise ValueError(f"허용되지 않은 API 경로: {path}")
        response = await self._request_json("GET", path, params=params)
        if response.status_code != 200:
            raise self._sanitized_error(response)
        try:
            body = response.json()
            return body["result"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TossApiError(response.status_code, "invalid-market-data-response") from exc

    async def _exchange_rate_get(self, path: str, params: dict[str, Any]) -> Any:
        if path not in EXCHANGE_RATE_READ_ONLY_PATHS:
            raise ValueError(f"허용되지 않은 API 경로: {path}")
        response = await self._request_json("GET", path, params=params)
        if response.status_code != 200:
            raise self._sanitized_error(response)
        try:
            body = response.json()
            result = body["result"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TossApiError(response.status_code, "invalid-exchange-rate-response") from exc
        if not isinstance(result, dict):
            raise TossApiError(response.status_code, "invalid-exchange-rate-response")
        return result

    async def _account_get(
        self,
        path: str,
        params: dict[str, Any],
        *,
        account_seq: int | None = None,
    ) -> Any:
        """GET an account-scoped endpoint with the X-Tossinvest-Account header.

        The path allowlist and the positive-integer seq check both run before
        any network activity, and failures never echo the raw request.
        ``/api/v1/accounts`` is account-listing only and sends no seq header.
        """
        if path not in ACCOUNT_READ_ONLY_PATHS:
            raise ValueError(f"허용되지 않은 API 경로: {path}")
        headers = {}
        token = await self.access_token()
        headers["Authorization"] = f"Bearer {token}"
        if path != "/api/v1/accounts":
            seq = as_account_seq(account_seq)
            headers["X-Tossinvest-Account"] = str(seq)
        response = await self._http.get(path, params=params, headers=headers)
        if response.status_code != 200:
            raise self._sanitized_error(response)
        try:
            body = response.json()
            result = body["result"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TossApiError(response.status_code, "invalid-account-data-response") from exc
        if not isinstance(result, (dict, list)):
            raise TossApiError(response.status_code, "invalid-account-data-response")
        return result

    async def _open_orders_get(self, path: str, params: dict[str, Any], *, account_seq: int) -> Any:
        """GET the open-orders endpoint with the X-Tossinvest-Account header.

        Gated by its own single-path allowlist (``OPEN_ORDERS_READ_ONLY_PATHS``),
        separate from both the market-data and the account-context allowlists.
        This method never issues a POST and never retries.
        """
        if path not in OPEN_ORDERS_READ_ONLY_PATHS:
            raise ValueError(f"허용되지 않은 API 경로: {path}")
        token = await self.access_token()
        headers = {"Authorization": f"Bearer {token}", "X-Tossinvest-Account": str(account_seq)}
        response = await self._http.get(path, params=params, headers=headers)
        if response.status_code != 200:
            raise self._sanitized_error(response)
        try:
            body = response.json()
            result = body["result"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TossApiError(response.status_code, "invalid-open-orders-response") from exc
        if not isinstance(result, dict):
            raise TossApiError(response.status_code, "invalid-open-orders-response")
        return result

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

    async def prices(self, symbols: list[str] | tuple[str, ...]) -> dict[str, Price]:
        """Batch current prices for 1-200 normalized unique symbols.

        Uses the official ``stocks``-style comma-separated batching on the
        read-only ``/api/v1/prices`` endpoint and maps results by their own
        symbol so provider response order is irrelevant.
        """
        normalized: list[str] = []
        for raw in symbols:
            symbol = normalize_symbol(raw)
            if symbol in normalized:
                raise ValueError(f"중복 심볼은 배치 조회할 수 없습니다: {symbol}")
            normalized.append(symbol)
        if not 1 <= len(normalized) <= MAX_BATCH_SYMBOLS:
            raise ValueError("배치 현재가 조회는 1~200개 심볼이어야 합니다.")
        result = await self._get("/api/v1/prices", {"symbols": ",".join(normalized)})
        if not isinstance(result, list):
            raise TossApiError(200, "invalid-prices-response")
        by_symbol = {price.symbol: price for price in (Price.from_api(item) for item in result)}
        missing = [symbol for symbol in normalized if symbol not in by_symbol]
        if missing:
            safe = "".join(ch for ch in ",".join(missing) if ch.isalnum() or ch in "-_")[:80]
            raise TossApiError(404, f"price-not-found:{safe}")
        return {symbol: by_symbol[symbol] for symbol in normalized}

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

    async def exchange_rate(self) -> ExchangeRate:
        result = await self._exchange_rate_get(
            "/api/v1/exchange-rate",
            {"baseCurrency": "USD", "quoteCurrency": "KRW"},
        )
        return ExchangeRate.from_api(result)

    async def snapshot(self, symbol: str) -> MarketSnapshot:
        stock, price, orderbook, trades = await asyncio.gather(
            self.stock(symbol),
            self.price(symbol),
            self.orderbook(symbol),
            self.trades(symbol),
        )
        candles, daily_candles = await asyncio.gather(
            self.candles(symbol, count=200),
            self.candles(symbol, interval="1d", count=200),
        )
        return MarketSnapshot(
            stock=stock,
            price=price,
            orderbook=orderbook,
            trades=trades,
            candles=candles,
            daily_candles=daily_candles,
        )

    # ------------------------------------------------------------------
    # v0.6 read-only account context.
    # ------------------------------------------------------------------

    async def accounts(self) -> tuple[Account, ...]:
        result = await self._account_get("/api/v1/accounts", {})
        if not isinstance(result, list):
            raise TossApiError(200, "invalid-accounts-response")
        accounts = tuple(Account.from_api(item) for item in result)
        seqs = [account.account_seq for account in accounts]
        if len(set(seqs)) != len(seqs):
            raise TossApiError(200, "duplicate-account-seq")
        return accounts

    async def holdings(self, account_seq: int, symbol: str | None = None) -> HoldingsOverview:
        # Validate before any token or network activity.
        normalized_symbol = normalize_symbol(symbol) if symbol is not None else None
        seq = as_account_seq(account_seq)
        params: dict[str, Any] = {}
        if normalized_symbol is not None:
            params["symbol"] = normalized_symbol
        result = await self._account_get("/api/v1/holdings", params, account_seq=seq)
        if not isinstance(result, dict):
            raise TossApiError(200, "invalid-holdings-response")
        return HoldingsOverview.from_api(result)

    async def buying_power(self, account_seq: int, currency: str) -> BuyingPower:
        # Strict validation before any token or network activity.
        if not isinstance(currency, str) or currency not in ACCOUNT_CURRENCY_CODES:
            raise ValueError("매수가능금액 조회 통화는 KRW 또는 USD만 지원합니다.")
        seq = as_account_seq(account_seq)
        result = await self._account_get(
            "/api/v1/buying-power", {"currency": currency}, account_seq=seq
        )
        if not isinstance(result, dict):
            raise TossApiError(200, "invalid-buying-power-response")
        power = BuyingPower.from_api(result)
        if power.currency != currency:
            # Fail closed when the response is not in the requested currency.
            raise TossApiError(200, "buying-power-currency-mismatch")
        return power

    def _select_account(self, accounts: tuple[Account, ...], account_seq: int | None) -> Account:
        """Explicit valid seq wins; otherwise auto-select a single BROKERAGE."""
        if account_seq is not None:
            seq = as_account_seq(account_seq)
            for account in accounts:
                if account.account_seq == seq:
                    return account
            raise ValueError("지정한 계좌 식별자를 찾을 수 없습니다.")
        brokerage = [a for a in accounts if a.is_brokerage]
        if len(brokerage) == 1:
            return brokerage[0]
        if not brokerage:
            raise ValueError("조회 가능한 종합매매(BROKERAGE) 계좌가 없습니다.")
        raise ValueError("종합매매 계좌가 여러 개입니다. --account-seq로 지정하세요.")

    async def account_context(self, symbol: str, account_seq: int | None = None) -> AccountContext:
        normalized = normalize_symbol(symbol)
        accounts = await self.accounts()
        account = self._select_account(accounts, account_seq)
        overview = await self.holdings(account.account_seq, symbol=normalized)
        item = overview.find_item(normalized)
        inferred_currency = item.currency if item else infer_account_currency(normalized)
        if inferred_currency not in ACCOUNT_CURRENCY_CODES:
            # Fail closed before any buying-power request for exotic currencies.
            raise ValueError(
                "지원하지 않는 보유 자산 통화입니다. KRW 또는 USD만 조회할 수 있습니다."
            )
        power = await self.buying_power(account.account_seq, inferred_currency)
        context = AccountContext(
            scope="account_read_only",
            order_endpoints_called=False,
            account=account,
            symbol=normalized,
            holding=item,
            holding_quantity=Decimal("0") if item is None else item.quantity,
            buying_power=power,
        )
        return context

    # ------------------------------------------------------------------
    # v0.8c read-only open orders.
    # ------------------------------------------------------------------

    async def open_orders(self, account_seq: int, symbol: str | None = None) -> OpenOrdersPage:
        # Validate before any token or network activity.
        normalized_symbol = normalize_symbol(symbol) if symbol is not None else None
        seq = as_account_seq(account_seq)
        params: dict[str, Any] = {"status": "OPEN"}
        if normalized_symbol is not None:
            params["symbol"] = normalized_symbol
        result = await self._open_orders_get("/api/v1/orders", params, account_seq=seq)
        return OpenOrdersPage.from_api(result)

    async def closed_orders(
        self,
        account_seq: int,
        *,
        start_date: date,
        end_date: date,
        limit: int = 20,
    ) -> ClosedOrdersPage:
        """Fetch one bounded first page of CLOSED orders for a KST date range."""
        seq = as_account_seq(account_seq)
        if (
            not isinstance(start_date, date)
            or isinstance(start_date, datetime)
            or not isinstance(end_date, date)
            or isinstance(end_date, datetime)
        ):
            raise ValueError("종료 주문 조회 기간은 date 값이어야 합니다.")
        if start_date > end_date:
            raise ValueError("종료 주문 조회 시작일은 종료일보다 늦을 수 없습니다.")
        if (end_date - start_date).days > 30:
            raise ValueError("종료 주문 조회 기간은 최대 31일(inclusive)입니다.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("종료 주문 조회 건수는 1~20이어야 합니다.")
        result = await self._open_orders_get(
            "/api/v1/orders",
            {
                "status": "CLOSED",
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
                "limit": limit,
            },
            account_seq=seq,
        )
        return ClosedOrdersPage.from_api(result)

    # ------------------------------------------------------------------
    # Phase-1 portfolio snapshot: one read-only fan-out over accounts,
    # holdings, KRW+USD buying power, and all OPEN orders. No mutation.
    # ------------------------------------------------------------------

    async def portfolio_snapshot(self, account_seq: int | None = None) -> PortfolioSnapshot:
        accounts = await self.accounts()
        account = self._select_account(accounts, account_seq)
        holdings, krw_power, usd_power, open_orders = await asyncio.gather(
            self.holdings(account.account_seq),
            self.buying_power(account.account_seq, "KRW"),
            self.buying_power(account.account_seq, "USD"),
            self.open_orders(account.account_seq),
        )
        return PortfolioSnapshot(
            scope="account_read_only",
            order_endpoints_called=False,
            account=account,
            krw_buying_power=krw_power,
            usd_buying_power=usd_power,
            holdings=holdings,
            open_orders=open_orders,
            synced_at=datetime.now(UTC),
        )
