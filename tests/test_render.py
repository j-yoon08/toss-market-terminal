from __future__ import annotations

from decimal import Decimal

from tests.helpers import sample_snapshot
from toss_market_terminal.models import Orderbook, OrderbookEntry, Trade
from toss_market_terminal.render import (
    chart_renderable,
    market_metrics,
    sparkline,
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
