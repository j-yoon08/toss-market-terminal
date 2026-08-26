from __future__ import annotations

from decimal import Decimal

import pytest

from toss_market_terminal.models import DataShapeError, Orderbook, Trade, as_decimal


def test_decimal_is_exact() -> None:
    assert as_decimal("185.70", "price") == Decimal("185.70")


def test_decimal_rejects_nonfinite() -> None:
    with pytest.raises(DataShapeError):
        as_decimal("NaN", "price")


@pytest.mark.parametrize("value", ["1e999999", "1E+10", "+1", "1."])
def test_decimal_rejects_non_plain_or_exponent_forms(value: str) -> None:
    with pytest.raises(DataShapeError):
        as_decimal(value, "price")


def test_parse_trade_and_orderbook() -> None:
    trade = Trade.from_api(
        {"price": "243.26", "volume": "8", "timestamp": "2026-01-01T00:00:00Z", "currency": "USD"}
    )
    orderbook = Orderbook.from_api(
        {
            "timestamp": None,
            "currency": "USD",
            "asks": [{"price": "243.27", "volume": "5"}],
            "bids": [{"price": "243.25", "volume": "7"}],
        }
    )
    assert trade.price == Decimal("243.26")
    assert orderbook.asks[0].volume == Decimal("5")
