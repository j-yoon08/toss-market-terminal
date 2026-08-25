"""Tests for toss_market_terminal.indicators."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from toss_market_terminal.indicators import aggregate_candles, ema_series, session_vwap_series
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
        aggregate_candles((), "3m")


def test_same_local_date_and_bucket_different_offsets_not_merged() -> None:
    # Adversarial regression: 09:04+09:00 and 09:04Z share the local date
    # (2026-01-05) and the wall-clock bucket (09:00) but have different UTC
    # offsets; they must land in two distinct buckets.
    candles = (
        candle("2026-01-05T09:04:00Z", "20", "21", "19", "20.5", "2"),
        candle("2026-01-05T09:04:00+09:00", "10", "11", "9", "10.5", "1"),
    )
    result = aggregate_candles(candles, "5m")
    assert len(result) == 2
    assert [item.timestamp for item in result] == [
        "2026-01-05T09:00:00+00:00",
        "2026-01-05T09:00:00+09:00",
    ]
    assert result[0].volume == Decimal("2")
    assert result[1].volume == Decimal("1")


def test_15m_exact_boundaries() -> None:
    candles = (
        # Newest-first: 10:16/10:15 form bucket [10:15, 10:30); 10:14 and older
        # stay in [10:00, 10:15).
        candle("2026-01-05T10:16:00Z", "31", "31", "30", "30.8", "2"),
        candle("2026-01-05T10:15:00Z", "30", "32", "29", "31", "3"),
        candle("2026-01-05T10:14:00Z", "25", "26", "24", "25.5", "4"),
        candle("2026-01-05T10:02:00Z", "20", "22", "19", "21", "2"),
        candle("2026-01-05T10:00:00Z", "18", "23", "17", "22", "3"),
    )
    result = aggregate_candles(candles, "15m")
    assert len(result) == 2
    newest, older = result
    assert newest.timestamp == "2026-01-05T10:15:00+00:00"
    assert newest.open_price == Decimal("30")
    assert newest.high_price == Decimal("32")
    assert newest.low_price == Decimal("29")
    assert newest.close_price == Decimal("30.8")
    assert newest.volume == Decimal("5")
    assert older.timestamp == "2026-01-05T10:00:00+00:00"
    assert older.open_price == Decimal("18")
    assert older.high_price == Decimal("26")
    assert older.low_price == Decimal("17")
    assert older.close_price == Decimal("25.5")
    assert older.volume == Decimal("9")


def test_1h_exact_boundaries_and_incomplete_latest_bucket() -> None:
    candles = (
        # Newest-first: 11:00 alone is an incomplete [11:00, 12:00) bucket;
        # 10:59 and older belong to [10:00, 11:00).
        candle("2026-01-05T11:00:00Z", "40", "41", "39", "40.5", "7"),
        candle("2026-01-05T10:59:00Z", "35", "36", "34", "35.5", "4"),
        candle("2026-01-05T10:01:00Z", "31", "33", "30", "32", "2"),
        candle("2026-01-05T10:00:00Z", "30", "33", "29", "32", "1"),
    )
    result = aggregate_candles(candles, "1h")
    assert len(result) == 2
    newest, older = result
    assert newest.timestamp == "2026-01-05T11:00:00+00:00"
    assert newest.open_price == Decimal("40")
    assert newest.close_price == Decimal("40.5")
    assert newest.volume == Decimal("7")
    assert older.timestamp == "2026-01-05T10:00:00+00:00"
    assert older.open_price == Decimal("30")  # earliest member 10:00 opens the hour
    assert older.high_price == Decimal("36")
    assert older.low_price == Decimal("29")
    assert older.close_price == Decimal("35.5")  # latest member 10:59 closes the hour
    assert older.volume == Decimal("7")


def test_1h_never_crosses_date_boundary() -> None:
    candles = (
        candle("2026-01-06T00:01:00Z", "55", "55", "55", "56", "5"),
        candle("2026-01-06T00:00:00Z", "50", "51", "49", "52", "4"),
        candle("2026-01-05T23:59:00Z", "45", "46", "44", "47", "3"),
        candle("2026-01-05T23:00:00Z", "40", "41", "39", "42", "2"),
    )
    result = aggregate_candles(candles, "1h")
    assert [item.timestamp for item in result] == [
        "2026-01-06T00:00:00+00:00",
        "2026-01-05T23:00:00+00:00",
    ]
    previous_day_last = result[1]
    assert previous_day_last.open_price == Decimal("40")
    assert previous_day_last.close_price == Decimal("47")
    assert previous_day_last.volume == Decimal("5")


def test_ema_known_exact_series() -> None:
    candles = tuple(
        candle(f"2026-01-05T10:0{minute}:00Z", str(close), str(close), str(close), str(close), "1")
        for minute, close in ((4, 12), (3, 8), (2, 6), (1, 4), (0, 2))  # newest-first
    )
    # Chronological closes 2, 4, 6, 8, 12; alpha = 2 / (3 + 1) = 0.5.
    # Seed at the third close: (2+4+6)/3 = 4; then 8*0.5+4*0.5 = 6; 12*0.5+6*0.5 = 9.
    assert ema_series(candles, 3) == (
        Decimal("9"),
        Decimal("6"),
        Decimal("4"),
        None,
        None,
    )


def test_ema_exact_period_seed_only() -> None:
    candles = tuple(
        candle(f"2026-01-05T10:0{minute}:00Z", str(close), str(close), str(close), str(close), "1")
        for minute, close in ((5, 60), (4, 50), (3, 40), (2, 30), (1, 20), (0, 10))
    )
    # Exactly `period` candles: only the newest position holds the SMA seed.
    assert ema_series(candles, 6)[0] == Decimal("35")
    assert ema_series(candles, 6)[1:] == (None,) * 5


def test_ema_insufficient_input_all_none() -> None:
    candles = (
        candle("2026-01-05T10:01:00Z", "1", "1", "1", "1", "1"),
        candle("2026-01-05T10:00:00Z", "2", "2", "2", "2", "1"),
    )
    assert ema_series(candles, 3) == (None, None)
    assert ema_series((), 3) == ()
    assert ema_series((), 1) == ()


def test_ema_invalid_periods_rejected() -> None:
    one = (candle("2026-01-05T10:00:00Z", "1", "1", "1", "1", "1"),)
    for bad_period in (0, -1, True, False, 2.5, "3", None):
        with pytest.raises(ValueError):
            ema_series(one, bad_period)


def test_ema_currency_mismatch_rejected() -> None:
    candles = (
        candle("2026-01-05T10:01:00Z", "1", "1", "1", "1", "1", "KRW"),
        candle("2026-01-05T10:00:00Z", "1", "1", "1", "1", "1", "USD"),
    )
    with pytest.raises(ValueError):
        ema_series(candles, 1)


def test_session_vwap_exact_values_newest_first() -> None:
    candles = (
        # Newest-first within one session; typical prices 16, 12, 11, 9 and
        # volumes 5, 3, 1, 1 => cumulative VWAP 13.6, 11.2, 10, 9.
        candle("2026-01-05T10:03:00Z", "15", "20", "12", "16", "5"),
        candle("2026-01-05T10:02:00Z", "12", "15", "9", "12", "3"),
        candle("2026-01-05T10:01:00Z", "11", "12", "10", "11", "1"),
        candle("2026-01-05T10:00:00Z", "9", "10", "8", "9", "1"),
    )
    assert session_vwap_series(candles) == (
        Decimal("13.6"),
        Decimal("11.2"),
        Decimal("10"),
        Decimal("9"),
    )


def test_session_vwap_resets_on_local_date_change() -> None:
    candles = (
        # New local date restarts the accumulator; no carry-over from day 1.
        candle("2026-01-06T00:01:00Z", "20", "20", "20", "20", "1"),
        candle("2026-01-06T00:00:00Z", "20", "20", "20", "20", "1"),
        candle("2026-01-05T23:59:00Z", "10", "10", "10", "10", "1"),
        candle("2026-01-05T23:58:00Z", "10", "10", "10", "10", "1"),
    )
    assert session_vwap_series(candles) == (
        Decimal("20"),
        Decimal("20"),
        Decimal("10"),
        Decimal("10"),
    )


def test_session_vwap_resets_on_offset_change_same_instant_range() -> None:
    # 09:00+09:00 (= 00:00Z) and 09:01Z share nothing session-wise once the
    # offset is part of session identity: two independent sessions.
    candles = (
        candle("2026-01-05T09:01:00Z", "30", "30", "30", "30", "1"),
        candle("2026-01-05T09:00:00+09:00", "10", "10", "10", "10", "1"),
    )
    assert session_vwap_series(candles) == (Decimal("30"), Decimal("10"))


def test_session_vwap_zero_then_positive_volume() -> None:
    candles = (
        # Zero volume yields None until cumulative volume turns positive; the
        # earlier zero-volume rows still contribute nothing afterwards.
        candle("2026-01-05T10:02:00Z", "50", "50", "50", "50", "4"),
        candle("2026-01-05T10:01:00Z", "40", "40", "40", "40", "0"),
        candle("2026-01-05T10:00:00Z", "30", "30", "30", "30", "0"),
    )
    assert session_vwap_series(candles) == (Decimal("50"), None, None)


def test_session_vwap_all_zero_volume_is_none() -> None:
    candles = (
        candle("2026-01-05T10:01:00Z", "10", "10", "10", "10", "0"),
        candle("2026-01-05T10:00:00Z", "20", "20", "20", "20", "0"),
    )
    assert session_vwap_series(candles) == (None, None)
    assert session_vwap_series(()) == ()


def test_session_vwap_currency_mismatch_rejected() -> None:
    candles = (
        candle("2026-01-05T10:01:00Z", "1", "1", "1", "1", "1", "KRW"),
        candle("2026-01-05T10:00:00Z", "1", "1", "1", "1", "1", "USD"),
    )
    with pytest.raises(ValueError):
        session_vwap_series(candles)
