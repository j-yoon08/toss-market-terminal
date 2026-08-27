from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static

from .alerts import AlertEvaluator, AlertEvent
from .client import TossApiError, TossMarketClient
from .config import CredentialError, Credentials
from .interpretation import (
    MarketInterpretation,
    build_market_interpretation,
    interpret_timeframe,
    interpretation_explanation,
)
from .live_audit import LiveAuditLog, LiveAuditLogError
from .live_chart import apply_trade_to_candles
from .live_order import (
    MANUAL_LIVE_ENV_KEY,
    MANUAL_LIVE_ENV_VALUE,
    LiveAuditRecord,
    LiveExecutionRequest,
    LiveOrderAccepted,
    LiveOrderAmbiguous,
    LiveOrderPlan,
    LiveOrderRejected,
    LiveOrderTransport,
    ManualLiveOrderExecutor,
    build_live_packet,
    create_live_plan,
    live_approval_phrase,
)
from .live_ticket import LiveApprovalScreen
from .models import (
    AccountContext,
    Candle,
    ClosedOrdersPage,
    DataShapeError,
    ExchangeRate,
    MarketSnapshot,
    OpenOrdersPage,
    Orderbook,
    PortfolioSnapshot,
    Trade,
    find_open_order_duplicates,
)
from .order_preview import (
    OrderPreview,
    OrderPreviewError,
    OrderSide,
    PaperPreviewService,
    canonical_decimal_text,
)
from .order_ticket import OrderConfirmScreen, OrderTicketScreen, build_ticket_capture
from .order_transport import TossOrderTransport
from .portfolio import PortfolioScreen
from .render import (
    CHART_MODE_LABELS,
    DOWN_COLOR,
    MUTED_COLOR,
    TIMEFRAME_LABELS_KO,
    UP_COLOR,
    ChartIndicatorBase,
    ChartIndicators,
    HoldingAveragePriceOverlay,
    chart_indicator_base,
    chart_indicators_from_base,
    chart_renderable,
    direction_style,
    ema_relation_label_ko,
    format_age,
    format_decimal,
    format_multiple,
    format_percent,
    format_signed,
    format_trade_time,
    market_metrics,
    market_signals,
    orderbook_signal_label_ko,
    rsi_zone_label_ko,
    trade_pressure_label_ko,
    volume_bar,
    vwap_distance_percent,
)
from .settings import Settings, SettingsError, SettingsStore, with_watchlist_symbol
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
PORTFOLIO_REFRESH_SECONDS = 30.0
EXCHANGE_RATE_REFRESH_SECONDS = 60.0
ORDER_HISTORY_REFRESH_SECONDS = 300.0
PRICE_STALE_SECONDS = 120.0
MAX_FUTURE_PRICE_SKEW_SECONDS = 30.0
COMPACT_WIDTH_THRESHOLD = 117
LiveCandleStatus = Literal["updated", "late", "unavailable"]
LiveTransportFactory = Callable[[str, int], LiveOrderTransport]
PortfolioLoader = Callable[[], Awaitable[PortfolioSnapshot]]
ExchangeRateLoader = Callable[[], Awaitable[ExchangeRate]]
ClosedOrdersLoader = Callable[[], Awaitable[ClosedOrdersPage]]


@dataclass
class _CancellationState:
    requested: bool = False


async def _to_thread_uninterruptibly[**P, R](
    state: _CancellationState,
    function: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Finish a mutation/audit thread even when Textual cancels its worker."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # asyncio cannot stop an already-running thread. Record the quit
            # request and keep ownership until the result/audit is durable.
            state.requested = True


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


_RECONNECTING_STATES = frozenset({"RECONNECTING", "CONNECTING", "SWITCHING", "SUBSCRIBED"})
_ERROR_STATES = frozenset({"ERROR", "AUTH_ERROR", "REJECTED"})


def connection_state_color(state: str, live: bool) -> str:
    """Restrained semantic color for a connection state: LIVE cyan/green, DEGRADED amber,
    reconnecting/transitional blue, error states red; otherwise the muted default.
    """
    if state == "DEGRADED":
        return "#f0ad4e"
    if state in _ERROR_STATES:
        return DOWN_COLOR
    if state in _RECONNECTING_STATES:
        return "#58a6ff"
    if live:
        return UP_COLOR
    return MUTED_COLOR


@dataclass(frozen=True, slots=True)
class WatchlistRow:
    symbol: str
    price: str
    currency: str
    active_alerts: int


def _submit_live_plan_once(
    plan: LiveOrderPlan,
    approval_phrase: str,
    access_token: str,
    account_seq: int,
    transport_factory: LiveTransportFactory,
) -> tuple[
    LiveOrderAccepted | LiveOrderRejected | LiveOrderAmbiguous,
    tuple[LiveAuditRecord, ...],
    bool,
]:
    """Run the synchronous one-shot executor off the Textual event loop."""

    transport = transport_factory(access_token, account_seq)
    executor = ManualLiveOrderExecutor(transport)
    close_failed = False
    try:
        outcome = executor.execute(
            LiveExecutionRequest(
                plan=plan,
                execute=True,
                acknowledge_final_approval=True,
                interactive_session=True,
            ),
            approval_phrase,
        )
    finally:
        close = getattr(transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                # Closing happens after the one-shot mutation result is known.
                # It must never overwrite accepted/rejected/ambiguous evidence.
                close_failed = True
    return outcome, executor.audit_records(), close_failed


class WatchlistAddScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar = [("escape", "cancel", "취소")]
    CSS = """
    WatchlistAddScreen {
        align: center middle;
        background: rgba(4, 7, 10, 0.75);
    }
    #watchlist-add-dialog {
        width: 52;
        height: 9;
        padding: 1 2;
        border: solid #607080;
        background: #0d131a;
    }
    #watchlist-add-title {
        height: 2;
        color: #d9e1e8;
        text-style: bold;
    }
    #watchlist-add-help {
        height: 2;
        color: #7d8998;
    }
    #watchlist-add-input {
        width: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="watchlist-add-dialog"):
            yield Static("관심 종목 추가", id="watchlist-add-title", markup=False)
            yield Input(placeholder="AAPL 또는 005930", id="watchlist-add-input")
            yield Static("Enter 저장 · Esc 취소", id="watchlist-add-help", markup=False)

    def on_mount(self) -> None:
        self.query_one("#watchlist-add-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            event.input.placeholder = "심볼을 입력하세요"
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_LINES = (
    ("q", "종료"),
    ("r", "REST 재동기화"),
    ("p", "포트폴리오 보기(계좌/보유/미체결)"),
    ("a", "관심종목 추가"),
    ("b", "PAPER 매수 미리보기(주문 전송 없음)"),
    ("s", "PAPER 매도 미리보기(주문 전송 없음)"),
    ("1 2 3 4 5", "1분/5분/15분/1시간/일봉"),
    ("c", "차트 포커스 전환"),
    ("i", "시장 신호 해석 보기"),
    ("↑ ↓ / j k", "관심 목록 이동"),
    ("Enter", "선택한 종목으로 전환"),
    ("?", "도움말 열기/닫기"),
    ("Esc", "닫기"),
)


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar = [
        Binding("escape", "close", "닫기"),
        Binding("question_mark", "close", "닫기"),
        Binding("p", "noop", "닫기", show=False),
    ]
    CSS = """
    HelpScreen {
        align: center middle;
        background: rgba(4, 7, 10, 0.75);
    }
    #help-dialog {
        width: 44;
        height: 16;
        padding: 1 2;
        border: solid #607080;
        background: #0d131a;
    }
    #help-title {
        height: 1;
        color: #d9e1e8;
        text-style: bold;
    }
    #help-body {
        height: 1fr;
        color: #aab7c4;
    }
    #help-footer {
        height: 1;
        color: #526273;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static("키보드 단축키", id="help-title", markup=False)
            body = "\n".join(f"{key:<10}{description}" for key, description in HELP_LINES)
            yield Static(body, id="help-body", markup=False)
            yield Static("Esc 또는 ? 닫기", id="help-footer", markup=False)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_noop(self) -> None:
        """모달 격리용 no-op. 어떤 상태도 바꾸지 않는다."""


def _bounded_cells(value: str, width: int = 52) -> str:
    """Truncate by terminal display cells, not Python code-point length."""
    if cell_len(value) <= width:
        return value
    text = value
    while text and cell_len(text + "…") > width:
        text = text[:-1]
    return text + "…"


def interpretation_detail_text(analysis: MarketInterpretation) -> str:
    selected = analysis.selected
    quality_label = {
        "fresh": "최신",
        "stale": "신선도 저하",
        "insufficient": "부족",
    }[selected.data_quality]
    lines = [
        f"{selected.label} · {selected.headline} · 신뢰도 {selected.confidence}",
        f"데이터 상태 · {quality_label}",
        "",
        "해석",
        interpretation_explanation(selected),
        "",
        "근거",
    ]
    lines.extend(f"• {item}" for item in selected.evidence)
    if not selected.evidence:
        lines.append("• 방향을 설명할 지표 데이터가 부족함")
    lines.extend(("", "반대 신호와 주의"))
    lines.extend(f"• {item}" for item in selected.risks)
    if not selected.risks:
        lines.append("• 현재 데이터에서 확인된 주요 반대 신호 없음")
    lines.extend(("", "조건별 관찰"))
    lines.append(
        f"• {selected.upside_scenario}" if selected.upside_scenario else "• 저항 가격대 데이터 부족"
    )
    lines.append(
        f"• {selected.downside_scenario}"
        if selected.downside_scenario
        else "• 지지 가격대 데이터 부족"
    )
    lines.extend(("", "시간대별 흐름"))
    lines.extend(
        f"• {item.label:<3} {item.headline} · 신뢰도 {item.confidence}"
        for item in analysis.timeframes
    )
    lines.extend(
        ("", analysis.alignment, "", "열기 시점의 공개 시세 기반 · 관찰 전용 · 실행 권고 아님")
    )
    return "\n".join(lines)


class MarketInterpretationScreen(ModalScreen[None]):
    BINDINGS: ClassVar = [
        Binding("escape", "close", "닫기"),
        Binding("i", "close", "닫기"),
        Binding("up,k", "scroll_up", "위", show=False),
        Binding("down,j", "scroll_down", "아래", show=False),
        Binding(
            "q,r,p,a,b,s,1,2,3,4,5,c,enter,question_mark",
            "noop",
            "",
            show=False,
        ),
    ]
    CSS = """
    MarketInterpretationScreen {
        align: center middle;
        background: rgba(4, 7, 10, 0.78);
    }
    #interpretation-dialog {
        width: 92%;
        max-width: 96;
        height: 94%;
        max-height: 40;
        padding: 1 2;
        border: solid #607080;
        background: #0d131a;
    }
    #interpretation-title {
        height: 2;
        color: #d9e1e8;
        text-style: bold;
    }
    #interpretation-scroll {
        height: 1fr;
        scrollbar-background: #0d131a;
        scrollbar-color: #3a4654;
    }
    #interpretation-body {
        color: #aab7c4;
    }
    #interpretation-footer {
        height: 1;
        color: #526273;
    }
    """

    def __init__(self, analysis: MarketInterpretation) -> None:
        super().__init__()
        self.analysis = analysis

    def compose(self) -> ComposeResult:
        with Vertical(id="interpretation-dialog"):
            yield Static("시장 신호 상세 해석", id="interpretation-title", markup=False)
            with VerticalScroll(id="interpretation-scroll"):
                yield Static(
                    interpretation_detail_text(self.analysis),
                    id="interpretation-body",
                    markup=False,
                )
            yield Static(
                "↑/↓/j/k 스크롤 · i 또는 Esc 닫기",
                id="interpretation-footer",
                markup=False,
            )

    def action_close(self) -> None:
        self.dismiss(None)

    def action_noop(self) -> None:
        """Keep every underlying app action inert while this modal is open."""

    def action_scroll_up(self) -> None:
        self.query_one("#interpretation-scroll", VerticalScroll).scroll_relative(
            y=-3, animate=False
        )

    def action_scroll_down(self) -> None:
        self.query_one("#interpretation-scroll", VerticalScroll).scroll_relative(y=3, animate=False)


class TossMarketApp(App[int]):
    TITLE = "Toss Market Terminal"
    SUB_TITLE = "READ ONLY"
    BINDINGS: ClassVar = [
        Binding("q", "quit", "종료"),
        Binding("r", "refresh", "재동기화"),
        Binding("p", "toggle_portfolio", "포트폴리오"),
        Binding("1", "intraday", "1-5 타임프레임"),
        Binding("2", "chart_5m", "5분봉", show=False),
        Binding("3", "chart_15m", "15분봉", show=False),
        Binding("4", "chart_1h", "1시간봉", show=False),
        Binding("5", "daily", "일봉", show=False),
        Binding("c", "toggle_focus", "차트 포커스"),
        Binding("i", "toggle_interpretation", "시장 해석"),
        Binding("a", "add_watchlist", "관심종목 추가"),
        Binding("b", "paper_buy_preview", "매수 미리보기(PAPER)", show=False),
        Binding("s", "paper_sell_preview", "매도 미리보기(PAPER)", show=False),
        Binding("question_mark", "toggle_help", "도움말"),
        Binding("up", "watch_up", "위 항목", show=False),
        Binding("down", "watch_down", "아래 항목", show=False),
        Binding("j", "watch_down", "아래 항목(j)", show=False),
        Binding("k", "watch_up", "위 항목(k)", show=False),
        Binding("enter", "watch_select", "심볼 전환", show=False),
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
        height: 4;
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
    #watchlist-panel { width: 15%; }
    #orderbook-panel { width: 24%; }
    #chart-panel { width: 42%; }
    #trades-panel { width: 19%; }
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
        height: 10;
        padding: 0 2;
        border-top: solid #2a3440;
        color: #9aa7b4;
    }
    #statusbar {
        height: 2;
        padding: 0 2;
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
    Screen.compact #summary { height: 5; }
    Screen.chart-focus #watchlist-panel { display: none; }
    Screen.chart-focus #chart-panel { width: 60%; }
    Screen.chart-focus #orderbook-panel { width: 20%; }
    Screen.chart-focus #trades-panel { width: 20%; }
    Screen.symbol-picker #watchlist-panel { display: block; width: 100%; }
    Screen.symbol-picker #orderbook-panel,
    Screen.symbol-picker #chart-panel,
    Screen.symbol-picker #trades-panel { display: none; }
    Screen.compact.symbol-picker #watchlist-panel { display: block; width: 100%; }
    Screen.compact.symbol-picker #orderbook-panel,
    Screen.compact.symbol-picker #chart-panel,
    Screen.compact.symbol-picker #trades-panel { display: none; }
    """

    def __init__(
        self,
        symbol: str | None,
        credentials_path: Path,
        *,
        initial_snapshot: MarketSnapshot | None = None,
        connect_live: bool = True,
        settings_path: Path | None = None,
        settings: Settings | None = None,
        watchlist_refresh_seconds: float = WATCHLIST_REFRESH_SECONDS,
        candle_resync_seconds: float = CANDLE_RESYNC_SECONDS,
        chart_render_interval_seconds: float = CHART_RENDER_INTERVAL_SECONDS,
        portfolio_refresh_seconds: float = PORTFOLIO_REFRESH_SECONDS,
        exchange_rate_refresh_seconds: float = EXCHANGE_RATE_REFRESH_SECONDS,
        order_history_refresh_seconds: float = ORDER_HISTORY_REFRESH_SECONDS,
        price_stale_seconds: float = PRICE_STALE_SECONDS,
        manual_live_orders: bool = False,
        live_audit_log: LiveAuditLog | None = None,
        live_transport_factory: LiveTransportFactory | None = None,
        account_seq: int | None = None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__()
        self.utc_now = utc_now
        if symbol is None and initial_snapshot is not None:
            symbol = initial_snapshot.stock.symbol
        self.symbol = normalize_symbol(symbol) if symbol is not None else ""
        self.market = infer_market(self.symbol) if self.symbol else ""
        self.credentials_path = credentials_path
        self.initial_snapshot = initial_snapshot
        self.connect_live = connect_live
        self.settings_path = settings_path
        self.settings = settings if settings is not None else Settings()
        self.watchlist_refresh_seconds = watchlist_refresh_seconds
        if account_seq is not None and account_seq <= 0:
            raise ValueError("account_seq는 양의 정수여야 합니다.")
        self.account_seq = account_seq
        if portfolio_refresh_seconds <= 0:
            raise ValueError("포트폴리오 재동기화 주기는 양수여야 합니다.")
        self.portfolio_refresh_seconds = portfolio_refresh_seconds
        if exchange_rate_refresh_seconds <= 0:
            raise ValueError("환율 재동기화 주기는 양수여야 합니다.")
        if order_history_refresh_seconds <= 0:
            raise ValueError("주문내역 재동기화 주기는 양수여야 합니다.")
        if price_stale_seconds <= 0:
            raise ValueError("시세 신선도 임계값은 양수여야 합니다.")
        self.exchange_rate_refresh_seconds = exchange_rate_refresh_seconds
        self.order_history_refresh_seconds = order_history_refresh_seconds
        self.price_stale_seconds = price_stale_seconds
        if candle_resync_seconds <= 0:
            raise ValueError("캔들 재동기화 주기는 양수여야 합니다.")
        if chart_render_interval_seconds < 0:
            raise ValueError("차트 렌더 간격은 0 이상이어야 합니다.")
        self.candle_resync_seconds = candle_resync_seconds
        self.chart_render_interval_seconds = chart_render_interval_seconds
        if not isinstance(manual_live_orders, bool):
            raise ValueError("manual_live_orders는 bool이어야 합니다.")
        self.manual_live_orders = manual_live_orders
        self.sub_title = "MANUAL LIVE" if manual_live_orders else "READ ONLY"
        self.live_audit_log = live_audit_log or LiveAuditLog()
        self.live_transport_factory = live_transport_factory or (
            lambda token, account_seq: TossOrderTransport(token, account_seq)
        )
        self.refresh_lock = asyncio.Lock()
        self.switch_lock = asyncio.Lock()
        self.watchlist_refresh_lock = asyncio.Lock()
        self.client: TossMarketClient | None = None
        self.feed_task: asyncio.Task[None] | None = None
        self.watchlist_task: asyncio.Task[None] | None = None
        self.candle_sync_task: asyncio.Task[None] | None = None
        self.chart_render_task: asyncio.Task[None] | None = None
        # In-memory only: `watch SYMBOL` never persists the launch symbol.
        self.watchlist_symbols = self._watchlist_with_active(self.settings.watchlist)
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
        self.last_orderbook_monotonic: float | None = None
        self.last_sync_monotonic: float | None = None
        self._live_candles: tuple[Candle, ...] = ()
        self._live_daily_candles: tuple[Candle, ...] = ()
        self._live_candle_revision = 0
        self._live_trade_buffer: deque[tuple[int, Trade]] = deque(maxlen=2_000)
        self._last_chart_render_monotonic: float | None = None
        self._indicator_base: ChartIndicatorBase | None = None
        self._indicator_base_snapshot: MarketSnapshot | None = None
        self._indicator_base_mode: str | None = None
        # v0.7b PAPER 미리보기 상태(메모리 전용, 디스크 저장 없음).
        self.last_paper_preview: OrderPreview | None = None
        self.last_live_outcome: (
            LiveOrderAccepted | LiveOrderRejected | LiveOrderAmbiguous | None
        ) = None
        self._ticket_open_lock = asyncio.Lock()
        self._live_submit_lock = asyncio.Lock()
        self._live_attempted_fingerprints: set[str] = set()
        # 테스트 주입 지점(connect_live=False). 실제 앱은 v0.6 읽기 전용
        # account_context만 이 경로로 호출한다.
        self.account_context_loader: Callable[[str], Awaitable[AccountContext]] | None = None
        self.open_orders_loader: Callable[[int, str], Awaitable[OpenOrdersPage]] | None = None
        self.access_token_loader: Callable[[], Awaitable[str]] | None = None
        # 미리보기 생성 서비스 팩토리(순수 도메인 PaperPreviewService). 테스트 대체 가능.
        self.paper_preview_service_factory: Callable[[], PaperPreviewService] | None = None
        # Phase-1 포트폴리오 상태(계좌 읽기 전용, GET만). 마지막 성공 스냅샷을
        # 유지하고, 실패해도 시장 WS/연결 상태는 절대 건드리지 않는다.
        self.portfolio_task: asyncio.Task[None] | None = None
        self.portfolio_refresh_lock = asyncio.Lock()
        self.portfolio_snapshot: PortfolioSnapshot | None = None
        self.portfolio_stale = False
        self.portfolio_error: str | None = None
        self.portfolio_synced_monotonic: float | None = None
        self._portfolio_screen: PortfolioScreen | None = None
        # 테스트 주입 지점(connect_live=False에서도 포트폴리오 새로고침을 검증할 수 있다).
        self.portfolio_loader: PortfolioLoader | None = None
        # Phase-2 insights are isolated from both the market stream and the
        # 30-second account snapshot. Each keeps its own last-good value.
        self.exchange_rate_task: asyncio.Task[None] | None = None
        self.order_history_task: asyncio.Task[None] | None = None
        self.exchange_rate_refresh_lock = asyncio.Lock()
        self.order_history_refresh_lock = asyncio.Lock()
        self.exchange_rate: ExchangeRate | None = None
        self.exchange_rate_stale = False
        self.exchange_rate_error: str | None = None
        self.exchange_rate_synced_monotonic: float | None = None
        self.closed_orders: ClosedOrdersPage | None = None
        self.order_history_account_seq: int | None = None
        self.order_history_stale = False
        self.order_history_error: str | None = None
        self.order_history_synced_monotonic: float | None = None
        self.order_history_wakeup = asyncio.Event()
        self.exchange_rate_loader: ExchangeRateLoader | None = None
        self.closed_orders_loader: ClosedOrdersLoader | None = None

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
                    id="orderbook", zebra_stripes=False, show_cursor=False, cell_padding=0
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
                self._maybe_start_portfolio_polling()
                return

        if not self.symbol and not self.connect_live:
            self._show_symbol_picker()
            self._maybe_start_portfolio_polling()
            return

        try:
            credentials = Credentials.load(self.credentials_path)
        except CredentialError as exc:
            self.exit(2, message=f"오류: {exc}")
            return
        self.client = TossMarketClient(credentials)
        await self._load_persisted_watchlist()
        # Independent of symbol selection: portfolio data has its own account
        # scope and must never block the market feed/symbol picker.
        self._maybe_start_portfolio_polling()
        if not self.symbol:
            await self._refresh_watchlist_prices()
            self._show_symbol_picker()
            self.watchlist_task = asyncio.create_task(self._run_watchlist_polling())
            return
        await self._refresh_snapshot()
        self.feed_task = asyncio.create_task(self._run_feed())
        self.watchlist_task = asyncio.create_task(self._run_watchlist_polling())
        self.candle_sync_task = asyncio.create_task(self._run_candle_resync())

    def _maybe_start_portfolio_polling(self) -> None:
        """Start the background account-refresh loop at most once.

        Ordinary disconnected fixture apps (``connect_live=False`` with no
        ``portfolio_loader`` injected) never start it, so unit tests that
        never touch the portfolio feature see no stray task/network attempt.
        Real ``connect_live`` runs always start it -- portfolio has its own
        account scope and must never depend on a symbol being selected.
        """
        if self.portfolio_task is None and (self.connect_live or self.portfolio_loader is not None):
            self.portfolio_task = asyncio.create_task(self._run_portfolio_polling())
        if self.exchange_rate_task is None and (
            self.client is not None or self.exchange_rate_loader is not None
        ):
            self.exchange_rate_task = asyncio.create_task(self._run_exchange_rate_polling())
        self._maybe_start_order_history_polling()

    def _maybe_start_order_history_polling(self) -> None:
        if self.order_history_task is not None or self.portfolio_snapshot is None:
            return
        if self.client is None and self.closed_orders_loader is None:
            return
        self.order_history_task = asyncio.create_task(self._run_order_history_polling())

    def _show_symbol_picker(self) -> None:
        """Render a neutral start state until the operator selects a watchlist row."""
        self.screen.set_class(True, "symbol-picker")
        self.connection_state = "SELECT"
        self.connection_detail = "↑/↓ 이동 · Enter 선택 · a 종목 추가"
        self.query_one("#summary", Static).update(
            "관심 종목을 선택하세요\n↑/↓ 이동 · Enter 선택 · a 종목 추가"
        )
        self._render_watchlist()
        self._render_chrome()

    async def on_unmount(self) -> None:
        for task in (
            self.chart_render_task,
            self.candle_sync_task,
            self.watchlist_task,
            self.feed_task,
            self.portfolio_task,
            self.exchange_rate_task,
            self.order_history_task,
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

    def action_add_watchlist(self) -> None:
        self.push_screen(WatchlistAddScreen(), self._add_watchlist_symbol)

    def action_toggle_help(self) -> None:
        self.push_screen(HelpScreen())

    def _interpretation_is_stale(self) -> bool:
        """True when the current price cannot be trusted for interpretation or orders.

        The TICK clock (`last_tick_monotonic`) is only renewed by an actual price
        observation: an initial/resync REST snapshot or a trade tick. Orderbook
        traffic (`last_orderbook_monotonic`) never counts as a price observation,
        so a feed that only delivers orderbook updates goes stale here.
        """
        if (
            self.indicator_degraded
            or self.candle_sync_degraded
            or self.connection_state not in {"LIVE", "PREVIEW", "SNAPSHOT"}
        ):
            return True
        if self.current_price is None or self.last_tick_monotonic is None:
            return True
        return time.monotonic() - self.last_tick_monotonic > self.price_stale_seconds

    def action_toggle_interpretation(self) -> None:
        if self.snapshot is None or self.current_price is None:
            self.notify(
                "분석할 종목과 시세를 먼저 선택하세요.",
                title="시장 해석",
                severity="warning",
            )
            return
        try:
            stale = self._interpretation_is_stale()
            indicators = None if stale else self._chart_indicators()
            signals = market_signals(
                self.snapshot,
                self.current_price,
                orderbook=self.orderbook,
                trades=tuple(self.trades),
            )
            analysis = build_market_interpretation(
                self.snapshot,
                self.current_price,
                self.current_currency,
                self.chart_mode,
                signals,
                selected_indicators=indicators,
                stale=stale,
            )
        except (ValueError, ArithmeticError) as exc:
            self.notify(
                safe_indicator_error(exc),
                title="시장 해석",
                severity="warning",
            )
            return
        self.push_screen(MarketInterpretationScreen(analysis))

    def action_toggle_portfolio(self) -> None:
        if self._portfolio_screen is not None:
            self._portfolio_screen.dismiss(None)
            return
        if self.client is None and self.portfolio_loader is None:
            self.notify(
                "계좌 정보를 사용할 수 없어 포트폴리오를 열 수 없습니다.",
                title="PORTFOLIO",
                severity="warning",
            )
            return
        screen = PortfolioScreen()
        self._portfolio_screen = screen
        self.push_screen(screen, self._on_portfolio_closed)
        self._maybe_start_portfolio_polling()

    def _on_portfolio_closed(self, _result: None) -> None:
        self._portfolio_screen = None

    async def _load_portfolio_snapshot(self) -> PortfolioSnapshot:
        loader = self.portfolio_loader
        if loader is not None:
            return await loader()
        if self.client is None:
            raise RuntimeError("client-unavailable")
        return await self.client.portfolio_snapshot(self.account_seq)

    async def _refresh_portfolio(self) -> bool:
        """One read-only account refresh. Never touches market/WS connection state."""
        if self.portfolio_refresh_lock.locked():
            return False
        async with self.portfolio_refresh_lock:
            try:
                snapshot = await self._load_portfolio_snapshot()
            except Exception as exc:
                self.portfolio_stale = True
                self.portfolio_error = safe_status_error(exc)
                if self._portfolio_screen is not None:
                    self._portfolio_screen.refresh_view()
                # Last-good overlay line must switch to its STALE label; this
                # never touches market/WS connection state.
                self._render_chart()
                return False
            previous_account_seq = (
                self.portfolio_snapshot.account.account_seq
                if self.portfolio_snapshot is not None
                else None
            )
            self.portfolio_snapshot = snapshot
            if (
                previous_account_seq is not None
                and previous_account_seq != snapshot.account.account_seq
            ):
                # Never render account A's last-good history beneath account B.
                self.closed_orders = None
                self.order_history_account_seq = None
                self.order_history_stale = False
                self.order_history_error = None
                self.order_history_synced_monotonic = None
                self.order_history_wakeup.set()
            self.portfolio_stale = False
            self.portfolio_error = None
            self.portfolio_synced_monotonic = time.monotonic()
            self._maybe_start_order_history_polling()
            if self._portfolio_screen is not None:
                self._portfolio_screen.refresh_view()
            # Recovery clears STALE and/or reflects a newly (un)held symbol;
            # this never touches market/WS connection state.
            self._render_chart()
            return True

    async def _run_portfolio_polling(self) -> None:
        """Bounded background refresh: prompt first fetch, then fixed interval."""
        while True:
            try:
                await self._refresh_portfolio()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.portfolio_stale = True
            await asyncio.sleep(self.portfolio_refresh_seconds)

    async def _load_exchange_rate(self) -> ExchangeRate:
        if self.exchange_rate_loader is not None:
            return await self.exchange_rate_loader()
        if self.client is None:
            raise RuntimeError("client-unavailable")
        return await self.client.exchange_rate()

    async def _refresh_exchange_rate(self) -> bool:
        if self.exchange_rate_refresh_lock.locked():
            return False
        async with self.exchange_rate_refresh_lock:
            try:
                exchange_rate = await self._load_exchange_rate()
            except Exception as exc:
                self.exchange_rate_stale = True
                self.exchange_rate_error = safe_status_error(exc)
                if self._portfolio_screen is not None:
                    self._portfolio_screen.refresh_view()
                return False
            self.exchange_rate = exchange_rate
            self.exchange_rate_stale = False
            self.exchange_rate_error = None
            self.exchange_rate_synced_monotonic = time.monotonic()
            if self._portfolio_screen is not None:
                self._portfolio_screen.refresh_view()
            return True

    async def _run_exchange_rate_polling(self) -> None:
        while True:
            try:
                await self._refresh_exchange_rate()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.exchange_rate_stale = True
            await asyncio.sleep(self.exchange_rate_refresh_seconds)

    async def _load_closed_orders(self, account_seq: int | None = None) -> ClosedOrdersPage:
        if self.closed_orders_loader is not None:
            return await self.closed_orders_loader()
        if self.client is None or self.portfolio_snapshot is None:
            raise RuntimeError("portfolio-unavailable")
        requested_account_seq = (
            self.portfolio_snapshot.account.account_seq if account_seq is None else account_seq
        )
        end_date = datetime.now(KST).date()
        start_date = end_date - timedelta(days=29)
        return await self.client.closed_orders(
            requested_account_seq,
            start_date=start_date,
            end_date=end_date,
            limit=20,
        )

    async def _refresh_order_history(self) -> bool:
        if self.order_history_refresh_lock.locked():
            return False
        async with self.order_history_refresh_lock:
            snapshot = self.portfolio_snapshot
            if snapshot is None:
                return False
            requested_account_seq = snapshot.account.account_seq
            try:
                page = await self._load_closed_orders(requested_account_seq)
            except Exception as exc:
                current = self.portfolio_snapshot
                if current is None or current.account.account_seq != requested_account_seq:
                    self.order_history_wakeup.set()
                    return False
                self.order_history_stale = True
                self.order_history_error = safe_status_error(exc)
                if self._portfolio_screen is not None:
                    self._portfolio_screen.refresh_view()
                return False
            current = self.portfolio_snapshot
            if current is None or current.account.account_seq != requested_account_seq:
                self.order_history_wakeup.set()
                return False
            self.closed_orders = page
            self.order_history_account_seq = requested_account_seq
            self.order_history_stale = False
            self.order_history_error = None
            self.order_history_synced_monotonic = time.monotonic()
            if self._portfolio_screen is not None:
                self._portfolio_screen.refresh_view()
            return True

    async def _run_order_history_polling(self) -> None:
        while True:
            try:
                await self._refresh_order_history()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.order_history_stale = True
            try:
                await asyncio.wait_for(
                    self.order_history_wakeup.wait(),
                    timeout=self.order_history_refresh_seconds,
                )
            except TimeoutError:
                pass
            self.order_history_wakeup.clear()

    async def _refresh_portfolio_details(self) -> None:
        await self._refresh_portfolio()
        refreshers: list[Awaitable[bool]] = []
        if self.client is not None or self.exchange_rate_loader is not None:
            refreshers.append(self._refresh_exchange_rate())
        if self.portfolio_snapshot is not None and (
            self.client is not None or self.closed_orders_loader is not None
        ):
            refreshers.append(self._refresh_order_history())
        if refreshers:
            await asyncio.gather(*refreshers)

    def action_paper_buy_preview(self) -> None:
        self.run_worker(self._open_paper_ticket(OrderSide.BUY), exclusive=False)

    def action_paper_sell_preview(self) -> None:
        self.run_worker(self._open_paper_ticket(OrderSide.SELL), exclusive=False)

    # ------------------------------------------------------------------
    # v0.7b PAPER 주문 미리보기 티켓(전송 경로 없음).
    # ------------------------------------------------------------------

    async def _load_account_context(self, captured_symbol: str) -> AccountContext:
        """캡처한 심볼의 읽기 전용 계좌 컨텍스트를 조회한다(v0.6 account_context).

        connect_live=False 환경(테스트)에서는 ``account_context_loader``로
        가짜 클라이언트의 account_context를 주입할 수 있다.
        """
        loader = self.account_context_loader
        if loader is not None:
            return await loader(captured_symbol)
        if self.client is None:
            raise RuntimeError("client-unavailable")
        return await self.client.account_context(captured_symbol, self.account_seq)

    async def _open_paper_ticket(self, side: OrderSide) -> None:
        if self._ticket_open_lock.locked():
            # 중복 b/s 입력 직렬화: 이미 진행 중이면 새 티켓을 만들지 않는다.
            self.notify(
                "이미 미리보기 창을 준비 중입니다.",
                title="PAPER PREVIEW",
                severity="warning",
            )
            return
        async with self._ticket_open_lock:
            if not self.symbol:
                self.notify(
                    "먼저 관심 목록에서 종목을 선택하세요.",
                    title="PAPER PREVIEW",
                    severity="warning",
                )
                return
            capture = build_ticket_capture(
                self.symbol,
                self.current_price,
                self.current_currency,
            )
            if capture is None:
                self.notify(
                    "미리보기를 만들 수 없습니다: 현재가 또는 통화 정보가 없습니다.",
                    title="PAPER PREVIEW",
                    severity="warning",
                )
                return
            if self._interpretation_is_stale():
                # Blocks PAPER and, by extension, any later LIVE promotion —
                # fail-closed before touching account/network loaders at all.
                self.notify(
                    "시세 신선도가 저하되어 미리보기를 열지 않았습니다.",
                    title="PAPER PREVIEW",
                    severity="warning",
                )
                return
            try:
                context = await self._load_account_context(capture.symbol)
            except Exception:
                self.notify(
                    "계좌 정보를 확인할 수 없어 미리보기를 열지 않았습니다.",
                    title="PAPER PREVIEW",
                    severity="warning",
                )
                return
            if (
                context.symbol != capture.symbol
                or context.buying_power.currency != capture.currency
            ):
                # 응답 전 종목/통화가 바뀐 stale race도 fail-closed로 닫는다.
                self.notify(
                    "종목 정보가 변경되어 미리보기를 열지 않았습니다.",
                    title="PAPER PREVIEW",
                    severity="warning",
                )
                return
            self.push_screen(
                OrderTicketScreen(
                    capture,
                    context,
                    side,
                    preview_service_factory=self.paper_preview_service_factory,
                ),
                lambda preview: self._on_paper_preview_built(side, preview),
            )

    def _on_paper_preview_built(self, side: OrderSide, preview: OrderPreview | None) -> None:
        """티켓 모달 결과 콜백: 미리보기가 나오면 확인 모달로 이어간다."""
        if preview is None:
            return  # Esc 취소: 아무 것도 저장하지 않는다.
        self.push_screen(
            OrderConfirmScreen(preview),
            lambda ok: self._on_paper_confirmed(preview, bool(ok)),
        )

    def _on_paper_confirmed(self, preview: OrderPreview, confirmed: bool) -> None:
        if not confirmed:
            return  # 취소: 아무 것도 저장하지 않는다.
        # 확정은 메모리 보관 + 알림일 뿐이다. 어떤 엔드포인트 호출도 없다.
        self.last_paper_preview = preview
        self.notify(
            "PAPER PREVIEW 생성 · 실제 주문은 전송되지 않았습니다.",
            title="PAPER PREVIEW",
            severity="information",
        )
        if not self.manual_live_orders:
            return
        if os.environ.get(MANUAL_LIVE_ENV_KEY) != MANUAL_LIVE_ENV_VALUE:
            self.notify(
                "환경 게이트가 꺼져 있어 LIVE 승인 화면을 열지 않았습니다.",
                title="LIVE ORDER BLOCKED",
                severity="warning",
            )
            return
        try:
            plan = create_live_plan(preview, datetime.now(UTC))
        except Exception:
            self.notify(
                "라이브 계획을 만들 수 없어 주문을 전송하지 않았습니다.",
                title="LIVE ORDER BLOCKED",
                severity="error",
            )
            return
        self.push_screen(
            LiveApprovalScreen(plan),
            lambda phrase: self._on_live_approval(plan, phrase),
        )

    def _on_live_approval(self, plan: LiveOrderPlan, phrase: str | None) -> None:
        if phrase is None:
            return
        self.run_worker(self._submit_live_plan(plan, phrase), exclusive=False)

    async def _load_open_orders(self, account_seq: int, symbol: str) -> OpenOrdersPage:
        if self.open_orders_loader is not None:
            return await self.open_orders_loader(account_seq, symbol)
        if self.client is None:
            raise RuntimeError("client-unavailable")
        return await self.client.open_orders(account_seq, symbol)

    async def _load_access_token(self) -> str:
        if self.access_token_loader is not None:
            return await self.access_token_loader()
        if self.client is None:
            raise RuntimeError("client-unavailable")
        return await self.client.access_token()

    def _revalidate_live_preview(
        self, plan: LiveOrderPlan, context: AccountContext
    ) -> OrderPreview:
        """Re-run paper risk checks against a fresh account snapshot."""

        intent = plan.intent
        if (
            context.scope != "account_read_only"
            or context.order_endpoints_called is not False
            or context.account.account_seq != intent.account_seq
            or context.account.masked_account_no != intent.masked_account_no
            or context.symbol != intent.symbol
            or context.buying_power.currency != intent.currency
        ):
            raise OrderPreviewError("실행 직전 계좌 컨텍스트가 계획과 일치하지 않습니다.")
        service = (
            self.paper_preview_service_factory()
            if self.paper_preview_service_factory is not None
            else PaperPreviewService()
        )
        fresh = service.create_preview(
            account_no=context.account.masked_account_no,
            account_seq=context.account.account_seq,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=canonical_decimal_text(intent.quantity),
            reference_last_price=canonical_decimal_text(intent.reference_last_price),
            holding_quantity=canonical_decimal_text(context.holding_quantity),
            cash_buying_power=canonical_decimal_text(context.buying_power.cash_buying_power),
            limit_price=(
                None if intent.limit_price is None else canonical_decimal_text(intent.limit_price)
            ),
            market=intent.market,
            currency=intent.currency,
        )
        if fresh.fingerprint != plan.preview_fingerprint:
            raise OrderPreviewError("실행 직전 미리보기 지문이 계획과 일치하지 않습니다.")
        return fresh

    async def _submit_live_plan(self, plan: LiveOrderPlan, approval_phrase: str) -> None:
        """Fail-closed live path: fresh reads, audit preflight, then one POST in a thread."""

        if self._live_submit_lock.locked():
            self.notify(
                "이미 라이브 제출을 처리 중입니다.",
                title="LIVE ORDER BLOCKED",
                severity="warning",
            )
            return
        async with self._live_submit_lock:
            packet = build_live_packet(plan)
            if approval_phrase != live_approval_phrase(packet):
                self.notify(
                    "승인 문구가 정확히 일치하지 않아 주문을 전송하지 않았습니다.",
                    title="LIVE ORDER BLOCKED",
                    severity="error",
                )
                return
            if (
                not self.manual_live_orders
                or os.environ.get(MANUAL_LIVE_ENV_KEY) != MANUAL_LIVE_ENV_VALUE
            ):
                self.notify(
                    "라이브 주문 실행 옵션과 환경 게이트가 모두 필요합니다.",
                    title="LIVE ORDER BLOCKED",
                    severity="error",
                )
                return
            if datetime.now(UTC) > plan.expires_at or self.symbol != plan.intent.symbol:
                self.notify(
                    "계획이 만료되었거나 종목이 변경되어 주문을 전송하지 않았습니다.",
                    title="LIVE ORDER BLOCKED",
                    severity="error",
                )
                return
            if self._interpretation_is_stale():
                # Fail-closed before any account/token/order call: a stale or
                # degraded current quote is not a safe basis for a live order.
                self.notify(
                    "시세 신선도가 저하되어 주문을 전송하지 않았습니다.",
                    title="LIVE ORDER BLOCKED",
                    severity="error",
                )
                return
            if plan.preview_fingerprint in self._live_attempted_fingerprints:
                self.notify(
                    "이 주문 계획은 이미 제출을 시도했습니다. 재시도하지 않습니다.",
                    title="LIVE ORDER BLOCKED",
                    severity="error",
                )
                return

            try:
                fresh_context = await self._load_account_context(plan.intent.symbol)
                self._revalidate_live_preview(plan, fresh_context)
                open_page = await self._load_open_orders(
                    plan.intent.account_seq, plan.intent.symbol
                )
                duplicates = find_open_order_duplicates(
                    open_page.orders, plan.intent.symbol, plan.intent.side.value
                )
                if duplicates:
                    self.notify(
                        "같은 종목·방향의 미체결 주문이 있어 새 주문을 차단했습니다.",
                        title="LIVE ORDER BLOCKED",
                        severity="error",
                    )
                    return
                await asyncio.to_thread(self.live_audit_log.prepare)
                access_token = await self._load_access_token()
            except Exception:
                self.notify(
                    "실행 직전 안전 점검에 실패해 주문을 전송하지 않았습니다.",
                    title="LIVE ORDER BLOCKED",
                    severity="error",
                )
                return

            # Reserve before crossing the mutation boundary. Any later uncertainty is no-retry.
            self._live_attempted_fingerprints.add(plan.preview_fingerprint)
            cancellation = _CancellationState()
            try:
                outcome, audit_records, close_failed = await _to_thread_uninterruptibly(
                    cancellation,
                    _submit_live_plan_once,
                    plan,
                    approval_phrase,
                    access_token,
                    plan.intent.account_seq,
                    self.live_transport_factory,
                )
            except Exception:
                self.notify(
                    "제출 결과를 확인할 수 없습니다. 재시도하지 말고 주문 내역을 확인하세요.",
                    title="LIVE ORDER AMBIGUOUS",
                    severity="error",
                )
                return

            audit_failed = False
            for record in audit_records:
                try:
                    await _to_thread_uninterruptibly(
                        cancellation, self.live_audit_log.append, record
                    )
                except LiveAuditLogError:
                    audit_failed = True
            self.last_live_outcome = outcome

            if close_failed:
                self.notify(
                    "주문 결과는 보존됐지만 전송 연결 정리에 실패했습니다.",
                    title="TRANSPORT CLOSE",
                    severity="warning",
                )

            if isinstance(outcome, LiveOrderAccepted):
                self.notify(
                    "주문이 브로커에 접수되었습니다. 체결 완료를 의미하지 않습니다.",
                    title="LIVE ORDER ACCEPTED",
                    severity="information",
                )
            elif isinstance(outcome, LiveOrderAmbiguous):
                self.notify(
                    "제출 결과가 불명확합니다. 절대 재시도하지 말고 주문 내역을 확인하세요.",
                    title="LIVE ORDER AMBIGUOUS",
                    severity="error",
                )
            else:
                self.notify(
                    "주문이 차단되거나 거절되었습니다. 자동 재시도하지 않습니다.",
                    title="LIVE ORDER REJECTED",
                    severity="error",
                )
            if audit_failed:
                self.notify(
                    "감사로그 기록에 실패했습니다. 재시도하지 말고 상태를 직접 확인하세요.",
                    title="AUDIT FAILURE",
                    severity="error",
                )

            if cancellation.requested:
                # The broker outcome and every available audit record are now
                # durable. Honour Textual's original quit/cancel request before
                # starting optional read-only reconciliation.
                raise asyncio.CancelledError

            # Read-only reconciliation only; never reinterpret acceptance as a fill.
            try:
                await self._load_account_context(plan.intent.symbol)
                await self._load_open_orders(plan.intent.account_seq, plan.intent.symbol)
            except Exception:
                self.notify(
                    "접수 후 계좌/주문 재조회에 실패했습니다. 체결 여부를 직접 확인하세요.",
                    title="RECONCILIATION",
                    severity="warning",
                )

    async def _add_watchlist_symbol(self, raw_symbol: str | None) -> None:
        if raw_symbol is None:
            return
        if self.settings_path is None:
            self.notify(
                "설정 경로가 없어 관심 종목을 저장할 수 없습니다.",
                title="WATCHLIST",
                severity="warning",
            )
            return
        try:
            normalized = normalize_symbol(raw_symbol)
        except ValueError:
            self.notify(
                "심볼 형식이 올바르지 않습니다.",
                title="WATCHLIST",
                severity="warning",
            )
            return

        if self.client is not None:
            try:
                stock = await self.client.stock(normalized)
            except TossApiError as exc:
                message = (
                    f"종목을 찾을 수 없습니다: {normalized}"
                    if exc.status_code == 404
                    else "공식 API에서 종목을 확인하지 못했습니다."
                )
                self.notify(message, title="WATCHLIST", severity="warning")
                return
            except Exception:
                self.notify(
                    "공식 API에서 종목을 확인하지 못했습니다.",
                    title="WATCHLIST",
                    severity="warning",
                )
                return
            if stock.symbol != normalized:
                self.notify(
                    "공식 API의 종목 응답이 일치하지 않습니다.",
                    title="WATCHLIST",
                    severity="warning",
                )
                return

        try:
            store = SettingsStore(self.settings_path)
            current = store.load()
            updated, created = with_watchlist_symbol(current, normalized)
            if created:
                store.save(updated)
        except SettingsError as exc:
            self.notify(str(exc)[:120], title="WATCHLIST", severity="warning")
            return
        except OSError:
            self.notify(
                "관심 종목 설정을 안전하게 저장하지 못했습니다.",
                title="WATCHLIST",
                severity="error",
            )
            return

        self.settings = updated
        self.watchlist_symbols = self._watchlist_with_active(updated.watchlist)
        self._render_watchlist()
        if self.client is not None:
            await self._refresh_watchlist_prices()
        if normalized in self.watchlist_symbols:
            self._move_cursor(self.watchlist_symbols.index(normalized))
        message = f"추가됨: {normalized}" if created else f"이미 등록됨: {normalized}"
        self.notify(message, title="WATCHLIST", severity="information")

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
        watchlist.add_column("PRICE", width=9)
        watchlist.add_column("A", width=2)
        orderbook = self.query_one("#orderbook", DataTable)
        orderbook.add_column("SIDE", width=5)
        orderbook.add_column("PRICE", width=7)
        orderbook.add_column("SIZE", width=5)
        orderbook.add_column("DEPTH", width=8)
        trades = self.query_one("#trades", DataTable)
        trades.add_column("TIME", width=8)
        trades.add_column("PRICE", width=9)
        trades.add_column("SIZE", width=7)
        trades.add_column("", width=1)

    async def _load_persisted_watchlist(self) -> None:
        """Merge the persisted watchlist into memory without persisting anything."""
        if self.settings_path is None:
            return
        try:
            persisted = SettingsStore(self.settings_path).load()
            self.watchlist_symbols = self._watchlist_with_active(persisted.watchlist)
            self.settings = persisted
        except Exception:
            self.watchlist_stale = True

    def _watchlist_with_active(self, persisted: tuple[str, ...]) -> tuple[str, ...]:
        configured = list(dict.fromkeys(persisted))
        if self.symbol and self.symbol not in configured:
            if len(configured) >= 12:
                configured = configured[:11]
            configured.append(self.symbol)
        return tuple(configured)

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
        if self.snapshot is None or self.current_price is None or not self.symbol:
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
            alert_marker = "•" if row is not None and row.active_alerts else " "
            count = str(row.active_alerts) if row is not None else "0"
            price_text = f"{row.price} {row.currency}" if row is not None else "—"
            if row is None or self.watchlist_stale:
                price_text = f"{price_text}*" if self.watchlist_stale else price_text
            # Text marker distinct from the movable DataTable row cursor.
            active_prefix = ">" if symbol == self.symbol else " "
            table.add_row(
                f"{active_prefix}{symbol}",
                Text(f" {price_text}", style=MUTED_COLOR),
                f"{alert_marker}{count}",
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
        if not self.symbol:
            return ()
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

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Route the watchlist table's consumed Enter key to symbol selection."""
        if event.data_table.id != "watchlist":
            return
        event.stop()
        await self.action_watch_select()

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
            if self.is_mounted:
                self.screen.set_class(False, "symbol-picker")
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
            self.last_orderbook_monotonic = None
            self.last_sync_monotonic = None
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
            if self.is_mounted:
                self._render_watchlist()

            if self.client is None:
                self.connection_state = "PREVIEW" if not self.connect_live else "SELECTED"
                self._render_chrome()
                return
            await self._refresh_snapshot()
            self.feed_task = asyncio.create_task(
                self._run_feed(symbol=normalized, market=self.market)
            )
            if self.connect_live and self.candle_sync_task is None:
                self.candle_sync_task = asyncio.create_task(self._run_candle_resync())

    async def _refresh_snapshot(self) -> bool:
        async with self.refresh_lock:
            return await self._refresh_snapshot_locked()

    async def _refresh_snapshot_locked(self) -> bool:
        if self.client is None or not self.symbol:
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
        self.last_sync_monotonic = time.monotonic()
        self.indicator_degraded = False
        self.candle_sync_degraded = False
        if was_live:
            self.connection_state = "LIVE"
            self.connection_detail = self.subscription_detail or previous_detail
            self.protocol_degraded = False
        self._render_all()
        return True

    def _tick_monotonic_for_timestamp(self, timestamp: str | None) -> float | None:
        """Truthful monotonic tick time for a provider timestamp, or ``None`` if untrustworthy.

        The provider timestamp is anchored against injected UTC "now" to compute
        an age, then that age is subtracted from the local monotonic clock so
        staleness checks reflect the provider's own clock, not wall-clock receipt
        time. Missing, naive, invalid, or implausibly-future timestamps must never
        be trusted as a fresh observation.
        """
        if not timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        now = self.utc_now()
        if now.tzinfo is None or now.utcoffset() is None:
            return None
        age_seconds = (now - parsed).total_seconds()
        if age_seconds < -MAX_FUTURE_PRICE_SKEW_SECONDS:
            return None
        return time.monotonic() - max(age_seconds, 0.0)

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
        newest_timestamp = (
            latest_trade.timestamp if latest_trade is not None else snapshot.price.timestamp
        )
        self.current_timestamp = newest_timestamp
        # A REST snapshot (initial load or resync) is itself a fresh price
        # observation, so it establishes/renews the TICK clock. Orderbook-only
        # traffic must never do this (see `last_orderbook_monotonic`). The clock
        # is anchored to the provider's own timestamp, not receipt time, so a
        # stale snapshot cannot masquerade as fresh.
        self.last_tick_monotonic = self._tick_monotonic_for_timestamp(newest_timestamp)
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
            self.last_sync_monotonic = time.monotonic()
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
        if not feed_symbol:
            return
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
                event_tick = self._tick_monotonic_for_timestamp(event.trade.timestamp)
                is_fresh = event_tick is not None and (
                    self.last_tick_monotonic is None or event_tick >= self.last_tick_monotonic
                )
                candle_status: LiveCandleStatus | None = None
                if is_fresh:
                    try:
                        candle_status = self._record_live_trade(event.trade)
                    except ValueError:
                        candle_status = None
                        self._set_candle_sync_degraded("WS live · candle update failed")
                self.trades.appendleft(event.trade)
                if is_fresh:
                    self.last_tick_monotonic = event_tick
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
                self.last_orderbook_monotonic = time.monotonic()
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
        # Two lines total: identity, then price/change/high-low-volume. "READ ONLY"
        # already appears in the topbar, so it is not repeated here.
        text.append(f"{self.symbol}  ", style="bold white")
        text.append(
            f"{self.snapshot.stock.name} · {self.snapshot.stock.market}\n", style=MUTED_COLOR
        )
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
        if metrics.day_high is not None and metrics.day_low is not None:
            text.append(
                f"   HIGH {format_decimal(metrics.day_high, self.current_currency)}   "
                f"LOW {format_decimal(metrics.day_low, self.current_currency)}",
                style=MUTED_COLOR,
            )
        if metrics.day_volume is not None:
            text.append(f"   VOL {format_decimal(metrics.day_volume)}", style=MUTED_COLOR)
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
                Text(f" {format_decimal(trade.price, trade.currency)}", style=style),
                format_decimal(trade.volume),
                Text(marker, style=style),
            )

    def _holding_average_overlay(self) -> HoldingAveragePriceOverlay | None:
        """Active held-symbol average-price overlay, re-derived fresh on every call.

        Never cached across renders or symbols: sourced only from
        ``self.snapshot``'s own symbol/currency and the current
        ``self.portfolio_snapshot``, so a symbol switch or a stale/failed
        portfolio refresh is reflected immediately with no extra network call
        and no independent overlay state to fall out of sync.
        """
        if self.snapshot is None or self.portfolio_snapshot is None:
            return None
        item = self.portfolio_snapshot.holdings.find_item(self.snapshot.stock.symbol)
        if item is None:
            return None
        if item.quantity <= 0 or item.average_purchase_price <= 0:
            return None
        if item.currency != self.snapshot.price.currency:
            return None
        return HoldingAveragePriceOverlay(
            price=item.average_purchase_price, stale=self.portfolio_stale
        )

    def _render_chart(self) -> None:
        if self.snapshot is None:
            return
        try:
            chart_content = self.query_one("#chart-content", Static)
        except NoMatches:
            return  # main chart isn't the active screen (e.g. portfolio modal open)
        if not chart_content.is_mounted:
            return
        width, height = chart_content.content_size
        previous_close = market_metrics(
            self.snapshot,
            self.current_price,
            orderbook=self.orderbook,
            trades=tuple(self.trades),
            current_timestamp=self.current_timestamp,
        ).previous_close
        chart_content.update(
            chart_renderable(
                self.snapshot,
                self.chart_mode,
                width,
                height,
                current_price=self.current_price,
                previous_close=previous_close,
                holding_average=self._holding_average_overlay(),
            )
        )
        title = f"MARKET CHART · {CHART_MODE_LABELS[self.chart_mode]}"
        try:
            self.query_one("#chart-panel .panel-title", Static).update(title)
        except NoMatches:
            return  # same race as above; the chart body already re-rendered

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
        imbalance = (
            f"{signals.orderbook_imbalance_percent:.1f}%"
            if signals.orderbook_imbalance_percent is not None
            else "—"
        )
        pressure = (
            f"{signals.trade_pressure_percent:.1f}%"
            if signals.trade_pressure_percent is not None
            else "—"
        )
        volume_ratio = format_multiple(signals.volume_spike_ratio)
        book_ratio = format_multiple(signals.bid_ask_ratio)
        timeframe_label = TIMEFRAME_LABELS_KO[self.chart_mode]
        try:
            indicators = self._chart_indicators()
        except (ValueError, ArithmeticError) as exc:
            # Fail-safe boundary: a bad candle (malformed/naive timestamp, currency
            # mismatch) must never kill the live feed task. Render the base stats
            # above, mark the connection truthfully DEGRADED with a sanitized
            # detail, and recover only after a subsequent successful REST snapshot
            # (see `_refresh_snapshot_locked`), not on the next tick.
            self.indicator_degraded = True
            self.connection_state = "DEGRADED"
            self.connection_detail = safe_indicator_error(exc)
            lines = (
                f"시장 해석 · {timeframe_label} 데이터 부족 · 신뢰도 분석 불가",
                f"근거 · {self.connection_detail}",
                "주의 · 지표 계산이 복구될 때까지 방향 해석 보류",
                "조건 · 가격대 시나리오 데이터 부족",
                (
                    f"호가 {orderbook_signal_label_ko(signals.orderbook_imbalance_percent)} "
                    f"{imbalance}"
                ),
                (
                    f"체결 {trade_pressure_label_ko(signals.trade_pressure_percent)} "
                    f"{pressure} · 1분량 {volume_ratio}"
                ),
                "EMA9/21 — · RSI —",
                "VWAP — · 지지 — · 저항 —",
                f"{self.market.upper()} 공개 시세 · 관찰 전용 · i 상세",
            )
            for index, line in enumerate(lines):
                text.append(
                    _bounded_cells(line) + ("\n" if index < len(lines) - 1 else ""),
                    style="#f0ad4e" if index < 4 else MUTED_COLOR,
                )
        else:
            analysis = interpret_timeframe(
                indicators,
                self.current_price,
                self.current_currency,
                signals=signals,
                stale=self._interpretation_is_stale(),
            )
            rsi_text = f"{indicators.rsi:.1f}" if indicators.rsi is not None else "—"
            vwap_percent = vwap_distance_percent(indicators.vwap, self.current_price)
            if indicators.vwap is not None and vwap_percent is not None:
                vwap_segment = f"VWAP 현재가 {format_percent(vwap_percent)}"
            else:
                vwap_segment = "VWAP 데이터 부족"
            reason = " · ".join(analysis.evidence[:2]) or "방향 근거 데이터 부족"
            risk = analysis.risks[0] if analysis.risks else "확인된 주요 반대 신호 없음"
            if analysis.headline in ("하락 우세", "조정 진행"):
                scenario = analysis.downside_scenario or "가격대 시나리오 데이터 부족"
            else:
                scenario = analysis.upside_scenario or "가격대 시나리오 데이터 부족"
            spread = (
                format_decimal(metrics.spread, self.current_currency)
                if metrics.spread is not None
                else "—"
            )
            support_value = (
                format_decimal(indicators.levels.support.price, self.current_currency)
                if indicators.levels.support is not None
                else "—"
            )
            resistance_value = (
                format_decimal(indicators.levels.resistance.price, self.current_currency)
                if indicators.levels.resistance is not None
                else "—"
            )
            lines = (
                f"시장 해석 · {timeframe_label} {analysis.headline} · 신뢰도 {analysis.confidence}",
                f"근거 · {reason}",
                f"주의 · {risk}",
                f"조건 · {scenario}",
                (
                    f"호가 {orderbook_signal_label_ko(signals.orderbook_imbalance_percent)} "
                    f"{imbalance} · 잔량비 {book_ratio} · 차이 {spread}"
                ),
                (
                    f"체결 {trade_pressure_label_ko(signals.trade_pressure_percent)} "
                    f"{pressure} · 1분량 {volume_ratio}"
                ),
                (
                    f"EMA9/21 {ema_relation_label_ko(indicators.ema_short, indicators.ema_long)} "
                    f"· RSI {rsi_text} {rsi_zone_label_ko(indicators.rsi)}"
                ),
                f"{vwap_segment} · 지지 {support_value} · 저항 {resistance_value}",
                f"{self.market.upper()} 공개 시세 · 관찰 전용 · i 상세",
            )
            for index, line in enumerate(lines):
                style = "bold #c9d1d9" if index == 0 else MUTED_COLOR
                if index == len(lines) - 1:
                    style = "#526273"
                text.append(
                    _bounded_cells(line) + ("\n" if index < len(lines) - 1 else ""),
                    style=style,
                )
        self.query_one("#market-stats", Static).update(text)

    def _render_chrome(self) -> None:
        now = datetime.now(KST).strftime("%H:%M:%S KST")
        live = self.stream_live
        state_color = connection_state_color(self.connection_state, live)
        top = Text()
        top.append("TOSS MARKET", style="bold white")
        if not self.manual_live_orders:
            top.append("   PAPER DEFAULT", style="#526273")
        elif os.environ.get(MANUAL_LIVE_ENV_KEY) == MANUAL_LIVE_ENV_VALUE:
            top.append("   MANUAL LIVE READY", style="#f28b82")
        else:
            top.append("   MANUAL LIVE BLOCKED (ENV OFF)", style="#f0ad4e")
        top.append(f"   {'●' if live else '○'} {self.connection_state}", style=state_color)
        top.append(f"   {now}", style=MUTED_COLOR)
        try:
            topbar = self.query_one("#topbar", Static)
        except NoMatches:
            return  # timer may race with Textual teardown
        topbar.update(top)

        # Compact two-line status: connection + freshness on line one, the latest
        # alert (if any) on line two. TICK/BOOK/SYNC ages use monotonic time and
        # read "—" (never a fabricated value) until each has actually landed.
        # TICK is the price observation clock only (trade tick or REST
        # snapshot) — BOOK (orderbook) traffic never advances it.
        status = Text()
        status.append(f"{'●' if live else '○'} {self.connection_state}", style=state_color)
        if self.connection_detail:
            status.append(f" · {self.connection_detail}", style=MUTED_COLOR)
        status.append(f"   TICK {format_age(self.last_tick_monotonic)}", style=MUTED_COLOR)
        status.append(f"   BOOK {format_age(self.last_orderbook_monotonic)}", style=MUTED_COLOR)
        status.append(f"   SYNC {format_age(self.last_sync_monotonic)}", style=MUTED_COLOR)
        if self.latest_alert is not None:
            status.append(
                f"\nALERT {self.latest_alert.rule.id} · {self.latest_alert.rule.symbol} · "
                f"{self.latest_alert.condition} · {self.latest_alert.observed_value}",
                style="#f0ad4e",
            )
        try:
            statusbar = self.query_one("#statusbar", Static)
        except NoMatches:
            return  # timer may race with Textual teardown
        statusbar.update(status)
