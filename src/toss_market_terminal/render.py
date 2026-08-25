from __future__ import annotations

from decimal import Decimal

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import MarketSnapshot, Orderbook, Trade

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def format_decimal(value: Decimal, currency: str | None = None) -> str:
    if currency == "KRW":
        return f"{value:,.0f}"
    exponent = -value.as_tuple().exponent
    places = min(max(exponent, 2), 6)
    return f"{value:,.{places}f}".rstrip("0").rstrip(".")


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


def orderbook_table(orderbook: Orderbook, depth: int = 8) -> Table:
    table = Table(title="호가", expand=True)
    table.add_column("구분", justify="center")
    table.add_column("가격", justify="right")
    table.add_column("잔량", justify="right")
    for entry in reversed(orderbook.asks[:depth]):
        table.add_row(
            "매도",
            f"[red]{format_decimal(entry.price, orderbook.currency)}[/]",
            format_decimal(entry.volume),
        )
    for entry in orderbook.bids[:depth]:
        table.add_row(
            "매수",
            f"[cyan]{format_decimal(entry.price, orderbook.currency)}[/]",
            format_decimal(entry.volume),
        )
    return table


def trades_table(trades: tuple[Trade, ...] | list[Trade], limit: int = 15) -> Table:
    table = Table(title="최근 체결", expand=True)
    table.add_column("시각")
    table.add_column("가격", justify="right")
    table.add_column("수량", justify="right")
    previous: Decimal | None = None
    for trade in list(trades)[:limit]:
        color = "white"
        if previous is not None:
            color = "green" if trade.price >= previous else "red"
        timestamp = trade.timestamp.split("T")[-1][:12]
        table.add_row(
            timestamp,
            f"[{color}]{format_decimal(trade.price, trade.currency)}[/]",
            format_decimal(trade.volume),
        )
        previous = trade.price
    return table


def snapshot_renderable(snapshot: MarketSnapshot) -> Group:
    title = Text()
    title.append(f"{snapshot.stock.symbol} ", style="bold")
    title.append(snapshot.stock.name)
    title.append(f"  {snapshot.stock.market}  ")
    price_text = format_decimal(snapshot.price.last_price, snapshot.price.currency)
    title.append(
        f"{price_text} {snapshot.price.currency}",
        style="bold green",
    )
    title.append(f"\n시세 시각: {snapshot.price.timestamp or '미제공'}", style="dim")

    layout = Table.grid(expand=True)
    layout.add_column(ratio=1)
    layout.add_column(ratio=1)
    layout.add_row(orderbook_table(snapshot.orderbook), trades_table(snapshot.trades))

    closes = [candle.close_price for candle in reversed(snapshot.candles)]
    chart = Panel(sparkline(closes), title="1분봉 종가 흐름")
    return Group(Panel(title, title="Toss Market Terminal · READ ONLY"), layout, chart)
