from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from rich.cells import cell_len
from rich.text import Text

from tests.helpers import sample_snapshot
from toss_market_terminal.indicators import (
    SupportResistance,
    aggregate_candles,
    ema_series,
    relative_volume,
    rsi_series,
    session_vwap_series,
)
from toss_market_terminal.models import Candle, Orderbook, OrderbookEntry, Trade
from toss_market_terminal.render import (
    CHART_MODE_LABELS,
    CURRENT_PRICE_COLOR,
    CURRENT_PRICE_DASH,
    DOWN_COLOR,
    MUTED_COLOR,
    PREVIOUS_CLOSE_DASH,
    UP_COLOR,
    NearestLevel,
    _candlestick_grid,
    chart_indicators,
    chart_renderable,
    downsample_candles,
    ema_relation_label_ko,
    format_multiple,
    format_trade_time,
    level_display_ko,
    market_metrics,
    market_signals,
    nearest_support_resistance,
    orderbook_signal_label,
    orderbook_signal_label_ko,
    rsi_zone_label_ko,
    select_chart_candles,
    sparkline,
    trade_pressure_label,
    trade_pressure_label_ko,
    volume_bar,
    vwap_distance_percent,
)


def _candle(
    timestamp: str,
    open_price: str,
    high_price: str,
    low_price: str,
    close_price: str,
    volume: str,
    currency: str = "USD",
) -> Candle:
    return Candle(
        timestamp,
        Decimal(open_price),
        Decimal(high_price),
        Decimal(low_price),
        Decimal(close_price),
        Decimal(volume),
        currency,
    )


def _style_at(line: Text, index: int) -> str | None:
    style: str | None = None
    for span in line.spans:
        if span.start <= index < span.end:
            style = str(span.style)
    return style


def _chronological_snapshot(candles: tuple[Candle, ...]):
    """Wrap newest-first ``candles`` into a snapshot, matching model conventions."""
    return replace(sample_snapshot(), candles=candles)


def test_trade_time_is_displayed_to_whole_seconds() -> None:
    assert format_trade_time("2026-08-25T23:31:42.000Z") == "23:31:42"
    assert format_trade_time("2026-08-25T23:31:42.987654+09:00") == "23:31:42"
    assert format_trade_time("23:31:42.123") == "23:31:42"


def test_signal_multiple_format_stays_within_market_stats_width() -> None:
    assert format_multiple(None) == "—"
    assert format_multiple(Decimal("9.99")) == "9.99배"
    assert format_multiple(Decimal("10")) == "10배"
    assert format_multiple(Decimal("99.99")) == "100배"
    line = f"체결 상승 우세 61.2% · 1분 거래량 {format_multiple(Decimal('99.99'))}"
    assert cell_len(line) <= 40


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


def test_market_signal_korean_labels_preserve_canonical_boundaries() -> None:
    assert orderbook_signal_label_ko(Decimal("60.01")) == "매수 우세"
    assert orderbook_signal_label_ko(Decimal("60")) == "수급 균형"
    assert orderbook_signal_label_ko(Decimal("39.99")) == "매도 우세"
    assert orderbook_signal_label_ko(None) == "데이터 부족"
    assert trade_pressure_label_ko(Decimal("60.01")) == "상승 우세"
    assert trade_pressure_label_ko(Decimal("60")) == "방향 혼조"
    assert trade_pressure_label_ko(Decimal("39.99")) == "하락 우세"
    assert trade_pressure_label_ko(None) == "데이터 부족"


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


def test_candlestick_grid_colors_by_direction_and_reflects_high_low_not_just_close() -> None:
    # Oldest (leftmost) candle is bullish, newest (rightmost) is bearish. Both wicks
    # extend well beyond their open/close body, so a close-only sampler would miss them.
    bullish = _candle("t1", "100", "110", "90", "105", "10")
    bearish = _candle("t2", "105", "108", "95", "100", "10")
    grid = _candlestick_grid((bullish, bearish), rows=5)
    assert len(grid) == 5

    top_row = grid[0]
    assert top_row.plain == "││"
    assert _style_at(top_row, 0) == UP_COLOR
    assert _style_at(top_row, 1) == DOWN_COLOR

    # Bullish's low (90) reaches the bottom row; bearish's low (95) does not, so its
    # column must go blank there even though both candles share the same price scale.
    bottom_row = grid[4]
    assert bottom_row.plain[0] == "│"
    assert bottom_row.plain[1] == " "

    # A row strictly between the two lows is still wick for both: proof the wick
    # tracks the low price, not merely wherever the close price happens to land.
    lower_mid_row = grid[3]
    assert lower_mid_row.plain == "││"


def test_chart_renderable_latest_candle_is_rightmost() -> None:
    # snapshot.candles is newest-first; index 0 (bearish) must render on the right.
    candles = (
        _candle("t3", "100", "101", "99", "95", "5"),  # newest: bearish
        _candle("t2", "100", "101", "99", "101", "5"),  # bullish
        _candle("t1", "100", "101", "99", "101", "5"),  # oldest: bullish
    )
    snapshot = _chronological_snapshot(candles)
    chart = chart_renderable(snapshot, "1m", width=3, height=6)
    lines = chart.plain.splitlines()
    price_line = next(line for line in lines if "█" in line or "│" in line)
    grid_row_index = lines.index(price_line)

    # Re-render the same grid directly (mirroring chart_renderable's ordering) so
    # we can inspect per-column style rather than glyphs alone.
    chronological = tuple(reversed(candles))
    buckets = downsample_candles(chronological, 3)
    grid = _candlestick_grid(buckets, rows=grid_row_index + 1)
    target = grid[grid_row_index]
    assert _style_at(target, 0) == UP_COLOR  # oldest, leftmost
    assert _style_at(target, 2) == DOWN_COLOR  # newest, rightmost


def test_downsample_candles_aggregates_proper_ohlcv_not_close_sampling() -> None:
    chronological = (
        _candle("t1", "10", "12", "9", "11", "1"),
        _candle("t2", "11", "13", "10", "12", "2"),
        _candle("t3", "12", "20", "11", "13", "3"),
        _candle("t4", "13", "14", "5", "6", "4"),
        _candle("t5", "6", "9", "4", "8", "5"),
        _candle("t6", "8", "10", "7", "9", "6"),
    )
    bucketed = downsample_candles(chronological, 3)
    assert len(bucketed) == 3
    first, second, third = bucketed
    assert first.open_price == Decimal("10")
    assert first.close_price == Decimal("12")
    assert first.high_price == Decimal("13")  # not just close-sampled max
    assert first.low_price == Decimal("9")
    assert first.volume == Decimal("3")

    assert second.open_price == Decimal("12")
    assert second.close_price == Decimal("6")
    assert second.high_price == Decimal("20")
    assert second.low_price == Decimal("5")
    assert second.volume == Decimal("7")

    assert third.open_price == Decimal("6")
    assert third.close_price == Decimal("9")
    assert third.high_price == Decimal("10")
    assert third.low_price == Decimal("4")
    assert third.volume == Decimal("11")

    # Fewer candles than target columns: pass through unchanged.
    assert downsample_candles(chronological[:2], 5) == chronological[:2]


def test_downsample_candles_rejects_non_positive_target() -> None:
    with pytest.raises(ValueError):
        downsample_candles((), 0)


def test_chart_renderable_supports_every_timeframe_label() -> None:
    snapshot = sample_snapshot()
    for mode, label in CHART_MODE_LABELS.items():
        chart = chart_renderable(snapshot, mode)
        assert f"PRICE · {label}" in chart.plain


def test_chart_renderable_rejects_unknown_mode_with_bounded_message() -> None:
    with pytest.raises(ValueError) as caught:
        chart_renderable(sample_snapshot(), "3m")
    assert "3m" in str(caught.value)

    with pytest.raises(ValueError) as caught_long:
        chart_renderable(sample_snapshot(), "x" * 500)
    assert len(str(caught_long.value)) < 200


@pytest.mark.parametrize("width", [20, 33, 48])
@pytest.mark.parametrize("height", [1, 5, 6, 10, 18])
def test_chart_renderable_stays_within_width_and_height_bounds(width: int, height: int) -> None:
    chart = chart_renderable(sample_snapshot(), "1m", width=width, height=height)
    lines = chart.plain.splitlines()
    assert len(lines) <= height
    for line in lines:
        assert cell_len(line) <= width


def test_chart_renderable_handles_empty_flat_and_zero_volume_without_crashing() -> None:
    empty = replace(sample_snapshot(), candles=(), daily_candles=())
    chart = chart_renderable(empty, "1m", width=20, height=10)
    assert chart.plain
    for line in chart.plain.splitlines():
        assert cell_len(line) <= 20

    flat_candles = tuple(_candle(f"t{i}", "100", "100", "100", "100", "0") for i in range(5))
    flat = replace(sample_snapshot(), candles=flat_candles)
    chart = chart_renderable(flat, "1m", width=20, height=12)
    lines = chart.plain.splitlines()
    assert len(lines) <= 12
    for line in lines:
        assert cell_len(line) <= 20

    one_candle = replace(sample_snapshot(), candles=(_candle("t1", "1", "1", "1", "1", "0"),))
    chart = chart_renderable(one_candle, "1m", width=20, height=12)
    for line in chart.plain.splitlines():
        assert cell_len(line) <= 20


# --- price axis, current-price line, previous-close line, time axis -------


def test_candlestick_grid_overlays_reference_lines_only_on_blank_cells() -> None:
    # Column 0's wick/body spans the full row range; column 1's is narrow, leaving
    # rows 0-1 blank there for the reference dashes to show through.
    wide = _candle("t1", "102", "110", "100", "108", "10")
    narrow = _candle("t2", "103", "104", "101", "102", "10")
    grid = _candlestick_grid((wide, narrow), rows=5, current_price_row=0, previous_close_row=1)
    row0, row1 = grid[0], grid[1]

    assert row0.plain[0] in ("│", "█")
    assert _style_at(row0, 0) == UP_COLOR  # candle glyph always wins over the overlay

    assert row0.plain[1] == CURRENT_PRICE_DASH
    assert _style_at(row0, 1) == CURRENT_PRICE_COLOR
    assert row1.plain[1] == PREVIOUS_CLOSE_DASH
    assert _style_at(row1, 1) == MUTED_COLOR


def test_chart_renderable_price_axis_shows_current_price_label() -> None:
    snapshot = replace(sample_snapshot(), candles=_rising_candles(30))
    chart = chart_renderable(snapshot, "1m", width=48, height=18, current_price=Decimal("135"))
    assert "135" in chart.plain
    for line in chart.plain.splitlines():
        assert cell_len(line) <= 48


def test_chart_renderable_narrow_width_disables_price_axis() -> None:
    snapshot = replace(sample_snapshot(), candles=_rising_candles(30))
    narrow = chart_renderable(snapshot, "1m", width=10, height=18, current_price=Decimal("135"))
    wide = chart_renderable(snapshot, "1m", width=48, height=18, current_price=Decimal("135"))
    # Below the axis width threshold the whole 10 columns go to candles (matches the
    # pre-axis behavior); at 48 columns some width is reserved for axis labels.
    narrow_price_line = narrow.plain.splitlines()[1]
    wide_price_line = wide.plain.splitlines()[1]
    assert cell_len(narrow_price_line) == 10
    assert cell_len(wide_price_line) < 48
    for line in narrow.plain.splitlines():
        assert cell_len(line) <= 10


def test_chart_renderable_previous_close_line_only_when_row_distinct() -> None:
    snapshot = replace(sample_snapshot(), candles=_rising_candles(30))

    far = chart_renderable(
        snapshot,
        "1m",
        width=48,
        height=18,
        current_price=Decimal("129"),
        previous_close=Decimal("100"),
    )
    far_price_rows = far.plain.splitlines()[1:11]
    assert any(PREVIOUS_CLOSE_DASH in row for row in far_price_rows)

    same_row = chart_renderable(
        snapshot,
        "1m",
        width=48,
        height=18,
        current_price=Decimal("129"),
        previous_close=Decimal("129"),
    )
    same_row_price_rows = same_row.plain.splitlines()[1:11]
    assert not any(PREVIOUS_CLOSE_DASH in row for row in same_row_price_rows)

    unavailable = chart_renderable(
        snapshot, "1m", width=48, height=18, current_price=Decimal("129")
    )
    unavailable_price_rows = unavailable.plain.splitlines()[1:11]
    assert not any(PREVIOUS_CLOSE_DASH in row for row in unavailable_price_rows)


def test_chart_renderable_time_axis_shows_leftmost_and_rightmost_labels() -> None:
    chronological = tuple(
        _candle(f"2026-01-05T09:{i:02d}:00Z", "100", "101", "99", "100", "10") for i in range(40)
    )
    snapshot = replace(sample_snapshot(), candles=tuple(reversed(chronological)))
    chart = chart_renderable(snapshot, "1m", width=48, height=18)
    last_line = chart.plain.splitlines()[-1]
    assert "09:00" in last_line
    assert "09:39" in last_line
    assert cell_len(last_line) <= 48


def test_chart_renderable_time_axis_uses_month_day_for_daily_mode() -> None:
    chronological = tuple(
        _candle(f"2026-01-{i + 1:02d}T09:30:00-04:00", "100", "101", "99", "100", "10")
        for i in range(20)
    )
    snapshot = replace(sample_snapshot(), daily_candles=tuple(reversed(chronological)))
    chart = chart_renderable(snapshot, "1d", width=48, height=18)
    last_line = chart.plain.splitlines()[-1]
    assert "01/01" in last_line
    assert "01/20" in last_line


def test_chart_renderable_time_axis_absent_when_height_too_short_for_chrome() -> None:
    snapshot = replace(sample_snapshot(), candles=_rising_candles(30))
    chart = chart_renderable(snapshot, "1m", width=48, height=5)
    for line in chart.plain.splitlines():
        assert cell_len(line) <= 48


def test_chart_renderable_current_price_above_high_shares_truthful_scale() -> None:
    chronological = tuple(
        _candle(f"2026-01-05T09:{i:02d}:00Z", "100", "101", "99", "100", "10") for i in range(40)
    )
    snapshot = replace(sample_snapshot(), candles=tuple(reversed(chronological)))
    chart = chart_renderable(snapshot, "1m", width=48, height=18, current_price=Decimal("120"))
    price_rows = chart.plain.splitlines()[1:11]
    # The current-price dash renders once, at its true off-chart level, instead
    # of being clamped onto the candle-extreme rows (where candles win blanks).
    current_rows = [row for row in price_rows if CURRENT_PRICE_DASH in row]
    assert len(current_rows) == 1
    # Candles, the reference line, and the axis labels share one scale whose
    # maximum is the current price: 120 labels the top row/current line and
    # the former candle-high label 101 is gone.
    assert "120" in current_rows[0]
    assert "120" in price_rows[0]
    assert "101" not in chart.plain
    for line in chart.plain.splitlines():
        assert cell_len(line) <= 48


def test_chart_renderable_omits_previous_close_outside_represented_range() -> None:
    chronological = tuple(
        _candle(f"2026-01-05T09:{i:02d}:00Z", "100", "101", "99", "100", "10") for i in range(40)
    )
    snapshot = replace(sample_snapshot(), candles=tuple(reversed(chronological)))
    outside = chart_renderable(
        snapshot,
        "1m",
        width=48,
        height=18,
        current_price=Decimal("100"),
        previous_close=Decimal("50"),
    )
    outside_price_rows = outside.plain.splitlines()[1:11]
    # 50 is below every represented price: no clamped dash or label.
    assert not any(PREVIOUS_CLOSE_DASH in row for row in outside_price_rows)
    assert "50" not in outside.plain

    inside = chart_renderable(
        snapshot,
        "1m",
        width=48,
        height=18,
        current_price=Decimal("100"),
        previous_close=Decimal("99.5"),
    )
    # Every candle wick spans the full price range, so the dotted line is
    # occluded by candle glyphs; the truthful in-range axis label must remain.
    assert "99.5" in inside.plain


# --- select_chart_candles / chart_indicators -----------------------------


def test_select_chart_candles_matches_mode_source() -> None:
    snapshot = sample_snapshot()
    assert select_chart_candles(snapshot, "1m") == snapshot.candles
    assert select_chart_candles(snapshot, "1d") == snapshot.daily_candles
    assert select_chart_candles(snapshot, "5m") == aggregate_candles(snapshot.candles, "5m")


def test_select_chart_candles_rejects_unknown_mode_with_bounded_message() -> None:
    with pytest.raises(ValueError) as caught:
        select_chart_candles(sample_snapshot(), "3m")
    assert "3m" in str(caught.value)


def _rising_candles(count: int, start_minute: int = 0) -> tuple[Candle, ...]:
    """Chronologically rising synthetic 1m candles, returned newest-first."""
    chronological = [
        _candle(
            f"2026-01-05T09:{start_minute + i:02d}:00Z",
            str(100 + i),
            str(101 + i),
            str(99 + i),
            str(100 + i),
            "10",
        )
        for i in range(count)
    ]
    return tuple(reversed(chronological))


def test_chart_indicators_matches_direct_pure_helper_calls_on_selected_candles() -> None:
    snapshot = replace(sample_snapshot(), candles=_rising_candles(25))
    result = chart_indicators(snapshot, "1m", Decimal("124"))
    candles = select_chart_candles(snapshot, "1m")
    assert result.ema_short == ema_series(candles, 9)[0]
    assert result.ema_long == ema_series(candles, 21)[0]
    assert result.rsi == rsi_series(candles, 14)[0]
    assert result.relative_volume == relative_volume(candles)
    assert result.vwap == session_vwap_series(candles)[0]
    assert result.ema_short is not None
    assert result.ema_long is not None
    assert result.rsi is not None
    assert result.vwap is not None


def test_chart_indicators_never_fabricates_daily_vwap() -> None:
    snapshot = replace(sample_snapshot(), daily_candles=_rising_candles(25))
    result = chart_indicators(snapshot, "1d", Decimal("124"))
    assert result.vwap is None
    # EMA/RSI still compute from the daily series itself; only VWAP is unavailable.
    assert result.ema_short is not None
    assert result.rsi is not None


def test_chart_indicators_insufficient_history_stays_none_not_zero() -> None:
    # sample_snapshot ships only two 1m candles: nowhere near EMA9/EMA21/RSI14 warmup,
    # and too few prior candles for a relative-volume baseline.
    snapshot = sample_snapshot()
    result = chart_indicators(snapshot, "1m", Decimal("110"))
    assert result.ema_short is None
    assert result.ema_long is None
    assert result.rsi is None
    assert result.relative_volume is None
    # Session VWAP only needs positive cumulative volume, which two candles supply.
    assert result.vwap is not None


# --- Korean indicator/level presentation helpers --------------------------


def test_ema_relation_label_ko_boundaries() -> None:
    assert ema_relation_label_ko(None, Decimal("1")) == "데이터 부족"
    assert ema_relation_label_ko(Decimal("1"), None) == "데이터 부족"
    assert ema_relation_label_ko(Decimal("2"), Decimal("1")) == "단기선 위"
    assert ema_relation_label_ko(Decimal("1"), Decimal("2")) == "단기선 아래"
    assert ema_relation_label_ko(Decimal("1"), Decimal("1")) == "단기선 겹침"


def test_rsi_zone_label_ko_boundaries() -> None:
    assert rsi_zone_label_ko(None) == "데이터 부족"
    assert rsi_zone_label_ko(Decimal("29.99")) == "낮은 구간"
    assert rsi_zone_label_ko(Decimal("30")) == "중립 구간"
    assert rsi_zone_label_ko(Decimal("70")) == "중립 구간"
    assert rsi_zone_label_ko(Decimal("70.01")) == "높은 구간"


def test_vwap_distance_percent_handles_missing_and_zero_vwap() -> None:
    assert vwap_distance_percent(None, Decimal("100")) is None
    assert vwap_distance_percent(Decimal("0"), Decimal("100")) is None
    assert vwap_distance_percent(Decimal("100"), Decimal("110")) == Decimal("10")


def test_nearest_support_resistance_picks_highest_support_lowest_resistance() -> None:
    levels = SupportResistance(
        previous_close=Decimal("100"),
        session_high=Decimal("105"),
        session_low=Decimal("95"),
        recent_high=Decimal("110"),
        recent_low=Decimal("90"),
        swing_high=Decimal("120"),
        swing_low=Decimal("80"),
    )
    result = nearest_support_resistance(levels, Decimal("102"))
    assert result.support == NearestLevel("전일 종가", Decimal("100"))
    assert result.resistance == NearestLevel("세션 고가", Decimal("105"))


def test_nearest_support_resistance_ties_prefer_earlier_field_order() -> None:
    levels = SupportResistance(previous_close=Decimal("100"), session_low=Decimal("100"))
    result = nearest_support_resistance(levels, Decimal("100"))
    assert result.support == NearestLevel("전일 종가", Decimal("100"))
    assert result.resistance == NearestLevel("전일 종가", Decimal("100"))


def test_nearest_support_resistance_missing_side_is_none() -> None:
    only_above = SupportResistance(session_high=Decimal("110"), swing_high=Decimal("120"))
    result = nearest_support_resistance(only_above, Decimal("100"))
    assert result.support is None
    assert result.resistance == NearestLevel("세션 고가", Decimal("110"))

    only_below = SupportResistance(session_low=Decimal("90"), swing_low=Decimal("80"))
    result = nearest_support_resistance(only_below, Decimal("100"))
    assert result.support == NearestLevel("세션 저가", Decimal("90"))
    assert result.resistance is None

    empty = nearest_support_resistance(SupportResistance(), Decimal("100"))
    assert empty.support is None
    assert empty.resistance is None


def test_level_display_ko_formats_label_and_price_or_dash() -> None:
    assert level_display_ko(None, "USD") == "—"
    close_level = NearestLevel("전일 종가", Decimal("42500"))
    assert level_display_ko(close_level, "KRW") == "전일 종가 42,500"
    swing_level = NearestLevel("스윙 저점", Decimal("110.50"))
    assert level_display_ko(swing_level, "USD") == "스윙 저점 110.5"
