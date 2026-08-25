from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tests.helpers import sample_snapshot
from toss_market_terminal.models import Candle, Orderbook, OrderbookEntry, Trade
from toss_market_terminal.render import (
    chart_renderable,
    market_metrics,
    market_signals,
    orderbook_signal_label,
    sparkline,
    trade_pressure_label,
    volume_bar,
)


def test_market_metrics_normalize_quote_to_candle_timezone() -> None:
    snapshot = sample_snapshot()
    metrics = market_metrics(snapshot)
    assert metrics.previous_close == Decimal("100")
    assert metrics.change == Decimal("10")
    assert metrics.change_percent == Decimal("10")
    assert metrics.day_high == Decimal("112")
    assert metrics.day_low == Decimal("98")
    assert metrics.day_volume == Decimal("1000")
    assert metrics.spread == Decimal("0.20")
    assert metrics.recent_vwap == Decimal("328") / Decimal("3")


def test_terminal_chart_and_depth_bar_are_bounded() -> None:
    assert sparkline([Decimal("1"), Decimal("2"), Decimal("3")]) == "▁▄█"
    assert volume_bar(Decimal("5"), Decimal("10"), width=8) == "████    "
    assert len(volume_bar(Decimal("99"), Decimal("10"), width=8)) == 8
    chart = chart_renderable(sample_snapshot(), "1d")
    assert "PRICE · DAILY" in chart.plain
    assert "VOLUME" in chart.plain


def test_live_orderbook_and_trades_override_snapshot_metrics() -> None:
    snapshot = sample_snapshot()
    live_orderbook = Orderbook(
        "USD",
        asks=(OrderbookEntry(Decimal("111"), Decimal("5")),),
        bids=(OrderbookEntry(Decimal("109"), Decimal("7")),),
        timestamp="2026-08-25T10:01:00+09:00",
    )
    live_trades = (Trade(Decimal("120"), Decimal("3"), "2026-08-25T10:01:00+09:00", "USD"),)
    metrics = market_metrics(
        snapshot,
        Decimal("120"),
        orderbook=live_orderbook,
        trades=live_trades,
    )
    assert metrics.spread == Decimal("2")
    assert metrics.recent_vwap == Decimal("120")
    assert metrics.day_high == Decimal("120")


def test_next_market_date_uses_latest_candle_as_previous_close() -> None:
    snapshot = sample_snapshot()
    metrics = market_metrics(
        snapshot,
        Decimal("115"),
        current_timestamp="2026-08-26T13:00:00+09:00",
    )
    assert metrics.previous_close == Decimal("110")
    assert metrics.change == Decimal("5")
    assert metrics.day_high is None
    assert metrics.day_low is None


def test_market_signals_use_displayed_book_recent_ticks_and_candle_median() -> None:
    snapshot = sample_snapshot()
    book = Orderbook(
        "USD",
        asks=(OrderbookEntry(Decimal("112.1"), Decimal("20")),),
        bids=(OrderbookEntry(Decimal("111.9"), Decimal("80")),),
        timestamp=None,
    )
    trades = (
        Trade(Decimal("110"), Decimal("3"), "2026-08-25T10:00:02Z", "USD"),
        Trade(Decimal("109"), Decimal("1"), "2026-08-25T10:00:01Z", "USD"),
        Trade(Decimal("111"), Decimal("2"), "2026-08-25T10:00:00Z", "USD"),
    )
    candle = snapshot.candles[0]
    candles = (
        replace(candle, volume=Decimal("400")),
        replace(candle, volume=Decimal("100")),
        replace(candle, volume=Decimal("100")),
        replace(candle, volume=Decimal("200")),
    )
    signals = market_signals(
        replace(snapshot, candles=candles),
        Decimal("112"),
        orderbook=book,
        trades=trades,
    )
    assert signals.orderbook_imbalance_percent == Decimal("80")
    assert signals.bid_ask_ratio == Decimal("4")
    expected_vwap = Decimal("661") / Decimal("6")
    assert signals.vwap_distance_percent == (Decimal("112") - expected_vwap) / expected_vwap * 100
    assert signals.volume_spike_ratio == Decimal("4")
    assert signals.trade_pressure_percent == Decimal("75")
    assert orderbook_signal_label(signals.orderbook_imbalance_percent) == "BID HEAVY"
    assert trade_pressure_label(signals.trade_pressure_percent) == "UPTICK HEAVY"


def test_market_signal_labels_have_strict_neutral_boundaries() -> None:
    assert orderbook_signal_label(Decimal("60")) == "BALANCED"
    assert orderbook_signal_label(Decimal("40")) == "BALANCED"
    assert orderbook_signal_label(Decimal("39.99")) == "ASK HEAVY"
    assert trade_pressure_label(Decimal("60")) == "MIXED"
    assert trade_pressure_label(Decimal("40")) == "MIXED"
    assert trade_pressure_label(Decimal("39.99")) == "DOWNTICK HEAVY"
    assert orderbook_signal_label(None) == "INSUFFICIENT"
    assert trade_pressure_label(None) == "INSUFFICIENT"


def test_market_signals_return_none_for_zero_or_insufficient_denominators() -> None:
    snapshot = sample_snapshot()
    empty = replace(
        snapshot,
        orderbook=Orderbook("USD", asks=(), bids=(), timestamp=None),
        trades=(
            Trade(Decimal("100"), Decimal("1"), "2026-08-25T10:00:00Z", "USD"),
            Trade(Decimal("100"), Decimal("2"), "2026-08-25T09:59:59Z", "USD"),
        ),
        candles=(
            Candle(
                "2026-08-25T10:00:00Z",
                Decimal("1"),
                Decimal("1"),
                Decimal("1"),
                Decimal("1"),
                Decimal("0"),
                "USD",
            ),
        ),
    )
    signals = market_signals(empty, Decimal("100"))
    assert signals.orderbook_imbalance_percent is None
    assert signals.bid_ask_ratio is None
    assert signals.volume_spike_ratio is None
    assert signals.trade_pressure_percent is None
