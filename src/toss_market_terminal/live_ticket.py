"""Final interactive approval screen for an already-built manual live plan."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Static

from .live_order import (
    LiveOrderPlan,
    build_live_packet,
    live_approval_phrase,
)
from .order_preview import canonical_decimal_text

_ISOLATION_KEYS = ("a", "b", "c", "j", "k", "p", "q", "r", "s", "m")
REVIEW_ARM_SECONDS = 0.75


def _isolation_bindings() -> tuple[Binding, ...]:
    return tuple(Binding(key, "noop", show=False) for key in _ISOLATION_KEYS)


class LiveApprovalScreen(ModalScreen[str | None]):
    """Require a fresh Enter after a bounded order-review pause."""

    BINDINGS: ClassVar = (
        Binding("enter", "submit", "주문 접수 요청"),
        Binding("escape", "cancel", "취소"),
        *_isolation_bindings(),
    )

    CSS = """
    LiveApprovalScreen {
        align: center middle;
        background: rgba(4, 7, 10, 0.86);
    }
    #live-approval-dialog {
        width: 96%;
        max-width: 86;
        height: auto;
        max-height: 92%;
        padding: 1 2;
        border: solid #f28b82;
        background: #0d131a;
    }
    #live-approval-banner {
        height: 1;
        color: #f28b82;
        text-style: bold;
    }
    #live-approval-summary {
        height: auto;
        color: #d9e1e8;
    }
    #live-approval-details {
        height: auto;
        margin-top: 1;
        color: #f0ad4e;
    }
    #live-approval-help {
        height: auto;
        color: #7d8998;
    }
    """

    def __init__(self, plan: LiveOrderPlan) -> None:
        super().__init__()
        self.plan = plan
        self.packet = build_live_packet(plan)
        self.executor_phrase = live_approval_phrase(self.packet)
        self._finished = False
        self._armed = False
        self._arm_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        intent = self.plan.intent
        price = (
            "시장가(MARKET)"
            if intent.limit_price is None
            else f"지정가 {canonical_decimal_text(intent.limit_price)} {intent.currency}"
        )
        quantity = canonical_decimal_text(intent.quantity)
        summary = (
            f"{intent.side.value} · {intent.symbol} · 수량 {quantity}\n"
            f"{price} · 계좌 {intent.masked_account_no}\n"
            "브로커 접수는 체결을 의미하지 않습니다. 제출 후 자동 재시도하지 않습니다."
        )
        with Vertical(id="live-approval-dialog"):
            yield Static(
                "LIVE ORDER · 실제 주문 전송 직전", id="live-approval-banner", markup=False
            )
            yield Static(summary, id="live-approval-summary", markup=False)
            yield Static(
                f"주문 지문: {self.packet.fingerprint}\n"
                "방향·종목·수량·가격·계좌를 모두 확인하세요.",
                id="live-approval-details",
                markup=False,
            )
            yield Static(
                "확인 잠금 중 · 잠시 후 Enter 주문 접수 요청 · Esc 취소",
                id="live-approval-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self._arm_timer = self.set_timer(REVIEW_ARM_SECONDS, self._arm_after_review)

    def _arm_after_review(self) -> None:
        if self._finished:
            return
        self._armed = True
        self.query_one("#live-approval-help", Static).update(
            "Enter 실제 주문 접수 요청(체결 아님) · Esc 취소"
        )

    def action_submit(self) -> None:
        if self._finished:
            return
        if not self._armed:
            if self._arm_timer is not None:
                self._arm_timer.reset()
            self.notify(
                "연속 입력을 차단했습니다. 주문 정보를 확인한 뒤 Enter를 다시 누르세요.",
                title="LIVE ORDER REVIEW",
                severity="warning",
            )
            return
        self._finished = True
        if self._arm_timer is not None:
            self._arm_timer.stop()
        self.dismiss(self.executor_phrase)

    def action_noop(self) -> None:
        """Prevent application shortcuts leaking through the modal."""

    def action_cancel(self) -> None:
        self._finished = True
        if self._arm_timer is not None:
            self._arm_timer.stop()
        self.dismiss(None)


__all__ = ["LiveApprovalScreen"]
