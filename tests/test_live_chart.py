from __future__ import annotations

from decimal import Decimal

import pytest

from toss_market_terminal.live_chart import apply_trade_to_candles
from toss_market_terminal.models import Candle, Trade


def candle(
    timestamp: str = "2026-08-25T10:00:00+00:00",
    *,
    open_price: str = "100",
    high: str = "105",
    low: str = "95",
    close: str = "102",
    volume: str = "10",
    currency: str = "USD",
) -> Candle:
    return Candle(
        timestamp=timestamp,
        open_price=Decimal(open_price),
        high_price=Decimal(high),
        low_price=Decimal(low),
        close_price=Decimal(close),
        volume=Decimal(volume),
        currency=currency,
    )


def trade(
    price: str,
    timestamp: str,
    *,
    volume: str = "2",
    currency: str = "USD",
) -> Trade:
    return Trade(Decimal(price), Decimal(volume), timestamp, currency)


def test_same_minute_trade_updates_hlcv_and_preserves_open() -> None:
    original = candle()
    result = apply_trade_to_candles((original,), trade("110", "2026-08-25T10:00:59.123Z"))
    assert result == (
        Candle(
            timestamp=original.timestamp,
            open_price=Decimal("100"),
            high_price=Decimal("110"),
            low_price=Decimal("95"),
            close_price=Decimal("110"),
            volume=Decimal("12"),
            currency="USD",
        ),
    )


def test_new_minute_trade_prepends_fresh_candle_without_filling_gaps() -> None:
    original = candle()
    result = apply_trade_to_candles((original,), trade("107", "2026-08-25T10:03:01+00:00"))
    assert len(result) == 2
    assert result[0] == Candle(
        timestamp="2026-08-25T10:03:00+00:00",
        open_price=Decimal("107"),
        high_price=Decimal("107"),
        low_price=Decimal("107"),
        close_price=Decimal("107"),
        volume=Decimal("2"),
        currency="USD",
    )
    assert result[1] is original


def test_late_trade_does_not_rewrite_newest_candle() -> None:
    candles = (candle("2026-08-25T10:01:00Z"), candle("2026-08-25T10:00:00Z"))
    assert apply_trade_to_candles(candles, trade("999", "2026-08-25T10:00:59.999Z")) == candles


def test_new_candle_keeps_bounded_newest_first_history() -> None:
    candles = tuple(candle(f"2026-08-25T09:{minute:02d}:00Z") for minute in range(59, 54, -1))
    result = apply_trade_to_candles(
        candles,
        trade("120", "2026-08-25T10:00:00Z"),
        limit=5,
    )
    assert len(result) == 5
    assert result[0].close_price == Decimal("120")
    assert result[-1] == candles[-2]


def test_daily_trade_uses_latest_candle_offset_for_market_date() -> None:
    daily = (candle("2026-08-25T00:00:00-04:00"),)
    same_market_day = trade("108", "2026-08-26T01:30:00Z")  # 21:30 on Aug 25 at -04:00
    result = apply_trade_to_candles(daily, same_market_day, interval="1d")
    assert len(result) == 1
    assert result[0].close_price == Decimal("108")


def test_daily_trade_on_new_market_date_prepends_candle() -> None:
    daily = (candle("2026-08-25T00:00:00-04:00"),)
    result = apply_trade_to_candles(
        daily,
        trade("108", "2026-08-26T14:00:00Z"),
        interval="1d",
    )
    assert len(result) == 2
    assert result[0].close_price == Decimal("108")
    assert result[0].timestamp == "2026-08-26T00:00:00-04:00"


@pytest.mark.parametrize(
    ("candles", "event", "message"),
    [
        ((candle(),), trade("101", "2026-08-25T10:00:30"), "timezone"),
        ((candle(),), trade("101", "not-a-time"), "timestamp"),
        ((candle(),), trade("101", "2026-08-25T10:00:30Z", currency="KRW"), "통화"),
        ((candle(),), trade("0", "2026-08-25T10:00:30Z"), "가격"),
        ((candle(),), trade("101", "2026-08-25T10:00:30Z", volume="-1"), "거래량"),
    ],
)
def test_invalid_live_trade_is_rejected(
    candles: tuple[Candle, ...], event: Trade, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        apply_trade_to_candles(candles, event)


def test_empty_history_starts_from_first_trade() -> None:
    result = apply_trade_to_candles((), trade("101", "2026-08-25T10:00:30Z"))
    assert result[0].open_price == result[0].close_price == Decimal("101")
    assert result[0].volume == Decimal("2")
