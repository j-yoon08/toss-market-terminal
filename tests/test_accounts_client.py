"""v0.6 read-only account client: path isolation, headers, envelopes, privacy."""

from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal

import httpx
import pytest

from toss_market_terminal.client import (
    ACCOUNT_CURRENCY_CODES,
    ACCOUNT_READ_ONLY_PATHS,
    READ_ONLY_PATHS,
    TossApiError,
    TossMarketClient,
)
from toss_market_terminal.config import Credentials
from toss_market_terminal.models import AccountContext


def credentials() -> Credentials:
    return Credentials("tsck_live_test", "tssk_live_test")


def token_handler(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/oauth2/token":
        return httpx.Response(
            200,
            json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600},
        )
    return None


ACCOUNTS_BODY = {
    "result": [
        {"accountNo": "12345678901", "accountSeq": 1, "accountType": "BROKERAGE"},
        {"accountNo": "98765432109", "accountSeq": 2, "accountType": "PENSION_SAVINGS"},
        {"accountNo": "00000000002", "accountSeq": 3, "accountType": "SOMETHING_NEW"},
    ]
}

HOLDINGS_RESULT = {
    "totalPurchaseAmount": {"krw": "6500000", "usd": None},
    "marketValue": {
        "amount": {"krw": "7200000", "usd": None},
        "amountAfterCost": {"krw": "7050000", "usd": None},
    },
    "profitLoss": {
        "amount": {"krw": "700000", "usd": None},
        "amountAfterCost": {"krw": "550000", "usd": None},
        "rate": "0.1077",
        "rateAfterCost": "0.0846",
    },
    "dailyProfitLoss": {"amount": {"krw": "100000", "usd": None}, "rate": "0.0141"},
    "items": [
        {
            "symbol": "005930",
            "name": "삼성전자",
            "marketCountry": "KR",
            "currency": "KRW",
            "quantity": "100",
            "lastPrice": "72000",
            "averagePurchasePrice": "65000",
            "marketValue": {
                "purchaseAmount": "6500000",
                "amount": "7200000",
                "amountAfterCost": "7050000",
            },
            "profitLoss": {
                "amount": "700000",
                "amountAfterCost": "550000",
                "rate": "0.1077",
                "rateAfterCost": "0.0846",
            },
            "dailyProfitLoss": {"amount": "100000", "rate": "0.0141"},
            "cost": {"commission": "14400", "tax": "135600"},
        }
    ],
}


def mock_client(handler) -> tuple[TossMarketClient, httpx.AsyncClient]:
    """Build a client bound to an httpx.MockTransport — never the real network.

    The mock transport is passed INTO TossMarketClient so every request,
    including the OAuth2 token POST, is served by ``handler``. A regression
    test asserts no socket connection can be attempted at all.
    """
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openapi.tossinvest.com",
    )
    client = TossMarketClient(credentials(), http_client=http_client)
    return client, http_client


def _assert_mock_transport_only(client: TossMarketClient) -> None:
    transport = getattr(client._http, "_transport", None)
    assert isinstance(transport, httpx.MockTransport), (
        "account tests must run against httpx.MockTransport only"
    )


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail hard if any account test tries to open a real TCP connection."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external network attempted in account tests")

    monkeypatch.setattr("socket.socket.connect", _blocked)


@pytest.mark.asyncio
async def test_all_account_client_tests_use_mock_transport_only() -> None:
    """Regression: the injected MockTransport must be wired into the client.

    Passing the AsyncClient without ``http_client=`` makes TossMarketClient
    build its own real client, so tests would hit the live token endpoint
    with fake credentials (observed as 401/429 responses).
    """
    handler = lambda request: token_handler(request) or httpx.Response(200, json={"result": {}})  # noqa: E731
    client, http_client = mock_client(handler)
    try:
        assert client._http is http_client
        _assert_mock_transport_only(client)
        # Token endpoint also served by the mock, not by the network.
        assert await client.access_token() == "test-token"
    finally:
        await client.close()


# --- Path isolation ---------------------------------------------------------


def test_account_read_only_paths_are_exactly_the_three_get_endpoints() -> None:
    assert ACCOUNT_READ_ONLY_PATHS == frozenset(
        {
            "/api/v1/accounts",
            "/api/v1/holdings",
            "/api/v1/buying-power",
        }
    )


def test_market_data_allowlist_unchanged_and_disjoint() -> None:
    assert READ_ONLY_PATHS == frozenset(
        {
            "/api/v1/stocks",
            "/api/v1/prices",
            "/api/v1/orderbook",
            "/api/v1/trades",
            "/api/v1/candles",
        }
    )
    assert READ_ONLY_PATHS.isdisjoint(ACCOUNT_READ_ONLY_PATHS)


def test_private_request_helper_rejects_non_get_methods() -> None:
    """POST/PUT/PATCH/DELETE can never be issued, even to an allowlisted path."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network call attempted: {request.method} {request.url.path}")

    client, _ = mock_client(handler)

    import anyio

    async def check() -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(ValueError):
                await client._request_json(method, "/api/v1/accounts")

    try:
        anyio.run(check)
    finally:
        anyio.run(client.close)


@pytest.mark.asyncio
async def test_unknown_path_is_rejected_before_network() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        response = token_handler(request)
        return response or httpx.Response(200, json={})

    client, _ = mock_client(handler)
    try:
        with pytest.raises(ValueError):
            await client._get("/api/v1/orders", {})
        with pytest.raises(ValueError):
            await client._get("/api/v1/prices/delete", {})
        assert not any(path.startswith("/api/v1/orders") for path in seen)
    finally:
        await client.close()


# --- accounts() -------------------------------------------------------------


@pytest.mark.asyncio
async def test_accounts_parses_official_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        assert request.url.path == "/api/v1/accounts"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json=ACCOUNTS_BODY,
        )

    client, _ = mock_client(handler)
    try:
        accounts = await client.accounts()
    finally:
        await client.close()

    assert [a.account_seq for a in accounts] == [1, 2, 3]
    assert [a.account_type for a in accounts] == ["BROKERAGE", "PENSION_SAVINGS", "SOMETHING_NEW"]
    assert accounts[0].masked_account_no == "*******8901"
    # Raw account numbers must not survive anywhere in the returned models.
    dumped = json.dumps([a.masked_account_no for a in accounts])
    assert "12345678901" not in dumped and "98765432109" not in dumped


@pytest.mark.asyncio
async def test_accounts_requires_result_envelope() -> None:
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
                await client.accounts()
        finally:
            await client.close()
        assert not isinstance(caught.value, (KeyError, TypeError))


@pytest.mark.asyncio
async def test_accounts_error_response_is_sanitized() -> None:
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
        with pytest.raises(Exception) as caught:
            await client.accounts()
    finally:
        await client.close()
    text = str(caught.value)
    assert "403" in text and raw_secret not in text and "denied" not in text


# --- holdings / buying_power ------------------------------------------------


def account_request_handler(
    request: httpx.Request,
    *,
    expected_seq: int,
    result: object,
    expect_symbol: str | None = None,
    expect_currency: str | None = None,
) -> httpx.Response:
    response = token_handler(request)
    if response is not None:
        return response
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.headers["x-tossinvest-account"] == str(expected_seq)
    if request.url.path == "/api/v1/holdings":
        if expect_symbol is not None:
            assert request.url.params.get("symbol") == expect_symbol
        return httpx.Response(200, json={"result": result})
    if request.url.path == "/api/v1/buying-power":
        if expect_currency is not None:
            assert request.url.params.get("currency") == expect_currency
        return httpx.Response(200, json={"result": result})
    raise AssertionError(f"unexpected path {request.url.path}")


@pytest.mark.asyncio
async def test_holdings_sends_account_header_and_parses_overview() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return account_request_handler(
            request,
            expected_seq=7,
            result=dict(HOLDINGS_RESULT),
            expect_symbol="005930",
        )

    client, _ = mock_client(handler)
    try:
        overview = await client.holdings(7, symbol="005930")
    finally:
        await client.close()
    assert overview.items[0].symbol == "005930"
    assert overview.items[0].quantity == Decimal("100")


@pytest.mark.asyncio
async def test_buying_power_sends_account_and_currency_headers_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return account_request_handler(
            request,
            expected_seq=7,
            result={"currency": "KRW", "cashBuyingPower": "5000000"},
            expect_currency="KRW",
        )

    client, _ = mock_client(handler)
    try:
        power = await client.buying_power(7, "KRW")
    finally:
        await client.close()
    assert power.cash_buying_power == Decimal("5000000")


@pytest.mark.asyncio
async def test_account_scoped_calls_require_positive_integer_seq() -> None:
    client, _ = mock_client(lambda request: httpx.Response(500))
    try:
        for bad_seq in (0, -1, "7", 1.5, True, None):
            with pytest.raises(ValueError):
                await client.holdings(bad_seq)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                await client.buying_power(bad_seq, "KRW")  # type: ignore[arg-type]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_buying_power_restricts_currency_to_krw_usd() -> None:
    client, _ = mock_client(lambda request: httpx.Response(500))
    try:
        for bad in ("EUR", "JPY", "", "krw"):
            with pytest.raises(ValueError):
                await client.buying_power(7, bad)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_account_constants_are_krw_usd_only() -> None:
    assert ACCOUNT_CURRENCY_CODES == frozenset({"KRW", "USD"})


@pytest.mark.asyncio
async def test_non_200_account_responses_raise_sanitized_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        return httpx.Response(
            400,
            content=json.dumps({"error": {"code": "account-header-required"}}).encode(),
        )

    client, _ = mock_client(handler)
    try:
        with pytest.raises(Exception) as caught:
            await client.holdings(7)
    finally:
        await client.close()
    assert "account-header-required" in str(caught.value)


# --- account_context ---------------------------------------------------------


def context_handler(request: httpx.Request) -> httpx.Response:
    response = token_handler(request)
    if response is not None:
        return response
    if request.url.path != "/api/v1/accounts":
        assert request.headers["x-tossinvest-account"] == "1"
    if request.url.path == "/api/v1/accounts":
        return httpx.Response(
            200,
            json={
                "result": [
                    {"accountNo": "12345678901", "accountSeq": 1, "accountType": "BROKERAGE"}
                ]
            },
        )
    if request.url.path == "/api/v1/holdings":
        return httpx.Response(200, json={"result": dict(HOLDINGS_RESULT)})
    if request.url.path == "/api/v1/buying-power":
        return httpx.Response(
            200, json={"result": {"currency": "KRW", "cashBuyingPower": "5000000"}}
        )
    raise AssertionError(f"unexpected path {request.url.path}")


@pytest.mark.asyncio
async def test_account_context_returns_context_directly() -> None:
    """account_context returns AccountContext itself, not a redundant tuple."""
    client, _ = mock_client(context_handler)
    try:
        context = await client.account_context("005930")
    finally:
        await client.close()
    assert isinstance(context, AccountContext)
    assert not isinstance(context, tuple)
    assert context.scope == "account_read_only"
    assert context.order_endpoints_called is False
    assert context.symbol == "005930"
    assert context.account.account_seq == 1
    assert context.buying_power.currency == "KRW"
    assert context.buying_power.cash_buying_power == Decimal("5000000")


@pytest.mark.asyncio
async def test_account_context_json_never_leaks_raw_account_no() -> None:
    client, _ = mock_client(context_handler)
    try:
        context = await client.account_context("005930")
    finally:
        await client.close()
    payload = json.dumps(asdict(context), ensure_ascii=False, default=str)
    assert "scope" in payload and "account_read_only" in payload
    assert "order_endpoints_called" in payload and "false" in payload.lower()
    assert "12345678901" not in payload
    assert "*******8901" in payload


@pytest.mark.asyncio
async def test_buying_power_response_currency_mismatch_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(200, json=context_handler(request).json())
        if request.url.path == "/api/v1/holdings":
            return httpx.Response(200, json={"result": dict(HOLDINGS_RESULT)})
        # Server answers USD for a KRW request: must fail closed.
        return httpx.Response(200, json={"result": {"currency": "USD", "cashBuyingPower": "3500"}})

    client, _ = mock_client(handler)
    try:
        with pytest.raises(TossApiError) as caught:
            await client.buying_power(7, "KRW")
        assert caught.value.code == "buying-power-currency-mismatch"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_account_context_fails_closed_on_non_krw_usd_holding() -> None:
    raw = dict(HOLDINGS_RESULT)
    raw["items"] = [dict(raw["items"][0], currency="XRD")]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/buying-power":
            raise AssertionError("buying-power must not be requested for exotic currency")
        return (
            context_handler(request)
            if request.url.path != "/api/v1/holdings"
            else (httpx.Response(200, json={"result": raw}))
        )

    client, _ = mock_client(handler)
    try:
        with pytest.raises(ValueError):
            await client.account_context("005930")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_account_context_infers_currency_when_symbol_not_held() -> None:
    raw = dict(HOLDINGS_RESULT)
    raw["items"] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = token_handler(request)
        if response is not None:
            return response
        if request.url.path == "/api/v1/holdings":
            return httpx.Response(200, json={"result": raw})
        if request.url.path == "/api/v1/buying-power":
            assert request.url.params.get("currency") == "KRW"
            return httpx.Response(
                200, json={"result": {"currency": "KRW", "cashBuyingPower": "10"}}
            )
        return context_handler(request)

    client, _ = mock_client(handler)
    try:
        context = await client.account_context("005930")
    finally:
        await client.close()
    assert context.holding is None
    assert context.holding_quantity == Decimal("0")
    assert context.buying_power.currency == "KRW"


@pytest.mark.asyncio
async def test_toss_api_error_traceback_assignment_never_raises_type_error() -> None:
    """Slotted dataclass exceptions break escaping tracebacks; plain class must not."""
    exc = TossApiError(429, "rate-limit-exceeded")
    exc.__traceback__ = None  # would be FrozenInstanceError/TypeError before the fix
    assert str(exc) == "Toss Open API 요청 실패 (HTTP 429, code=rate-limit-exceeded)"
