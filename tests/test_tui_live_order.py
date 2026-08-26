from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from textual.widgets import Static

from tests.test_order_ticket import make_context, ticket_snapshot
from toss_market_terminal.live_audit import LiveAuditLog
from toss_market_terminal.live_order import (
    LiveOrderAmbiguous,
    LiveOrderPacket,
    LiveOrderPlan,
    build_live_packet,
    create_live_plan,
    live_approval_phrase,
)
from toss_market_terminal.models import BuyingPower, OpenOrder, OpenOrdersPage
from toss_market_terminal.order_preview import OrderSide, OrderType, build_preview
from toss_market_terminal.tui import TossMarketApp


@pytest.fixture(autouse=True)
def prohibit_external_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(self: socket.socket, *args: object) -> None:
        raise AssertionError("external network attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def preview():
    return build_preview(
        account_no="*******8901",
        account_seq=1,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity="1",
        limit_price="40",
        reference_last_price="40",
        holding_quantity="5",
        cash_buying_power="500",
        market="us",
        currency="USD",
    )


def plan() -> LiveOrderPlan:
    return create_live_plan(preview(), datetime.now(UTC), ttl_seconds=300)


class RecordingTransport:
    def __init__(
        self,
        response: Mapping[str, object] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.response = response or {
            "order_id": "broker-order-id",
            "client_order_id": "",
        }
        self.failure = failure
        self.packets: list[object] = []
        self.factory_args: list[tuple[str, int]] = []
        self.close_count = 0
        self.close_failure = False

    def factory(self, token: str, account_seq: int) -> RecordingTransport:
        self.factory_args.append((token, account_seq))
        return self

    def submit(self, packet: LiveOrderPacket) -> Mapping[str, object]:
        self.packets.append(packet)
        if self.failure is not None:
            raise self.failure
        body = dict(self.response)
        body["client_order_id"] = packet.client_order_id
        return body

    def close(self) -> None:
        self.close_count += 1
        if self.close_failure:
            raise RuntimeError("close provider secret")


def make_live_app(tmp_path: Path, transport: RecordingTransport) -> TossMarketApp:
    app = TossMarketApp(
        "AAPL",
        tmp_path / "unused.json",
        initial_snapshot=ticket_snapshot(),
        connect_live=False,
        manual_live_orders=True,
        live_audit_log=LiveAuditLog(tmp_path / "audit-state" / "audit.jsonl"),
        live_transport_factory=transport.factory,
    )
    context = make_context()

    async def account_loader(symbol: str):
        assert symbol == "AAPL"
        return context

    async def open_loader(account_seq: int, symbol: str) -> OpenOrdersPage:
        assert (account_seq, symbol) == (1, "AAPL")
        return OpenOrdersPage(orders=())

    async def token_loader() -> str:
        return "test-access-token-not-a-real-secret"

    app.account_context_loader = account_loader
    app.open_orders_loader = open_loader
    app.access_token_loader = token_loader
    return app


def open_order(status: str = "PENDING") -> OpenOrder:
    return OpenOrder.from_api(
        {
            "orderId": "existing-order",
            "symbol": "AAPL",
            "side": "BUY",
            "orderType": "LIMIT",
            "status": status,
            "quantity": "1",
            "price": "40",
        }
    )


async def test_exact_live_gates_submit_once_audit_and_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    account_calls = 0
    original_loader = app.account_context_loader

    async def counted_loader(symbol: str):
        nonlocal account_calls
        account_calls += 1
        assert original_loader is not None
        return await original_loader(symbol)

    app.account_context_loader = counted_loader
    current_plan = plan()
    phrase = live_approval_phrase(build_live_packet(current_plan))

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(current_plan, phrase)

    assert len(transport.packets) == 1
    assert transport.factory_args == [("test-access-token-not-a-real-secret", 1)]
    assert transport.close_count == 1
    assert account_calls == 2  # immediate preflight + read-only reconciliation
    assert app.last_live_outcome is not None
    assert app.last_live_outcome.status == "accepted"

    audit_path = tmp_path / "audit-state" / "audit.jsonl"
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["status"] == "accepted"
    serialized = json.dumps(rows)
    for forbidden in (
        "broker-order-id",
        "test-access-token",
        "accountSeq",
        "approval",
        "raw",
    ):
        assert forbidden not in serialized


async def test_close_failure_cannot_override_accepted_result_or_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    transport.close_failure = True
    app = make_live_app(tmp_path, transport)
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert len(transport.packets) == 1
    assert transport.close_count == 1
    assert app.last_live_outcome is not None
    assert app.last_live_outcome.status == "accepted"
    audit_text = (tmp_path / "audit-state" / "audit.jsonl").read_text(encoding="utf-8")
    assert '"status":"accepted"' in audit_text
    assert "close provider secret" not in audit_text


@pytest.mark.parametrize("gate", ["wrong-phrase", "env-off", "runtime-off"])
async def test_missing_gate_never_reads_account_or_submits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    current_plan = plan()
    phrase = live_approval_phrase(build_live_packet(current_plan))
    if gate != "env-off":
        monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    if gate == "wrong-phrase":
        phrase = "wrong"
    if gate == "runtime-off":
        app.manual_live_orders = False

    reads = 0

    async def unexpected_read(symbol: str):
        nonlocal reads
        reads += 1
        raise AssertionError(symbol)

    app.account_context_loader = unexpected_read
    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(current_plan, phrase)

    assert reads == 0
    assert transport.packets == []
    assert not (tmp_path / "audit-state" / "audit.jsonl").exists()


async def test_duplicate_open_order_blocks_before_token_and_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    token_calls = 0

    async def duplicate_loader(account_seq: int, symbol: str) -> OpenOrdersPage:
        return OpenOrdersPage((open_order(),))

    async def token_loader() -> str:
        nonlocal token_calls
        token_calls += 1
        return "unused"

    app.open_orders_loader = duplicate_loader
    app.access_token_loader = token_loader
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert token_calls == 0
    assert transport.packets == []


async def test_audit_preflight_wrong_mode_blocks_before_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    audit_path = tmp_path / "bad-audit" / "audit.jsonl"
    audit_path.parent.mkdir(mode=0o700)
    audit_path.write_text("", encoding="utf-8")
    audit_path.chmod(0o644)
    app.live_audit_log = LiveAuditLog(audit_path)
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert transport.packets == []
    assert audit_path.read_text(encoding="utf-8") == ""


async def test_fresh_buying_power_drop_blocks_before_open_orders_and_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)
    stale = make_context()
    stale = replace(stale, buying_power=BuyingPower(currency="USD", cash_buying_power=Decimal("1")))
    open_calls = 0

    async def low_power_loader(symbol: str):
        return stale

    async def unexpected_open(account_seq: int, symbol: str) -> OpenOrdersPage:
        nonlocal open_calls
        open_calls += 1
        return OpenOrdersPage(())

    app.account_context_loader = low_power_loader
    app.open_orders_loader = unexpected_open
    current_plan = plan()

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(
            current_plan, live_approval_phrase(build_live_packet(current_plan))
        )

    assert open_calls == 0
    assert transport.packets == []


async def test_same_fingerprint_never_retries_after_first_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport(failure=TimeoutError("provider secret body"))
    app = make_live_app(tmp_path, transport)
    current_plan = plan()
    phrase = live_approval_phrase(build_live_packet(current_plan))

    async with app.run_test(size=(90, 30)):
        await app._submit_live_plan(current_plan, phrase)
        await app._submit_live_plan(current_plan, phrase)

    assert len(transport.packets) == 1
    assert isinstance(app.last_live_outcome, LiveOrderAmbiguous)
    audit_path = tmp_path / "audit-state" / "audit.jsonl"
    text = audit_path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 1
    assert "provider secret body" not in text
    assert "ambiguous" in text


async def test_live_mode_opens_compact_exact_phrase_modal_after_paper_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", "1")
    transport = RecordingTransport()
    app = make_live_app(tmp_path, transport)

    async with app.run_test(size=(90, 30)) as pilot:
        app._on_paper_confirmed(preview(), True)
        await pilot.pause()
        assert type(app.screen).__name__ == "LiveApprovalScreen"
        region = app.screen.query_one("#live-approval-dialog").region
        assert 0 < region.width <= 90 and 0 < region.height <= 30
        shown = app.screen.query_one("#live-approval-phrase", Static).render().plain
        assert app.screen.required_phrase in shown
        assert len(app.screen.packet.fingerprint) == 64
        await pilot.press("escape")
        await pilot.pause()

    assert transport.packets == []


async def test_live_runtime_flag_with_env_off_never_opens_approval_modal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOSS_ENABLE_MANUAL_LIVE_ORDERS", raising=False)
    app = make_live_app(tmp_path, RecordingTransport())

    async with app.run_test(size=(90, 30)) as pilot:
        app._on_paper_confirmed(preview(), True)
        await pilot.pause()
        assert type(app.screen).__name__ != "LiveApprovalScreen"
        assert app.last_paper_preview is not None
        messages = [notification.message for notification in app._notifications]
        assert any("환경 게이트" in message for message in messages)
