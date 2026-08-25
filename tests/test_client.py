from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from toss_market_terminal.client import READ_ONLY_PATHS, TossMarketClient
from toss_market_terminal.config import Credentials


@pytest.mark.asyncio
async def test_snapshot_calls_only_allowlisted_market_data() -> None:
    seen: list[str] = []
    candle_queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/v1/candles":
            candle_queries.append(dict(request.url.params))
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.headers["authorization"] == "Bearer test-token"
        responses = {
            "/api/v1/stocks": {
                "result": [
                    {
                        "symbol": "AAPL",
                        "name": "애플",
                        "englishName": "APPLE INC",
                        "market": "NASDAQ",
                        "currency": "USD",
                    }
                ]
            },
            "/api/v1/prices": {
                "result": [
                    {
                        "symbol": "AAPL",
                        "lastPrice": "185.70",
                        "currency": "USD",
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                ]
            },
            "/api/v1/orderbook": {
                "result": {"currency": "USD", "timestamp": None, "asks": [], "bids": []}
            },
            "/api/v1/trades": {
                "result": [
                    {
                        "price": "185.70",
                        "volume": "2",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "currency": "USD",
                    }
                ]
            },
            "/api/v1/candles": {
                "result": {
                    "candles": [
                        {
                            "timestamp": "2026-01-01T00:00:00Z",
                            "openPrice": "185",
                            "highPrice": "186",
                            "lowPrice": "184",
                            "closePrice": "185.7",
                            "volume": "100",
                            "currency": "USD",
                        }
                    ],
                    "nextBefore": None,
                }
            },
        }
        return httpx.Response(200, json=responses[request.url.path])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        snapshot = await client.snapshot("AAPL")

    assert snapshot.stock.symbol == "AAPL"
    assert len(snapshot.daily_candles) == 1
    assert seen.count("/oauth2/token") == 1
    assert seen.count("/api/v1/candles") == 2
    assert {query["interval"] for query in candle_queries} == {"1m", "1d"}
    assert any(query["interval"] == "1d" and query["count"] == "40" for query in candle_queries)
    assert set(seen[1:]) == READ_ONLY_PATHS
    assert not any(
        "account" in path or "order" in path for path in seen if path != "/api/v1/orderbook"
    )


@pytest.mark.asyncio
async def test_error_does_not_include_raw_response_or_secret() -> None:
    secret = "tssk_live_never_print_this"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            content=json.dumps({"error": "invalid_client", "debug": secret}).encode(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(Credentials("tsck_live_test", secret), http_client=http_client)
        with pytest.raises(Exception) as caught:
            await client.access_token()
    assert "invalid_client" in str(caught.value)
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_batch_prices_maps_results_independent_of_provider_order() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.url.path == "/api/v1/prices"
        symbols = request.url.params["symbols"]
        requested.append(symbols)
        assert "," in symbols
        # Deliberately shuffled response order.
        payload = {
            "005930": {
                "symbol": "005930",
                "lastPrice": "71000",
                "currency": "KRW",
                "timestamp": None,
            },
            "AAPL": {"symbol": "AAPL", "lastPrice": "185.70", "currency": "USD", "timestamp": None},
            "NVDA": {"symbol": "NVDA", "lastPrice": "130.10", "currency": "USD", "timestamp": None},
        }
        ordered = ["NVDA", "AAPL", "005930"]
        body = [payload[symbol] for symbol in ordered]
        return httpx.Response(200, json={"result": body})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        prices = await client.prices(["aapl", "NVDA", "005930"])

    assert requested == ["AAPL,NVDA,005930"]
    assert list(prices) == ["AAPL", "NVDA", "005930"]
    assert prices["NVDA"].last_price == Decimal("130.10")
    assert prices["005930"].currency == "KRW"


@pytest.mark.asyncio
async def test_batch_prices_rejects_out_of_bounds_and_duplicates() -> None:
    client = TossMarketClient.__new__(TossMarketClient)
    with pytest.raises(ValueError):
        await client.prices([])
    with pytest.raises(ValueError):
        await client.prices([f"S{i}" for i in range(201)])
    with pytest.raises(ValueError):
        await client.prices(["AAPL", "aapl"])
