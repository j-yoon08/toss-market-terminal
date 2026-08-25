from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median

from rich.cells import set_cell_size
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .indicators import aggregate_candles
from .models import Candle, MarketSnapshot, Orderbook, Trade

SPARK_CHARS = "▁▂▃▄▅▆▇█"
UP_COLOR = "#2dd4bf"
DOWN_COLOR = "#fb7185"
MUTED_COLOR = "#7d8998"

CHART_MODE_LABELS = {
    "1m": "1 MINUTE",
    "5m": "5 MINUTE",
    "15m": "15 MINUTE",
    "1h": "1 HOUR",
    "1d": "DAILY",
}
_CHART_MAX_ECHO = 32
_MIN_CHART_DIMENSION = 1
_CHART_CHROME_LINES = 3  # summary line, blank separator, "VOLUME" header
_PRICE_ROW_RATIO = 0.7


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


def _candlestick_grid(buckets: Sequence[Candle], rows: int) -> list[Text]:
    lines = [Text() for _ in range(rows)]
    if not buckets or rows <= 0:
        return lines
    low = min(candle.low_price for candle in buckets)
    high = max(candle.high_price for candle in buckets)
    span = high - low
    if span <= 0:
        span = Decimal("1")
    for candle in buckets:
        style = UP_COLOR if candle.close_price >= candle.open_price else DOWN_COLOR
        top_row = _price_row(candle.high_price, low, span, rows)
        bottom_row = _price_row(candle.low_price, low, span, rows)
        body_top_row = _price_row(max(candle.open_price, candle.close_price), low, span, rows)
        body_bottom_row = _price_row(min(candle.open_price, candle.close_price), low, span, rows)
        for row in range(rows):
            if body_top_row <= row <= body_bottom_row:
                char = "█"
            elif top_row <= row <= bottom_row:
                char = "│"
            else:
                char = " "
            lines[row].append(char, style=style if char != " " else None)
    return lines


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
) -> Text:
    """Render a candlestick + volume chart for ``mode`` as pure Rich ``Text``.

    Supported ``mode`` values: ``1m`` and ``1d`` use ``snapshot.candles`` /
    ``snapshot.daily_candles`` as-is; ``5m``, ``15m``, and ``1h`` aggregate
    ``snapshot.candles`` via :func:`toss_market_terminal.indicators.aggregate_candles`.
    Candles render chronologically, oldest on the left and newest on the
    right; when more candles exist than fit in ``width`` columns they are
    downsampled via :func:`downsample_candles` (not close-only sampling).
    Every plain line stays within ``cell_len(line) <= width`` and the line
    count stays within ``height``; ``width``/``height`` below 1 are clamped
    up to 1. Raises ``ValueError`` for an unsupported ``mode``.
    """
    if mode not in CHART_MODE_LABELS:
        raise ValueError(
            f"지원하지 않는 차트 모드입니다: {_chart_echo(mode)} "
            f"(지원: {', '.join(CHART_MODE_LABELS)})"
        )
    chart_width = max(_MIN_CHART_DIMENSION, int(width))
    chart_height = max(_MIN_CHART_DIMENSION, int(height))
    currency = snapshot.price.currency
    label = CHART_MODE_LABELS[mode]

    if mode == "1d":
        source: Sequence[Candle] = snapshot.daily_candles
    elif mode == "1m":
        source = snapshot.candles
    else:
        source = aggregate_candles(snapshot.candles, mode)
    chronological = tuple(reversed(source))

    lines: list[Text] = [Text(set_cell_size(f"PRICE · {label}", chart_width), style="bold #c9d1d9")]

    if not chronological:
        lines.append(Text(set_cell_size("데이터 없음", chart_width), style=MUTED_COLOR))
        return _join_lines(lines[:chart_height])

    buckets = downsample_candles(chronological, chart_width)

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

    lines.extend(_candlestick_grid(buckets, price_rows))
    if include_chrome:
        low_price = min(candle.low_price for candle in buckets)
        high_price = max(candle.high_price for candle in buckets)
        summary = set_cell_size(
            f"LOW {format_decimal(low_price, currency)}   "
            f"HIGH {format_decimal(high_price, currency)}",
            chart_width,
        )
        lines.append(Text(summary, style=MUTED_COLOR))
        lines.append(Text())
        lines.append(Text(set_cell_size("VOLUME", chart_width), style="bold #c9d1d9"))
        lines.extend(_volume_grid(buckets, volume_rows))
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
