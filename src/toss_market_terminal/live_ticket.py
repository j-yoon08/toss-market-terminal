"""Final interactive approval screen for an already-built manual live plan."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .live_order import (
    LiveOrderPlan,
    build_live_packet,
    live_approval_phrase,
    live_ui_approval_phrase,
)
from .order_preview import canonical_decimal_text

_ISOLATION_KEYS = ("a", "b", "c", "j", "k", "q", "r", "s", "m")


def _isolation_bindings() -> tuple[Binding, ...]:
    return tuple(Binding(key, "noop", show=False) for key in _ISOLATION_KEYS)


class LiveApprovalScreen(ModalScreen[str | None]):
    """Require exact per-plan phrase entry immediately before live submission."""

    BINDINGS: ClassVar = (
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
    #live-approval-phrase {
        height: auto;
        margin-top: 1;
        color: #f0ad4e;
    }
    #live-approval-input { width: 1fr; }
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
        self.required_phrase = live_ui_approval_phrase(self.packet)
        self._finished = False

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
                f"아래 짧은 문구를 정확히 입력:\n{self.required_phrase}",
                id="live-approval-phrase",
                markup=False,
            )
            yield Input(
                placeholder="전체 승인 문구 입력",
                id="live-approval-input",
                max_length=180,
            )
            yield Static(
                "Enter 제출 요청 · Esc 취소 · 결과 불명확 시 절대 재시도하지 않음",
                id="live-approval-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#live-approval-input", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self._finished:
            return
        if event.value != self.required_phrase:
            self.notify(
                "확인 문구가 일치하지 않습니다. 주문은 전송되지 않았습니다.",
                title="LIVE ORDER BLOCKED",
                severity="warning",
            )
            event.input.select_all()
            return
        self._finished = True
        self.dismiss(self.executor_phrase)

    def action_noop(self) -> None:
        """Prevent application shortcuts leaking through the modal."""

    def action_cancel(self) -> None:
        self._finished = True
        self.dismiss(None)


__all__ = ["LiveApprovalScreen"]
