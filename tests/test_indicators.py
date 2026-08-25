"""Tests for toss_market_terminal.indicators."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from toss_market_terminal.indicators import aggregate_candles
from toss_market_terminal.models import Candle


def candle(
    timestamp: str,
    o: str,
    h: str,
    low: str,
    c: str,
    volume: str,
    currency: str = "USD",
) -> Candle:
    return Candle(
        timestamp=timestamp,
        open_price=Decimal(o),
        high_price=Decimal(h),
        low_price=Decimal(low),
        close_price=Decimal(c),
        volume=Decimal(volume),
        currency=currency,
    )


def test_empty_returns_empty() -> None:
    assert aggregate_candles((), "5m") == ()


def test_1m_is_identity() -> None:
    candles = (
        candle("2026-01-05T09:04:00+00:00", "10", "11", "9", "10.5", "100"),
        candle("2026-01-05T09:03:00Z", "10", "12", "9", "11", "50"),
    )
    assert aggregate_candles(candles, "1m") == candles


def test_two_full_buckets_newest_first() -> None:
    candles = (
        # Newest-first input; the two newest minutes form one 5m bucket.
        candle("2026-01-05T10:06:00Z", "30", "31", "29", "30.5", "6"),
        candle("2026-01-05T10:05:00Z", "29", "32", "28", "30", "5"),
        candle("2026-01-05T10:04:00Z", "20", "25", "19", "24", "4"),
        candle("2026-01-05T10:03:00Z", "15", "18", "14", "17", "3"),
        candle("2026-01-05T10:02:00Z", "10", "12", "9", "11", "2"),
        candle("2026-01-05T10:01:00Z", "10", "13", "8", "12", "1"),
    )
    result = aggregate_candles(candles, "5m")
    assert len(result) == 2
    newest, older = result
    # Bucket [10:05, 10:10): open from earliest member 10:05, close from latest 10:06.
    assert newest.timestamp == "2026-01-05T10:05:00+00:00"
    assert newest.open_price == Decimal("29")
    assert newest.high_price == Decimal("32")
    assert newest.low_price == Decimal("28")
    assert newest.close_price == Decimal("30.5")
    assert newest.volume == Decimal("11")
    assert newest.currency == "USD"
    # Bucket [10:00, 10:05) spans 10:01..10:04 only.
    assert older.timestamp == "2026-01-05T10:00:00+00:00"
    assert older.open_price == Decimal("10")
    assert older.high_price == Decimal("25")
    assert older.low_price == Decimal("8")
    assert older.close_price == Decimal("24")
    assert older.volume == Decimal("10")


def test_incomplete_latest_bucket_included() -> None:
    candles = (
        candle("2026-01-05T10:07:00Z", "40", "41", "39", "40.5", "7"),
        candle("2026-01-05T10:06:00Z", "40", "44", "38", "43", "6"),
        candle("2026-01-05T10:05:00Z", "40", "42", "37", "41", "5"),
    )
    result = aggregate_candles(candles, "5m")
    assert len(result) == 1
    bucket = result[0]
    assert bucket.timestamp == "2026-01-05T10:05:00+00:00"
    assert bucket.close_price == Decimal("40.5")
    assert bucket.volume == Decimal("18")
    assert bucket.currency == "USD"


def test_buckets_never_cross_date_boundary() -> None:
    candles = (
        candle("2026-01-06T00:01:00Z", "55", "55", "55", "56", "5"),
        candle("2026-01-06T00:00:00Z", "50", "51", "49", "52", "4"),
        candle("2026-01-05T23:59:00Z", "45", "46", "44", "47", "3"),
        candle("2026-01-05T23:58:00Z", "40", "41", "39", "42", "2"),
    )
    result = aggregate_candles(candles, "5m")
    assert [item.timestamp for item in result] == [
        "2026-01-06T00:00:00+00:00",
        "2026-01-05T23:55:00+00:00",
    ]
    first_day_last = result[1]
    # The 23:55 bucket holds only 23:59/23:58 members; no 00:00 data leaks in.
    assert first_day_last.open_price == Decimal("40")
    assert first_day_last.close_price == Decimal("47")
    assert first_day_last.volume == Decimal("5")


def test_timezone_offset_shares_local_wall_clock_bucket() -> None:
    # 00:04+09:00 and 2026-01-05 23:59Z are the same instant but different local dates.
    candles = (
        candle("2026-01-06T00:04:00+09:00", "70", "71", "69", "70.5", "9"),
        candle("2026-01-05T23:59:00Z", "60", "61", "59", "60.5", "8"),
    )
    result = aggregate_candles(candles, "5m")
    assert len(result) == 2
    assert [item.timestamp for item in result] == [
        "2026-01-06T00:00:00+09:00",
        "2026-01-05T23:55:00+00:00",
    ]
    # Same wall-clock minute in a different zone lands in that zone's own bucket.
    shifted = (
        candle("2026-01-05T09:04:00+09:00", "80", "81", "79", "80.5", "1"),
        candle("2026-01-05T00:04:00Z", "80", "82", "78", "81", "2"),
    )
    result_shifted = aggregate_candles(shifted, "5m")
    assert len(result_shifted) == 2


def test_malformed_timestamp_rejected() -> None:
    with pytest.raises(ValueError):
        aggregate_candles((candle("not-a-timestamp", "1", "1", "1", "1", "1"),), "5m")


def test_naive_timestamp_rejected() -> None:
    naive = datetime(2026, 1, 5, 10, 0).astimezone(UTC).replace(tzinfo=None)
    assert naive.tzinfo is None
    with pytest.raises(ValueError):
        aggregate_candles((candle(naive.isoformat(), "1", "1", "1", "1", "1"),), "5m")


def test_currency_mismatch_rejected() -> None:
    candles = (
        candle("2026-01-05T10:01:00Z", "1", "1", "1", "1", "1", "KRW"),
        candle("2026-01-05T10:00:00Z", "1", "1", "1", "1", "1", "USD"),
    )
    with pytest.raises(ValueError):
        aggregate_candles(candles, "5m")


def test_unsupported_timeframe_rejected() -> None:
    with pytest.raises(ValueError):
        aggregate_candles((), "15m")
