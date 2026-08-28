from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def patterned_candles(*, count: int = 220, final_phase: int = 4) -> tuple[Candle, ...]:
    """Newest-first repeating price regimes with a deterministic next path."""
    start = datetime(2026, 8, 25, 13, 0, tzinfo=UTC)
    pattern = tuple(Decimal(100 + value) for value in range(10)) + tuple(
        Decimal(110 - value) for value in range(10)
    )
    offset = (final_phase - (count - 1)) % len(pattern)
    chronological: list[Candle] = []
    previous = pattern[offset]
    for index in range(count):
        phase = (offset + index) % len(pattern)
        close = pattern[phase]
        opened = previous
        chronological.append(
            Candle(
                timestamp=(start + timedelta(minutes=index)).isoformat(),
                open_price=opened,
                high_price=max(opened, close) + Decimal("0.2"),
                low_price=min(opened, close) - Decimal("0.2"),
                close_price=close,
                volume=Decimal(100 + phase * 7),
                currency="USD",
            )
        )
        previous = close
    return tuple(reversed(chronological))
