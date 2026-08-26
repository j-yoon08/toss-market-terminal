"""Read-only portfolio domain helpers and the Textual portfolio screen.

Boundary: this module never calls an order endpoint and never touches a raw
account number. Every value it renders comes from
:class:`toss_market_terminal.models.PortfolioSnapshot`, itself only ever
built by ``TossMarketClient.portfolio_snapshot`` (GET-only). The screen is
strictly read-only: no row selection, no ticket/approval flow.
"""

from __future__ import annotations

from datetime import UTC, datetime
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

from .models import (
    ClosedOrdersPage,
    ExchangeRate,
    HoldingsItem,
    OpenOrder,
    PortfolioSnapshot,
    find_open_order_duplicates,
)
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


def portfolio_weight(item: HoldingsItem, holdings: tuple[HoldingsItem, ...]) -> Decimal | None:
    """Position weight within stock market value of the same currency.

    Buying power is not cash balance and is deliberately excluded. A negative
    or non-positive provider value cannot produce a trustworthy percentage.
    """
    value = item.market_value.amount_after_cost
    same_currency = tuple(holding for holding in holdings if holding.currency == item.currency)
    if value < 0 or any(holding.market_value.amount_after_cost < 0 for holding in same_currency):
        return None
    total = sum(
        (holding.market_value.amount_after_cost for holding in same_currency),
        Decimal("0"),
    )
    if total <= 0:
        return None
    return value / total * Decimal("100")


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
    text.append("보유 종목 · 비중은 통화별 주식 평가액 기준\n", style="bold #c9d1d9")
    items = snapshot.holdings.items
    if not items:
        text.append("보유 종목 없음\n", style=MUTED_COLOR)
        return text
    orders = snapshot.open_orders.orders
    wide = width >= PORTFOLIO_WIDE_WIDTH_THRESHOLD
    for item in items:
        sellable = sellable_quantity(item, orders)
        weight = portfolio_weight(item, items)
        weight_text = "—" if weight is None else f"{weight:.1f}%"
        name = set_cell_size(item.name, 12) if wide else item.name
        text.append(
            f"{item.symbol} {name} · 수량 {format_decimal(item.quantity)} · "
            f"매도가능 {format_decimal(sellable)} · 비중 {weight_text}\n",
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
        text.append(
            f"  평가손익 {format_decimal(item.profit_loss.amount_after_cost, item.currency)} "
            f"({format_percent(item.profit_loss.rate_after_cost * 100)})",
            style=_pl_style(item.profit_loss.amount_after_cost),
        )
        text.append(" · ", style=MUTED_COLOR)
        text.append(
            f"오늘 {format_decimal(item.daily_profit_loss.amount, item.currency)} "
            f"({format_percent(item.daily_profit_loss.rate * 100)}) {item.currency}\n",
            style=_pl_style(item.daily_profit_loss.amount),
        )
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
) -> dict[str, tuple[Decimal, Decimal, Decimal, Decimal]]:
    """(purchase, market, total P/L, daily P/L) summed per currency."""
    totals: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    zero = (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
    for item in items:
        purchase, market, pl, daily = totals.get(item.currency, zero)
        totals[item.currency] = (
            purchase + item.market_value.purchase_amount,
            market + item.market_value.amount_after_cost,
            pl + item.profit_loss.amount_after_cost,
            daily + item.daily_profit_loss.amount,
        )
    return totals


def totals_section_text(snapshot: PortfolioSnapshot) -> Text:
    text = Text()
    text.append("합계 (통화별 개별 계산 · 환산 없음)\n", style="bold #c9d1d9")
    totals = _per_currency_totals(snapshot.holdings.items)
    if not totals:
        text.append("보유 종목 없음", style=MUTED_COLOR)
        return text
    lines = sorted(totals.items())
    for index, (currency, values) in enumerate(lines):
        purchase, market, pl, daily = values
        total_rate = (pl / purchase * Decimal("100")) if purchase != 0 else None
        total_rate_text = format_percent(total_rate) if total_rate is not None else "—"
        if index:
            text.append("\n")
        text.append(
            f"{currency} 평가금액 {format_decimal(market, currency)} · "
            f"{currency} 평가손익 {format_decimal(pl, currency)} ({total_rate_text})",
            style=_pl_style(pl),
        )
        text.append(" · ", style=MUTED_COLOR)
        text.append(
            f"오늘손익 {format_decimal(daily, currency)}",
            style=_pl_style(daily),
        )
    return text


def exchange_rate_section_text(
    snapshot: PortfolioSnapshot,
    exchange_rate: ExchangeRate | None,
    *,
    stale: bool,
    error: str | None,
    synced_monotonic: float | None,
    now: datetime | None = None,
) -> Text:
    text = Text()
    text.append("참고 원화환산 · 주식 평가액만 포함\n", style="bold #c9d1d9")
    if exchange_rate is None:
        text.append("환율을 아직 불러오지 못했습니다.", style=MUTED_COLOR)
        if error:
            text.append(f" · {error}", style="#f28b82")
        return text
    current = now or datetime.now(UTC)
    validity_stale = not (exchange_rate.valid_from <= current < exchange_rate.valid_until)
    is_stale = stale or validity_stale
    totals = _per_currency_totals(snapshot.holdings.items)
    krw = totals.get("KRW", (Decimal("0"),) * 4)
    usd = totals.get("USD", (Decimal("0"),) * 4)
    mid = exchange_rate.mid_rate
    converted_market = krw[1] + usd[1] * mid
    converted_pl = krw[2] + usd[2] * mid
    converted_daily = krw[3] + usd[3] * mid
    valid_from = exchange_rate.valid_from.astimezone(_KST).strftime("%m/%d %H:%M:%S")
    status = "FX STALE" if is_stale else "FX FRESH"
    text.append(
        f"USD 1 = {format_decimal(mid, 'KRW')} KRW · 매매기준율 · {valid_from} KST · "
        f"{status} · {format_age(synced_monotonic)} 전\n",
        style="#f0ad4e" if is_stale else MUTED_COLOR,
    )
    text.append(
        f"환산 평가액 {format_decimal(converted_market, 'KRW')} KRW · ",
        style="#d9e1e8",
    )
    text.append(
        f"평가손익 {format_decimal(converted_pl, 'KRW')} · ",
        style=_pl_style(converted_pl),
    )
    text.append(
        f"오늘손익 {format_decimal(converted_daily, 'KRW')}",
        style=_pl_style(converted_daily),
    )
    if is_stale and error:
        text.append(f" · {error}", style="#f28b82")
    return text


def closed_orders_section_text(
    page: ClosedOrdersPage | None,
    *,
    stale: bool,
    error: str | None,
    synced_monotonic: float | None,
) -> Text:
    text = Text()
    status = "ORDER HISTORY STALE" if stale else "ORDER HISTORY FRESH"
    text.append("최근 종료 주문 · 최근 30일 · 최대 20건\n", style="bold #c9d1d9")
    if page is None:
        text.append("주문내역을 아직 불러오지 못했습니다.", style=MUTED_COLOR)
        if error:
            text.append(f" · {error}", style="#f28b82")
        return text
    text.append(
        f"{status} · {format_age(synced_monotonic)} 전",
        style="#f0ad4e" if stale else MUTED_COLOR,
    )
    if stale and error:
        text.append(f" · {error}", style="#f28b82")
    text.append("\n")
    if not page.orders:
        text.append("기간 내 종료 주문 없음\n", style=MUTED_COLOR)
    for order in page.orders:
        timestamp = order.ordered_at.astimezone(_KST).strftime("%m/%d %H:%M")
        side_style = UP_COLOR if order.side == "BUY" else DOWN_COLOR
        text.append(f"{timestamp} {order.side} {order.symbol}", style=side_style)
        text.append(
            f" · {order.status} · 주문 {format_decimal(order.quantity)}주 · "
            f"체결 {format_decimal(order.filled_quantity)}주 · 통화 {order.currency}\n",
            style="#d9e1e8",
        )
        average = (
            "—"
            if order.average_filled_price is None
            else format_decimal(order.average_filled_price, order.currency)
        )
        amount = (
            "—"
            if order.filled_amount is None
            else format_decimal(order.filled_amount, order.currency)
        )
        commission = (
            "—" if order.commission is None else format_decimal(order.commission, order.currency)
        )
        tax = "—" if order.tax is None else format_decimal(order.tax, order.currency)
        text.append(
            f"  평균체결 {average} · 체결금액 {amount} · 수수료 {commission} · 세금 {tax}\n",
            style=MUTED_COLOR,
        )
    if page.has_more:
        text.append("추가 주문내역 있음 · 화면에는 최근 20건만 표시\n", style="#f0ad4e")
    return text


def realized_profit_loss_section_text() -> Text:
    text = Text()
    text.append("실현손익\n", style="bold #c9d1d9")
    text.append("공식 API 미제공 · 현재 평단으로 임의 계산하지 않음", style=MUTED_COLOR)
    return text


def portfolio_body_text(
    snapshot: PortfolioSnapshot | None,
    width: int,
    *,
    exchange_rate: ExchangeRate | None = None,
    exchange_stale: bool = False,
    exchange_error: str | None = None,
    exchange_synced_monotonic: float | None = None,
    closed_orders: ClosedOrdersPage | None = None,
    history_stale: bool = False,
    history_error: str | None = None,
    history_synced_monotonic: float | None = None,
) -> Text:
    if snapshot is None:
        return Text("계좌 정보를 아직 불러오지 못했습니다.", style=MUTED_COLOR)
    text = Text()
    text.append_text(holdings_section_text(snapshot, width))
    text.append("\n")
    text.append_text(open_orders_section_text(snapshot, width))
    text.append("\n")
    text.append_text(totals_section_text(snapshot))
    text.append("\n\n")
    text.append_text(
        exchange_rate_section_text(
            snapshot,
            exchange_rate,
            stale=exchange_stale,
            error=exchange_error,
            synced_monotonic=exchange_synced_monotonic,
        )
    )
    text.append("\n\n")
    text.append_text(
        closed_orders_section_text(
            closed_orders,
            stale=history_stale,
            error=history_error,
            synced_monotonic=history_synced_monotonic,
        )
    )
    text.append("\n")
    text.append_text(realized_profit_loss_section_text())
    return text


class PortfolioScreen(ModalScreen[None]):
    """Read-only portfolio view. No row selection or mutation endpoints."""

    BINDINGS: ClassVar = (
        Binding("escape", "close", "닫기"),
        Binding("p", "close", "닫기", show=False),
        Binding("r", "refresh_account", "전체 새로고침"),
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
                "Esc/p 닫기 · r 전체 새로고침 · GET only · 주문 mutation 없음",
                id="portfolio-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self.set_interval(1.0, self.refresh_view)
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
        content.update(
            portfolio_body_text(
                snapshot,
                width,
                exchange_rate=getattr(self.app, "exchange_rate", None),
                exchange_stale=bool(getattr(self.app, "exchange_rate_stale", False)),
                exchange_error=getattr(self.app, "exchange_rate_error", None),
                exchange_synced_monotonic=getattr(self.app, "exchange_rate_synced_monotonic", None),
                closed_orders=getattr(self.app, "closed_orders", None),
                history_stale=bool(getattr(self.app, "order_history_stale", False)),
                history_error=getattr(self.app, "order_history_error", None),
                history_synced_monotonic=getattr(self.app, "order_history_synced_monotonic", None),
            )
        )

    def action_noop(self) -> None:
        """모달 격리용 no-op. 어떤 상태도 바꾸지 않는다."""

    def action_close(self) -> None:
        self.dismiss(None)

    def action_refresh_account(self) -> None:
        refresh = getattr(self.app, "_refresh_portfolio_details", None)
        if refresh is None:
            refresh = getattr(self.app, "_refresh_portfolio", None)
        if refresh is None:
            return
        self.run_worker(refresh(), exclusive=False)


__all__ = [
    "PORTFOLIO_WIDE_WIDTH_THRESHOLD",
    "PortfolioScreen",
    "closed_orders_section_text",
    "exchange_rate_section_text",
    "holdings_section_text",
    "open_orders_section_text",
    "portfolio_body_text",
    "portfolio_header_text",
    "portfolio_weight",
    "realized_profit_loss_section_text",
    "sellable_quantity",
    "totals_section_text",
]
