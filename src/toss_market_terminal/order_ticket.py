"""v0.7b Textual paper-order ticket UX.

이 모듈은 TUI에서 사용하는 **PAPER 주문 미리보기 티켓** 화면 두 개를 제공합니다.

경계 선언:
  * 어떤 엔드포인트도 알지 못하며, HTTP 호출·주문 전송을 하지 않습니다.
  * 계좌 정보는 v0.6 읽기 전용 ``account_context`` 결과로만 받습니다.
  * 미리보기 생성은 오직 ``PaperPreviewService``(v0.7a 순수 도메인)를 통해 이뤄집니다.
  * 검증 실패·조회 실패는 항상 짧고 안전한 한국어 메시지로 정제됩니다(sanitize).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar, TypedDict, Unpack

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Input, Static

from .client import TossApiError
from .models import AccountContext, infer_market
from .order_preview import (
    SAFETY_POLICY_VERSION,
    SUPPORTED_CURRENCIES,
    OrderPreview,
    OrderPreviewError,
    OrderSide,
    OrderType,
    PaperPreviewService,
    canonical_decimal_text,
    parse_decimal_input,
)
from .pretrade import PretradeFacts

PAPER_PREVIEW_BANNER = "PAPER_PREVIEW · 실제 주문 전송 없음"

SIDE_LABELS: dict[OrderSide, str] = {OrderSide.BUY: "매수(BUY)", OrderSide.SELL: "매도(SELL)"}
ORDER_TYPE_LABELS: dict[OrderType, str] = {
    OrderType.LIMIT: "지정가(LIMIT)",
    OrderType.MARKET: "시장가(MARKET)",
}

TICKET_ERROR_MAX_LENGTH = 200

#: 앱에서 읽기 전용 계좌 컨텍스트를 가져오는 주입 지점(v0.6 account_context 전용).
AccountLoader = Callable[[str], Awaitable[AccountContext]]

#: 티켓이 미리보기 생성에 사용하는 순수 서비스 팩토리(테스트 대체용).
PreviewServiceFactory = Callable[[], PaperPreviewService]

#: 모달 위에서 절대 앱(관심목록/차트) 동작으로 새어 나가면 안 되는 키 방어 목록.
_ISOLATION_KEYS: tuple[str, ...] = ("a", "b", "c", "j", "k", "p", "q", "r", "s")


def sanitize_ticket_error(exc: Exception) -> str:
    """실패를 짧고 안전한 한국어 한 줄로 정제한다.

    원문 응답 본문·계좌번호·토큰은 절대 포함하지 않는다. 도메인 검증 오류
    (OrderPreviewError)는 이미 안전한 문구이므로 그대로 쓴다.
    """
    if isinstance(exc, OrderPreviewError):
        text = " ".join(str(exc).split())[:TICKET_ERROR_MAX_LENGTH]
        return text or "미리보기 생성에 실패했습니다."
    if isinstance(exc, TossApiError):
        return f"계좌 조회에 실패했습니다 (HTTP {exc.status_code}, code={exc.code})."
    return "계좌 정보를 확인할 수 없어 미리보기를 만들 수 없습니다."


@dataclass(frozen=True, slots=True)
class TicketCapture:
    """b/s 키를 누른 순간의 불변 스냅샷(심볼·참고 현재가·통화)."""

    symbol: str
    reference_price: Decimal
    currency: str


class _TicketInputKwargs(TypedDict, total=False):
    """``_TicketInput``이 ``Input``에 그대로 전달하는 유효 키워드 인자."""

    id: str
    placeholder: str
    max_length: int


class _TicketInput(Input):
    """티켓 전용 입력란. 포커스 중에도 `m`이 글자로 입력되지 않고 유형 전환으로 간다."""

    def __init__(
        self,
        toggle_callback: Callable[[], None],
        **kwargs: Unpack[_TicketInputKwargs],
    ) -> None:
        self._toggle_callback = toggle_callback
        super().__init__(**kwargs)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "m":
            event.stop()
            event.prevent_default()
            self._toggle_callback()
            return
        await super()._on_key(event)


def _isolation_bindings() -> tuple[Binding, ...]:
    """모달 아래 앱 바인딩으로 키가 새는 것을 구조적으로 차단하는 no-op 바인딩."""
    return tuple(Binding(key, "noop", show=False) for key in _ISOLATION_KEYS)


class OrderTicketScreen(ModalScreen["OrderPreview | None"]):
    """컴팩트한 PAPER 주문 미리보기 작성 모달. 전송은 존재하지 않는다."""

    BINDINGS: ClassVar = (
        Binding("escape", "cancel", "취소"),
        Binding("m", "toggle_order_type", "LIMIT/MARKET 전환"),
        *_isolation_bindings(),
    )

    CSS = """
    OrderTicketScreen {
        align: center middle;
        background: rgba(4, 7, 10, 0.78);
    }
    #ticket-dialog {
        width: 92%;
        max-width: 62;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: solid #607080;
        background: #0d131a;
    }
    #ticket-banner {
        height: 1;
        color: #f0ad4e;
        text-style: bold;
    }
    #ticket-summary {
        height: 2;
        color: #aab7c4;
    }
    #ticket-ordertype {
        height: 1;
        color: #d9e1e8;
    }
    #ticket-error {
        height: 1;
        color: #f28b82;
    }
    #ticket-help {
        height: 1;
        color: #526273;
    }
    """

    def __init__(
        self,
        capture: TicketCapture,
        context: AccountContext,
        side: OrderSide,
        *,
        preview_service: PaperPreviewService | None = None,
        preview_service_factory: PreviewServiceFactory | None = None,
    ) -> None:
        super().__init__()
        self.capture = capture
        self.context = context
        self.side = side
        self.preview_service_factory = preview_service_factory
        self.preview_service = preview_service or PaperPreviewService()
        self.order_type = OrderType.LIMIT
        self._finished = False

    # ------------------------------------------------------------------ UI

    def compose(self) -> ComposeResult:
        with Vertical(id="ticket-dialog"):
            yield Static(PAPER_PREVIEW_BANNER, id="ticket-banner", markup=False)
            yield Static(self._summary_text(), id="ticket-summary", markup=False)
            yield Static(self._order_type_text(), id="ticket-ordertype", markup=False)
            yield _TicketInput(
                self._toggle_order_type,
                id="ticket-quantity",
                placeholder="수량",
                max_length=12,
            )
            yield _TicketInput(
                self._toggle_order_type,
                id="ticket-price",
                placeholder=f"지정가 ({self.capture.currency})",
                max_length=15,
            )
            yield Static("", id="ticket-error", markup=False)
            yield Static(
                "Enter 다음 · m LIMIT/MARKET · Esc 취소 · PAPER 미리보기만 생성",
                id="ticket-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self._sync_price_input()
        self.query_one("#ticket-quantity", Input).focus()

    def _summary_text(self) -> str:
        account = self.context.account
        power = self.context.buying_power
        head = (
            f"{SIDE_LABELS[self.side]} · {self.capture.symbol} · 계좌 {account.masked_account_no}"
        )
        balances = (
            f"보유 {canonical_decimal_text(self.context.holding_quantity)} · "
            f"매수가능 {canonical_decimal_text(power.cash_buying_power)} {power.currency}"
        )
        reference = (
            f"기준가 {canonical_decimal_text(self.capture.reference_price)} {self.capture.currency}"
        )
        return "\n".join([head, balances, reference])

    def _order_type_text(self) -> str:
        text = f"주문 유형: {ORDER_TYPE_LABELS[self.order_type]}"
        if self.order_type is OrderType.MARKET:
            text += " · 지정가 입력 비활성"
        return text

    def _sync_price_input(self) -> None:
        price = self.query_one("#ticket-price", Input)
        if self.order_type is OrderType.MARKET:
            price.value = ""
            price.disabled = True
        else:
            price.disabled = False
            price.value = canonical_decimal_text(self.capture.reference_price)

    # ------------------------------------------------------------- events

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"ticket-quantity", "ticket-price"}:
            self._clear_error()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self._finished:
            return
        if event.input.id == "ticket-quantity":
            if self.order_type is OrderType.MARKET:
                self._build_preview()
            else:
                self.query_one("#ticket-price", Input).focus()
        elif event.input.id == "ticket-price":
            self._build_preview()

    # ------------------------------------------------------------ actions

    def action_noop(self) -> None:
        """모달 격리용 no-op. 어떤 상태도 바꾸지 않는다."""

    def action_toggle_order_type(self) -> None:
        self._toggle_order_type()

    def _toggle_order_type(self) -> None:
        if self._finished:
            return
        self.order_type = (
            OrderType.MARKET if self.order_type is OrderType.LIMIT else OrderType.LIMIT
        )
        self._sync_price_input()
        self.query_one("#ticket-ordertype", Static).update(self._order_type_text())
        self._clear_error()

    def action_cancel(self) -> None:
        self._finished = True
        self.dismiss(None)

    # ------------------------------------------------------------ preview

    def _show_error(self, message: str, *, focus_id: str | None = None) -> None:
        self.query_one("#ticket-error", Static).update(message[:TICKET_ERROR_MAX_LENGTH])
        if focus_id is not None:
            self.query_one(f"#{focus_id}", Input).focus()

    def _clear_error(self) -> None:
        self.query_one("#ticket-error", Static).update("")

    def _build_preview(self) -> None:
        """입력값으로 PAPER 미리보기를 만든다. 모든 실패는 fail-closed."""
        if self._finished:
            return
        # 응답 지연 사이 종목이 바뀌었으면 미리보기를 만들지 않는다(stale race).
        if getattr(self.app, "symbol", None) != self.capture.symbol:
            self._show_error("종목이 변경되어 미리보기를 만들 수 없습니다. 닫고 다시 시도하세요.")
            return
        if self.context.symbol != self.capture.symbol:
            self._show_error("계좌 정보의 종목이 일치하지 않아 미리보기를 만들 수 없습니다.")
            return
        if self.context.buying_power.currency != self.capture.currency:
            self._show_error("계좌 통화와 시세 통화가 일치하지 않습니다.")
            return

        quantity_input = self.query_one("#ticket-quantity", Input)
        try:
            quantity = parse_decimal_input(quantity_input.value.strip(), "수량")
        except OrderPreviewError as exc:
            self._show_error(str(exc), focus_id="ticket-quantity")
            return

        limit_price: Decimal | None = None
        if self.order_type is OrderType.LIMIT:
            try:
                limit_price = parse_decimal_input(
                    self.query_one("#ticket-price", Input).value.strip(), "지정가"
                )
            except OrderPreviewError as exc:
                self._show_error(str(exc), focus_id="ticket-price")
                return

        account = self.context.account
        service = self.preview_service
        if self.preview_service_factory is not None:
            service = self.preview_service_factory()
        try:
            preview = service.create_preview(
                account_no=account.masked_account_no,
                account_seq=account.account_seq,
                symbol=self.capture.symbol,
                side=self.side,
                order_type=self.order_type,
                quantity=canonical_decimal_text(quantity),
                reference_last_price=canonical_decimal_text(self.capture.reference_price),
                holding_quantity=canonical_decimal_text(self.context.holding_quantity),
                cash_buying_power=canonical_decimal_text(
                    self.context.buying_power.cash_buying_power
                ),
                limit_price=None if limit_price is None else canonical_decimal_text(limit_price),
                market=infer_market(self.capture.symbol),
                currency=self.capture.currency,
            )
        except OrderPreviewError as exc:
            self._show_error(sanitize_ticket_error(exc))
            return
        self._finished = True
        self.dismiss(preview)


class OrderConfirmScreen(ModalScreen[bool]):
    """PAPER 미리보기 확인 모달. Enter는 로컬 확정일 뿐 어떤 호출도 하지 않는다."""

    BINDINGS: ClassVar = (
        Binding("enter", "confirm", "PAPER 미리보기 확정"),
        Binding("escape", "cancel", "취소"),
        *_isolation_bindings(),
    )

    CSS = """
    OrderConfirmScreen {
        align: center middle;
        background: rgba(4, 7, 10, 0.82);
    }
    #order-confirm-dialog {
        width: 96%;
        max-width: 84;
        height: auto;
        max-height: 94%;
        padding: 1 2;
        border: solid #34404d;
        background: #0d131a;
    }
    #confirm-banner {
        height: 1;
        color: #f0ad4e;
        text-style: bold;
    }
    #confirm-body {
        height: auto;
        color: #d9e1e8;
    }
    #confirm-facts-title {
        height: 1;
        margin-top: 1;
        color: #f2f5f7;
        text-style: bold;
    }
    #confirm-facts {
        height: auto;
    }
    .pretrade-fact {
        height: auto;
        color: #d9e1e8;
    }
    .fact-pass { color: #70d7a5; }
    .fact-warn { color: #f0c36a; }
    .fact-block { color: #ff8f87; text-style: bold; }
    .fact-unavailable { color: #8996a5; }
    #confirm-help {
        height: auto;
        margin-top: 1;
        color: #8996a5;
    }
    """

    def __init__(
        self,
        preview: OrderPreview,
        facts: PretradeFacts | None = None,
        *,
        facts_valid_for_seconds: float | None = None,
    ) -> None:
        super().__init__()
        if facts is not None and (
            facts.mode != "PAPER"
            or facts.symbol != preview.intent.symbol
            or facts.side is not preview.intent.side
            or facts.order_type is not preview.intent.order_type
        ):
            raise ValueError("PAPER preview와 pre-trade facts가 일치하지 않습니다.")
        if facts_valid_for_seconds is not None and (
            isinstance(facts_valid_for_seconds, bool)
            or not math.isfinite(facts_valid_for_seconds)
            or facts_valid_for_seconds < 0
        ):
            raise ValueError("facts_valid_for_seconds는 0 이상의 유한한 수여야 합니다.")
        self.preview = preview
        self.facts = facts
        self.facts_valid_for_seconds = facts_valid_for_seconds
        self._facts_expired = False
        self._facts_expiry_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="order-confirm-dialog"):
            yield Static(PAPER_PREVIEW_BANNER, id="confirm-banner", markup=False)
            yield Static(self._body_text(), id="confirm-body", markup=False)
            yield Static(self._facts_title(), id="confirm-facts-title", markup=False)
            with Vertical(id="confirm-facts"):
                if self.facts is None:
                    yield Static(
                        "[정보 없음] 사전 점검 · 현재 화면에 전달된 사실 정보 없음",
                        classes="pretrade-fact fact-unavailable",
                        markup=False,
                    )
                else:
                    for row in self.facts.rows:
                        yield Static(
                            f"[{row.status_label}] {row.label} · {row.detail}",
                            id=f"confirm-fact-{row.key}",
                            classes=f"pretrade-fact fact-{row.status.value.lower()}",
                            markup=False,
                        )
            yield Static(
                "Enter PAPER 미리보기 확정 · Esc 취소 · 실제 주문 전송 없음",
                id="confirm-help",
                markup=False,
            )

    def _facts_title(self) -> str:
        if self.facts is None:
            return "사전 점검 · 정보 없음"
        return f"사전 점검 · 종합 {self.facts.overall_label} · 표시 전용"

    def _body_text(self) -> str:
        intent = self.preview.intent
        if intent.order_type is OrderType.MARKET:
            price_line = (
                f"시장가(MARKET) · 참고가 {canonical_decimal_text(intent.reference_last_price)}"
                f" {intent.currency}"
            )
        else:
            if intent.limit_price is None:
                raise RuntimeError("LIMIT 주문의 지정가가 누락되었습니다.")
            price_line = f"지정가 {canonical_decimal_text(intent.limit_price)} {intent.currency}"
        return "\n".join(
            [
                f"방향 {SIDE_LABELS[intent.side]} · 심볼 {intent.symbol}",
                f"주문 유형: {ORDER_TYPE_LABELS[intent.order_type]}",
                f"가격: {price_line}",
                f"수량: {canonical_decimal_text(intent.quantity)}",
                f"추정 금액: {canonical_decimal_text(self.preview.estimated_notional)}"
                f" {intent.currency}",
                f"보유 수량: {canonical_decimal_text(intent.holding_quantity)} · "
                f"매수가능금액: {canonical_decimal_text(intent.cash_buying_power)}"
                f" {intent.currency}",
                f"계좌: {intent.masked_account_no}",
                f"지문: {self.preview.fingerprint[:8]} · 정책 {SAFETY_POLICY_VERSION}",
            ]
        )

    def on_mount(self) -> None:
        if self.facts_valid_for_seconds is not None:
            self._facts_expiry_timer = self.set_timer(
                self.facts_valid_for_seconds,
                self._mark_facts_expired,
            )

    def _mark_facts_expired(self) -> None:
        if self._facts_expired:
            return
        self._facts_expired = True
        self.query_one("#confirm-facts-title", Static).update(
            "사전 점검 · 차단 · 확인 중 시세 유효시간 경과"
        )
        if self.facts is not None:
            widget = self.query_one("#confirm-fact-quote_freshness", Static)
            for class_name in (
                "fact-pass",
                "fact-warn",
                "fact-unavailable",
            ):
                widget.remove_class(class_name)
            widget.add_class("fact-block")
            widget.update("[차단] 시세 신선도 · 확인 화면 체류 중 유효시간 경과 · 다시 열어야 함")
        self.query_one("#confirm-help", Static).update(
            "시세 재확인 필요 · Esc 취소 후 주문 티켓을 다시 여세요"
        )

    def _stop_expiry_timer(self) -> None:
        if self._facts_expiry_timer is not None:
            self._facts_expiry_timer.stop()

    # ------------------------------------------------------------ actions

    def action_noop(self) -> None:
        """모달 격리용 no-op. 어떤 상태도 바꾸지 않는다."""

    def action_confirm(self) -> None:
        if self._facts_expired:
            self.notify(
                "시세 유효시간이 지났습니다. 취소 후 주문 티켓을 다시 여세요.",
                title="PAPER PREVIEW BLOCKED",
                severity="warning",
            )
            return
        self._stop_expiry_timer()
        self.dismiss(True)

    def action_cancel(self) -> None:
        self._stop_expiry_timer()
        self.dismiss(False)


def build_ticket_capture(
    symbol: str,
    reference_price: Decimal | None,
    currency: str,
) -> TicketCapture | None:
    """미리보기 티켓 열기를 위한 불변 캡처. 조건이 부족하면 None(fail-closed)."""
    if reference_price is None or reference_price <= 0:
        return None
    if currency not in SUPPORTED_CURRENCIES:
        return None
    return TicketCapture(symbol=symbol, reference_price=reference_price, currency=currency)


__all__ = [
    "PAPER_PREVIEW_BANNER",
    "AccountLoader",
    "OrderConfirmScreen",
    "OrderTicketScreen",
    "PreviewServiceFactory",
    "TicketCapture",
    "build_ticket_capture",
    "sanitize_ticket_error",
]
