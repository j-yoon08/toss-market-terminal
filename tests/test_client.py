from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from toss_market_terminal.client import READ_ONLY_PATHS, TossApiError, TossMarketClient
from toss_market_terminal.config import Credentials
from toss_market_terminal.models import MarketSnapshot


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
    assert all(query["count"] == "200" for query in candle_queries)
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


# --- Provider response <-> request symbol/currency binding -----------------


def _token_or(
    path_responses: dict[str, dict],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600},
            )
        return httpx.Response(200, json=path_responses[request.url.path])

    return handler


@pytest.mark.asyncio
async def test_stock_rejects_response_for_a_different_symbol() -> None:
    handler = _token_or(
        {
            "/api/v1/stocks": {
                "result": [
                    {
                        "symbol": "NVDA",
                        "name": "엔비디아",
                        "englishName": "NVIDIA CORP",
                        "market": "NASDAQ",
                        "currency": "USD",
                    }
                ]
            }
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        with pytest.raises(TossApiError) as caught:
            await client.stock("AAPL")
    assert caught.value.status_code == 200
    assert caught.value.code == "stock-symbol-mismatch"
    assert "NVDA" not in str(caught.value)


@pytest.mark.asyncio
async def test_price_rejects_response_for_a_different_symbol() -> None:
    handler = _token_or(
        {
            "/api/v1/prices": {
                "result": [
                    {"symbol": "NVDA", "lastPrice": "130.10", "currency": "USD", "timestamp": None}
                ]
            }
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        with pytest.raises(TossApiError) as caught:
            await client.price("AAPL")
    assert caught.value.status_code == 200
    assert caught.value.code == "price-symbol-mismatch"
    assert "NVDA" not in str(caught.value)


@pytest.mark.asyncio
async def test_batch_prices_rejects_an_unrequested_returned_symbol() -> None:
    handler = _token_or(
        {
            "/api/v1/prices": {
                "result": [
                    {"symbol": "AAPL", "lastPrice": "185.70", "currency": "USD", "timestamp": None},
                    {"symbol": "NVDA", "lastPrice": "130.10", "currency": "USD", "timestamp": None},
                ]
            }
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        with pytest.raises(TossApiError) as caught:
            await client.prices(["AAPL", "MSFT"])
    assert caught.value.status_code == 200
    assert caught.value.code == "price-unrequested-symbol"
    assert "NVDA" not in str(caught.value)


@pytest.mark.asyncio
async def test_batch_prices_rejects_a_duplicate_returned_symbol() -> None:
    handler = _token_or(
        {
            "/api/v1/prices": {
                "result": [
                    {"symbol": "AAPL", "lastPrice": "185.70", "currency": "USD", "timestamp": None},
                    {"symbol": "AAPL", "lastPrice": "186.00", "currency": "USD", "timestamp": None},
                ]
            }
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        with pytest.raises(TossApiError) as caught:
            await client.prices(["AAPL", "MSFT"])
    assert caught.value.status_code == 200
    assert caught.value.code == "price-duplicate-symbol"


@pytest.mark.asyncio
async def test_batch_prices_accepts_lowercase_returned_symbol_keyed_normalized() -> None:
    handler = _token_or(
        {
            "/api/v1/prices": {
                "result": [
                    {"symbol": "aapl", "lastPrice": "185.70", "currency": "USD", "timestamp": None},
                    {"symbol": "MSFT", "lastPrice": "410.00", "currency": "USD", "timestamp": None},
                ]
            }
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        prices = await client.prices(["AAPL", "MSFT"])
    assert list(prices) == ["AAPL", "MSFT"]
    assert prices["AAPL"].last_price == Decimal("185.70")


@pytest.mark.asyncio
async def test_batch_prices_rejects_case_variant_duplicate_returned_symbol() -> None:
    handler = _token_or(
        {
            "/api/v1/prices": {
                "result": [
                    {"symbol": "AAPL", "lastPrice": "185.70", "currency": "USD", "timestamp": None},
                    {"symbol": "aapl", "lastPrice": "186.00", "currency": "USD", "timestamp": None},
                ]
            }
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        with pytest.raises(TossApiError) as caught:
            await client.prices(["AAPL", "MSFT"])
    assert caught.value.status_code == 200
    assert caught.value.code == "price-duplicate-symbol"


def _snapshot_responses(**currency_overrides: str) -> dict[str, dict]:
    """One otherwise-fully-consistent snapshot response set, USD everywhere.

    Pass e.g. ``trade="KRW"`` to break exactly one leg's currency so a test
    can attribute the resulting rejection to that leg.
    """
    currency = {
        "stock": "USD",
        "price": "USD",
        "orderbook": "USD",
        "trade": "USD",
        "candle": "USD",
        **currency_overrides,
    }
    return {
        "/api/v1/stocks": {
            "result": [
                {
                    "symbol": "AAPL",
                    "name": "애플",
                    "englishName": "APPLE INC",
                    "market": "NASDAQ",
                    "currency": currency["stock"],
                }
            ]
        },
        "/api/v1/prices": {
            "result": [
                {
                    "symbol": "AAPL",
                    "lastPrice": "185.70",
                    "currency": currency["price"],
                    "timestamp": "2026-01-01T00:00:00Z",
                }
            ]
        },
        "/api/v1/orderbook": {
            "result": {
                "currency": currency["orderbook"],
                "timestamp": None,
                "asks": [],
                "bids": [],
            }
        },
        "/api/v1/trades": {
            "result": [
                {
                    "price": "185.70",
                    "volume": "2",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "currency": currency["trade"],
                }
            ]
        },
        # Same fixture answers both the 1m and 1d candle requests (as in
        # test_snapshot_calls_only_allowlisted_market_data above), so this
        # exercises both "every 1m candle" and "the daily candle" at once.
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
                        "currency": currency["candle"],
                    }
                ],
                "nextBefore": None,
            }
        },
    }


async def _run_snapshot(responses: dict[str, dict]) -> MarketSnapshot:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_token_or(responses)),
        base_url="https://openapi.tossinvest.com",
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        return await client.snapshot("AAPL")


@pytest.mark.asyncio
async def test_snapshot_succeeds_when_fully_consistent() -> None:
    snapshot = await _run_snapshot(_snapshot_responses())
    assert snapshot.stock.symbol == "AAPL"
    assert snapshot.price.symbol == "AAPL"
    assert snapshot.stock.currency == "USD"
    assert snapshot.price.currency == "USD"
    assert snapshot.orderbook.currency == "USD"
    assert all(trade.currency == "USD" for trade in snapshot.trades)
    assert all(candle.currency == "USD" for candle in snapshot.candles)
    assert all(candle.currency == "USD" for candle in snapshot.daily_candles)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("price", "snapshot-price-currency-mismatch"),
        ("orderbook", "snapshot-orderbook-currency-mismatch"),
        ("trade", "snapshot-trade-currency-mismatch"),
        ("candle", "snapshot-candle-currency-mismatch"),
    ],
)
async def test_snapshot_rejects_currency_mismatch(field: str, expected_code: str) -> None:
    responses = _snapshot_responses(**{field: "KRW"})
    with pytest.raises(TossApiError) as caught:
        await _run_snapshot(responses)
    assert caught.value.status_code == 200
    assert caught.value.code == expected_code


# --- GET-only early-401 recovery --------------------------------------------


def _seed_stale_token(client: TossMarketClient, token: str = "stale-token") -> None:
    """Preloads a still-fresh-looking cached token without hitting the network."""
    client._access_token = token
    client._token_expires_at = time.monotonic() + 3600


@pytest.mark.asyncio
async def test_market_get_stale_token_401_recovers_with_one_refresh_and_retry() -> None:
    token_calls = 0
    price_attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth2/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "fresh-token", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.url.path == "/api/v1/prices"
        auth = request.headers["authorization"]
        price_attempts.append(auth)
        if auth == "Bearer fresh-token":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "symbol": "AAPL",
                            "lastPrice": "185.70",
                            "currency": "USD",
                            "timestamp": None,
                        }
                    ]
                },
            )
        return httpx.Response(401, json={"error": "invalid-token"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        _seed_stale_token(client)
        price = await client.price("AAPL")

    assert price.last_price == Decimal("185.70")
    assert price_attempts == ["Bearer stale-token", "Bearer fresh-token"]
    assert token_calls == 1


@pytest.mark.asyncio
async def test_account_get_stale_token_401_recovers_with_one_refresh_and_retry() -> None:
    token_calls = 0
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth2/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "fresh-token", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.url.path == "/api/v1/buying-power"
        assert request.headers["x-tossinvest-account"] == "7"
        auth = request.headers["authorization"]
        attempts.append(auth)
        if auth == "Bearer fresh-token":
            return httpx.Response(
                200, json={"result": {"currency": "USD", "cashBuyingPower": "500"}}
            )
        return httpx.Response(401, json={"error": "invalid-token"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        _seed_stale_token(client)
        power = await client.buying_power(7, "USD")

    assert power.cash_buying_power == Decimal("500")
    assert attempts == ["Bearer stale-token", "Bearer fresh-token"]
    assert token_calls == 1


@pytest.mark.asyncio
async def test_exchange_rate_get_stale_token_401_recovers_with_one_refresh_and_retry() -> None:
    token_calls = 0
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth2/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "fresh-token", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.url.path == "/api/v1/exchange-rate"
        auth = request.headers["authorization"]
        attempts.append(auth)
        if auth == "Bearer fresh-token":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "baseCurrency": "USD",
                        "quoteCurrency": "KRW",
                        "rate": "1350.5",
                        "midRate": "1350.5",
                        "basisPoint": "0",
                        "rateChangeType": "EQUAL",
                        "validFrom": "2026-01-01T00:00:00Z",
                        "validUntil": "2026-01-01T00:01:00Z",
                    }
                },
            )
        return httpx.Response(401, json={"error": "invalid-token"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        _seed_stale_token(client)
        rate = await client.exchange_rate()

    assert rate.mid_rate == Decimal("1350.5")
    assert attempts == ["Bearer stale-token", "Bearer fresh-token"]
    assert token_calls == 1


@pytest.mark.asyncio
async def test_get_stops_after_exactly_two_attempts_when_401_persists() -> None:
    token_calls = 0
    candle_attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth2/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"fresh-token-{token_calls}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        assert request.url.path == "/api/v1/candles"
        candle_attempts.append(request.headers["authorization"])
        return httpx.Response(401, json={"error": "invalid-token"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        _seed_stale_token(client)
        with pytest.raises(TossApiError) as caught:
            await client.candles("AAPL", count=10)

    assert caught.value.status_code == 401
    assert len(candle_attempts) == 2
    assert candle_attempts[0] != candle_attempts[1]
    assert token_calls == 1


@pytest.mark.asyncio
async def test_non_401_error_is_never_retried() -> None:
    candle_attempts = 0
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal candle_attempts, token_calls
        if request.url.path == "/oauth2/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "fresh-token", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.url.path == "/api/v1/candles"
        candle_attempts += 1
        return httpx.Response(500, content=b"internal error")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        with pytest.raises(TossApiError) as caught:
            await client.candles("AAPL", count=10)

    assert caught.value.status_code == 500
    assert candle_attempts == 1
    assert token_calls == 1  # only the initial token issuance, no 401 recovery triggered


@pytest.mark.asyncio
async def test_concurrent_candle_401s_trigger_exactly_one_refresh() -> None:
    token_calls = 0
    current_valid_token = "fresh-token-0"  # deliberately not the seeded stale token

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, current_valid_token
        if request.url.path == "/oauth2/token":
            token_calls += 1
            current_valid_token = f"fresh-token-{token_calls}"
            return httpx.Response(
                200,
                json={
                    "access_token": current_valid_token,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        assert request.url.path == "/api/v1/candles"
        if request.headers["authorization"] == f"Bearer {current_valid_token}":
            return httpx.Response(200, json={"result": {"candles": [], "nextBefore": None}})
        return httpx.Response(401, json={"error": "invalid-token"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(
            Credentials("tsck_live_test", "tssk_live_test"), http_client=http_client
        )
        _seed_stale_token(client)
        results = await asyncio.gather(
            client.candles("AAPL", interval="1m", count=10),
            client.candles("AAPL", interval="1d", count=10),
        )

    assert results == [(), ()]
    assert token_calls == 1


@pytest.mark.asyncio
async def test_401_recovery_never_leaks_token_or_body_on_final_failure() -> None:
    secret = "tssk_live_never_print_this"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "fresh-token", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.url.path == "/api/v1/candles"
        return httpx.Response(
            401, content=json.dumps({"error": "invalid-token", "debug": secret}).encode()
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        client = TossMarketClient(Credentials("tsck_live_test", secret), http_client=http_client)
        _seed_stale_token(client)
        with pytest.raises(TossApiError) as caught:
            await client.candles("AAPL", count=10)

    assert "invalid-token" in str(caught.value)
    assert secret not in str(caught.value)
    assert "stale-token" not in str(caught.value)
