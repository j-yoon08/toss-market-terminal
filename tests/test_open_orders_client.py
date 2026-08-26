"""v0.8c read-only open-orders client: path isolation, query, headers, envelopes."""

from __future__ import annotations

import json

import httpx
import pytest

from toss_market_terminal.client import (
    ACCOUNT_READ_ONLY_PATHS,
    OPEN_ORDERS_READ_ONLY_PATHS,
    READ_ONLY_PATHS,
    TossApiError,
    TossMarketClient,
)
from toss_market_terminal.config import Credentials
from toss_market_terminal.models import OpenOrdersPage


def credentials() -> Credentials:
    return Credentials("tsck_live_test", "tssk_live_test")


def token_handler(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/oauth2/token":
        return httpx.Response(
            200,
            json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600},
        )
    return None


OFFICIAL_ORDER = {
    "orderId": "bAGzNvMOOTa5Uy0xVzYNbxDJ3Qpobwau4jDF3hyZZGWbpHm7wha8CFZc7aXVOWAl",
    "symbol": "005930",
    "side": "BUY",
    "orderType": "LIMIT",
    "timeInForce": "DAY",
    "status": "PENDING",
    "price": "70000",
    "quantity": "10",
    "orderAmount": None,
    "currency": "KRW",
    "orderedAt": "2026-03-29T09:30:00+09:00",
    "canceledAt": None,
    "execution": {
        "filledQuantity": "0",
        "averageFilledPrice": None,
        "filledAmount": None,
        "commission": None,
        "tax": None,
        "filledAt": None,
        "settlementDate": None,
    },
}

OPEN_ORDERS_RESULT = {"orders": [OFFICIAL_ORDER], "nextCursor": None, "hasNext": False}


def mock_client(handler) -> tuple[TossMarketClient, httpx.AsyncClient]:
    """Build a client bound to an httpx.MockTransport — never the real network."""
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openapi.tossinvest.com",
    )
    client = TossMarketClient(credentials(), http_client=http_client)
    return client, http_client


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail hard if any open-orders test tries to open a real TCP connection."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external network attempted in open-orders tests")

    monkeypatch.setattr("socket.socket.connect", _blocked)


# --- Path isolation -----------------------------------------------------------


def test_open_orders_allowlist_is_exactly_one_path() -> None:
    assert OPEN_ORDERS_READ_ONLY_PATHS == frozenset({"/api/v1/orders"})


def test_open_orders_allowlist_is_disjoint_from_other_allowlists() -> None:
    assert OPEN_ORDERS_READ_ONLY_PATHS.isdisjoint(READ_ONLY_PATHS)
    assert OPEN_ORDERS_READ_ONLY_PATHS.isdisjoint(ACCOUNT_READ_ONLY_PATHS)


@pytest.mark.asyncio
async def test_open_orders_get_rejects_unknown_path_before_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network call attempted: {request.method} {request.url.path}")

    client, _ = mock_client(handler)
    try:
        with pytest.raises(ValueError):
            await client._open_orders_get("/api/v1/orders/history", {}, account_seq=1)
    finally:
        await client.close()


# --- open_orders() -------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_orders_sends_exact_get_path_query_and_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        seen.append(request)
        return httpx.Response(200, json={"result": OPEN_ORDERS_RESULT})

    client, _ = mock_client(handler)
    try:
        page = await client.open_orders(7, symbol="005930")
    finally:
        await client.close()

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/api/v1/orders"
    assert dict(request.url.params) == {"status": "OPEN", "symbol": "005930"}
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.headers["x-tossinvest-account"] == "7"
    assert isinstance(page, OpenOrdersPage)
    assert page.orders[0].symbol == "005930"


@pytest.mark.asyncio
async def test_open_orders_without_symbol_omits_symbol_query_param() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        seen.append(request)
        return httpx.Response(200, json={"result": OPEN_ORDERS_RESULT})

    client, _ = mock_client(handler)
    try:
        await client.open_orders(7)
    finally:
        await client.close()

    assert dict(seen[0].url.params) == {"status": "OPEN"}


@pytest.mark.asyncio
async def test_open_orders_normalizes_lowercase_symbol_before_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        seen.append(request)
        return httpx.Response(200, json={"result": OPEN_ORDERS_RESULT})

    client, _ = mock_client(handler)
    try:
        await client.open_orders(7, symbol="aapl")
    finally:
        await client.close()

    assert seen[0].url.params.get("symbol") == "AAPL"


@pytest.mark.asyncio
async def test_open_orders_never_issues_post() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        assert request.method == "GET", f"unexpected method {request.method}"
        return httpx.Response(200, json={"result": OPEN_ORDERS_RESULT})

    client, _ = mock_client(handler)
    try:
        await client.open_orders(7)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_open_orders_rejects_non_positive_or_non_integer_account_seq() -> None:
    client, _ = mock_client(lambda request: httpx.Response(500))
    try:
        for bad_seq in (0, -1, "7", 1.5, True, None):
            with pytest.raises(ValueError):
                await client.open_orders(bad_seq)  # type: ignore[arg-type]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_open_orders_rejects_malformed_symbol_before_network() -> None:
    client, _ = mock_client(lambda request: httpx.Response(500))
    try:
        with pytest.raises(ValueError):
            await client.open_orders(7, symbol="not a symbol!!")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_open_orders_rejects_paged_envelope_next_cursor_not_null() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        return httpx.Response(
            200,
            json={
                "result": {
                    "orders": [OFFICIAL_ORDER],
                    "nextCursor": "some-cursor",
                    "hasNext": False,
                }
            },
        )

    client, _ = mock_client(handler)
    try:
        with pytest.raises(ValueError):
            await client.open_orders(7)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_open_orders_rejects_paged_envelope_has_next_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        return httpx.Response(
            200,
            json={"result": {"orders": [OFFICIAL_ORDER], "nextCursor": None, "hasNext": True}},
        )

    client, _ = mock_client(handler)
    try:
        with pytest.raises(ValueError):
            await client.open_orders(7)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_open_orders_requires_result_envelope() -> None:
    bodies: list[object] = [[], {"data": []}, {"result": "nope"}, {}]
    for body in bodies:

        def handler(request: httpx.Request, _body: object = body) -> httpx.Response:
            response = token_handler(request)
            if response is not None:
                return response
            return httpx.Response(200, json=_body)

        client, _ = mock_client(handler)
        try:
            with pytest.raises(Exception) as caught:
                await client.open_orders(7)
        finally:
            await client.close()
        assert not isinstance(caught.value, (KeyError, TypeError))


@pytest.mark.asyncio
async def test_open_orders_malformed_order_item_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        return httpx.Response(
            200,
            json={
                "result": {
                    "orders": [{"symbol": "005930"}],
                    "nextCursor": None,
                    "hasNext": False,
                }
            },
        )

    client, _ = mock_client(handler)
    try:
        with pytest.raises(ValueError):
            await client.open_orders(7)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_open_orders_error_response_is_sanitized() -> None:
    raw_secret = "tssk_live_leak_me"

    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        return httpx.Response(
            403,
            content=json.dumps(
                {"error": {"code": "forbidden", "message": f"denied {raw_secret}"}}
            ).encode(),
        )

    client, _ = mock_client(handler)
    try:
        with pytest.raises(TossApiError) as caught:
            await client.open_orders(7)
    finally:
        await client.close()
    text = str(caught.value)
    assert "403" in text and raw_secret not in text and "denied" not in text
