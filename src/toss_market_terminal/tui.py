from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Static

from .alerts import AlertEvaluator, AlertEvent
from .client import TossApiError, TossMarketClient
from .config import CredentialError, Credentials
from .live_chart import apply_trade_to_candles
from .models import Candle, DataShapeError, MarketSnapshot, Orderbook, Trade
from .render import (
    CHART_MODE_LABELS,
    DOWN_COLOR,
    MUTED_COLOR,
    TIMEFRAME_LABELS_KO,
    UP_COLOR,
    ChartIndicatorBase,
    ChartIndicators,
    chart_indicator_base,
    chart_indicators_from_base,
    chart_renderable,
    direction_style,
    ema_relation_label_ko,
    format_decimal,
    format_multiple,
    format_percent,
    format_signed,
    format_trade_time,
    level_display_ko,
    market_metrics,
    market_signals,
    orderbook_signal_label_ko,
    rsi_zone_label_ko,
    trade_pressure_label_ko,
    volume_bar,
    vwap_distance_percent,
)
from .settings import Settings, SettingsStore
from .stream import (
    OrderbookEvent,
    StreamStatus,
    TossMarketStream,
    TradeEvent,
    infer_market,
    normalize_symbol,
)

KST = ZoneInfo("Asia/Seoul")
WATCHLIST_REFRESH_SECONDS = 15.0
CANDLE_RESYNC_SECONDS = 30.0
CHART_RENDER_INTERVAL_SECONDS = 0.25
COMPACT_WIDTH_THRESHOLD = 117
LiveCandleStatus = Literal["updated", "late", "unavailable"]


def safe_status_error(exc: Exception) -> str:
    if isinstance(exc, (TossApiError, DataShapeError)):
        return str(exc)[:120]
    return f"{type(exc).__name__}: REST snapshot failed"


def safe_indicator_error(exc: Exception) -> str:
    """Bounded, non-secret description of a chart-indicator failure.

    Never echoes the raw exception text (which may embed candle timestamps
    or currency codes) back to the UI; only the exception type name is shown.
    """
    return f"{type(exc).__name__}: 지표 계산 실패"


@dataclass(frozen=True, slots=True)
class WatchlistRow:
    symbol: str
    price: str
    currency: str
    active_alerts: int


class TossMarketApp(App[int]):
    TITLE = "Toss Market Terminal"
    SUB_TITLE = "READ ONLY"
    BINDINGS: ClassVar = [
        ("q", "quit", "종료"),
        ("r", "refresh", "재동기화"),
        ("1", "intraday", "1분봉"),
        ("2", "chart_5m", "5분봉"),
        ("3", "chart_15m", "15분봉"),
        ("4", "chart_1h", "1시간봉"),
        ("5", "daily", "일봉"),
        ("c", "toggle_focus", "차트 포커스"),
        ("up", "watch_up", "위 항목"),
        ("down", "watch_down", "아래 항목"),
        ("j", "watch_down", "아래 항목(j)"),
        ("k", "watch_up", "위 항목(k)"),
        ("enter", "watch_select", "심볼 전환"),
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
    #watchlist-panel { width: 18%; }
    #orderbook-panel { width: 28%; }
    #chart-panel { width: 33%; }
    #trades-panel { width: 21%; }
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
        height: 12;
        padding: 0 2;
        border-top: solid #2a3440;
        color: #9aa7b4;
    }
    #statusbar {
        height: 4;
        padding: 1 2 0 2;
        background: #0d131a;
        color: #7d8998;
    }
    Footer {
        height: 1;
        background: #111923;
    }
    Screen.compact #chart-panel { display: none; }
    Screen.compact #watchlist-panel { display: none; }
    Screen.compact #orderbook-panel { width: 52%; }
    Screen.compact #trades-panel { width: 48%; }
    Screen.compact #summary { height: 7; }
    Screen.chart-focus #watchlist-panel { display: none; }
    Screen.chart-focus #chart-panel { width: 65%; }
    Screen.chart-focus #orderbook-panel { width: 18%; }
    Screen.chart-focus #trades-panel { width: 17%; }
    """

    def __init__(
        self,
        symbol: str,
        credentials_path: Path,
        *,
        initial_snapshot: MarketSnapshot | None = None,
        connect_live: bool = True,
        settings_path: Path | None = None,
        settings: Settings | None = None,
        watchlist_refresh_seconds: float = WATCHLIST_REFRESH_SECONDS,
        candle_resync_seconds: float = CANDLE_RESYNC_SECONDS,
        chart_render_interval_seconds: float = CHART_RENDER_INTERVAL_SECONDS,
    ) -> None:
        super().__init__()
        self.symbol = normalize_symbol(symbol)
        self.market = infer_market(self.symbol)
        self.credentials_path = credentials_path
        self.initial_snapshot = initial_snapshot
        self.connect_live = connect_live
        self.settings_path = settings_path
        self.settings = settings if settings is not None else Settings()
        self.watchlist_refresh_seconds = watchlist_refresh_seconds
        if candle_resync_seconds <= 0:
            raise ValueError("캔들 재동기화 주기는 양수여야 합니다.")
        if chart_render_interval_seconds < 0:
            raise ValueError("차트 렌더 간격은 0 이상이어야 합니다.")
        self.candle_resync_seconds = candle_resync_seconds
        self.chart_render_interval_seconds = chart_render_interval_seconds
        self.refresh_lock = asyncio.Lock()
        self.switch_lock = asyncio.Lock()
        self.watchlist_refresh_lock = asyncio.Lock()
        self.client: TossMarketClient | None = None
        self.feed_task: asyncio.Task[None] | None = None
        self.watchlist_task: asyncio.Task[None] | None = None
        self.candle_sync_task: asyncio.Task[None] | None = None
        self.chart_render_task: asyncio.Task[None] | None = None
        # In-memory only: `watch SYMBOL` never persists the launch symbol.
        configured = list(dict.fromkeys(self.settings.watchlist))
        if self.symbol not in configured:
            if len(configured) >= 12:
                configured = configured[:11]
            configured.append(self.symbol)
        self.watchlist_symbols = tuple(configured)
        self.watchlist_rows: dict[str, WatchlistRow] = {}
        self.watchlist_stale = False
        self.alert_evaluator = AlertEvaluator()
        self.latest_alert: AlertEvent | None = None
        self.trades: deque[Trade] = deque(maxlen=50)
        self.orderbook: Orderbook | None = None
        self.snapshot: MarketSnapshot | None = None
        self.current_price: Decimal | None = None
        self.current_currency = ""
        self.current_timestamp: str | None = None
        self.chart_mode = "1m"
        self.chart_focus = False
        self.connection_state = "STARTING"
        self.connection_detail = ""
        self.stream_live = False
        self.subscription_detail = ""
        self.protocol_degraded = False
        self.indicator_degraded = False
        self.candle_sync_degraded = False
        self.last_tick_monotonic: float | None = None
        self._live_candles: tuple[Candle, ...] = ()
        self._live_daily_candles: tuple[Candle, ...] = ()
        self._live_candle_revision = 0
        self._live_trade_buffer: deque[tuple[int, Trade]] = deque(maxlen=2_000)
        self._last_chart_render_monotonic: float | None = None
        self._indicator_base: ChartIndicatorBase | None = None
        self._indicator_base_snapshot: MarketSnapshot | None = None
        self._indicator_base_mode: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="topbar", markup=False)
        yield Static("초기 데이터 조회 중…", id="summary", markup=False)
        with Horizontal(id="main"):
            with Vertical(id="watchlist-panel", classes="market-panel"):
                yield Static("WATCHLIST", classes="panel-title")
                yield DataTable(id="watchlist", cursor_type="row", cell_padding=0)
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
                yield DataTable(id="trades", zebra_stripes=False, show_cursor=False, cell_padding=0)
        yield Static(id="statusbar", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self._prepare_tables()
        self.set_interval(1.0, self._render_chrome)
        if self.initial_snapshot is not None:
            self._apply_snapshot(self.initial_snapshot)
            self._refresh_watchlist_rows_from_snapshot()
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
        await self._load_persisted_watchlist()
        await self._refresh_snapshot()
        self.feed_task = asyncio.create_task(self._run_feed())
        self.watchlist_task = asyncio.create_task(self._run_watchlist_polling())
        self.candle_sync_task = asyncio.create_task(self._run_candle_resync())

    async def on_unmount(self) -> None:
        for task in (
            self.chart_render_task,
            self.candle_sync_task,
            self.watchlist_task,
            self.feed_task,
        ):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.client:
            await self.client.close()

    def on_resize(self, event: events.Resize) -> None:
        compact = event.size.width < COMPACT_WIDTH_THRESHOLD
        self.screen.set_class(compact, "compact")
        if compact:
            self.chart_focus = False
        self.screen.set_class(self.chart_focus, "chart-focus")
        self.call_after_refresh(self._render_chart)

    async def action_refresh(self) -> None:
        await self._refresh_snapshot()

    def _set_chart_mode(self, mode: str) -> None:
        self.chart_mode = mode
        self._render_chart()
        self._render_stats()

    def action_intraday(self) -> None:
        self._set_chart_mode("1m")

    def action_chart_5m(self) -> None:
        self._set_chart_mode("5m")

    def action_chart_15m(self) -> None:
        self._set_chart_mode("15m")

    def action_chart_1h(self) -> None:
        self._set_chart_mode("1h")

    def action_daily(self) -> None:
        self._set_chart_mode("1d")

    def action_toggle_focus(self) -> None:
        if self.screen.has_class("compact"):
            return
        self.chart_focus = not self.chart_focus
        self.screen.set_class(self.chart_focus, "chart-focus")
        self.call_after_refresh(self._render_chart)

    def _prepare_tables(self) -> None:
        watchlist = self.query_one("#watchlist", DataTable)
        watchlist.add_column("SYMBOL", width=7)
        watchlist.add_column("PRICE", width=12)
        watchlist.add_column("A", width=2)
        orderbook = self.query_one("#orderbook", DataTable)
        orderbook.add_columns("SIDE", "PRICE", "SIZE", "DEPTH")
        trades = self.query_one("#trades", DataTable)
        trades.add_column("TIME", width=9)
        trades.add_column("PRICE", width=10)
        trades.add_column("SIZE", width=7)
        trades.add_column("", width=1)

    async def _load_persisted_watchlist(self) -> None:
        """Merge the persisted watchlist into memory without persisting anything."""
        if self.settings_path is None:
            return
        try:
            persisted = SettingsStore(self.settings_path).load()
            merged = list(persisted.watchlist)
            if self.symbol not in merged:
                if len(merged) >= 12:
                    merged = merged[:11]
                merged.append(self.symbol)
            self.watchlist_symbols = tuple(merged)
            self.settings = persisted
        except Exception:
            self.watchlist_stale = True

    async def _run_watchlist_polling(self) -> None:
        while True:
            await asyncio.sleep(self.watchlist_refresh_seconds)
            try:
                await self._refresh_watchlist_prices()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.watchlist_stale = True
                self._render_watchlist()

    async def _refresh_watchlist_prices(self) -> bool:
        if self.client is None or not self.watchlist_symbols:
            return False
        if self.watchlist_refresh_lock.locked():
            return False
        async with self.watchlist_refresh_lock:
            try:
                prices = await self.client.prices(list(self.watchlist_symbols))
            except Exception:
                self.watchlist_stale = True
                self._render_watchlist()
                return False
            for symbol, price in prices.items():
                active_alerts = sum(
                    1 for rule in self.settings.alerts if rule.symbol == symbol and rule.enabled
                )
                self.watchlist_rows[symbol] = WatchlistRow(
                    symbol=symbol,
                    price=format_decimal(price.last_price),
                    currency=price.currency,
                    active_alerts=active_alerts,
                )
                if symbol != self.symbol:
                    self._evaluate_alerts(
                        symbol=symbol,
                        current_price=price.last_price,
                        timestamp=price.timestamp,
                    )
            self.watchlist_stale = False
            self._render_watchlist()
            return True

    def _refresh_watchlist_rows_from_snapshot(self) -> None:
        if self.snapshot is None or self.current_price is None:
            return
        active_alerts = sum(
            1 for rule in self.settings.alerts if rule.symbol == self.symbol and rule.enabled
        )
        self.watchlist_rows[self.symbol] = WatchlistRow(
            symbol=self.symbol,
            price=format_decimal(self.current_price),
            currency=self.current_currency or self.snapshot.price.currency,
            active_alerts=active_alerts,
        )

    def _render_watchlist(self) -> None:
        table = self.query_one("#watchlist", DataTable)
        table.clear()
        for symbol in self.watchlist_symbols:
            row = self.watchlist_rows.get(symbol)
            marker = "•" if row is not None and row.active_alerts else " "
            count = str(row.active_alerts) if row is not None else "0"
            price_text = f"{row.price} {row.currency}" if row is not None else "—"
            if row is None or self.watchlist_stale:
                price_text = f"{price_text}*" if self.watchlist_stale else price_text
            table.add_row(
                symbol,
                Text(price_text, style=MUTED_COLOR),
                f"{marker}{count}",
            )

    def _evaluate_alerts(
        self,
        *,
        symbol: str,
        current_price: Decimal | None,
        timestamp: str | None,
        include_full_state: bool = False,
    ) -> tuple[AlertEvent, ...]:
        metrics = None
        signals = None
        if (
            include_full_state
            and symbol == self.symbol
            and self.snapshot is not None
            and current_price is not None
        ):
            metrics = market_metrics(
                self.snapshot,
                current_price,
                orderbook=self.orderbook,
                trades=tuple(self.trades),
                current_timestamp=timestamp,
            )
            signals = market_signals(
                self.snapshot,
                current_price,
                orderbook=self.orderbook,
                trades=tuple(self.trades),
            )
        events = self.alert_evaluator.evaluate_all(
            self.settings.alerts,
            symbol=symbol,
            current_price=current_price,
            metrics=metrics,
            signals=signals,
            timestamp=timestamp,
        )
        for event in events:
            self._emit_alert(event)
        return events

    def _evaluate_active_alerts(self) -> tuple[AlertEvent, ...]:
        return self._evaluate_alerts(
            symbol=self.symbol,
            current_price=self.current_price,
            timestamp=self.current_timestamp,
            include_full_state=True,
        )

    def _emit_alert(self, event: AlertEvent) -> None:
        self.latest_alert = event
        if self.is_mounted:
            self.notify(event.message, title="MARKET ALERT", severity="information", timeout=8)
            self.bell()
            self._render_chrome()

    # --- navigation / switching -------------------------------------------------
    @property
    def _highlighted_index(self) -> int:
        try:
            return self.watchlist_symbols.index(self.symbol)
        except ValueError:
            return 0

    def action_watch_up(self) -> None:
        if not self.watchlist_symbols:
            return
        table = self.query_one("#watchlist", DataTable)
        self._move_cursor(max(0, table.cursor_row - 1))

    def action_watch_down(self) -> None:
        if not self.watchlist_symbols:
            return
        table = self.query_one("#watchlist", DataTable)
        self._move_cursor(min(len(self.watchlist_symbols) - 1, table.cursor_row + 1))

    def _move_cursor(self, index: int) -> None:
        table = self.query_one("#watchlist", DataTable)
        if table.row_count:
            table.move_cursor(
                row=min(index, table.row_count - 1),
                column=0,
            )

    async def action_watch_select(self) -> None:
        table = self.query_one("#watchlist", DataTable)
        row_key = table.cursor_row
        if not self.watchlist_symbols or row_key >= len(self.watchlist_symbols):
            return
        target = self.watchlist_symbols[row_key]
        if target != self.symbol:
            await self.switch_symbol(target)

    async def switch_symbol(self, target: str) -> None:
        """Switch active symbol after fully stopping the previous stream."""
        normalized = normalize_symbol(target)
        async with self.switch_lock:
            if normalized == self.symbol:
                return
            old_feed = self.feed_task
            self.feed_task = None
            if old_feed is not None:
                old_feed.cancel()
                await asyncio.gather(old_feed, return_exceptions=True)
            old_chart_render = self.chart_render_task
            self.chart_render_task = None
            if old_chart_render is not None:
                old_chart_render.cancel()
                await asyncio.gather(old_chart_render, return_exceptions=True)

            self.symbol = normalized
            self.market = infer_market(normalized)
            self.stream_live = False
            self.subscription_detail = ""
            self.protocol_degraded = False
            self.orderbook = None
            self.trades.clear()
            self.current_price = None
            self.current_currency = ""
            self.current_timestamp = None
            self.snapshot = None
            self.last_tick_monotonic = None
            self.indicator_degraded = False
            self.candle_sync_degraded = False
            self._live_candles = ()
            self._live_daily_candles = ()
            self._live_candle_revision = 0
            self._live_trade_buffer.clear()
            self._last_chart_render_monotonic = None
            self._indicator_base = None
            self._indicator_base_snapshot = None
            self._indicator_base_mode = None
            if normalized not in self.watchlist_symbols:
                symbols = list(self.watchlist_symbols)
                if len(symbols) >= 12:
                    symbols = symbols[:11]
                symbols.append(normalized)
                self.watchlist_symbols = tuple(symbols)
            self.connection_state = "SWITCHING"
            self.connection_detail = ""
            self._render_chrome()

            if self.client is None:
                return
            await self._refresh_snapshot()
            self.feed_task = asyncio.create_task(
                self._run_feed(symbol=normalized, market=self.market)
            )

    async def _refresh_snapshot(self) -> bool:
        async with self.refresh_lock:
            return await self._refresh_snapshot_locked()

    async def _refresh_snapshot_locked(self) -> bool:
        if self.client is None:
            return False
        requested_symbol = self.symbol
        start_revision = self._live_candle_revision
        was_live = self.stream_live
        previous_detail = self.connection_detail
        self.connection_state = "SYNCING"
        self.connection_detail = "REST snapshot"
        self._render_chrome()
        try:
            snapshot = await self.client.snapshot(requested_symbol)
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
        if self.symbol != requested_symbol or snapshot.stock.symbol != requested_symbol:
            return False
        replay_trades = tuple(
            trade for revision, trade in self._live_trade_buffer if revision > start_revision
        )
        self._apply_snapshot(snapshot, replay_trades=replay_trades)
        self.indicator_degraded = False
        self.candle_sync_degraded = False
        if was_live:
            self.connection_state = "LIVE"
            self.connection_detail = self.subscription_detail or previous_detail
            self.protocol_degraded = False
        self._render_all()
        return True

    def _apply_snapshot(
        self,
        snapshot: MarketSnapshot,
        *,
        replay_trades: tuple[Trade, ...] = (),
    ) -> None:
        live_candles = snapshot.candles
        live_daily_candles = snapshot.daily_candles
        for trade in replay_trades:
            live_candles = apply_trade_to_candles(live_candles, trade)
            live_daily_candles = apply_trade_to_candles(live_daily_candles, trade, interval="1d")
        self._live_candles = live_candles
        self._live_daily_candles = live_daily_candles
        self.snapshot = replace(
            snapshot,
            candles=live_candles,
            daily_candles=live_daily_candles,
        )
        self.orderbook = snapshot.orderbook
        self.trades = deque(snapshot.trades, maxlen=50)
        for trade in replay_trades:
            self.trades.appendleft(trade)
        latest_trade = replay_trades[-1] if replay_trades else None
        self.current_price = latest_trade.price if latest_trade else snapshot.price.last_price
        self.current_currency = latest_trade.currency if latest_trade else snapshot.price.currency
        self.current_timestamp = (
            latest_trade.timestamp if latest_trade else snapshot.price.timestamp
        )
        self._indicator_base = None
        self._indicator_base_snapshot = None
        self._indicator_base_mode = None
        self._refresh_watchlist_rows_from_snapshot()
        self._evaluate_active_alerts()
        if self.is_mounted:
            self._render_watchlist()

    def _record_live_trade(self, trade: Trade) -> LiveCandleStatus:
        if self.snapshot is None:
            return "unavailable"
        base_candles = self._live_candles or self.snapshot.candles
        base_daily = self._live_daily_candles or self.snapshot.daily_candles
        updated_candles = apply_trade_to_candles(base_candles, trade)
        if base_candles and updated_candles == base_candles:
            return "late"
        updated_daily = apply_trade_to_candles(base_daily, trade, interval="1d")
        self._live_candles = updated_candles
        self._live_daily_candles = updated_daily
        self._live_candle_revision += 1
        self._live_trade_buffer.append((self._live_candle_revision, trade))
        return "updated"

    def _publish_live_chart(self) -> None:
        if self.snapshot is None or not self._live_candles:
            return
        self.snapshot = replace(
            self.snapshot,
            candles=self._live_candles,
            daily_candles=self._live_daily_candles,
        )
        self._indicator_base = None
        self._indicator_base_snapshot = None
        self._indicator_base_mode = None
        self._last_chart_render_monotonic = time.monotonic()
        self._render_chart()
        self._render_stats()

    def _schedule_live_chart_render(self) -> None:
        if self.snapshot is None:
            return
        now = time.monotonic()
        last = self._last_chart_render_monotonic
        elapsed = self.chart_render_interval_seconds if last is None else now - last
        delay = max(0.0, self.chart_render_interval_seconds - elapsed)
        pending = self.chart_render_task
        if pending is not None and not pending.done() and delay > 0:
            return
        if delay == 0:
            self._publish_live_chart()
            return
        self.chart_render_task = asyncio.create_task(
            self._publish_live_chart_after(delay, self.symbol)
        )

    async def _publish_live_chart_after(self, delay: float, symbol: str) -> None:
        try:
            await asyncio.sleep(delay)
            last = self._last_chart_render_monotonic
            elapsed = (
                self.chart_render_interval_seconds if last is None else time.monotonic() - last
            )
            if self.symbol == symbol and elapsed >= self.chart_render_interval_seconds:
                self._publish_live_chart()
        finally:
            if self.chart_render_task is asyncio.current_task():
                self.chart_render_task = None

    def _set_candle_sync_degraded(self, detail: str = "WS live · candle sync failed") -> None:
        self.candle_sync_degraded = True
        if self.stream_live:
            self.connection_state = "DEGRADED"
            self.connection_detail = detail
        self._render_chrome()

    async def _run_candle_resync(self) -> None:
        while True:
            await asyncio.sleep(self.candle_resync_seconds)
            try:
                await self._refresh_chart_candles()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._set_candle_sync_degraded()

    async def _refresh_chart_candles(self) -> bool:
        if self.client is None or self.snapshot is None:
            return False
        async with self.refresh_lock:
            requested_symbol = self.symbol
            start_revision = self._live_candle_revision
            try:
                candles, daily_candles = await asyncio.gather(
                    self.client.candles(requested_symbol, count=200),
                    self.client.candles(requested_symbol, interval="1d", count=200),
                )
            except Exception:
                self._set_candle_sync_degraded()
                return False
            if self.symbol != requested_symbol or self.snapshot is None:
                return False
            replay_trades = tuple(
                trade for revision, trade in self._live_trade_buffer if revision > start_revision
            )
            for trade in replay_trades:
                candles = apply_trade_to_candles(candles, trade)
                daily_candles = apply_trade_to_candles(daily_candles, trade, interval="1d")
            self._live_candles = candles
            self._live_daily_candles = daily_candles
            self.candle_sync_degraded = False
            self._publish_live_chart()
            if self.stream_live and not self.protocol_degraded and not self.indicator_degraded:
                self.connection_state = "LIVE"
                self.connection_detail = self.subscription_detail
            self._render_chrome()
            return True

    async def _run_feed(self, *, symbol: str | None = None, market: str | None = None) -> None:
        if self.client is None:
            return
        feed_symbol = symbol or self.symbol
        feed_market = market or infer_market(feed_symbol)
        stream = TossMarketStream(self.client)
        async for event in stream.events(feed_symbol, feed_market):
            if self.symbol != feed_symbol:
                return
            if isinstance(event, StreamStatus):
                await self._handle_stream_status(event)
            elif isinstance(event, TradeEvent):
                if event.symbol != feed_symbol:
                    continue
                self._recover_protocol_status()
                try:
                    candle_status: LiveCandleStatus | None = self._record_live_trade(event.trade)
                except ValueError:
                    candle_status = None
                    self._set_candle_sync_degraded("WS live · candle update failed")
                self.trades.appendleft(event.trade)
                self.last_tick_monotonic = time.monotonic()
                if candle_status != "late":
                    self.current_price = event.trade.price
                    self.current_currency = event.trade.currency
                    self.current_timestamp = event.trade.timestamp
                    self._evaluate_active_alerts()
                    self._render_summary()
                self._render_trades()
                if candle_status == "updated":
                    self._schedule_live_chart_render()
                else:
                    self._render_stats()
                self._render_chrome()
            elif isinstance(event, OrderbookEvent):
                if event.symbol != feed_symbol:
                    continue
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
        if (
            not self.protocol_degraded
            or not self.stream_live
            or self.indicator_degraded
            or self.candle_sync_degraded
        ):
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
                format_trade_time(trade.timestamp),
                Text(format_decimal(trade.price, trade.currency), style=style),
                format_decimal(trade.volume),
                Text(marker, style=style),
            )

    def _render_chart(self) -> None:
        if self.snapshot is None:
            return
        chart_content = self.query_one("#chart-content", Static)
        if not chart_content.is_mounted:
            return
        width, height = chart_content.content_size
        chart_content.update(chart_renderable(self.snapshot, self.chart_mode, width, height))
        title = f"MARKET CHART · {CHART_MODE_LABELS[self.chart_mode]}"
        self.query_one("#chart-panel .panel-title", Static).update(title)

    def _chart_indicators(self) -> ChartIndicators:
        """Cached snapshot+mode indicator base, cheaply re-projected onto the live price.

        Avoids repeating the full EMA9/EMA21/RSI14/VWAP/pivot computation on
        every trade/orderbook tick: the expensive half is cached per
        ``(snapshot identity, chart_mode)`` and only the nearest-level
        projection reruns each call. May raise ``ValueError`` for malformed/
        naive candle timestamps or a currency mismatch; callers must handle
        that to keep rendering resilient to bad data.
        """
        if (
            self._indicator_base is None
            or self._indicator_base_snapshot is not self.snapshot
            or self._indicator_base_mode != self.chart_mode
        ):
            base = chart_indicator_base(self.snapshot, self.chart_mode)
            self._indicator_base = base
            self._indicator_base_snapshot = self.snapshot
            self._indicator_base_mode = self.chart_mode
        return chart_indicators_from_base(self._indicator_base, self.current_price)

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
        signals = market_signals(
            self.snapshot,
            self.current_price,
            orderbook=self.orderbook,
            trades=tuple(self.trades),
        )
        text = Text()
        text.append("시장 신호 요약\n", style="bold #c9d1d9")
        if metrics.spread is not None:
            text.append(
                f"매수·매도 호가 차이 {format_decimal(metrics.spread, self.current_currency)}",
                style=MUTED_COLOR,
            )
            if metrics.spread_percent is not None:
                text.append(f" · {metrics.spread_percent:.3f}%\n", style=MUTED_COLOR)
        if metrics.recent_vwap is not None:
            text.append(
                f"체결 평균 {format_decimal(metrics.recent_vwap, self.current_currency)}",
                style=MUTED_COLOR,
            )
            if signals.vwap_distance_percent is not None:
                text.append(f" · 현재가 {format_percent(signals.vwap_distance_percent)}\n")
            else:
                text.append("\n")
        imbalance = (
            f"{signals.orderbook_imbalance_percent:.1f}%"
            if signals.orderbook_imbalance_percent is not None
            else "—"
        )
        ratio = format_multiple(signals.bid_ask_ratio)
        pressure = (
            f"{signals.trade_pressure_percent:.1f}%"
            if signals.trade_pressure_percent is not None
            else "—"
        )
        volume_ratio = format_multiple(signals.volume_spike_ratio)
        text.append(
            f"호가 {orderbook_signal_label_ko(signals.orderbook_imbalance_percent)} "
            f"{imbalance} · 잔량비 {ratio}\n",
            style=MUTED_COLOR,
        )
        text.append(
            f"체결 {trade_pressure_label_ko(signals.trade_pressure_percent)} {pressure} · "
            f"1분 거래량 {volume_ratio}\n",
            style=MUTED_COLOR,
        )

        timeframe_label = TIMEFRAME_LABELS_KO[self.chart_mode]
        try:
            indicators = self._chart_indicators()
        except ValueError as exc:
            # Fail-safe boundary: a bad candle (malformed/naive timestamp, currency
            # mismatch) must never kill the live feed task. Render the base stats
            # above, mark the connection truthfully DEGRADED with a sanitized
            # detail, and recover only after a subsequent successful REST snapshot
            # (see `_refresh_snapshot_locked`), not on the next tick.
            self.indicator_degraded = True
            self.connection_state = "DEGRADED"
            self.connection_detail = safe_indicator_error(exc)
            text.append(
                f"{timeframe_label} 지표 계산 불가 · {self.connection_detail}\n",
                style="#f0ad4e",
            )
        else:
            text.append(
                f"{timeframe_label} 지표 · EMA9/21 "
                f"{ema_relation_label_ko(indicators.ema_short, indicators.ema_long)}\n",
                style=MUTED_COLOR,
            )
            rsi_text = f"{indicators.rsi:.1f}" if indicators.rsi is not None else "—"
            text.append(
                f"RSI {rsi_text} {rsi_zone_label_ko(indicators.rsi)} · "
                f"거래량 {format_multiple(indicators.relative_volume)}\n",
                style=MUTED_COLOR,
            )
            vwap_percent = vwap_distance_percent(indicators.vwap, self.current_price)
            if indicators.vwap is not None and vwap_percent is not None:
                text.append(
                    f"VWAP {format_decimal(indicators.vwap, self.current_currency)} · "
                    f"현재가 {format_percent(vwap_percent)}\n",
                    style=MUTED_COLOR,
                )
            else:
                text.append("VWAP 데이터 부족\n", style=MUTED_COLOR)
            text.append(
                f"지지 {level_display_ko(indicators.levels.support, self.current_currency)}\n",
                style=MUTED_COLOR,
            )
            text.append(
                f"저항 {level_display_ko(indicators.levels.resistance, self.current_currency)}\n",
                style=MUTED_COLOR,
            )
        text.append(f"{self.market.upper()} 공개 시세 · 조회 전용", style="#526273")
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
        if self.latest_alert is not None:
            status.append(
                f"\nALERT {self.latest_alert.rule.id} · {self.latest_alert.rule.symbol} · "
                f"{self.latest_alert.condition} · {self.latest_alert.observed_value}",
                style="#f0ad4e",
            )
        self.query_one("#statusbar", Static).update(status)
