from __future__ import annotations

from decimal import Decimal

import pytest

from toss_market_terminal.models import (
    Candle,
    DataShapeError,
    Orderbook,
    OrderbookEntry,
    Price,
    StockInfo,
    Trade,
    as_decimal,
)


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


# ---------------------------------------------------------------------------
# Fixtures for otherwise-valid market-data payloads. Adversarial tests below
# mutate exactly one field off a valid baseline so failures are attributable.
# ---------------------------------------------------------------------------


def _trade_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "price": "100.00",
        "volume": "1",
        "timestamp": "2026-01-01T00:00:00Z",
        "currency": "USD",
    }
    payload.update(overrides)
    return payload


def _candle_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": "2026-01-01T00:00:00Z",
        "openPrice": "100",
        "highPrice": "110",
        "lowPrice": "90",
        "closePrice": "105",
        "volume": "10",
        "currency": "USD",
    }
    payload.update(overrides)
    return payload


def _price_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": "AAPL",
        "lastPrice": "185.70",
        "currency": "USD",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Trade: price > 0, volume >= 0.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["-1", "-0.01"])
def test_trade_rejects_negative_price(value: str) -> None:
    with pytest.raises(DataShapeError):
        Trade.from_api(_trade_payload(price=value))


def test_trade_rejects_zero_price() -> None:
    with pytest.raises(DataShapeError):
        Trade.from_api(_trade_payload(price="0"))


def test_trade_rejects_nan_price() -> None:
    with pytest.raises(DataShapeError):
        Trade.from_api(_trade_payload(price="NaN"))


def test_trade_rejects_negative_volume() -> None:
    with pytest.raises(DataShapeError):
        Trade.from_api(_trade_payload(volume="-1"))


def test_trade_accepts_zero_volume() -> None:
    trade = Trade.from_api(_trade_payload(volume="0"))
    assert trade.volume == Decimal("0")


@pytest.mark.parametrize(
    "timestamp",
    [
        "not-a-timestamp",
        "2026-01-01",  # date only, not a full timestamp
        "",
        "2026-13-01T00:00:00Z",  # invalid month
    ],
)
def test_trade_rejects_invalid_timestamp(timestamp: str) -> None:
    with pytest.raises(DataShapeError):
        Trade.from_api(_trade_payload(timestamp=timestamp))


def test_trade_rejects_naive_timestamp() -> None:
    with pytest.raises(DataShapeError):
        Trade.from_api(_trade_payload(timestamp="2026-01-01T00:00:00"))


@pytest.mark.parametrize(
    "timestamp",
    ["2026-01-01T00:00:00Z", "2026-01-01T09:00:00+09:00", "2026-01-01T00:00:00.987Z"],
)
def test_trade_accepts_trailing_z_and_explicit_offset(timestamp: str) -> None:
    trade = Trade.from_api(_trade_payload(timestamp=timestamp))
    # The original string is preserved verbatim for display, not reformatted.
    assert trade.timestamp == timestamp


def test_trade_rejects_unsupported_currency() -> None:
    with pytest.raises(DataShapeError):
        Trade.from_api(_trade_payload(currency="EUR"))


# ---------------------------------------------------------------------------
# OrderbookEntry: price > 0, quantity >= 0.
# ---------------------------------------------------------------------------


def test_orderbook_entry_rejects_negative_price() -> None:
    with pytest.raises(DataShapeError):
        OrderbookEntry.from_api({"price": "-1", "volume": "1"})


def test_orderbook_entry_rejects_negative_volume() -> None:
    with pytest.raises(DataShapeError):
        OrderbookEntry.from_api({"price": "1", "volume": "-1"})


def test_orderbook_entry_accepts_zero_volume() -> None:
    entry = OrderbookEntry.from_api({"price": "1", "volume": "0"})
    assert entry.volume == Decimal("0")


def test_orderbook_rejects_naive_timestamp_when_present() -> None:
    with pytest.raises(DataShapeError):
        Orderbook.from_api(
            {
                "timestamp": "2026-01-01T00:00:00",
                "currency": "USD",
                "asks": [],
                "bids": [],
            }
        )


# ---------------------------------------------------------------------------
# Candle: open/high/low/close > 0, volume >= 0, high >= low,
# low <= open/close <= high.
# ---------------------------------------------------------------------------


def test_candle_rejects_high_below_low() -> None:
    with pytest.raises(DataShapeError):
        Candle.from_api(_candle_payload(highPrice="90", lowPrice="110"))


@pytest.mark.parametrize("open_price", ["89", "111"])
def test_candle_rejects_open_outside_low_high_range(open_price: str) -> None:
    with pytest.raises(DataShapeError):
        Candle.from_api(_candle_payload(openPrice=open_price))


@pytest.mark.parametrize("close_price", ["89", "111"])
def test_candle_rejects_close_outside_low_high_range(close_price: str) -> None:
    with pytest.raises(DataShapeError):
        Candle.from_api(_candle_payload(closePrice=close_price))


@pytest.mark.parametrize("field", ["openPrice", "highPrice", "lowPrice", "closePrice"])
def test_candle_rejects_nonpositive_ohlc(field: str) -> None:
    with pytest.raises(DataShapeError):
        Candle.from_api(_candle_payload(**{field: "0"}))


def test_candle_rejects_negative_volume() -> None:
    with pytest.raises(DataShapeError):
        Candle.from_api(_candle_payload(volume="-1"))


def test_candle_accepts_zero_volume() -> None:
    candle = Candle.from_api(_candle_payload(volume="0"))
    assert candle.volume == Decimal("0")


def test_candle_rejects_naive_timestamp() -> None:
    with pytest.raises(DataShapeError):
        Candle.from_api(_candle_payload(timestamp="2026-01-01T00:00:00"))


def test_candle_accepts_ohlc_touching_the_boundary() -> None:
    # open == low, close == high is a legitimate (if extreme) bar.
    candle = Candle.from_api(_candle_payload(openPrice="90", closePrice="110"))
    assert candle.open_price == Decimal("90")
    assert candle.close_price == Decimal("110")


# ---------------------------------------------------------------------------
# Price: price > 0; timestamp validated when present but stays optional.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", "NaN"])
def test_price_rejects_nonpositive_or_nonfinite(value: str) -> None:
    with pytest.raises(DataShapeError):
        Price.from_api(_price_payload(lastPrice=value))


def test_price_allows_missing_timestamp() -> None:
    price = Price.from_api(_price_payload(timestamp=None))
    assert price.timestamp is None


def test_price_rejects_naive_timestamp() -> None:
    with pytest.raises(DataShapeError):
        Price.from_api(_price_payload(timestamp="2026-01-01T00:00:00"))


def test_price_rejects_unsupported_currency() -> None:
    with pytest.raises(DataShapeError):
        Price.from_api(_price_payload(currency="EUR"))


def test_stock_info_rejects_unsupported_currency() -> None:
    with pytest.raises(DataShapeError):
        StockInfo.from_api(
            {"symbol": "AAPL", "name": "Apple", "market": "NASDAQ", "currency": "EUR"}
        )


def test_stock_info_accepts_supported_currency() -> None:
    stock = StockInfo.from_api(
        {"symbol": "AAPL", "name": "Apple", "market": "NASDAQ", "currency": "USD"}
    )
    assert stock.currency == "USD"


# ---------------------------------------------------------------------------
# Signed fields (change/P&L/return) intentionally do not get a positivity
# constraint -- they share ``as_decimal``, which only requires finiteness.
# ---------------------------------------------------------------------------


def test_signed_decimal_field_permits_negative_finite_value() -> None:
    assert as_decimal("-3.21", "profitLoss.rate") == Decimal("-3.21")


def test_signed_decimal_field_still_rejects_nonfinite_value() -> None:
    with pytest.raises(DataShapeError):
        as_decimal("Infinity", "profitLoss.rate")


# ---------------------------------------------------------------------------
# Errors must name only the invalid field/domain, never echo raw payload
# values back (which could carry sensitive or attacker-controlled content).
# ---------------------------------------------------------------------------


def test_invalid_price_error_never_echoes_raw_value() -> None:
    secret_value = "sk_live_super_secret_do_not_leak"
    with pytest.raises(DataShapeError) as excinfo:
        Trade.from_api(_trade_payload(price=secret_value))
    assert secret_value not in str(excinfo.value)
    assert "trade.price" in str(excinfo.value)


def test_invalid_timestamp_error_never_echoes_raw_value() -> None:
    secret_value = "<script>alert(document.cookie)</script>"
    with pytest.raises(DataShapeError) as excinfo:
        Trade.from_api(_trade_payload(timestamp=secret_value))
    assert secret_value not in str(excinfo.value)
    assert "trade.timestamp" in str(excinfo.value)
