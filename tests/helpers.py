from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from toss_market_terminal.models import (
    Candle,
    MarketSnapshot,
    Orderbook,
    OrderbookEntry,
    Price,
    StockInfo,
    Trade,
)


def sample_snapshot(*, fresh_price: bool = False) -> MarketSnapshot:
    price_timestamp = datetime.now(UTC).isoformat() if fresh_price else "2026-08-26T04:00:00+09:00"
    return MarketSnapshot(
        stock=StockInfo("AAPL", "애플", "APPLE INC", "NASDAQ", "USD"),
        price=Price("AAPL", Decimal("110.00"), "USD", price_timestamp),
        orderbook=Orderbook(
            "USD",
            asks=(
                OrderbookEntry(Decimal("110.10"), Decimal("20")),
                OrderbookEntry(Decimal("110.20"), Decimal("50")),
            ),
            bids=(
                OrderbookEntry(Decimal("109.90"), Decimal("30")),
                OrderbookEntry(Decimal("109.80"), Decimal("10")),
            ),
            timestamp="2026-08-25T10:00:00+09:00",
        ),
        trades=(
            Trade(Decimal("110"), Decimal("2"), "2026-08-25T10:00:00+09:00", "USD"),
            Trade(Decimal("108"), Decimal("1"), "2026-08-25T09:59:59+09:00", "USD"),
        ),
        candles=(
            Candle(
                "2026-08-25T10:00:00+09:00",
                Decimal("109"),
                Decimal("111"),
                Decimal("108"),
                Decimal("110"),
                Decimal("100"),
                "USD",
            ),
            Candle(
                "2026-08-25T09:59:00+09:00",
                Decimal("108"),
                Decimal("110"),
                Decimal("107"),
                Decimal("109"),
                Decimal("80"),
                "USD",
            ),
        ),
        daily_candles=(
            Candle(
                "2026-08-25T09:30:00-04:00",
                Decimal("101"),
                Decimal("112"),
                Decimal("98"),
                Decimal("110"),
                Decimal("1000"),
                "USD",
            ),
            Candle(
                "2026-08-24T09:30:00-04:00",
                Decimal("99"),
                Decimal("102"),
                Decimal("97"),
                Decimal("100"),
                Decimal("900"),
                "USD",
            ),
        ),
    )
