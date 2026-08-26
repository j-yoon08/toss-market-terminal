"""v0.8c read-only open-orders models: strict parsing + duplicate detection."""

from __future__ import annotations

from decimal import Decimal

import pytest

from toss_market_terminal.models import (
    TERMINAL_OPEN_ORDER_STATUSES,
    OpenOrder,
    OpenOrdersPage,
    find_open_order_duplicates,
)


def official_order(**overrides: object) -> dict[str, object]:
    # Exact shape from the official pendingMixed example (first order).
    order: dict[str, object] = {
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
    order.update(overrides)
    return order


def official_page(orders: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "orders": [official_order()] if orders is None else orders,
        "nextCursor": None,
        "hasNext": False,
    }


# --- OpenOrder ----------------------------------------------------------------


def test_open_order_parses_official_example() -> None:
    order = OpenOrder.from_api(official_order())
    assert order.order_id == "bAGzNvMOOTa5Uy0xVzYNbxDJ3Qpobwau4jDF3hyZZGWbpHm7wha8CFZc7aXVOWAl"
    assert order.symbol == "005930"
    assert order.side == "BUY"
    assert order.order_type == "LIMIT"
    assert order.status == "PENDING"
    assert order.quantity == Decimal("10")
    assert order.price == Decimal("70000")


def test_open_order_market_order_price_is_none() -> None:
    order = OpenOrder.from_api(official_order(orderType="MARKET", price=None))
    assert order.price is None


def test_open_order_preserves_unknown_order_type_and_status() -> None:
    order = OpenOrder.from_api(official_order(orderType="MYSTERY_TYPE", status="MYSTERY_STATUS"))
    assert order.order_type == "MYSTERY_TYPE"
    assert order.status == "MYSTERY_STATUS"


def test_open_order_rejects_unknown_side() -> None:
    with pytest.raises(ValueError):
        OpenOrder.from_api(official_order(side="HOLD"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.pop("orderId"),
        lambda raw: raw.pop("symbol"),
        lambda raw: raw.pop("side"),
        lambda raw: raw.pop("orderType"),
        lambda raw: raw.pop("status"),
        lambda raw: raw.pop("quantity"),
        lambda raw: raw.update(orderId=""),
        lambda raw: raw.update(symbol=""),
        lambda raw: raw.update(quantity="NaN"),
        lambda raw: raw.update(quantity="Infinity"),
        lambda raw: raw.update(quantity=100.5),
        lambda raw: raw.update(quantity="1" + "0" * 30),
        lambda raw: raw.update(price=100.5),
        lambda raw: raw.update(price="NaN"),
    ],
)
def test_open_order_malformed_shapes_fail_closed(mutate: object) -> None:
    raw = official_order()
    mutate(raw)  # type: ignore[operator]
    with pytest.raises(ValueError):
        OpenOrder.from_api(raw)


def test_open_order_rejects_non_dict_shape() -> None:
    with pytest.raises(ValueError):
        OpenOrder.from_api(["orderId"])  # type: ignore[arg-type]


# --- OpenOrdersPage -------------------------------------------------------


def test_open_orders_page_parses_official_pending_mixed_example() -> None:
    raw = official_page(
        [
            official_order(),
            official_order(
                orderId="RpP3_wtsiKe9btBvdendaHoBqOIY_Zb_xPkRfYaqCIvf2FXtMDv_mo7VnD7KB-ia",
                symbol="AAPL",
                side="SELL",
                status="PARTIAL_FILLED",
                price="185.5",
                quantity="5",
            ),
        ]
    )
    page = OpenOrdersPage.from_api(raw)
    assert len(page.orders) == 2
    assert page.orders[0].symbol == "005930"
    assert page.orders[1].symbol == "AAPL"
    assert page.orders[1].status == "PARTIAL_FILLED"


def test_open_orders_page_allows_empty_orders_list() -> None:
    page = OpenOrdersPage.from_api(official_page([]))
    assert page.orders == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.pop("orders"),
        lambda raw: raw.pop("nextCursor"),
        lambda raw: raw.pop("hasNext"),
        lambda raw: raw.update(orders={"symbol": "005930"}),
        lambda raw: raw.update(nextCursor="some-cursor"),
        lambda raw: raw.update(hasNext=True),
        lambda raw: raw.update(hasNext="false"),
        lambda raw: raw.update(orders=[{"symbol": "005930"}]),
    ],
)
def test_open_orders_page_malformed_shapes_fail_closed(mutate: object) -> None:
    raw = official_page()
    mutate(raw)  # type: ignore[operator]
    with pytest.raises(ValueError):
        OpenOrdersPage.from_api(raw)


def test_open_orders_page_rejects_non_dict_shape() -> None:
    with pytest.raises(ValueError):
        OpenOrdersPage.from_api([])  # type: ignore[arg-type]


# --- find_open_order_duplicates ---------------------------------------------


def test_terminal_statuses_are_exactly_the_official_closed_set() -> None:
    assert TERMINAL_OPEN_ORDER_STATUSES == frozenset(
        {"FILLED", "CANCELED", "REJECTED", "REPLACED", "CANCEL_REJECTED", "REPLACE_REJECTED"}
    )


def test_duplicate_finder_matches_normalized_symbol_and_exact_side() -> None:
    orders = (
        OpenOrder.from_api(official_order()),  # 005930 BUY PENDING
        OpenOrder.from_api(official_order(orderId="b", symbol="005930", side="SELL")),
        OpenOrder.from_api(official_order(orderId="c", symbol="AAPL", side="BUY")),
    )
    matches = find_open_order_duplicates(orders, "005930", "BUY")
    assert [order.order_id for order in matches] == [orders[0].order_id]


def test_duplicate_finder_normalizes_lowercase_symbol_input() -> None:
    orders = (OpenOrder.from_api(official_order(symbol="AAPL")),)
    matches = find_open_order_duplicates(orders, "aapl", "BUY")
    assert len(matches) == 1


def test_duplicate_finder_does_not_cross_side_polarity() -> None:
    orders = (OpenOrder.from_api(official_order(side="BUY")),)
    assert find_open_order_duplicates(orders, "005930", "SELL") == ()


def test_duplicate_finder_excludes_definitely_terminal_statuses() -> None:
    orders = tuple(
        OpenOrder.from_api(official_order(orderId=status, status=status))
        for status in TERMINAL_OPEN_ORDER_STATUSES
    )
    assert find_open_order_duplicates(orders, "005930", "BUY") == ()


@pytest.mark.parametrize(
    "status", ["PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE", "SOME_NEW_STATUS"]
)
def test_duplicate_finder_treats_active_and_unknown_statuses_as_possible_duplicates(
    status: str,
) -> None:
    orders = (OpenOrder.from_api(official_order(status=status)),)
    assert len(find_open_order_duplicates(orders, "005930", "BUY")) == 1


def test_duplicate_finder_rejects_invalid_symbol_and_side() -> None:
    orders = (OpenOrder.from_api(official_order()),)
    with pytest.raises(ValueError):
        find_open_order_duplicates(orders, "not a symbol!!", "BUY")
    with pytest.raises(ValueError):
        find_open_order_duplicates(orders, "005930", "HOLD")
