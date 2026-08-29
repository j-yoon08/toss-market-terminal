from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import ClassVar

from .config import DEFAULT_CREDENTIALS_PATH
from .models import (
    Candle,
    MarketSnapshot,
    Orderbook,
    OrderbookEntry,
    Price,
    StockInfo,
    Trade,
)
from .settings import DEFAULT_SETTINGS_PATH, Settings
from .tui import TossMarketApp


class DemoTossMarketApp(TossMarketApp):
    SUB_TITLE: ClassVar[str] = "DEMO · OFFLINE · PAPER ONLY"


def _intraday_candles(now: datetime, count: int = 220) -> tuple[Candle, ...]:
    pattern = tuple(Decimal(100 + value) for value in range(10)) + tuple(
        Decimal(110 - value) for value in range(10)
    )
    chronological: list[Candle] = []
    previous = pattern[0]
    start = now - timedelta(minutes=count - 1)
    for index in range(count):
        close = pattern[index % len(pattern)]
        opened = previous
        chronological.append(
            Candle(
                timestamp=(start + timedelta(minutes=index)).isoformat(),
                open_price=opened,
                high_price=max(opened, close) + Decimal("0.20"),
                low_price=min(opened, close) - Decimal("0.20"),
                close_price=close,
                volume=Decimal(120 + (index % 20) * 8),
                currency="USD",
            )
        )
        previous = close
    return tuple(reversed(chronological))


def _daily_candles(now: datetime, count: int = 60) -> tuple[Candle, ...]:
    candles: list[Candle] = []
    for index in range(count):
        day = now.date() - timedelta(days=index)
        timestamp = datetime.combine(day, time.min, tzinfo=UTC)
        close = Decimal(100 + ((count - index) % 12))
        opened = close - Decimal("1.25")
        candles.append(
            Candle(
                timestamp=timestamp.isoformat(),
                open_price=opened,
                high_price=close + Decimal("1.50"),
                low_price=opened - Decimal("1.00"),
                close_price=close,
                volume=Decimal(10_000 + index * 150),
                currency="USD",
            )
        )
    return tuple(candles)


def demo_snapshot(*, now: datetime | None = None) -> MarketSnapshot:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("demo now는 timezone-aware여야 합니다.")
    current = current.astimezone(UTC)
    timestamp = current.isoformat()
    return MarketSnapshot(
        stock=StockInfo("AAPL", "Apple Demo", "APPLE DEMO", "NASDAQ", "USD"),
        price=Price("AAPL", Decimal("109.00"), "USD", timestamp),
        orderbook=Orderbook(
            "USD",
            asks=(
                OrderbookEntry(Decimal("109.10"), Decimal("42")),
                OrderbookEntry(Decimal("109.20"), Decimal("68")),
                OrderbookEntry(Decimal("109.30"), Decimal("90")),
            ),
            bids=(
                OrderbookEntry(Decimal("108.90"), Decimal("55")),
                OrderbookEntry(Decimal("108.80"), Decimal("73")),
                OrderbookEntry(Decimal("108.70"), Decimal("88")),
            ),
            timestamp=timestamp,
        ),
        trades=(
            Trade(Decimal("109.00"), Decimal("3"), timestamp, "USD"),
            Trade(
                Decimal("108.95"),
                Decimal("2"),
                (current - timedelta(seconds=1)).isoformat(),
                "USD",
            ),
        ),
        candles=_intraday_candles(current),
        daily_candles=_daily_candles(current),
    )


def build_demo_app(*, now: datetime | None = None) -> TossMarketApp:
    return DemoTossMarketApp(
        "AAPL",
        DEFAULT_CREDENTIALS_PATH,
        initial_snapshot=demo_snapshot(now=now),
        connect_live=False,
        settings_path=DEFAULT_SETTINGS_PATH,
        settings=Settings(watchlist=("AAPL",)),
        manual_live_orders=False,
        offline_demo=True,
    )
