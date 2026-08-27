from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median

from rich.cells import cell_len, set_cell_size
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .indicators import (
    SupportResistance,
    aggregate_candles,
    ema_series,
    relative_volume,
    rsi_series,
    session_vwap_series,
    support_resistance,
)
from .models import Candle, MarketSnapshot, Orderbook, Trade

SPARK_CHARS = "▁▂▃▄▅▆▇█"
UP_COLOR = "#2dd4bf"
DOWN_COLOR = "#fb7185"
MUTED_COLOR = "#7d8998"
CURRENT_PRICE_COLOR = "#f2c14e"
CURRENT_PRICE_DASH = "─"
PREVIOUS_CLOSE_DASH = "·"
HOLDING_AVERAGE_COLOR = "#6c7a8c"
HOLDING_AVERAGE_DASH = "╌"

CHART_MODE_LABELS = {
    "1m": "1 MINUTE",
    "5m": "5 MINUTES",
    "15m": "15 MINUTES",
    "1h": "1 HOUR",
    "1d": "DAILY",
}
TIMEFRAME_LABELS_KO = {
    "1m": "1분봉",
    "5m": "5분봉",
    "15m": "15분봉",
    "1h": "1시간봉",
    "1d": "일봉",
}
_CHART_MAX_ECHO = 32
_MIN_CHART_DIMENSION = 1
_CHART_CHROME_LINES = 3  # blank separator, "VOLUME" header, bottom time axis
_PRICE_ROW_RATIO = 0.7
_MIN_WIDTH_FOR_PRICE_AXIS = 16


@dataclass(frozen=True, slots=True)
class MarketMetrics:
    previous_close: Decimal | None
    change: Decimal | None
    change_percent: Decimal | None
    day_high: Decimal | None
    day_low: Decimal | None
    day_volume: Decimal | None
    spread: Decimal | None
    spread_percent: Decimal | None
    recent_vwap: Decimal | None


@dataclass(frozen=True, slots=True)
class MarketSignals:
    orderbook_imbalance_percent: Decimal | None
    bid_ask_ratio: Decimal | None
    vwap_distance_percent: Decimal | None
    volume_spike_ratio: Decimal | None
    trade_pressure_percent: Decimal | None


def orderbook_signal_label(value: Decimal | None) -> str:
    if value is None:
        return "INSUFFICIENT"
    if value > Decimal("60"):
        return "BID HEAVY"
    if value < Decimal("40"):
        return "ASK HEAVY"
    return "BALANCED"


def trade_pressure_label(value: Decimal | None) -> str:
    if value is None:
        return "INSUFFICIENT"
    if value > Decimal("60"):
        return "UPTICK HEAVY"
    if value < Decimal("40"):
        return "DOWNTICK HEAVY"
    return "MIXED"


def orderbook_signal_label_ko(value: Decimal | None) -> str:
    """Return a compact Korean display label without changing the signal contract."""
    return {
        "INSUFFICIENT": "데이터 부족",
        "BID HEAVY": "매수 우세",
        "ASK HEAVY": "매도 우세",
        "BALANCED": "수급 균형",
    }[orderbook_signal_label(value)]


def trade_pressure_label_ko(value: Decimal | None) -> str:
    """Return a compact Korean display label without changing the signal contract."""
    return {
        "INSUFFICIENT": "데이터 부족",
        "UPTICK HEAVY": "상승 우세",
        "DOWNTICK HEAVY": "하락 우세",
        "MIXED": "방향 혼조",
    }[trade_pressure_label(value)]


def format_decimal(value: Decimal, currency: str | None = None) -> str:
    if currency == "KRW":
        return f"{value:,.0f}"
    exponent = -value.as_tuple().exponent
    places = min(max(exponent, 2), 6)
    return f"{value:,.{places}f}".rstrip("0").rstrip(".")


def format_signed(value: Decimal, currency: str) -> str:
    prefix = "+" if value > 0 else ""
    return f"{prefix}{format_decimal(value, currency)}"


def format_percent(value: Decimal) -> str:
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.2f}%"


def format_multiple(value: Decimal | None) -> str:
    """Format a ratio compactly enough for the 40-cell market stats panel."""
    if value is None:
        return "—"
    places = 0 if abs(value) >= Decimal("10") else 2
    return f"{value:.{places}f}배"


def format_age(monotonic_value: float | None) -> str:
    """Bounded, truthful age since ``monotonic_value``; ``"—"`` when never observed."""
    if monotonic_value is None:
        return "—"
    return f"{max(0.0, time.monotonic() - monotonic_value):.1f}s"


def format_trade_time(timestamp: str) -> str:
    """Format an ISO-like trade timestamp to whole-second wall-clock time."""
    return timestamp.rsplit("T", 1)[-1][:8]


def sparkline(values: list[Decimal]) -> str:
    if not values:
        return "데이터 없음"
    low, high = min(values), max(values)
    if low == high:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(values)
    span = high - low
    return "".join(
        SPARK_CHARS[int((value - low) / span * (len(SPARK_CHARS) - 1))] for value in values
    )


def volume_bar(value: Decimal, maximum: Decimal, width: int = 8) -> str:
    if maximum <= 0 or value <= 0:
        return " " * width
    filled = max(1, min(width, int(value / maximum * width)))
    return "█" * filled + " " * (width - filled)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same_market_date(quote_timestamp: str | None, candle_timestamp: str) -> bool:
    quote_time = _parse_timestamp(quote_timestamp)
    candle_time = _parse_timestamp(candle_timestamp)
    if quote_time is None or candle_time is None:
        return True
    if quote_time.utcoffset() is not None and candle_time.utcoffset() is not None:
        quote_time = quote_time.astimezone(candle_time.tzinfo)
    return quote_time.date() == candle_time.date()


def market_metrics(
    snapshot: MarketSnapshot,
    current_price: Decimal | None = None,
    *,
    orderbook: Orderbook | None = None,
    trades: Sequence[Trade] | None = None,
    current_timestamp: str | None = None,
) -> MarketMetrics:
    price = current_price if current_price is not None else snapshot.price.last_price
    previous_close: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    day_volume: Decimal | None = None
    if snapshot.daily_candles:
        latest = snapshot.daily_candles[0]
        quote_timestamp = current_timestamp or snapshot.price.timestamp
        if _same_market_date(quote_timestamp, latest.timestamp):
            day_high = max(latest.high_price, price)
            day_low = min(latest.low_price, price)
            day_volume = latest.volume
            if len(snapshot.daily_candles) >= 2:
                previous_close = snapshot.daily_candles[1].close_price
        else:
            previous_close = latest.close_price

    change = price - previous_close if previous_close is not None else None
    change_percent = (
        change / previous_close * Decimal("100")
        if change is not None and previous_close not in {None, Decimal("0")}
        else None
    )

    active_orderbook = orderbook if orderbook is not None else snapshot.orderbook
    spread: Decimal | None = None
    spread_percent: Decimal | None = None
    if active_orderbook.asks and active_orderbook.bids:
        best_ask = active_orderbook.asks[0].price
        best_bid = active_orderbook.bids[0].price
        spread = best_ask - best_bid
        midpoint = (best_ask + best_bid) / Decimal("2")
        if midpoint:
            spread_percent = spread / midpoint * Decimal("100")

    active_trades = trades if trades is not None else snapshot.trades
    total_volume = sum((trade.volume for trade in active_trades), Decimal("0"))
    recent_vwap = (
        sum((trade.price * trade.volume for trade in active_trades), Decimal("0")) / total_volume
        if total_volume
        else None
    )
    return MarketMetrics(
        previous_close=previous_close,
        change=change,
        change_percent=change_percent,
        day_high=day_high,
        day_low=day_low,
        day_volume=day_volume,
        spread=spread,
        spread_percent=spread_percent,
        recent_vwap=recent_vwap,
    )


def market_signals(
    snapshot: MarketSnapshot,
    current_price: Decimal | None = None,
    *,
    orderbook: Orderbook | None = None,
    trades: Sequence[Trade] | None = None,
    depth: int = 7,
) -> MarketSignals:
    price = current_price if current_price is not None else snapshot.price.last_price
    active_orderbook = orderbook if orderbook is not None else snapshot.orderbook
    asks = active_orderbook.asks[:depth]
    bids = active_orderbook.bids[:depth]
    ask_volume = sum((entry.volume for entry in asks), Decimal("0"))
    bid_volume = sum((entry.volume for entry in bids), Decimal("0"))
    total_book_volume = ask_volume + bid_volume
    imbalance = bid_volume / total_book_volume * Decimal("100") if total_book_volume > 0 else None
    bid_ask_ratio = bid_volume / ask_volume if ask_volume > 0 else None

    active_trades = tuple(trades) if trades is not None else snapshot.trades
    trade_volume = sum((trade.volume for trade in active_trades), Decimal("0"))
    recent_vwap = (
        sum((trade.price * trade.volume for trade in active_trades), Decimal("0")) / trade_volume
        if trade_volume > 0
        else None
    )
    vwap_distance = (
        (price - recent_vwap) / recent_vwap * Decimal("100")
        if recent_vwap is not None and recent_vwap != 0
        else None
    )

    positive_previous_volumes = [
        candle.volume for candle in snapshot.candles[1:] if candle.volume > 0
    ]
    volume_spike = None
    if snapshot.candles and len(positive_previous_volumes) >= 3:
        baseline = median(positive_previous_volumes)
        if baseline > 0:
            volume_spike = snapshot.candles[0].volume / baseline

    uptick_volume = Decimal("0")
    downtick_volume = Decimal("0")
    items = list(active_trades)
    for index, trade in enumerate(items[:-1]):
        older = items[index + 1]
        if trade.price > older.price:
            uptick_volume += trade.volume
        elif trade.price < older.price:
            downtick_volume += trade.volume
    directional_volume = uptick_volume + downtick_volume
    trade_pressure = (
        uptick_volume / directional_volume * Decimal("100") if directional_volume > 0 else None
    )

    return MarketSignals(
        orderbook_imbalance_percent=imbalance,
        bid_ask_ratio=bid_ask_ratio,
        vwap_distance_percent=vwap_distance,
        volume_spike_ratio=volume_spike,
        trade_pressure_percent=trade_pressure,
    )


def direction_style(change: Decimal | None) -> str:
    if change is None or change == 0:
        return "bold white"
    return f"bold {UP_COLOR}" if change > 0 else f"bold {DOWN_COLOR}"


def orderbook_table(orderbook: Orderbook, current_price: Decimal, depth: int = 7) -> Table:
    entries = list(orderbook.asks[:depth]) + list(orderbook.bids[:depth])
    max_volume = max((entry.volume for entry in entries), default=Decimal("0"))
    table = Table(expand=True, box=None, pad_edge=False)
    table.add_column("SIDE", width=5)
    table.add_column("PRICE", justify="right")
    table.add_column("SIZE", justify="right")
    table.add_column("DEPTH", width=8)
    for entry in reversed(orderbook.asks[:depth]):
        table.add_row(
            Text("ASK", style=DOWN_COLOR),
            Text(format_decimal(entry.price, orderbook.currency), style=DOWN_COLOR),
            format_decimal(entry.volume),
            Text(volume_bar(entry.volume, max_volume), style="#70414e"),
        )
    table.add_row(
        Text("LAST", style="bold white"),
        Text(format_decimal(current_price, orderbook.currency), style="bold white"),
        "—",
        Text("────────", style="#3a4654"),
    )
    for entry in orderbook.bids[:depth]:
        table.add_row(
            Text("BID", style=UP_COLOR),
            Text(format_decimal(entry.price, orderbook.currency), style=UP_COLOR),
            format_decimal(entry.volume),
            Text(volume_bar(entry.volume, max_volume), style="#275e5e"),
        )
    return table


def trades_table(trades: tuple[Trade, ...] | list[Trade], limit: int = 15) -> Table:
    items = list(trades)[:limit]
    table = Table(expand=True, box=None, pad_edge=False)
    table.add_column("TIME", width=9)
    table.add_column("PRICE", justify="right")
    table.add_column("SIZE", justify="right")
    table.add_column("", width=1)
    for index, trade in enumerate(items):
        older_price = items[index + 1].price if index + 1 < len(items) else trade.price
        if trade.price > older_price:
            marker, style = "▲", UP_COLOR
        elif trade.price < older_price:
            marker, style = "▼", DOWN_COLOR
        else:
            marker, style = "·", MUTED_COLOR
        table.add_row(
            format_trade_time(trade.timestamp),
            Text(format_decimal(trade.price, trade.currency), style=style),
            format_decimal(trade.volume),
            Text(marker, style=style),
        )
    return table


def _chart_echo(value: object) -> str:
    """Bounded echo of an offending value for error messages."""
    text = str(value)
    if len(text) > _CHART_MAX_ECHO:
        text = text[:_CHART_MAX_ECHO] + "…"
    return text


def select_chart_candles(snapshot: MarketSnapshot, mode: str) -> tuple[Candle, ...]:
    """Newest-first source candles for chart ``mode``.

    ``1m`` and ``1d`` use ``snapshot.candles`` / ``snapshot.daily_candles``
    as-is; ``5m``, ``15m``, and ``1h`` aggregate ``snapshot.candles`` via
    :func:`toss_market_terminal.indicators.aggregate_candles`. Raises
    ``ValueError`` for an unsupported ``mode``.
    """
    if mode not in CHART_MODE_LABELS:
        raise ValueError(
            f"지원하지 않는 차트 모드입니다: {_chart_echo(mode)} "
            f"(지원: {', '.join(CHART_MODE_LABELS)})"
        )
    if mode == "1d":
        return tuple(snapshot.daily_candles)
    if mode == "1m":
        return tuple(snapshot.candles)
    return aggregate_candles(snapshot.candles, mode)


_LEVEL_LABELS_KO: tuple[tuple[str, str], ...] = (
    ("previous_close", "전일 종가"),
    ("session_high", "세션 고가"),
    ("session_low", "세션 저가"),
    ("recent_high", "최근 고가"),
    ("recent_low", "최근 저가"),
    ("swing_high", "스윙 고점"),
    ("swing_low", "스윙 저점"),
)


@dataclass(frozen=True, slots=True)
class NearestLevel:
    label: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class NearestLevels:
    """Nearest support/resistance; either side is ``None`` when no candidate qualifies."""

    support: NearestLevel | None
    resistance: NearestLevel | None


def nearest_support_resistance(levels: SupportResistance, current_price: Decimal) -> NearestLevels:
    """Nearest support (highest candidate <= price) and resistance (lowest candidate >= price).

    Considers every named field of ``levels``, skipping ``None`` ones. When
    several qualifying candidates tie on price, the one listed first in
    :data:`_LEVEL_LABELS_KO` (dataclass field order) wins.
    """
    candidates = [
        (label, price)
        for field, label in _LEVEL_LABELS_KO
        if (price := getattr(levels, field)) is not None
    ]
    below = [item for item in candidates if item[1] <= current_price]
    above = [item for item in candidates if item[1] >= current_price]
    support = max(below, key=lambda item: item[1]) if below else None
    resistance = min(above, key=lambda item: item[1]) if above else None
    return NearestLevels(
        support=NearestLevel(*support) if support is not None else None,
        resistance=NearestLevel(*resistance) if resistance is not None else None,
    )


@dataclass(frozen=True, slots=True)
class ChartIndicators:
    """Latest-candle EMA/RSI/volume/VWAP snapshot plus nearest levels for a chart mode."""

    mode: str
    ema_short: Decimal | None
    ema_long: Decimal | None
    rsi: Decimal | None
    relative_volume: Decimal | None
    vwap: Decimal | None
    levels: NearestLevels


@dataclass(frozen=True, slots=True)
class ChartIndicatorBase:
    """The snapshot+mode half of :class:`ChartIndicators`: everything but nearest levels.

    Depends only on ``(snapshot, mode)``, never on a live current price, so
    callers that re-render on every trade/orderbook tick (see
    :class:`toss_market_terminal.tui.TossMarketApp`) can cache one instance
    per ``(snapshot identity, mode)`` pair and cheaply re-derive
    :class:`ChartIndicators` per tick via :func:`chart_indicators_from_base`
    instead of repeating the full EMA/RSI/VWAP/pivot computation each time.
    """

    mode: str
    ema_short: Decimal | None
    ema_long: Decimal | None
    rsi: Decimal | None
    relative_volume: Decimal | None
    vwap: Decimal | None
    levels: SupportResistance


def chart_indicator_base(snapshot: MarketSnapshot, mode: str) -> ChartIndicatorBase:
    """Compute the cacheable, current-price-independent half of :func:`chart_indicators`.

    Candles come from :func:`select_chart_candles`, so every field reflects
    the same source the chart itself renders. Session VWAP is only computed
    for intraday modes; ``1d`` always reports ``vwap=None`` rather than
    fabricate a session that does not exist. Every field stays ``None`` when
    its underlying pure helper (in :mod:`toss_market_terminal.indicators`)
    lacks enough data. Raises ``ValueError`` for malformed/naive candle
    timestamps or a currency mismatch (see :mod:`toss_market_terminal.indicators`).
    """
    candles = select_chart_candles(snapshot, mode)
    ema_short_series = ema_series(candles, 9)
    ema_long_series = ema_series(candles, 21)
    rsi_values = rsi_series(candles, 14)
    vwap = session_vwap_series(candles)[0] if mode != "1d" and candles else None
    daily_candles = candles if mode == "1d" else snapshot.daily_candles
    levels = support_resistance(candles, daily_candles=daily_candles)
    return ChartIndicatorBase(
        mode=mode,
        ema_short=ema_short_series[0] if ema_short_series else None,
        ema_long=ema_long_series[0] if ema_long_series else None,
        rsi=rsi_values[0] if rsi_values else None,
        relative_volume=relative_volume(candles),
        vwap=vwap,
        levels=levels,
    )


def chart_indicators_from_base(base: ChartIndicatorBase, current_price: Decimal) -> ChartIndicators:
    """Cheaply project a cached :class:`ChartIndicatorBase` onto a live ``current_price``.

    Only the nearest-support/resistance projection runs here; a current-price
    crossing a cached level therefore still updates ``levels`` correctly
    without recomputing EMA/RSI/VWAP/pivots.
    """
    return ChartIndicators(
        mode=base.mode,
        ema_short=base.ema_short,
        ema_long=base.ema_long,
        rsi=base.rsi,
        relative_volume=base.relative_volume,
        vwap=base.vwap,
        levels=nearest_support_resistance(base.levels, current_price),
    )


def chart_indicators(
    snapshot: MarketSnapshot, mode: str, current_price: Decimal
) -> ChartIndicators:
    """Compute EMA9/EMA21, RSI14, relative volume, session VWAP, and nearest levels for ``mode``.

    Convenience wrapper around :func:`chart_indicator_base` and
    :func:`chart_indicators_from_base` for one-shot callers; callers that
    re-render per tick (e.g. on every trade/orderbook event) should cache the
    base and call :func:`chart_indicators_from_base` directly instead.
    """
    return chart_indicators_from_base(chart_indicator_base(snapshot, mode), current_price)


def ema_relation_label_ko(ema_short: Decimal | None, ema_long: Decimal | None) -> str:
    """Plain Korean description of EMA9 vs EMA21 position; not advice."""
    if ema_short is None or ema_long is None:
        return "데이터 부족"
    if ema_short > ema_long:
        return "단기선 위"
    if ema_short < ema_long:
        return "단기선 아래"
    return "단기선 겹침"


def rsi_zone_label_ko(rsi: Decimal | None) -> str:
    """Plain Korean RSI zone description; not advice."""
    if rsi is None:
        return "데이터 부족"
    if rsi < Decimal("30"):
        return "낮은 구간"
    if rsi > Decimal("70"):
        return "높은 구간"
    return "중립 구간"


def vwap_distance_percent(vwap: Decimal | None, current_price: Decimal) -> Decimal | None:
    """Current price's percent distance from session ``vwap``; ``None`` when unavailable."""
    if vwap is None or vwap == 0:
        return None
    return (current_price - vwap) / vwap * Decimal("100")


def level_display_ko(level: NearestLevel | None, currency: str | None) -> str:
    """Format a nearest support/resistance level as ``"라벨 가격"``, or ``"—"`` when absent."""
    if level is None:
        return "—"
    return f"{level.label} {format_decimal(level.price, currency)}"


def downsample_candles(candles: Sequence[Candle], target_columns: int) -> tuple[Candle, ...]:
    """Aggregate chronological (oldest-first) ``candles`` into ``target_columns`` buckets.

    Each bucket spans a contiguous, non-overlapping slice of ``candles`` (no
    reordering) with proper OHLCV: open is its first member's open, close is
    its last member's close, high/low are the bucket extremes, and volume is
    the sum. Returns ``candles`` unchanged when it already fits within
    ``target_columns``. Raises ``ValueError`` when ``target_columns`` is not
    positive.
    """
    if target_columns <= 0:
        raise ValueError(f"차트 컬럼 수는 양수여야 합니다: {_chart_echo(target_columns)}")
    items = tuple(candles)
    count = len(items)
    if count <= target_columns:
        return items
    bucketed: list[Candle] = []
    for index in range(target_columns):
        start = index * count // target_columns
        end = (index + 1) * count // target_columns
        group = items[start:end]
        if not group:
            continue
        bucketed.append(
            Candle(
                timestamp=group[0].timestamp,
                open_price=group[0].open_price,
                high_price=max(candle.high_price for candle in group),
                low_price=min(candle.low_price for candle in group),
                close_price=group[-1].close_price,
                volume=sum((candle.volume for candle in group), Decimal("0")),
                currency=group[-1].currency,
            )
        )
    return tuple(bucketed)


def _price_row(value: Decimal, low: Decimal, span: Decimal, rows: int) -> int:
    """Row index (0 = top) that ``value`` maps to across ``rows`` price bands."""
    if rows <= 1:
        return 0
    normalized = (value - low) / span
    if normalized < 0:
        normalized = Decimal("0")
    elif normalized > 1:
        normalized = Decimal("1")
    row_from_top = round(float((Decimal("1") - normalized) * (rows - 1)))
    return max(0, min(rows - 1, row_from_top))


def _price_bounds(candles: Sequence[Candle]) -> tuple[Decimal, Decimal, Decimal]:
    """Low/high/span across ``candles``, with a span floor to keep row math defined.

    Invariant under :func:`downsample_candles`: a bucket's low/high are the
    extremes of its members, so this returns the same low/high whether called
    on the full chronological series or on its downsampled buckets.
    """
    low = min(candle.low_price for candle in candles)
    high = max(candle.high_price for candle in candles)
    span = high - low
    if span <= 0:
        span = Decimal("1")
    return low, high, span


def _candlestick_grid(
    buckets: Sequence[Candle],
    rows: int,
    *,
    scale: tuple[Decimal, Decimal, Decimal] | None = None,
    current_price_row: int | None = None,
    previous_close_row: int | None = None,
    holding_average_row: int | None = None,
) -> list[Text]:
    """Candlestick body/wick grid, optionally overlaid with reference-price dashes.

    ``current_price_row``/``previous_close_row``/``holding_average_row`` (row
    indices from the same price scale used to build ``buckets``' rows) only
    fill in cells that are otherwise blank -- a candle's body/wick always
    wins so the reference lines read as a line *behind* the candles, never
    over them. Among the reference lines themselves, ``current_price_row``
    is checked first so the live price stays visually dominant over the
    holding-average line on a collision, and ``holding_average_row`` in turn
    wins over ``previous_close_row``. ``scale`` is the ``(low, high, span)``
    triple shared with the caller's row math; when omitted it is derived
    from ``buckets`` alone.
    """
    lines = [Text() for _ in range(rows)]
    if not buckets or rows <= 0:
        return lines
    low, _high, span = scale if scale is not None else _price_bounds(buckets)
    for candle in buckets:
        style = UP_COLOR if candle.close_price >= candle.open_price else DOWN_COLOR
        top_row = _price_row(candle.high_price, low, span, rows)
        bottom_row = _price_row(candle.low_price, low, span, rows)
        body_top_row = _price_row(max(candle.open_price, candle.close_price), low, span, rows)
        body_bottom_row = _price_row(min(candle.open_price, candle.close_price), low, span, rows)
        for row in range(rows):
            if body_top_row <= row <= body_bottom_row:
                lines[row].append("█", style=style)
            elif top_row <= row <= bottom_row:
                lines[row].append("│", style=style)
            elif row == current_price_row:
                lines[row].append(CURRENT_PRICE_DASH, style=CURRENT_PRICE_COLOR)
            elif row == holding_average_row:
                lines[row].append(HOLDING_AVERAGE_DASH, style=HOLDING_AVERAGE_COLOR)
            elif row == previous_close_row:
                lines[row].append(PREVIOUS_CLOSE_DASH, style=MUTED_COLOR)
            else:
                lines[row].append(" ")
    return lines


def _axis_label_width(labels: Sequence[str]) -> int:
    """One leading space plus the widest label, in display cells."""
    widest = max((cell_len(text) for text in labels), default=0)
    return widest + 1


def _nearest_free_axis_row(
    start_row: int, price_rows: int, *, step: int, occupied: frozenset[int]
) -> int | None:
    """Nearest row from ``start_row`` (moving inward by ``step``) absent from ``occupied``.

    Returns ``None`` when every row from ``start_row`` to the grid's far edge
    is occupied (e.g. a single-row grid whose only row is ``occupied``).
    """
    row = start_row
    while 0 <= row < price_rows:
        if row not in occupied:
            return row
        row += step
    return None


def _axis_suffix(label: str, width: int) -> str:
    return set_cell_size(f" {label}", width)


def _format_axis_time(timestamp: str, mode: str) -> str:
    """Compact bottom-axis time label: ``MM/DD`` for daily, ``HH:MM`` otherwise."""
    parsed = _parse_timestamp(timestamp)
    if parsed is None:
        return ""
    return parsed.strftime("%m/%d") if mode == "1d" else parsed.strftime("%H:%M")


def _time_axis_line(buckets: Sequence[Candle], mode: str) -> str:
    """Bottom time axis: up to three non-overlapping labels (left/mid/right column)."""
    columns = len(buckets)
    if columns == 0:
        return ""
    cells = [" "] * columns
    placed: list[tuple[int, int]] = []
    for index in sorted({0, (columns - 1) // 2, columns - 1}):
        text = _format_axis_time(buckets[index].timestamp, mode)
        if not text:
            continue
        if index == 0:
            start = 0
        elif index == columns - 1:
            start = max(0, columns - len(text))
        else:
            start = max(0, min(columns - len(text), index - len(text) // 2))
        end = min(columns, start + len(text))
        text = text[: end - start]
        if any(start < prev_end and end > prev_start for prev_start, prev_end in placed):
            continue
        cells[start:end] = list(text)
        placed.append((start, end))
    return "".join(cells)


def _volume_grid(buckets: Sequence[Candle], rows: int) -> list[Text]:
    lines = [Text() for _ in range(rows)]
    if not buckets or rows <= 0:
        return lines
    max_volume = max((candle.volume for candle in buckets), default=Decimal("0"))
    for candle in buckets:
        style = UP_COLOR if candle.close_price >= candle.open_price else DOWN_COLOR
        filled = 0
        if max_volume > 0 and candle.volume > 0:
            filled = max(1, min(rows, round(float(candle.volume / max_volume) * rows)))
        for row in range(rows):
            filled_from_bottom = rows - row
            char = "█" if filled_from_bottom <= filled else " "
            lines[row].append(char, style=style if char != " " else None)
    return lines


@dataclass(frozen=True, slots=True)
class HoldingAveragePriceOverlay:
    """Immutable, primitive-only holding-average overlay input for :func:`chart_renderable`.

    ``price`` must already be in the chart's own currency and ``stale`` marks
    that it comes from the last-good portfolio snapshot after a refresh
    failure -- symbol/currency/quantity matching is the caller's
    responsibility (see :mod:`toss_market_terminal.tui`), never this module's.
    """

    price: Decimal
    stale: bool = False


def _holding_average_label_ko(
    price: Decimal, currency: str | None, *, stale: bool, direction: str = ""
) -> str:
    prefix = f"{direction} " if direction else ""
    suffix = " STALE" if stale else ""
    return f"{prefix}보유 평단 {format_decimal(price, currency)}{suffix}"


def _join_lines(lines: Sequence[Text]) -> Text:
    result = Text()
    for index, line in enumerate(lines):
        if index:
            result.append("\n")
        result.append(line)
    return result


def chart_renderable(
    snapshot: MarketSnapshot,
    mode: str = "1m",
    width: int = 48,
    height: int = 18,
    *,
    current_price: Decimal | None = None,
    previous_close: Decimal | None = None,
    holding_average: HoldingAveragePriceOverlay | None = None,
) -> Text:
    """Render a candlestick + volume chart for ``mode`` as pure Rich ``Text``.

    Supported ``mode`` values: ``1m`` and ``1d`` use ``snapshot.candles`` /
    ``snapshot.daily_candles`` as-is; ``5m``, ``15m``, and ``1h`` aggregate
    ``snapshot.candles`` via :func:`toss_market_terminal.indicators.aggregate_candles`.
    Candles render chronologically, oldest on the left and newest on the
    right; when more candles exist than fit in the price columns they are
    downsampled via :func:`downsample_candles` (not close-only sampling).
    Every plain line stays within ``cell_len(line) <= width`` and the line
    count stays within ``height``; ``width``/``height`` below 1 are clamped
    up to 1. Raises ``ValueError`` for an unsupported ``mode``.

    A bounded right-side price axis shows the visible high/low plus, when
    room allows, a bold ``current_price`` line/label and a subtler
    ``previous_close`` line/label. Candles, reference lines, and axis labels
    share one price scale that includes ``current_price`` when it falls
    outside the candle high/low, so an above-high or below-low current price
    renders truthfully instead of clamping onto a boundary. A
    ``previous_close`` outside the represented range is omitted rather than
    clamped. With no ``current_price`` the latest candle's close is used
    instead, and with no ``previous_close`` (or one that lands on the same
    row as the current price) no previous-close line is drawn. A narrow
    ``width`` disables the price axis entirely rather than starve the candle
    columns. When enough rows are available, a bottom time axis labels the
    leftmost/middle/rightmost rendered candles.

    ``holding_average``, when given, never expands the price scale (unlike
    ``current_price``). When its price falls within the represented range it
    draws a dim, non-directional dashed line behind the candles plus a
    ``보유 평단 <price>`` axis label; a candle body/wick always wins that
    cell, and the current-price line/label wins any row collision with it.
    When its price falls outside the represented range no line is drawn at
    all (never clamped onto a boundary row) and the axis instead shows a
    truthful ``↑ 보유 평단 <price>`` / ``↓ 보유 평단 <price>`` indicator at
    the top/bottom of the axis. A stale (last-good) overlay appends
    `` STALE`` to whichever label is shown.
    """
    source = select_chart_candles(snapshot, mode)
    chart_width = max(_MIN_CHART_DIMENSION, int(width))
    chart_height = max(_MIN_CHART_DIMENSION, int(height))
    currency = snapshot.price.currency
    label = CHART_MODE_LABELS[mode]
    chronological = tuple(reversed(source))

    lines: list[Text] = [Text(set_cell_size(f"PRICE · {label}", chart_width), style="bold #c9d1d9")]

    if not chronological:
        lines.append(Text(set_cell_size("데이터 없음", chart_width), style=MUTED_COLOR))
        return _join_lines(lines[:chart_height])

    resolved_current_price = (
        current_price if current_price is not None else chronological[-1].close_price
    )
    low, high, span = _price_bounds(chronological)
    if current_price is not None:
        if current_price > high:
            high = current_price
        elif current_price < low:
            low = current_price
        span = max(high - low, Decimal("1"))

    holding_average_in_range = holding_average is not None and low <= holding_average.price <= high
    holding_average_label: str | None = None
    if holding_average is not None:
        if holding_average_in_range:
            holding_average_label = _holding_average_label_ko(
                holding_average.price, currency, stale=holding_average.stale
            )
        else:
            direction = "↑" if holding_average.price > high else "↓"
            holding_average_label = _holding_average_label_ko(
                holding_average.price, currency, stale=holding_average.stale, direction=direction
            )

    axis_labels = [
        format_decimal(high, currency),
        format_decimal(low, currency),
        format_decimal(resolved_current_price, currency),
    ]
    if previous_close is not None:
        axis_labels.append(format_decimal(previous_close, currency))
    if holding_average_label is not None:
        axis_labels.append(holding_average_label)
    axis_width = _axis_label_width(axis_labels)
    show_axis = chart_width >= _MIN_WIDTH_FOR_PRICE_AXIS and axis_width < chart_width - 3
    candle_width = chart_width - axis_width if show_axis else chart_width

    buckets = downsample_candles(chronological, candle_width)

    remaining = max(0, chart_height - 1)
    price_rows = 0
    volume_rows = 0
    include_chrome = False
    if remaining > _CHART_CHROME_LINES + 1:
        body = remaining - _CHART_CHROME_LINES
        price_rows = max(1, min(body - 1, round(body * _PRICE_ROW_RATIO)))
        volume_rows = body - price_rows
        include_chrome = True
    elif remaining > 0:
        price_rows = remaining

    current_price_row = (
        _price_row(resolved_current_price, low, span, price_rows) if price_rows else None
    )
    previous_close_row = None
    if price_rows and previous_close is not None and low <= previous_close <= high:
        candidate_row = _price_row(previous_close, low, span, price_rows)
        if candidate_row != current_price_row:
            previous_close_row = candidate_row

    holding_average_row = None
    if price_rows and holding_average_in_range:
        candidate_row = _price_row(holding_average.price, low, span, price_rows)
        if candidate_row != current_price_row:
            holding_average_row = candidate_row

    price_grid = _candlestick_grid(
        buckets,
        price_rows,
        scale=(low, high, span),
        current_price_row=current_price_row,
        previous_close_row=previous_close_row,
        holding_average_row=holding_average_row,
    )
    if show_axis and price_rows:
        axis_rows: dict[int, tuple[str, str]] = {
            0: (format_decimal(high, currency), MUTED_COLOR),
            price_rows - 1: (format_decimal(low, currency), MUTED_COLOR),
        }
        if previous_close_row is not None and previous_close is not None:
            axis_rows[previous_close_row] = (
                format_decimal(previous_close, currency),
                MUTED_COLOR,
            )
        if holding_average_label is not None:
            if holding_average_row is not None:
                axis_rows[holding_average_row] = (holding_average_label, HOLDING_AVERAGE_COLOR)
            elif not holding_average_in_range:
                above_high = holding_average.price > high
                boundary_row = 0 if above_high else price_rows - 1
                opposite_boundary_row = price_rows - 1 if above_high else 0
                step = 1 if above_high else -1
                # The out-of-range arrow indicator is anchored to a boundary
                # row, not a real price, so it must yield its row rather than
                # silently vanish when that boundary happens to coincide with
                # the (visually dominant) current-price row: walk inward to
                # the nearest free row, preferring one that also avoids the
                # previous-close line. The opposite boundary's true high/low
                # label is always off-limits too -- overwriting it would show
                # a directionally misleading arrow in place of a real price.
                # Give up (omit the arrow) once every row from the boundary
                # inward is claimed by the current price or that far edge.
                mandatory_occupied = frozenset(
                    row for row in (current_price_row, opposite_boundary_row) if row is not None
                )
                preferred_occupied = mandatory_occupied | (
                    frozenset({previous_close_row})
                    if previous_close_row is not None
                    else frozenset()
                )
                placement_row = _nearest_free_axis_row(
                    boundary_row, price_rows, step=step, occupied=preferred_occupied
                )
                if placement_row is None:
                    placement_row = _nearest_free_axis_row(
                        boundary_row, price_rows, step=step, occupied=mandatory_occupied
                    )
                if placement_row is not None:
                    axis_rows[placement_row] = (holding_average_label, HOLDING_AVERAGE_COLOR)
        if current_price_row is not None:
            axis_rows[current_price_row] = (
                format_decimal(resolved_current_price, currency),
                CURRENT_PRICE_COLOR,
            )
        for row_index, row_text in enumerate(price_grid):
            row_label = axis_rows.get(row_index)
            if row_label is not None:
                suffix_text, suffix_style = row_label
                row_text.append(_axis_suffix(suffix_text, axis_width), style=suffix_style)
            else:
                row_text.append(" " * axis_width)
    lines.extend(price_grid)

    if include_chrome:
        lines.append(Text())
        lines.append(Text(set_cell_size("VOLUME", chart_width), style="bold #c9d1d9"))
        volume_grid = _volume_grid(buckets, volume_rows)
        if show_axis:
            for row_text in volume_grid:
                row_text.append(" " * axis_width)
        lines.extend(volume_grid)
        time_axis = _time_axis_line(buckets, mode)
        lines.append(Text(set_cell_size(time_axis, chart_width), style=MUTED_COLOR))
    return _join_lines(lines)


def snapshot_renderable(snapshot: MarketSnapshot) -> Group:
    metrics = market_metrics(snapshot)
    price_style = direction_style(metrics.change)
    header = Text()
    header.append("TOSS MARKET  ", style="bold white")
    header.append("READ ONLY", style=MUTED_COLOR)
    header.append(f"\n{snapshot.stock.symbol}  ", style="bold white")
    header.append(f"{snapshot.stock.name} · {snapshot.stock.market}\n", style=MUTED_COLOR)
    header.append(
        f"{format_decimal(snapshot.price.last_price, snapshot.price.currency)} "
        f"{snapshot.price.currency}",
        style=price_style,
    )
    if metrics.change is not None and metrics.change_percent is not None:
        header.append(
            f"   {format_signed(metrics.change, snapshot.price.currency)}  "
            f"{format_percent(metrics.change_percent)}",
            style=price_style,
        )

    layout = Table.grid(expand=True)
    layout.add_column(ratio=34)
    layout.add_column(ratio=40)
    layout.add_column(ratio=26)
    layout.add_row(
        Panel(orderbook_table(snapshot.orderbook, snapshot.price.last_price), title="ORDER BOOK"),
        Panel(chart_renderable(snapshot), title="CHART"),
        Panel(trades_table(snapshot.trades), title="LIVE TRADES"),
    )
    return Group(Panel(header, border_style="#30363d"), layout)
