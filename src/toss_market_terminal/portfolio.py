"""Phase-1 read-only portfolio domain helpers and the Textual portfolio screen.

Boundary: this module never calls an order endpoint and never touches a raw
account number. Every value it renders comes from
:class:`toss_market_terminal.models.PortfolioSnapshot`, itself only ever
built by ``TossMarketClient.portfolio_snapshot`` (GET-only). The screen is
strictly read-only: no row selection, no ticket/approval flow.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar
from zoneinfo import ZoneInfo

from rich.cells import set_cell_size
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Static

from .models import HoldingsItem, OpenOrder, PortfolioSnapshot, find_open_order_duplicates
from .render import DOWN_COLOR, MUTED_COLOR, UP_COLOR, format_age, format_decimal, format_percent

#: Portfolio wall-clock timestamps are always shown in KST regardless of the
#: host machine's local timezone, so freshness reads the same for every operator.
_KST = ZoneInfo("Asia/Seoul")

#: Content width (cells) at/above which holdings/orders render one row per
#: line; below it every row renders as compact two-line text instead.
PORTFOLIO_WIDE_WIDTH_THRESHOLD = 100

#: Keys that must never leak from the portfolio modal into the app underneath.
#: ``enter`` is included deliberately: the screen has no row selection, so a
#: stray Enter must not silently switch the active market symbol.
_ISOLATION_KEYS: tuple[str, ...] = ("a", "b", "c", "enter", "j", "k", "m", "q", "s")


def _isolation_bindings() -> tuple[Binding, ...]:
    return tuple(Binding(key, "noop", show=False) for key in _ISOLATION_KEYS)


def sellable_quantity(item: HoldingsItem, open_orders: tuple[OpenOrder, ...]) -> Decimal:
    """Holding quantity minus reserved SELL orders for the same symbol, floored at 0.

    Display/risk-context only -- not an executable sell limit. Reuses
    :func:`find_open_order_duplicates`'s fail-closed terminal-status
    exclusion, so an active or unrecognized order status always reserves its
    remaining quantity (a conservative, never over-generous estimate).
    """
    reserved = sum(
        (
            order.remaining_quantity
            for order in find_open_order_duplicates(open_orders, item.symbol, "SELL")
        ),
        Decimal("0"),
    )
    return max(item.quantity - reserved, Decimal("0"))


def _pl_style(amount: Decimal) -> str:
    if amount > 0:
        return UP_COLOR
    if amount < 0:
        return DOWN_COLOR
    return MUTED_COLOR


def portfolio_header_text(
    snapshot: PortfolioSnapshot | None,
    *,
    stale: bool,
    error: str | None,
    synced_monotonic: float | None,
) -> Text:
    text = Text()
    text.append("포트폴리오", style="bold #d9e1e8")
    if snapshot is None:
        text.append("\n계좌 정보를 아직 불러오지 못했습니다.", style=MUTED_COLOR)
        if error:
            text.append(f"\n{error}", style="#f28b82")
        return text
    account = snapshot.account
    text.append(f"\n계좌 {account.masked_account_no} ({account.account_type})", style=MUTED_COLOR)
    text.append(
        f"\nKRW 매수가능 {format_decimal(snapshot.krw_buying_power.cash_buying_power, 'KRW')} KRW",
        style=MUTED_COLOR,
    )
    text.append(
        f"\nUSD 매수가능 {format_decimal(snapshot.usd_buying_power.cash_buying_power, 'USD')} USD",
        style=MUTED_COLOR,
    )
    status_style = "#f0ad4e" if stale else UP_COLOR
    # "FRESH", not "LIVE": this is a periodic REST snapshot, not the market
    # WebSocket's liveness -- the two must never be conflated in the UI.
    status_label = "ACCOUNT STALE" if stale else "ACCOUNT FRESH"
    synced_wall_clock = snapshot.synced_at.astimezone(_KST).strftime("%m/%d %H:%M:%S")
    text.append(
        f"\n{status_label} · 동기화 {synced_wall_clock} KST · {format_age(synced_monotonic)} 전",
        style=status_style,
    )
    if stale and error:
        text.append(f" · {error}", style="#f28b82")
    return text


def holdings_section_text(snapshot: PortfolioSnapshot, width: int) -> Text:
    text = Text()
    text.append("보유 종목\n", style="bold #c9d1d9")
    items = snapshot.holdings.items
    if not items:
        text.append("보유 종목 없음\n", style=MUTED_COLOR)
        return text
    orders = snapshot.open_orders.orders
    wide = width >= PORTFOLIO_WIDE_WIDTH_THRESHOLD
    for item in items:
        sellable = sellable_quantity(item, orders)
        pl_style = _pl_style(item.profit_loss.amount_after_cost)
        pl_line = (
            f"손익 {format_decimal(item.profit_loss.amount_after_cost, item.currency)} "
            f"({format_percent(item.profit_loss.rate_after_cost * 100)}) {item.currency}"
        )
        if wide:
            # ``item.name`` may be Korean (double-cell characters); pad/truncate
            # by display-cell width, not Python string length, or CJK names
            # misalign or silently overrun the fixed-width columns.
            name_cell = set_cell_size(item.name, 12)
            text.append(
                f"{item.symbol:<8} {name_cell} "
                f"수량 {format_decimal(item.quantity):>8} "
                f"매도가능 {format_decimal(sellable):>8} "
                f"평단 {format_decimal(item.average_purchase_price, item.currency):>10} "
                f"현재가 {format_decimal(item.last_price, item.currency):>10} "
                f"매입 {format_decimal(item.market_value.purchase_amount, item.currency):>12} "
                f"평가 {format_decimal(item.market_value.amount_after_cost, item.currency):>12}  ",
                style="#d9e1e8",
            )
            text.append(pl_line + "\n", style=pl_style)
        else:
            text.append(
                f"{item.symbol} {item.name} · 수량 {format_decimal(item.quantity)} · "
                f"매도가능 {format_decimal(sellable)}\n",
                style="#d9e1e8",
            )
            text.append(
                f"  평단 {format_decimal(item.average_purchase_price, item.currency)} · "
                f"현재가 {format_decimal(item.last_price, item.currency)} · "
                f"매입 {format_decimal(item.market_value.purchase_amount, item.currency)} · "
                f"평가 {format_decimal(item.market_value.amount_after_cost, item.currency)} "
                f"{item.currency}\n",
                style=MUTED_COLOR,
            )
            text.append(f"  {pl_line}\n", style=pl_style)
    return text


def open_orders_section_text(snapshot: PortfolioSnapshot, width: int) -> Text:
    text = Text()
    text.append("미체결 주문\n", style="bold #c9d1d9")
    orders = snapshot.open_orders.orders
    if not orders:
        text.append("미체결 주문 없음\n", style=MUTED_COLOR)
        return text
    wide = width >= PORTFOLIO_WIDE_WIDTH_THRESHOLD
    for order in orders:
        side_style = UP_COLOR if order.side == "BUY" else DOWN_COLOR
        # Literal, not a guessed label: MARKET orders carry no price at all
        # (official schema), so show exactly that instead of a fabricated
        # "market price" string that could be mistaken for a real value.
        price_label = "-" if order.price is None else format_decimal(order.price)
        if wide:
            text.append(f"{order.side:<4} ", style=side_style)
            text.append(
                f"{order.symbol:<8} "
                f"총수량 {format_decimal(order.quantity):>8} "
                f"체결 {format_decimal(order.filled_quantity):>8} "
                f"잔량 {format_decimal(order.remaining_quantity):>8} "
                f"{order.order_type:<7} {price_label:>10}  {order.status}\n",
                style="#d9e1e8",
            )
        else:
            text.append(f"{order.side} {order.symbol}", style=side_style)
            text.append(
                f" · {order.order_type} {price_label} · {order.status}\n", style=MUTED_COLOR
            )
            text.append(
                f"  총수량 {format_decimal(order.quantity)} · "
                f"체결 {format_decimal(order.filled_quantity)} · "
                f"잔량 {format_decimal(order.remaining_quantity)}\n",
                style=MUTED_COLOR,
            )
    return text


def _per_currency_totals(
    items: tuple[HoldingsItem, ...],
) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    """(purchase, market value after cost, P/L after cost) summed per currency.

    Computed directly from each item's own currency-consistent fields --
    never from the overview's single blended ``rate``/``rateAfterCost``,
    which would silently imply an FX conversion between KRW and USD that
    this client never performs.
    """
    totals: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    for item in items:
        purchase, market, pl = totals.get(item.currency, (Decimal("0"), Decimal("0"), Decimal("0")))
        totals[item.currency] = (
            purchase + item.market_value.purchase_amount,
            market + item.market_value.amount_after_cost,
            pl + item.profit_loss.amount_after_cost,
        )
    return totals


def totals_section_text(snapshot: PortfolioSnapshot) -> Text:
    text = Text()
    text.append("합계 (통화별 개별 계산 · 환산 없음)\n", style="bold #c9d1d9")
    totals = _per_currency_totals(snapshot.holdings.items)
    if not totals:
        text.append("보유 종목 없음", style=MUTED_COLOR)
        return text
    lines: list[tuple[str, Decimal, Decimal, Decimal | None]] = []
    for currency in sorted(totals):
        purchase, market, pl = totals[currency]
        rate = (pl / purchase * Decimal("100")) if purchase != 0 else None
        lines.append((currency, market, pl, rate))
    for index, (currency, market, pl, rate) in enumerate(lines):
        rate_text = format_percent(rate) if rate is not None else "—"
        line = (
            f"{currency} 평가금액 {format_decimal(market, currency)} · "
            f"{currency} 평가손익 {format_decimal(pl, currency)} ({rate_text})"
        )
        if index:
            text.append("\n")
        text.append(line, style=_pl_style(pl))
    return text


def portfolio_body_text(snapshot: PortfolioSnapshot | None, width: int) -> Text:
    if snapshot is None:
        return Text("계좌 정보를 아직 불러오지 못했습니다.", style=MUTED_COLOR)
    text = Text()
    text.append_text(holdings_section_text(snapshot, width))
    text.append("\n")
    text.append_text(open_orders_section_text(snapshot, width))
    text.append("\n")
    text.append_text(totals_section_text(snapshot))
    return text


class PortfolioScreen(ModalScreen[None]):
    """Read-only phase-1 portfolio view. No row selection, no order endpoints."""

    BINDINGS: ClassVar = (
        Binding("escape", "close", "닫기"),
        Binding("p", "close", "닫기", show=False),
        Binding("r", "refresh_account", "계좌 새로고침"),
        *_isolation_bindings(),
    )

    CSS = """
    PortfolioScreen {
        align: center middle;
        background: rgba(4, 7, 10, 0.85);
    }
    #portfolio-dialog {
        width: 96%;
        height: 92%;
        padding: 1 2;
        border: solid #607080;
        background: #0d131a;
    }
    #portfolio-header {
        height: auto;
        color: #d9e1e8;
    }
    #portfolio-body {
        height: 1fr;
        margin-top: 1;
        scrollbar-background: #0b1118;
        scrollbar-color: #3a4654;
    }
    #portfolio-content {
        height: auto;
        color: #d9e1e8;
    }
    #portfolio-help {
        height: 1;
        color: #526273;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="portfolio-dialog"):
            yield Static(id="portfolio-header", markup=False)
            with VerticalScroll(id="portfolio-body"):
                yield Static(id="portfolio-content", markup=False)
            yield Static(
                "Esc/p 닫기 · r 계좌 새로고침 · scope=account_read_only · 주문 API 호출 없음",
                id="portfolio-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self.call_after_refresh(self.refresh_view)

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_view()

    def refresh_view(self) -> None:
        try:
            header = self.query_one("#portfolio-header", Static)
            content = self.query_one("#portfolio-content", Static)
        except NoMatches:
            return  # a resize/refresh call raced with screen teardown
        snapshot: PortfolioSnapshot | None = getattr(self.app, "portfolio_snapshot", None)
        stale = bool(getattr(self.app, "portfolio_stale", False))
        error = getattr(self.app, "portfolio_error", None)
        synced_monotonic = getattr(self.app, "portfolio_synced_monotonic", None)
        header.update(
            portfolio_header_text(
                snapshot, stale=stale, error=error, synced_monotonic=synced_monotonic
            )
        )
        width = content.size.width or self.size.width
        content.update(portfolio_body_text(snapshot, width))

    def action_noop(self) -> None:
        """모달 격리용 no-op. 어떤 상태도 바꾸지 않는다."""

    def action_close(self) -> None:
        self.dismiss(None)

    def action_refresh_account(self) -> None:
        refresh = getattr(self.app, "_refresh_portfolio", None)
        if refresh is None:
            return
        self.run_worker(refresh(), exclusive=False)


__all__ = [
    "PORTFOLIO_WIDE_WIDTH_THRESHOLD",
    "PortfolioScreen",
    "holdings_section_text",
    "open_orders_section_text",
    "portfolio_body_text",
    "portfolio_header_text",
    "sellable_quantity",
    "totals_section_text",
]
