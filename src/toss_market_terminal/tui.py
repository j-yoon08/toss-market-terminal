from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from .client import TossMarketClient
from .config import Credentials
from .models import MarketSnapshot, Orderbook, Trade
from .render import format_decimal, sparkline
from .stream import OrderbookEvent, StreamStatus, TossMarketStream, TradeEvent, infer_market


class TossMarketApp(App[None]):
    TITLE = "Toss Market Terminal"
    SUB_TITLE = "READ ONLY"
    BINDINGS: ClassVar = [("q", "quit", "종료"), ("r", "refresh", "REST 재동기화")]
    CSS = """
    Screen { background: #0b0f14; color: #e6edf3; }
    #summary { height: 5; border: solid #30363d; padding: 0 2; }
    #status { height: 3; border: solid #30363d; padding: 0 1; color: #8b949e; }
    #main { height: 1fr; }
    #orderbook, #trades { width: 1fr; border: solid #30363d; }
    #chart { height: 5; border: solid #30363d; padding: 0 2; }
    DataTable > .datatable--header { background: #161b22; color: #f0f6fc; }
    """

    def __init__(self, symbol: str, credentials_path: Path) -> None:
        super().__init__()
        self.symbol = symbol
        self.market = infer_market(symbol)
        self.credentials_path = credentials_path
        self.client: TossMarketClient | None = None
        self.feed_task: asyncio.Task[None] | None = None
        self.trades: deque[Trade] = deque(maxlen=50)
        self.orderbook: Orderbook | None = None
        self.snapshot: MarketSnapshot | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("초기 데이터 조회 중…", id="summary")
        yield Static("연결 준비 중", id="status")
        with Horizontal(id="main"):
            yield DataTable(id="orderbook", zebra_stripes=True)
            yield DataTable(id="trades", zebra_stripes=True)
        yield Static("1분봉 데이터 준비 중…", id="chart")
        yield Footer()

    async def on_mount(self) -> None:
        orderbook = self.query_one("#orderbook", DataTable)
        orderbook.add_columns("구분", "가격", "잔량")
        trades = self.query_one("#trades", DataTable)
        trades.add_columns("시각", "가격", "수량")
        credentials = Credentials.load(self.credentials_path)
        self.client = TossMarketClient(credentials)
        await self._refresh_snapshot()
        self.feed_task = asyncio.create_task(self._run_feed())

    async def on_unmount(self) -> None:
        if self.feed_task:
            self.feed_task.cancel()
            await asyncio.gather(self.feed_task, return_exceptions=True)
        if self.client:
            await self.client.close()

    async def action_refresh(self) -> None:
        await self._refresh_snapshot()

    async def _refresh_snapshot(self) -> None:
        if self.client is None:
            return
        self.query_one("#status", Static).update("REST 스냅샷 동기화 중…")
        try:
            snapshot = await self.client.snapshot(self.symbol)
        except Exception as exc:
            self.query_one("#status", Static).update(f"스냅샷 실패: {exc}")
            return
        self.snapshot = snapshot
        self.orderbook = snapshot.orderbook
        self.trades = deque(snapshot.trades, maxlen=50)
        self._render_all()

    async def _run_feed(self) -> None:
        if self.client is None:
            return
        stream = TossMarketStream(self.client)
        async for event in stream.events(self.symbol, self.market):
            if isinstance(event, StreamStatus):
                self.query_one("#status", Static).update(
                    f"WebSocket: {event.state} {event.detail}".rstrip()
                )
                if event.state == "subscribed":
                    await self._refresh_snapshot()
            elif isinstance(event, TradeEvent):
                self.trades.appendleft(event.trade)
                self._render_trades()
                self._render_summary(event.trade.price, event.trade.currency, event.trade.timestamp)
            elif isinstance(event, OrderbookEvent):
                self.orderbook = event.orderbook
                self._render_orderbook()

    def _render_all(self) -> None:
        if self.snapshot is None:
            return
        self._render_summary(
            self.snapshot.price.last_price,
            self.snapshot.price.currency,
            self.snapshot.price.timestamp,
        )
        self._render_orderbook()
        self._render_trades()
        closes = [c.close_price for c in reversed(self.snapshot.candles)]
        self.query_one("#chart", Static).update(f"1분봉 종가\n{sparkline(closes)}")

    def _render_summary(self, price: Decimal, currency: str, timestamp: str | None) -> None:
        name = self.snapshot.stock.name if self.snapshot else self.symbol
        market = self.snapshot.stock.market if self.snapshot else self.market.upper()
        formatted = format_decimal(price, currency)
        self.query_one("#summary", Static).update(
            f"[b]{self.symbol}[/b]  {name}  {market}\n"
            f"[b green]{formatted} {currency}[/b green]\n"
            f"시세 시각: {timestamp or '미제공'}"
        )

    def _render_orderbook(self) -> None:
        if self.orderbook is None:
            return
        table = self.query_one("#orderbook", DataTable)
        table.clear()
        for entry in reversed(self.orderbook.asks[:8]):
            table.add_row(
                "매도",
                format_decimal(entry.price, self.orderbook.currency),
                format_decimal(entry.volume),
            )
        for entry in self.orderbook.bids[:8]:
            table.add_row(
                "매수",
                format_decimal(entry.price, self.orderbook.currency),
                format_decimal(entry.volume),
            )

    def _render_trades(self) -> None:
        table = self.query_one("#trades", DataTable)
        table.clear()
        for trade in list(self.trades)[:18]:
            table.add_row(
                trade.timestamp.split("T")[-1][:12],
                format_decimal(trade.price, trade.currency),
                format_decimal(trade.volume),
            )
