from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Static

from .client import TossApiError, TossMarketClient
from .config import CredentialError, Credentials
from .models import DataShapeError, MarketSnapshot, Orderbook, Trade
from .render import (
    DOWN_COLOR,
    MUTED_COLOR,
    UP_COLOR,
    chart_renderable,
    direction_style,
    format_decimal,
    format_percent,
    format_signed,
    market_metrics,
    volume_bar,
)
from .stream import OrderbookEvent, StreamStatus, TossMarketStream, TradeEvent, infer_market

KST = ZoneInfo("Asia/Seoul")


def safe_status_error(exc: Exception) -> str:
    if isinstance(exc, (TossApiError, DataShapeError)):
        return str(exc)[:120]
    return f"{type(exc).__name__}: REST snapshot failed"


class TossMarketApp(App[int]):
    TITLE = "Toss Market Terminal"
    SUB_TITLE = "READ ONLY"
    BINDINGS: ClassVar = [
        ("q", "quit", "종료"),
        ("r", "refresh", "재동기화"),
        ("1", "intraday", "1분봉"),
        ("d", "daily", "일봉"),
    ]
    CSS = """
    Screen {
        background: #090d12;
        color: #d9e1e8;
    }
    #topbar {
        height: 3;
        padding: 1 2 0 2;
        background: #0d131a;
        color: #c9d1d9;
    }
    #summary {
        height: 6;
        margin: 0 1;
        padding: 0 2;
        border: solid #2a3440;
        background: #0b1118;
    }
    #main {
        height: 1fr;
        margin: 0 1;
        layout: horizontal;
    }
    .market-panel {
        height: 1fr;
        border: solid #2a3440;
        background: #0b1118;
    }
    #orderbook-panel { width: 34%; }
    #chart-panel { width: 40%; }
    #trades-panel { width: 26%; }
    .panel-title {
        height: 2;
        padding: 0 1;
        background: #111923;
        color: #aab7c4;
        text-style: bold;
    }
    DataTable {
        height: 1fr;
        background: #0b1118;
        color: #d9e1e8;
        scrollbar-background: #0b1118;
        scrollbar-color: #3a4654;
        scrollbar-color-hover: #4b5a69;
        scrollbar-color-active: #607080;
    }
    DataTable > .datatable--header {
        background: #0e151d;
        color: #7d8998;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #17212c;
        color: #f0f6fc;
    }
    #chart-content {
        height: 1fr;
        padding: 1 2;
    }
    #market-stats {
        height: 7;
        padding: 0 2;
        border-top: solid #2a3440;
        color: #9aa7b4;
    }
    #statusbar {
        height: 3;
        padding: 1 2 0 2;
        background: #0d131a;
        color: #7d8998;
    }
    Footer {
        height: 1;
        background: #111923;
    }
    Screen.compact #chart-panel { display: none; }
    Screen.compact #orderbook-panel { width: 52%; }
    Screen.compact #trades-panel { width: 48%; }
    Screen.compact #summary { height: 7; }
    """

    def __init__(
        self,
        symbol: str,
        credentials_path: Path,
        *,
        initial_snapshot: MarketSnapshot | None = None,
        connect_live: bool = True,
    ) -> None:
        super().__init__()
        self.symbol = symbol
        self.market = infer_market(symbol)
        self.credentials_path = credentials_path
        self.initial_snapshot = initial_snapshot
        self.connect_live = connect_live
        self.refresh_lock = asyncio.Lock()
        self.client: TossMarketClient | None = None
        self.feed_task: asyncio.Task[None] | None = None
        self.trades: deque[Trade] = deque(maxlen=50)
        self.orderbook: Orderbook | None = None
        self.snapshot: MarketSnapshot | None = None
        self.current_price: Decimal | None = None
        self.current_currency = ""
        self.current_timestamp: str | None = None
        self.chart_mode = "1m"
        self.connection_state = "STARTING"
        self.connection_detail = ""
        self.stream_live = False
        self.subscription_detail = ""
        self.protocol_degraded = False
        self.last_tick_monotonic: float | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="topbar", markup=False)
        yield Static("초기 데이터 조회 중…", id="summary", markup=False)
        with Horizontal(id="main"):
            with Vertical(id="orderbook-panel", classes="market-panel"):
                yield Static("ORDER BOOK", classes="panel-title")
                yield DataTable(
                    id="orderbook", zebra_stripes=False, show_cursor=False, cell_padding=1
                )
            with Vertical(id="chart-panel", classes="market-panel"):
                yield Static("MARKET CHART", classes="panel-title")
                yield Static(id="chart-content", markup=False)
                yield Static(id="market-stats", markup=False)
            with Vertical(id="trades-panel", classes="market-panel"):
                yield Static("LIVE TRADES", classes="panel-title")
                yield DataTable(id="trades", zebra_stripes=False, show_cursor=False, cell_padding=1)
        yield Static(id="statusbar", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self._prepare_tables()
        self.set_interval(1.0, self._render_chrome)
        if self.initial_snapshot is not None:
            self._apply_snapshot(self.initial_snapshot)
            self.connection_state = "PREVIEW" if not self.connect_live else "SNAPSHOT"
            self._render_all()
            if not self.connect_live:
                return

        try:
            credentials = Credentials.load(self.credentials_path)
        except CredentialError as exc:
            self.exit(2, message=f"오류: {exc}")
            return
        self.client = TossMarketClient(credentials)
        await self._refresh_snapshot()
        self.feed_task = asyncio.create_task(self._run_feed())

    async def on_unmount(self) -> None:
        if self.feed_task:
            self.feed_task.cancel()
            await asyncio.gather(self.feed_task, return_exceptions=True)
        if self.client:
            await self.client.close()

    def on_resize(self, event: events.Resize) -> None:
        self.screen.set_class(event.size.width < 117, "compact")

    async def action_refresh(self) -> None:
        await self._refresh_snapshot()

    def action_intraday(self) -> None:
        self.chart_mode = "1m"
        self._render_chart()

    def action_daily(self) -> None:
        self.chart_mode = "1d"
        self._render_chart()

    def _prepare_tables(self) -> None:
        orderbook = self.query_one("#orderbook", DataTable)
        orderbook.add_columns("SIDE", "PRICE", "SIZE", "DEPTH")
        trades = self.query_one("#trades", DataTable)
        trades.add_columns("TIME", "PRICE", "SIZE", "")

    async def _refresh_snapshot(self) -> bool:
        async with self.refresh_lock:
            return await self._refresh_snapshot_locked()

    async def _refresh_snapshot_locked(self) -> bool:
        if self.client is None:
            return False
        was_live = self.stream_live
        previous_detail = self.connection_detail
        self.connection_state = "SYNCING"
        self.connection_detail = "REST snapshot"
        self._render_chrome()
        try:
            snapshot = await self.client.snapshot(self.symbol)
        except Exception as exc:
            if was_live:
                self.protocol_degraded = False
                self.connection_state = "DEGRADED"
                self.connection_detail = "WS live · REST sync failed"
            else:
                self.connection_state = "ERROR"
                self.connection_detail = safe_status_error(exc)
            self._render_chrome()
            return False
        self._apply_snapshot(snapshot)
        if was_live:
            self.connection_state = "LIVE"
            self.connection_detail = self.subscription_detail or previous_detail
            self.protocol_degraded = False
        self._render_all()
        return True

    def _apply_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.snapshot = snapshot
        self.orderbook = snapshot.orderbook
        self.trades = deque(snapshot.trades, maxlen=50)
        self.current_price = snapshot.price.last_price
        self.current_currency = snapshot.price.currency
        self.current_timestamp = snapshot.price.timestamp

    async def _run_feed(self) -> None:
        if self.client is None:
            return
        stream = TossMarketStream(self.client)
        async for event in stream.events(self.symbol, self.market):
            if isinstance(event, StreamStatus):
                await self._handle_stream_status(event)
            elif isinstance(event, TradeEvent):
                self._recover_protocol_status()
                self.trades.appendleft(event.trade)
                self.current_price = event.trade.price
                self.current_currency = event.trade.currency
                self.current_timestamp = event.trade.timestamp
                self.last_tick_monotonic = time.monotonic()
                self._render_trades()
                self._render_summary()
                self._render_stats()
                self._render_chrome()
            elif isinstance(event, OrderbookEvent):
                self._recover_protocol_status()
                self.orderbook = event.orderbook
                self.last_tick_monotonic = time.monotonic()
                self._render_orderbook()
                self._render_stats()
                self._render_chrome()

    async def _handle_stream_status(self, event: StreamStatus) -> None:
        if event.state == "pong":
            return
        if event.state in {"connecting", "connected", "reconnecting"}:
            self.stream_live = False
            self.protocol_degraded = False
            self.connection_state = event.state.upper()
            self.connection_detail = event.detail
        elif event.state == "subscribed":
            self.stream_live = True
            self.protocol_degraded = False
            self.subscription_detail = event.detail
            self.connection_state = "SUBSCRIBED"
            self.connection_detail = event.detail
            self._render_chrome()
            synchronized = await self._refresh_snapshot()
            if synchronized:
                self.connection_state = "LIVE"
                self.connection_detail = event.detail
            else:
                self.connection_state = "DEGRADED"
                self.connection_detail = "WS live · REST sync failed"
        elif event.state in {"auth_error", "error", "rejected"}:
            self.stream_live = False
            self.protocol_degraded = False
            self.connection_state = event.state.upper()
            self.connection_detail = event.detail
        elif event.state == "protocol_error" and self.stream_live:
            self.protocol_degraded = True
            self.connection_state = "DEGRADED"
            self.connection_detail = event.detail
        else:
            self.connection_state = event.state.upper()
            self.connection_detail = event.detail
        self._render_chrome()

    def _recover_protocol_status(self) -> None:
        if not self.protocol_degraded or not self.stream_live:
            return
        self.protocol_degraded = False
        self.connection_state = "LIVE"
        self.connection_detail = self.subscription_detail

    def _render_all(self) -> None:
        if self.snapshot is None:
            return
        self._render_summary()
        self._render_orderbook()
        self._render_trades()
        self._render_chart()
        self._render_stats()
        self._render_chrome()

    def _render_summary(self) -> None:
        if self.snapshot is None or self.current_price is None:
            return
        metrics = market_metrics(
            self.snapshot,
            self.current_price,
            orderbook=self.orderbook,
            trades=tuple(self.trades),
            current_timestamp=self.current_timestamp,
        )
        style = direction_style(metrics.change)
        text = Text()
        text.append(f"{self.symbol}  ", style="bold white")
        text.append(
            f"{self.snapshot.stock.name} · {self.snapshot.stock.market}  ", style=MUTED_COLOR
        )
        text.append("PUBLIC MARKET DATA\n", style="#526273")
        text.append(
            f"{format_decimal(self.current_price, self.current_currency)} {self.current_currency}",
            style=style,
        )
        if metrics.change is not None and metrics.change_percent is not None:
            text.append(
                f"   {format_signed(metrics.change, self.current_currency)}  "
                f"{format_percent(metrics.change_percent)}",
                style=style,
            )
        text.append("\n")
        if metrics.day_high is not None and metrics.day_low is not None:
            text.append(
                f"HIGH {format_decimal(metrics.day_high, self.current_currency)}   "
                f"LOW {format_decimal(metrics.day_low, self.current_currency)}   ",
                style=MUTED_COLOR,
            )
        if metrics.day_volume is not None:
            text.append(f"VOL {format_decimal(metrics.day_volume)}", style=MUTED_COLOR)
        self.query_one("#summary", Static).update(text)

    def _render_orderbook(self) -> None:
        if self.orderbook is None or self.current_price is None:
            return
        table = self.query_one("#orderbook", DataTable)
        table.clear()
        entries = list(self.orderbook.asks[:7]) + list(self.orderbook.bids[:7])
        max_volume = max((entry.volume for entry in entries), default=Decimal("0"))
        for entry in reversed(self.orderbook.asks[:7]):
            table.add_row(
                Text("ASK", style=DOWN_COLOR),
                Text(format_decimal(entry.price, self.orderbook.currency), style=DOWN_COLOR),
                format_decimal(entry.volume),
                Text(volume_bar(entry.volume, max_volume), style="#70414e"),
            )
        table.add_row(
            Text("LAST", style="bold white"),
            Text(format_decimal(self.current_price, self.orderbook.currency), style="bold white"),
            "—",
            Text("────────", style="#3a4654"),
        )
        for entry in self.orderbook.bids[:7]:
            table.add_row(
                Text("BID", style=UP_COLOR),
                Text(format_decimal(entry.price, self.orderbook.currency), style=UP_COLOR),
                format_decimal(entry.volume),
                Text(volume_bar(entry.volume, max_volume), style="#275e5e"),
            )

    def _render_trades(self) -> None:
        table = self.query_one("#trades", DataTable)
        table.clear()
        items = list(self.trades)[:18]
        for index, trade in enumerate(items):
            older_price = items[index + 1].price if index + 1 < len(items) else trade.price
            if trade.price > older_price:
                marker, style = "▲", UP_COLOR
            elif trade.price < older_price:
                marker, style = "▼", DOWN_COLOR
            else:
                marker, style = "·", MUTED_COLOR
            table.add_row(
                trade.timestamp.split("T")[-1][:12],
                Text(format_decimal(trade.price, trade.currency), style=style),
                format_decimal(trade.volume),
                Text(marker, style=style),
            )

    def _render_chart(self) -> None:
        if self.snapshot is None:
            return
        self.query_one("#chart-content", Static).update(
            chart_renderable(self.snapshot, self.chart_mode)
        )
        title = "MARKET CHART · 1 MINUTE" if self.chart_mode == "1m" else "MARKET CHART · DAILY"
        self.query_one("#chart-panel .panel-title", Static).update(title)

    def _render_stats(self) -> None:
        if self.snapshot is None or self.current_price is None:
            return
        metrics = market_metrics(
            self.snapshot,
            self.current_price,
            orderbook=self.orderbook,
            trades=tuple(self.trades),
            current_timestamp=self.current_timestamp,
        )
        text = Text()
        text.append("MARKET STATS\n", style="bold #c9d1d9")
        if metrics.spread is not None:
            text.append(
                f"SPREAD {format_decimal(metrics.spread, self.current_currency)}",
                style=MUTED_COLOR,
            )
            if metrics.spread_percent is not None:
                text.append(f" · {metrics.spread_percent:.3f}%\n", style=MUTED_COLOR)
        if metrics.recent_vwap is not None:
            text.append(
                f"RECENT VWAP {format_decimal(metrics.recent_vwap, self.current_currency)}\n",
                style=MUTED_COLOR,
            )
        text.append(f"FEED {self.market.upper()} · READ ONLY", style="#526273")
        self.query_one("#market-stats", Static).update(text)

    def _render_chrome(self) -> None:
        now = datetime.now(KST).strftime("%H:%M:%S KST")
        live = self.stream_live
        state_color = (
            "#f0ad4e"
            if self.connection_state == "DEGRADED"
            else (UP_COLOR if live else MUTED_COLOR)
        )
        top = Text()
        top.append("TOSS MARKET", style="bold white")
        top.append("   READ ONLY", style="#526273")
        top.append(f"   {'●' if live else '○'} {self.connection_state}", style=state_color)
        top.append(f"   {now}", style=MUTED_COLOR)
        self.query_one("#topbar", Static).update(top)

        tick_age = "—"
        if self.last_tick_monotonic is not None:
            tick_age = f"{max(0.0, time.monotonic() - self.last_tick_monotonic):.1f}s"
        status = Text()
        status.append(f"WS {self.connection_state}", style=state_color)
        if self.connection_detail:
            status.append(f" · {self.connection_detail}", style=MUTED_COLOR)
        status.append(f"   LAST TICK {tick_age}", style=MUTED_COLOR)
        if self.current_timestamp:
            status.append(f"   MARKET TIME {self.current_timestamp}", style="#526273")
        self.query_one("#statusbar", Static).update(status)
