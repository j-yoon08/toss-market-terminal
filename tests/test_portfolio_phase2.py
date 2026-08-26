from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tests.test_portfolio import (
    build_snapshot,
    official_item,
    official_order,
    official_overview,
)
from toss_market_terminal.client import (
    ACCOUNT_READ_ONLY_PATHS,
    EXCHANGE_RATE_READ_ONLY_PATHS,
    OPEN_ORDERS_READ_ONLY_PATHS,
    READ_ONLY_PATHS,
    TossMarketClient,
)
from toss_market_terminal.config import Credentials
from toss_market_terminal.models import ClosedOrder, ClosedOrdersPage, ExchangeRate
from toss_market_terminal.portfolio import (
    closed_orders_section_text,
    exchange_rate_section_text,
    portfolio_body_text,
    portfolio_weight,
)

RAW_ORDER_ID = "raw-order-id-must-never-render"


def official_exchange_rate(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "baseCurrency": "USD",
        "quoteCurrency": "KRW",
        "rate": "1390.20",
        "midRate": "1388.50",
        "basisPoint": "170",
        "rateChangeType": "UP",
        "validFrom": "2026-08-27T00:30:00+09:00",
        "validUntil": "2026-08-27T00:31:00+09:00",
    }
    raw.update(overrides)
    return raw


def official_closed_order(**overrides: object) -> dict[str, object]:
    raw = official_order(
        orderId=RAW_ORDER_ID,
        status="FILLED",
        quantity="2",
        currency="USD",
        symbol="AAPL",
        price="180",
        orderedAt="2026-08-26T13:30:00+09:00",
        execution={
            "filledQuantity": "2",
            "averageFilledPrice": "179.50",
            "filledAmount": "359",
            "commission": "0.42",
            "tax": "0",
            "filledAt": "2026-08-26T13:30:01+09:00",
            "settlementDate": "2026-08-28",
        },
    )
    raw.update(overrides)
    return raw


def official_closed_page(
    orders: list[dict[str, object]] | None = None, *, has_next: bool = False
) -> dict[str, object]:
    return {
        "orders": [official_closed_order()] if orders is None else orders,
        "nextCursor": "opaque-cursor" if has_next else None,
        "hasNext": has_next,
    }


def test_exchange_rate_allowlist_is_single_path_and_disjoint() -> None:
    assert EXCHANGE_RATE_READ_ONLY_PATHS == frozenset({"/api/v1/exchange-rate"})
    assert EXCHANGE_RATE_READ_ONLY_PATHS.isdisjoint(READ_ONLY_PATHS)
    assert EXCHANGE_RATE_READ_ONLY_PATHS.isdisjoint(ACCOUNT_READ_ONLY_PATHS)
    assert EXCHANGE_RATE_READ_ONLY_PATHS.isdisjoint(OPEN_ORDERS_READ_ONLY_PATHS)


def test_exchange_rate_parses_official_usd_krw_shape() -> None:
    rate = ExchangeRate.from_api(official_exchange_rate())
    assert rate.base_currency == "USD"
    assert rate.quote_currency == "KRW"
    assert rate.mid_rate == Decimal("1388.50")
    assert rate.valid_from.tzinfo is not None
    assert rate.valid_until > rate.valid_from


@pytest.mark.parametrize(
    "overrides",
    [
        {"baseCurrency": "EUR"},
        {"midRate": "0"},
        {"midRate": "NaN"},
        {"rateChangeType": "UNKNOWN"},
        {"validFrom": "2026-08-27T00:30:00"},
        {
            "validFrom": "2026-08-27T00:31:00+09:00",
            "validUntil": "2026-08-27T00:30:00+09:00",
        },
    ],
)
def test_exchange_rate_rejects_untrusted_shapes(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ExchangeRate.from_api(official_exchange_rate(**overrides))


def test_exchange_rate_and_closed_order_reject_exponent_decimals() -> None:
    with pytest.raises(ValueError):
        ExchangeRate.from_api(official_exchange_rate(midRate="1e1000000"))
    with pytest.raises(ValueError):
        ClosedOrder.from_api(official_closed_order(quantity="1e999999"))


def test_closed_order_parses_execution_but_never_retains_order_id() -> None:
    order = ClosedOrder.from_api(official_closed_order())
    assert order.symbol == "AAPL"
    assert order.filled_quantity == Decimal("2")
    assert order.average_filled_price == Decimal("179.50")
    assert order.settlement_date == date(2026, 8, 28)
    assert "order_id" not in {field.name for field in dataclasses.fields(order)}
    assert RAW_ORDER_ID not in repr(order)


@pytest.mark.parametrize(
    "raw",
    [
        official_closed_order(quantity="0"),
        official_closed_order(currency="EUR"),
        official_closed_order(orderedAt="2026-08-26T13:30:00"),
        official_closed_order(execution={"filledQuantity": "3"}),
    ],
)
def test_closed_order_rejects_unsafe_shapes(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ClosedOrder.from_api(raw)


def test_closed_order_requires_nullable_price_key() -> None:
    raw = official_closed_order()
    raw.pop("price")
    with pytest.raises(ValueError):
        ClosedOrder.from_api(raw)


def test_closed_order_requires_but_does_not_retain_order_id() -> None:
    raw = official_closed_order()
    raw.pop("orderId")
    with pytest.raises(ValueError):
        ClosedOrder.from_api(raw)


def test_closed_orders_page_validates_cursor_consistency_without_retaining_cursor() -> None:
    page = ClosedOrdersPage.from_api(official_closed_page(has_next=True))
    assert page.has_more
    assert "cursor" not in {field.name for field in dataclasses.fields(page)}
    with pytest.raises(ValueError):
        ClosedOrdersPage.from_api({"orders": [], "nextCursor": None, "hasNext": True})


def test_closed_orders_page_rejects_more_than_twenty_rows() -> None:
    with pytest.raises(ValueError):
        ClosedOrdersPage.from_api(
            official_closed_page([official_closed_order() for _ in range(21)])
        )


@pytest.mark.asyncio
async def test_client_exchange_rate_uses_exact_get_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "test-token", "expires_in": 3600},
            )
        seen.append(request)
        return httpx.Response(200, json={"result": official_exchange_rate()})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openapi.tossinvest.com",
    )
    client = TossMarketClient(Credentials("id", "secret"), http_client=http)
    try:
        rate = await client.exchange_rate()
    finally:
        await http.aclose()
    assert rate.mid_rate == Decimal("1388.50")
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/exchange-rate"
    assert dict(seen[0].url.params) == {
        "baseCurrency": "USD",
        "quoteCurrency": "KRW",
    }


@pytest.mark.asyncio
async def test_client_closed_orders_is_bounded_get_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "test-token", "expires_in": 3600},
            )
        seen.append(request)
        return httpx.Response(200, json={"result": official_closed_page()})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openapi.tossinvest.com",
    )
    client = TossMarketClient(Credentials("id", "secret"), http_client=http)
    try:
        page = await client.closed_orders(
            7,
            start_date=date(2026, 7, 29),
            end_date=date(2026, 8, 27),
            limit=20,
        )
    finally:
        await http.aclose()
    assert len(page.orders) == 1
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/orders"
    assert dict(seen[0].url.params) == {
        "status": "CLOSED",
        "from": "2026-07-29",
        "to": "2026-08-27",
        "limit": "20",
    }
    assert seen[0].headers["x-tossinvest-account"] == "7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_date": date(2026, 8, 28), "end_date": date(2026, 8, 27)},
        {"start_date": date(2026, 7, 28), "end_date": date(2026, 8, 27)},
        {"start_date": date(2026, 7, 1), "end_date": date(2026, 8, 27)},
        {"start_date": date(2026, 8, 1), "end_date": date(2026, 8, 27), "limit": 21},
    ],
)
async def test_client_closed_orders_rejects_invalid_bounds_before_network(
    kwargs: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network attempted: {request.method} {request.url.path}")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openapi.tossinvest.com",
    )
    client = TossMarketClient(Credentials("id", "secret"), http_client=http)
    try:
        with pytest.raises(ValueError):
            await client.closed_orders(1, **kwargs)  # type: ignore[arg-type]
    finally:
        await http.aclose()


def phase2_snapshot():
    items = [
        official_item(),
        official_item(
            symbol="AAPL",
            name="Apple",
            marketCountry="US",
            currency="USD",
            quantity="10",
            averagePurchasePrice="150",
            lastPrice="180",
            marketValue={
                "purchaseAmount": "1500",
                "amount": "1800",
                "amountAfterCost": "1775",
            },
            profitLoss={
                "amount": "300",
                "amountAfterCost": "275",
                "rate": "0.2",
                "rateAfterCost": "0.183333",
            },
            dailyProfitLoss={"amount": "25", "rate": "0.014286"},
            cost={"commission": "5", "tax": "20"},
        ),
        official_item(
            symbol="MSFT",
            name="Microsoft",
            marketCountry="US",
            currency="USD",
            quantity="5",
            averagePurchasePrice="300",
            lastPrice="360",
            marketValue={
                "purchaseAmount": "1500",
                "amount": "1800",
                "amountAfterCost": "1775",
            },
            profitLoss={
                "amount": "300",
                "amountAfterCost": "275",
                "rate": "0.2",
                "rateAfterCost": "0.183333",
            },
            dailyProfitLoss={"amount": "25", "rate": "0.014286"},
            cost={"commission": "5", "tax": "20"},
        ),
    ]
    return build_snapshot(holdings_raw=official_overview(items))


def test_portfolio_weight_is_per_currency_and_excludes_buying_power() -> None:
    snapshot = phase2_snapshot()
    krw, aapl, msft = snapshot.holdings.items
    assert portfolio_weight(krw, snapshot.holdings.items) == Decimal("100")
    assert portfolio_weight(aapl, snapshot.holdings.items) == Decimal("50")
    assert portfolio_weight(msft, snapshot.holdings.items) == Decimal("50")


def test_phase2_render_shows_weight_daily_fx_history_and_no_realized_guess() -> None:
    snapshot = phase2_snapshot()
    rate = ExchangeRate.from_api(official_exchange_rate())
    history = ClosedOrdersPage.from_api(official_closed_page(has_next=True))
    text = portfolio_body_text(
        snapshot,
        90,
        exchange_rate=rate,
        exchange_synced_monotonic=1.0,
        closed_orders=history,
        history_synced_monotonic=1.0,
    ).plain
    assert "비중 100.0%" in text
    assert text.count("비중 50.0%") == 2
    assert "오늘손익" in text
    assert "매매기준율" in text
    assert "환산 평가액" in text
    assert "최근 종료 주문" in text
    assert "평균체결 179.5" in text
    assert "추가 주문내역 있음" in text
    assert "공식 API 미제공 · 현재 평단으로 임의 계산하지 않음" in text
    assert RAW_ORDER_ID not in text


def test_exchange_render_marks_provider_validity_expired_rate_stale() -> None:
    snapshot = phase2_snapshot()
    rate = ExchangeRate.from_api(official_exchange_rate())
    text = exchange_rate_section_text(
        snapshot,
        rate,
        stale=False,
        error=None,
        synced_monotonic=1.0,
        now=rate.valid_until + timedelta(seconds=1),
    ).plain
    assert "FX STALE" in text


def test_history_render_keeps_last_good_with_sanitized_error() -> None:
    page = ClosedOrdersPage.from_api(official_closed_page())
    text = closed_orders_section_text(
        page,
        stale=True,
        error="RuntimeError: REST snapshot failed",
        synced_monotonic=1.0,
    ).plain
    assert "ORDER HISTORY STALE" in text
    assert "REST snapshot failed" in text
    assert RAW_ORDER_ID not in text


def test_naive_exchange_times_and_raw_ids_never_reach_render() -> None:
    with pytest.raises(ValueError):
        ExchangeRate.from_api(
            official_exchange_rate(validFrom=datetime.now(UTC).replace(tzinfo=None).isoformat())
        )
    page = ClosedOrdersPage.from_api(official_closed_page())
    assert RAW_ORDER_ID not in repr(page)
